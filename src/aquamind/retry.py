"""AquaMind 重试退避与错误分类。

分类的唯一标准是「换个时间再试一次，服务端会不会给出不同结果」：限流（429）、
服务端错误（5xx）、超时、网络抖动属于可重试；认证失败（401/403）、参数错误
（其他 4xx）以及无法识别的错误重试也不会变好，直接上抛而不是白白消耗配额。

本模块与适配器完全解耦：``classify_error`` 只做分类判断，``with_retry`` 是独立的
工具函数，调用方（未来的 runner / 批量执行器）自行决定是否用它包裹适配器调用，
``adapters/openai.py`` 不感知也不需要感知重试逻辑。

日志只记录错误分类与退避参数，不记录异常消息与 context，避免响应体、prompt、
密钥随重试日志进入日志系统。
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar

import httpx

from .exceptions import AdapterError

# 模块级 logger：AquaMind 不自建 handler，交给宿主应用决定如何输出
logger = logging.getLogger(__name__)

T = TypeVar("T")
# 重试工厂必须是**无参可调用对象**。两种误用的失败方式并不相同，必须分开说：
#   1) 直接把协程对象本身传进来 —— 首次尝试就抛
#      TypeError('coroutine' object is not callable)，一次都跑不起来；
#   2) 工厂每次返回**同一个**协程对象 —— 该协程第二次被 await 时抛
#      RuntimeError(cannot reuse already awaited coroutine)。这类误用更隐蔽：
#      首次尝试可能成功，看起来「能用」，一旦真的需要重试才炸。
CoroFactory = Callable[[], Awaitable[T]]

# AdapterError.context.error_type 只保存了异常**类名**（M1-D06a 契约，见
# adapters/openai.py 用 type(exc).__name__ 填充），拿不到实例，只能按名字归类。
# 这两个集合只服务于**名字路径**；原生 httpx 异常由 _kind_from_httpx_exception
# 按 isinstance(TransportError) 自动覆盖，httpx 未来新增的传输层异常无需在此登记。
# 例外是需要特殊分类的子类（如 TimeoutException 归 TIMEOUT 而非 NETWORK_ERROR），
# 这类必须显式登记到名字路径，否则经 openai.py 包装后会丢失正确分类。
#
# ProtocolError 族（RemoteProtocolError / ProtocolError / ProxyError /
# UnsupportedProtocol）在 httpx 里是 TransportError 子类而非 NetworkError 子类，
# 但它们同样是「服务端中途断开」「代理不可用」这类瞬时故障，重试有意义。
_TIMEOUT_ERROR_NAMES = frozenset(
    {
        "TimeoutException",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
    }
)
_NETWORK_ERROR_NAMES = frozenset(
    {
        "NetworkError",
        "ConnectError",
        "ReadError",
        "WriteError",
        "CloseError",
        # ProtocolError 族：瞬时传输故障，可重试（详见上方注释）
        "RemoteProtocolError",
        "ProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
    }
)


class ErrorKind(Enum):
    """错误分类枚举：把一次失败归入可重试 / 不可重试两大阵营。

    分类结果只表达「值不值得再试一次」，不表达严重程度，也不替代异常本身：
    原始异常始终原样上抛，调用方拿到的仍是带完整 context 的 AdapterError。
    """

    RATE_LIMITED = "RATE_LIMITED"
    """HTTP 429 触发服务端限流：等待后大概率可恢复，可重试。"""

    SERVER_ERROR = "SERVER_ERROR"
    """HTTP 5xx 服务端错误：多为瞬时故障（过载、重启），可重试。"""

    TIMEOUT = "TIMEOUT"
    """请求超时（httpx.TimeoutException 族）：请求可能已到达服务端，
    重试需调用方自行确认操作是否幂等。"""

    NETWORK_ERROR = "NETWORK_ERROR"
    """网络层错误（httpx.NetworkError 族：连接失败、读写中断等）：可重试。"""

    AUTH_ERROR = "AUTH_ERROR"
    """HTTP 401/403 认证或授权失败：重试不会改变凭据，不可重试。"""

    CLIENT_ERROR = "CLIENT_ERROR"
    """其他 HTTP 4xx 客户端错误（参数非法、端点不存在等）：不可重试。"""

    UNKNOWN = "UNKNOWN"
    """无法归类的错误：保守起见按不可重试处理，避免对未知故障盲目重试。"""


# 可重试分类集合：只有这四类会进入退避重试，其余一律立即上抛。
# 用 frozenset 而非 set：模块级常量对外暴露，调用方不应能就地改写它。
RETRYABLE_KINDS = frozenset(
    {
        ErrorKind.RATE_LIMITED,
        ErrorKind.SERVER_ERROR,
        ErrorKind.TIMEOUT,
        ErrorKind.NETWORK_ERROR,
    }
)


def _kind_from_status_code(status_code: int) -> ErrorKind:
    """按 HTTP 状态码判定错误分类。

    判定依据：429 是服务端主动限流，语义上独立于 5xx（限流通常伴随
    Retry-After，且退避窗口更保守）；401/403 是凭据问题，与 4xx 里的参数错误
    性质不同，必须单独成一类——它决定了「重试永远不会成功」这条硬结论。

    Args:
        status_code: HTTP 状态码。

    Returns:
        ErrorKind: 对应的错误分类；不在 4xx/5xx/429 范围内时返回 UNKNOWN。
    """
    if status_code == 429:
        return ErrorKind.RATE_LIMITED
    if 500 <= status_code <= 599:
        return ErrorKind.SERVER_ERROR
    if status_code in (401, 403):
        return ErrorKind.AUTH_ERROR
    if 400 <= status_code <= 499:
        return ErrorKind.CLIENT_ERROR
    # 1xx / 3xx / 非法值：都不是本次调用失败的原因，保守按 UNKNOWN 处理
    return ErrorKind.UNKNOWN


def _kind_from_httpx_exception(exc: BaseException) -> ErrorKind | None:
    """按 httpx 原生异常类型判定分类。

    Args:
        exc: 待判定的异常实例。

    Returns:
        ErrorKind | None: 命中已知 httpx 异常族时返回分类，否则返回 None
            （由调用方决定如何兜底）。
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # 未经 raise_for_status 的原生路径，状态码只能从响应对象上取
        return _kind_from_status_code(exc.response.status_code)
    # 超时必须排在传输错误之前判定：TimeoutException 本身也是 TransportError 子类，
    # 顺序颠倒会把超时误判成网络错误，两者的退避口径不同（超时通常更长）
    if isinstance(exc, httpx.TimeoutException):
        return ErrorKind.TIMEOUT
    # 这里用 TransportError 而非更窄的 NetworkError：RemoteProtocolError /
    # ProtocolError / ProxyError / UnsupportedProtocol 这四个「服务端中途断开、
    # 代理不可用、协议协商失败」都是传输层瞬时故障，但它们继承的是 TransportError
    # 而不是 NetworkError，改用基类即可自动覆盖，不必在原生路径里逐个列举——
    # 也因此 httpx 未来新增同类异常时原生路径无需改动。
    if isinstance(exc, httpx.TransportError):
        return ErrorKind.NETWORK_ERROR
    return None


def _kind_from_error_type_name(name: str) -> ErrorKind:
    """按异常类名字符串判定分类（仅用于 AdapterError.context.error_type 路径）。

    Args:
        name: 异常类名，如 "TimeoutException"（由 ``type(exc).__name__`` 产生）。

    Returns:
        ErrorKind: 对应的错误分类；名字不在已知族内时返回 UNKNOWN。
    """
    if name in _TIMEOUT_ERROR_NAMES:
        return ErrorKind.TIMEOUT
    if name in _NETWORK_ERROR_NAMES:
        return ErrorKind.NETWORK_ERROR
    return ErrorKind.UNKNOWN


def classify_error(exc: BaseException) -> ErrorKind:
    """把一次失败归类为 ErrorKind，供调用方决定是否重试。

    分类优先级（自上而下，命中即返回）：

    1. **AdapterError**：以 ``context["status_code"]`` 为准——同一端点的状态码
       比错误名字更能说明问题（同样是 4xx，401 与 400 的重试结论完全相反）；
       没有状态码时退回 ``context["error_type"]`` 里的异常类名；两者都没有则 UNKNOWN。
    2. **httpx.HTTPStatusError**：从响应对象取状态码。
    3. **httpx.TimeoutException**：超时族。
    4. **httpx.NetworkError**：网络族。
    5. 其余异常：UNKNOWN，保守按不可重试处理。

    Args:
        exc: 待分类的异常，通常是适配器抛出的 AdapterError，也可能是 httpx 原生异常。

    Returns:
        ErrorKind: 错误分类，永远不抛异常、也不修改传入对象。
    """
    if isinstance(exc, AdapterError):
        context: dict[str, Any] = exc.context
        status_code = context.get("status_code")
        # 只认真正的 int：context 是开放字典，调用方可能塞入字符串或 None，
        # 静默当成有效状态码会让分类结论与实际错误脱节
        if isinstance(status_code, int):
            return _kind_from_status_code(status_code)
        error_type = context.get("error_type")
        if isinstance(error_type, str):
            return _kind_from_error_type_name(error_type)
        return ErrorKind.UNKNOWN
    kind = _kind_from_httpx_exception(exc)
    if kind is not None:
        return kind
    return ErrorKind.UNKNOWN


def is_retryable(exc: BaseException) -> bool:
    """便捷判断：这次失败是否值得重试。

    供不使用 with_retry、只想要一个布尔结论的调用方使用（例如批量执行器决定
    是否把该样本挪进延迟队列）。

    Args:
        exc: 待判断的异常。

    Returns:
        bool: True 表示 classify_error 的结果属于 RETRYABLE_KINDS。
    """
    return classify_error(exc) in RETRYABLE_KINDS


async def with_retry(
    coro_factory: CoroFactory[T],
    max_retries: int = 3,
    base_delay: float = 1.0,
    jitter: float = 0.5,
) -> T:
    """按指数退避 + 随机抖动执行一次异步调用，失败时自动重试。

    重试策略：

    - 首次尝试不计入 ``max_retries``，``max_retries=3`` 表示最多尝试 4 次；
    - 仅对 ``RETRYABLE_KINDS``（限流 / 5xx / 超时 / 网络）重试，其余分类立即上抛，
      不消耗重试次数；
    - 第 n 次重试前等待 ``base_delay * 2 ** (n - 1) + random.uniform(0, jitter)`` 秒，
      抖动用于打散并发调用方的重试时刻，避免同一瞬间再次压垮服务端；
    - ``max_retries`` 小于等于 0 时退化为「只试一次」。

    Args:
        coro_factory: 无参异步可调用对象，**每次调用产生新的协程**。
            直接传协程对象本身会在首次尝试就抛 TypeError（coroutine object is not
            callable）；工厂复用同一个协程对象则在该协程第二次被 await 时抛
            RuntimeError（cannot reuse already awaited coroutine）。
        max_retries: 首次尝试之外的最大重试次数。
        base_delay: 基础退避秒数，第 n 次重试的退避为 base_delay * 2 ** (n - 1)。
        jitter: 随机抖动秒数上限，实际等待 = 退避 + uniform(0, jitter)。

    Returns:
        T: ``coro_factory()`` 首次成功时的返回值。

    Raises:
        Exception: 不可重试的分类立即原样抛出；重试次数耗尽时抛出最后一次异常，
            traceback 保留失败现场。
    """
    attempt = 0
    while True:
        try:
            return await coro_factory()
        # 只捕获 Exception：asyncio.CancelledError / KeyboardInterrupt / SystemExit
        # 均继承 BaseException，被吞掉重试会破坏任务取消与中断语义
        except Exception as exc:
            kind = classify_error(exc)
            if kind not in RETRYABLE_KINDS:
                raise
            if attempt >= max_retries:
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, jitter)
            # 只记录分类与退避参数，不拼入异常消息：异常消息可能含响应体片段，
            # 重试是高频路径，拼进去等于把响应内容批量灌进日志
            logger.info(
                "重试 %d/%d：错误类型=%s，等待 %.2f 秒",
                attempt + 1,
                max_retries,
                kind.name,
                delay,
            )
            await asyncio.sleep(delay)
            attempt += 1
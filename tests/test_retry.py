"""重试退避与错误分类模块单元测试。

覆盖范围：
- ErrorKind 七个成员与 RETRYABLE_KINDS 的划分；
- classify_error 的四条判定路径——AdapterError 状态码、AdapterError 异常类名、
  httpx 原生异常（HTTPStatusError / 超时 / 网络）、以及兜底 UNKNOWN；
- with_retry 的重试与不重试分支、重试次数耗尽、指数退避与抖动的数值正确性；
- is_retryable 便捷判断。

所有等待均被 mock（asyncio.sleep），所有抖动均被 mock（random.uniform），
因此测试不产生真实延迟、不触真实 API（零 key）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import call, patch

import httpx
import pytest

from aquamind import retry as retry_module
from aquamind.exceptions import AdapterError
from aquamind.retry import RETRYABLE_KINDS, ErrorKind, classify_error, is_retryable, with_retry

# 与 retry._TIMEOUT_ERROR_NAMES 对应的代表名：TimeoutException 是 httpx 超时族基类，
# ConnectTimeout 同时属于超时族与传输族，是分类顺序最容易写错的那一个
_TIMEOUT_NAMES = ("TimeoutException", "ConnectTimeout", "ReadTimeout", "PoolTimeout")
# 与 retry._NETWORK_ERROR_NAMES 对应的代表名；末尾四个是 M1-D07-fix 新登记的
# ProtocolError 族——它们在 httpx 里并非 NetworkError 子类，只能靠名字登记生效
_PROTOCOL_ERROR_NAMES = (
    "RemoteProtocolError",
    "ProtocolError",
    "ProxyError",
    "UnsupportedProtocol",
)
_NETWORK_NAMES = ("NetworkError", "ConnectError", "ReadError", "WriteError", "CloseError")

# 日志安全性测试用的两个哨兵串：模拟密钥与响应体。
# 刻意不写成 "sk-" / "pypi-" 开头，避免被验收脚本的密钥正则
# （(pypi|sk)-[A-Za-z0-9_-]{16,}）当成真实密钥误报。断言「日志里没有它」的
# 有效性不依赖前缀形状，任何足够独特的哨兵字符串都成立。
_FAKE_SECRET = "fake-secret-DO-NOT-LOG-1234567890"
# 模拟服务端回显的响应体内容（部分网关会把请求体原样回显）
_SENSITIVE_BODY = "sensitive-response-body-should-never-be-logged"


def _adapter_error(message: str = "模拟调用失败", **context: Any) -> AdapterError:
    """构造携带指定 context 的 AdapterError。"""
    return AdapterError(message=message, context=context)


def _factory(*outcomes: Any) -> tuple[Any, list[int]]:
    """构造按脚本产出的重试工厂。

    Args:
        outcomes: 依次使用的返回值或要抛出的异常；脚本用尽后重复最后一个，
            以便构造「连续失败直到耗尽」的场景。

    Returns:
        tuple: (无参异步工厂, 调用序号列表)。调用序号列表用于断言实际尝试次数。
    """
    calls: list[int] = []
    state = {"index": 0}

    async def _run() -> Any:
        index = state["index"]
        calls.append(index)
        state["index"] = index + 1
        outcome = outcomes[index] if index < len(outcomes) else outcomes[-1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return _run, calls


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    """构造带响应对象的 httpx.HTTPStatusError。"""
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("模拟状态码错误", request=request, response=response)


@pytest.fixture
def waits() -> Iterator[list[float]]:
    """拦截 with_retry 内部的 asyncio.sleep，记录每次等待秒数（不真实等待）。"""
    recorded: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        recorded.append(delay)

    with patch.object(retry_module.asyncio, "sleep", new=_fake_sleep):
        yield recorded


@pytest.fixture
def fixed_jitter() -> None:
    """把 random.uniform 固定为 0.25 秒，让退避数值可精确断言。"""
    with patch.object(retry_module.random, "uniform", return_value=0.25):
        yield


class TestErrorKind:
    """ErrorKind 枚举与可重试集合的定义。"""

    def test_has_seven_members(self) -> None:
        """枚举恰好包含七个分类，缺一或多一都会改变重试策略。"""
        assert [kind.name for kind in ErrorKind] == [
            "RATE_LIMITED",
            "SERVER_ERROR",
            "TIMEOUT",
            "NETWORK_ERROR",
            "AUTH_ERROR",
            "CLIENT_ERROR",
            "UNKNOWN",
        ]

    def test_retryable_kinds_only_cover_transient_faults(self) -> None:
        """可重试集合只含四类瞬时故障；认证、参数与未知一律不可重试。"""
        assert RETRYABLE_KINDS == frozenset(
            {
                ErrorKind.RATE_LIMITED,
                ErrorKind.SERVER_ERROR,
                ErrorKind.TIMEOUT,
                ErrorKind.NETWORK_ERROR,
            }
        )
        # 显式断言三类不可重试成员不在集合内，避免将来有人误加
        assert ErrorKind.AUTH_ERROR not in RETRYABLE_KINDS
        assert ErrorKind.CLIENT_ERROR not in RETRYABLE_KINDS
        assert ErrorKind.UNKNOWN not in RETRYABLE_KINDS


class TestClassifyAdapterError:
    """classify_error 的 AdapterError 路径：状态码优先，异常类名兜底。"""

    @pytest.mark.parametrize(
        ("status_code", "expected"),
        [
            (429, ErrorKind.RATE_LIMITED),
            (500, ErrorKind.SERVER_ERROR),
            (502, ErrorKind.SERVER_ERROR),
            (503, ErrorKind.SERVER_ERROR),
            (401, ErrorKind.AUTH_ERROR),
            (403, ErrorKind.AUTH_ERROR),
            (400, ErrorKind.CLIENT_ERROR),
            (404, ErrorKind.CLIENT_ERROR),
        ],
    )
    def test_classify_by_status_code(self, status_code: int, expected: ErrorKind) -> None:
        """按 HTTP 状态码分类；429/5xx 可重试，401/403 与其他 4xx 不可重试。"""
        assert classify_error(_adapter_error(status_code=status_code)) is expected

    @pytest.mark.parametrize("status_code", [100, 200, 302, 600, 999])
    def test_status_code_outside_http_error_range_is_unknown(self, status_code: int) -> None:
        """1xx/3xx/非法值不是本次失败的原因，保守归为 UNKNOWN。"""
        assert classify_error(_adapter_error(status_code=status_code)) is ErrorKind.UNKNOWN

    @pytest.mark.parametrize("name", _TIMEOUT_NAMES)
    def test_classify_by_error_type_timeout(self, name: str) -> None:
        """无状态码时按 error_type 异常类名归入 TIMEOUT。"""
        assert classify_error(_adapter_error(error_type=name)) is ErrorKind.TIMEOUT

    @pytest.mark.parametrize("name", _NETWORK_NAMES)
    def test_classify_by_error_type_network(self, name: str) -> None:
        """无状态码时按 error_type 异常类名归入 NETWORK_ERROR。"""
        assert classify_error(_adapter_error(error_type=name)) is ErrorKind.NETWORK_ERROR

    @pytest.mark.parametrize("name", _PROTOCOL_ERROR_NAMES)
    def test_protocol_error_family_is_registered_as_network(self, name: str) -> None:
        """M1-D07-fix：ProtocolError 族四个异常名必须登记为 NETWORK_ERROR。

        它们在 httpx 里不是 NetworkError 的子类，此前落到 UNKNOWN 导致不重试；
        服务端中途断开、代理不可用都是典型瞬时故障，漏掉会明显拉低恢复率。
        """
        exc = _adapter_error(error_type=name)
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR
        # 登记为可重试只是第一步，真正影响行为的是 is_retryable 的结论
        assert is_retryable(exc) is True

    @pytest.mark.parametrize("name", _PROTOCOL_ERROR_NAMES)
    async def test_protocol_error_family_retryable_via_error_type(
        self, waits: list[float], name: str
    ) -> None:
        """M1-D07-fix：ProtocolError 族经 with_retry 会真正触发一次重试。"""
        # 第一次抛该族异常（可重试），第二次成功：断言确实重试了而不是直接上抛
        factory, calls = _factory(_adapter_error(error_type=name), "重试后成功")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "重试后成功"
        assert len(calls) == 2

    @pytest.mark.parametrize("name", ["InvalidURL", "JSONDecodeError", "ValueError", ""])
    def test_unknown_error_type_name_is_unknown(self, name: str) -> None:
        """error_type 名字不在已知族内（含空串）时归为 UNKNOWN。"""
        assert classify_error(_adapter_error(error_type=name)) is ErrorKind.UNKNOWN

    def test_non_int_status_code_falls_back_to_error_type(self) -> None:
        """状态码不是 int（字符串）时不得静默采信，退回按 error_type 分类。"""
        exc = _adapter_error(status_code="429", error_type="ReadTimeout")
        assert classify_error(exc) is ErrorKind.TIMEOUT

    def test_missing_both_fields_is_unknown(self) -> None:
        """状态码与 error_type 都拿不到时归为 UNKNOWN（不可重试）。"""
        assert classify_error(_adapter_error(adapter="openai")) is ErrorKind.UNKNOWN
        assert classify_error(AdapterError(message="无 context")) is ErrorKind.UNKNOWN


class TestClassifyHttpxError:
    """classify_error 的 httpx 原生异常路径与兜底。"""

    @pytest.mark.parametrize(
        ("status_code", "expected"),
        [
            (429, ErrorKind.RATE_LIMITED),
            (503, ErrorKind.SERVER_ERROR),
            (403, ErrorKind.AUTH_ERROR),
            (400, ErrorKind.CLIENT_ERROR),
        ],
    )
    def test_http_status_error_by_response(self, status_code: int, expected: ErrorKind) -> None:
        """HTTPStatusError 按响应对象上的状态码分类。"""
        assert classify_error(_http_status_error(status_code)) is expected

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.TimeoutException("超时"),
            httpx.ConnectTimeout("连接超时"),
            httpx.ReadTimeout("读超时"),
        ],
    )
    def test_timeout_exception(self, exc: httpx.TimeoutException) -> None:
        """超时族归为 TIMEOUT；ConnectTimeout 同时是传输异常，顺序不能颠倒。"""
        assert classify_error(exc) is ErrorKind.TIMEOUT

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("连接失败"),
            httpx.ReadError("读中断"),
            httpx.WriteError("写中断"),
        ],
    )
    def test_network_exception(self, exc: httpx.NetworkError) -> None:
        """网络族归为 NETWORK_ERROR。"""
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR

    @pytest.mark.parametrize(
        "exc",
        [ValueError("普通异常"), httpx.InvalidURL("非法 URL"), RuntimeError("未分类")],
    )
    def test_other_exceptions_are_unknown(self, exc: Exception) -> None:
        """既非 AdapterError 也非已知 httpx 异常时归为 UNKNOWN。"""
        assert classify_error(exc) is ErrorKind.UNKNOWN

    # 以下四条是 M1-D07-fix 补充：ProtocolError 族在 httpx 里继承 TransportError
    # 而非 NetworkError，原生异常路径此前用 NetworkError 判定，命中不到它们，
    # 直传原生异常会得到 UNKNOWN（不重试）。改用 TransportError 基类后应全部可重试。
    def test_classify_remote_protocol_error_native(self) -> None:
        """原生 httpx.RemoteProtocolError（服务端中途断开）应可重试。"""
        exc = httpx.RemoteProtocolError("服务端在响应中途断开")
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR
        assert is_retryable(exc) is True

    def test_classify_protocol_error_native(self) -> None:
        """原生 httpx.ProtocolError 应可重试。"""
        exc = httpx.ProtocolError("协议协商失败")
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR
        assert is_retryable(exc) is True

    def test_classify_proxy_error_native(self) -> None:
        """原生 httpx.ProxyError（代理不可用）应可重试。"""
        exc = httpx.ProxyError("代理连接失败")
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR
        assert is_retryable(exc) is True

    def test_classify_unsupported_protocol_native(self) -> None:
        """原生 httpx.UnsupportedProtocol 应可重试。"""
        exc = httpx.UnsupportedProtocol("不支持的协议")
        assert classify_error(exc) is ErrorKind.NETWORK_ERROR
        assert is_retryable(exc) is True

    def test_timeout_still_wins_over_transport_error(self) -> None:
        """M1-D07-fix 回归：超时也是 TransportError 子类，超时判定必须排在其之前。"""
        # ConnectTimeout / PoolTimeout 同时是 TimeoutException 与 TransportError，
        # 若顺序颠倒会被误判成 NETWORK_ERROR，退避口径随之失真
        for exc in (httpx.ConnectTimeout("连接超时"), httpx.PoolTimeout("池超时")):
            assert classify_error(exc) is ErrorKind.TIMEOUT

    def test_http_status_error_not_treated_as_transport_error(self) -> None:
        """M1-D07-fix 回归：HTTPStatusError 不是 TransportError，须走状态码分支。"""
        # 守卫判定顺序：若它被 TransportError 分支抢先命中，429 会被错判成网络错误
        assert not issubclass(httpx.HTTPStatusError, httpx.TransportError)
        exc = _http_status_error(429)
        assert classify_error(exc) is ErrorKind.RATE_LIMITED


class TestIsRetryable:
    """is_retryable 便捷判断。"""

    def test_rate_limited_is_retryable(self) -> None:
        """429 属于可重试分类。"""
        assert is_retryable(_adapter_error(status_code=429)) is True

    def test_auth_error_is_not_retryable(self) -> None:
        """401 不可重试。"""
        assert is_retryable(_adapter_error(status_code=401)) is False

    def test_unknown_exception_is_not_retryable(self) -> None:
        """未分类异常保守按不可重试处理。"""
        assert is_retryable(RuntimeError("未知")) is False


class TestWithRetrySuccess:
    """with_retry 的成功路径。"""

    async def test_first_attempt_succeeds_without_retry(self, waits: list[float]) -> None:
        """首次成功即返回，不产生任何等待，也不重复调用工厂。"""
        factory, calls = _factory("成功结果")
        result = await with_retry(factory)
        assert result == "成功结果"
        assert calls == [0]
        assert waits == []

    async def test_returns_factory_value_on_late_success(self, waits: list[float]) -> None:
        """失败若干次后成功，返回的是成功那次的值而非最后一次失败。"""
        factory, calls = _factory(_adapter_error(status_code=500), "最终结果")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "最终结果"
        assert calls == [0, 1]


class TestWithRetryRetries:
    """with_retry 对可重试分类的重试行为。"""

    @pytest.mark.parametrize("status_code", [429, 500, 503])
    async def test_retries_then_succeeds(
        self, waits: list[float], status_code: int
    ) -> None:
        """限流与 5xx 均重试一次后成功，返回值来自成功那次调用。"""
        factory, calls = _factory(_adapter_error(status_code=status_code), "重试成功")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "重试成功"
        # 断言恰好两次尝试：不多试一次，也确实重试了
        assert calls == [0, 1]

    async def test_retries_timeout_then_succeeds(self, waits: list[float]) -> None:
        """无状态码但 error_type 为超时的 AdapterError 同样可重试。"""
        factory, calls = _factory(_adapter_error(error_type="TimeoutException"), "超时后成功")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "超时后成功"
        assert calls == [0, 1]

    async def test_retries_network_then_succeeds(self, waits: list[float]) -> None:
        """网络族异常可重试。"""
        factory, calls = _factory(_adapter_error(error_type="ConnectError"), "网络后成功")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "网络后成功"
        assert calls == [0, 1]

    async def test_retries_httpx_native_timeout(self, waits: list[float]) -> None:
        """未经适配器包装的 httpx 原生异常也能触发重试。"""
        factory, calls = _factory(httpx.ConnectTimeout("连接超时"), "原生异常后成功")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "原生异常后成功"
        assert calls == [0, 1]


class TestWithRetryNoRetry:
    """with_retry 对不可重试分类的行为。"""

    @pytest.mark.parametrize("status_code", [401, 403, 400, 404])
    async def test_client_and_auth_errors_are_not_retried(
        self, waits: list[float], status_code: int
    ) -> None:
        """401/403/其他 4xx 直接抛出：工厂只被调用一次，且不等待。"""
        factory, calls = _factory(_adapter_error(status_code=status_code))
        with pytest.raises(AdapterError) as excinfo:
            await with_retry(factory)
        # 断言异常原样上抛，调用方仍能拿到状态码做归因
        assert excinfo.value.context["status_code"] == status_code
        assert calls == [0]
        assert waits == []

    async def test_unknown_error_is_not_retried(self, waits: list[float]) -> None:
        """未分类异常按不可重试处理，不盲目重试未知故障。"""
        factory, calls = _factory(RuntimeError("未知故障"))
        with pytest.raises(RuntimeError, match="未知故障"):
            await with_retry(factory)
        assert calls == [0]
        assert waits == []

    async def test_non_retryable_does_not_consume_retry_budget(
        self, waits: list[float]
    ) -> None:
        """不可重试错误立即上抛，不进入退避逻辑、也不占用重试次数。"""
        factory, calls = _factory(_adapter_error(status_code=401), "不应被取到")
        with pytest.raises(AdapterError):
            await with_retry(factory, max_retries=5)
        # 第二次尝试根本不存在，证明失败没有被重试掩盖
        assert calls == [0]


class TestWithRetryExhaustion:
    """with_retry 重试次数耗尽的行为。"""

    async def test_raises_after_max_retries(self, waits: list[float]) -> None:
        """max_retries=2 表示最多尝试 3 次，之后抛出最后一次异常。"""
        factory, calls = _factory(_adapter_error(status_code=429, message="第N次失败"))
        with pytest.raises(AdapterError) as excinfo:
            await with_retry(factory, max_retries=2, base_delay=0.0, jitter=0.0)
        # 首次尝试 + 2 次重试 = 3 次
        assert calls == [0, 1, 2]
        assert "第N次失败" in excinfo.value.message

    async def test_raises_last_exception_of_distinct_kinds(
        self, waits: list[float]
    ) -> None:
        """连续多种可重试故障后，抛出的是最后一次那个异常而非首个。"""
        factory, _ = _factory(
            _adapter_error(status_code=429, message="首次限流"),
            _adapter_error(status_code=500, message="转成服务端错误"),
        )
        with pytest.raises(AdapterError) as excinfo:
            await with_retry(factory, max_retries=1, base_delay=0.0, jitter=0.0)
        # 排障需要看到真实失败现场，首个异常在此已被后一次覆盖
        assert excinfo.value.message == "转成服务端错误"

    async def test_max_retries_zero_disables_retry(self, waits: list[float]) -> None:
        """max_retries=0 退化为只试一次，失败即抛。"""
        factory, calls = _factory(_adapter_error(status_code=503))
        with pytest.raises(AdapterError):
            await with_retry(factory, max_retries=0, base_delay=0.0, jitter=0.0)
        assert calls == [0]
        assert waits == []


class TestWithRetryBackoff:
    """with_retry 的指数退避与抖动数值。"""

    async def test_backoff_grows_exponentially_with_jitter(
        self, waits: list[float], fixed_jitter: None
    ) -> None:
        """第 n 次重试等待 base_delay * 2**(n-1) + uniform(0, jitter)。"""
        factory, _ = _factory(_adapter_error(status_code=429))
        with pytest.raises(AdapterError):
            await with_retry(factory, max_retries=2, base_delay=1.0)
        # 1.0*1 + 0.25 = 1.25；1.0*2 + 0.25 = 2.25（抖动固定为 0.25）
        assert waits == pytest.approx([1.25, 2.25])

    async def test_jitter_is_added_each_retry(self, waits: list[float]) -> None:
        """抖动逐次独立采样，每次等待都在退避值之上再加一段随机量。"""
        factory, _ = _factory(_adapter_error(status_code=500))
        with (
            patch.object(retry_module.random, "uniform", side_effect=[0.0, 0.5, 0.25]) as uniform,
            pytest.raises(AdapterError),
        ):
            await with_retry(factory, max_retries=3, base_delay=0.5, jitter=0.5)
        # 抖动上界固定为 0.5，三次采样结果依次叠加到各自的退避值上
        assert uniform.call_args_list == [call(0, 0.5)] * 3
        # 0.5*1+0.0、0.5*2+0.5、0.5*4+0.25
        assert waits == pytest.approx([0.5 + 0.0, 1.0 + 0.5, 2.0 + 0.25])

    async def test_zero_delay_config_waits_zero(self, waits: list[float]) -> None:
        """base_delay=0 且 jitter=0 时确实等待 0 秒（仍走一次 sleep 调用）。"""
        factory, _ = _factory(_adapter_error(status_code=429))
        with pytest.raises(AdapterError):
            await with_retry(factory, max_retries=1, base_delay=0.0, jitter=0.0)
        assert waits == [0.0]


class TestWithRetrySafety:
    """with_retry 的健壮性边界。"""

    async def test_cancellation_is_not_swallowed(self, waits: list[float]) -> None:
        """asyncio.CancelledError 不被重试吞掉，否则任务取消语义会被破坏。"""
        factory, calls = _factory(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert calls == [0]
        assert waits == []

    async def test_each_retry_creates_a_fresh_coroutine(
        self, waits: list[float]
    ) -> None:
        """每次重试都重新调用工厂取得新协程，复用同一协程对象会抛 RuntimeError。"""
        factory, calls = _factory(_adapter_error(status_code=500), "第二次成功")
        result = await with_retry(factory, base_delay=0.0, jitter=0.0)
        assert result == "第二次成功"
        # 工厂被调用两次即证明生成了两个独立协程
        assert len(calls) == 2

    async def test_passing_coroutine_directly_fails_fast(
        self, waits: list[float]
    ) -> None:
        """M1-D07-fix：直接把协程对象当工厂传入，首次尝试即抛 TypeError。

        这是与「工厂复用同一协程对象」**不同的**第二种误用：报错发生在第一次
        调用工厂时（协程对象不可调用），而不是第二次 await 时。docstring 已按
        两种场景分别描述，这里把「首调即 TypeError」钉死，防止表述再次退化。
        """

        async def _work() -> str:
            return "不会走到这里"

        coro = _work()
        try:
            with pytest.raises(TypeError, match="not callable"):
                await with_retry(coro, base_delay=0.0, jitter=0.0)
        finally:
            # 该协程按设计从未被 await，显式 close 掉，避免留下 RuntimeWarning 噪声
            coro.close()
        # 一次都没进重试循环，异常不是被降级成可重试分类再抛的
        assert waits == []

    async def test_reused_coroutine_raises_on_second_await(
        self, waits: list[float]
    ) -> None:
        """M1-D07-fix：工厂复用同一协程对象时，第二次 await 抛 RuntimeError。"""
        shared: Any = None
        state = {"attempt": 0}

        def _factory_reusing_one_coroutine() -> Any:
            # 故意让每次「调用」都返回同一个协程对象：这是真实存在的误用，
            # 首次尝试可能成功，直到真的需要重试才暴露问题
            nonlocal shared
            if shared is None:

                async def _once() -> str:
                    state["attempt"] += 1
                    if state["attempt"] == 1:
                        raise _adapter_error(status_code=500)
                    return "ok"

                shared = _once()
            return shared

        with pytest.raises(RuntimeError, match="already awaited"):
            await with_retry(_factory_reusing_one_coroutine, base_delay=0.0, jitter=0.0)
        # 第二次 await 才炸，证明这与「首次即 TypeError」是两种不同失败
        assert state["attempt"] == 1


class TestWithRetryLogging:
    """with_retry 重试日志的内容与安全性（M1-D07-fix P3-4 回归护栏）。"""

    async def test_retry_log_does_not_leak_sensitive_info(
        self,
        caplog: pytest.LogCaptureFixture,
        waits: list[float],
    ) -> None:
        """重试日志只含分类名/次数/等待时间，不得带出异常消息与 context 取值。

        这是自动化护栏：把异常 message 或 context.response_body 拼进重试日志是
        一行改动，没有本用例时不会有任何测试报警，而这些内容会随重试（高频路径）
        批量进入日志系统。
        """
        secret = _FAKE_SECRET
        body = _SENSITIVE_BODY
        factory, calls = _factory(
            _adapter_error(
                message=f"请求失败，apikey={secret}",
                status_code=429,
                response_body=body,
            ),
            "重试后成功",
        )
        with caplog.at_level(logging.INFO, logger=retry_module.logger.name):
            result = await with_retry(factory, max_retries=1, base_delay=0.0, jitter=0.0)

        assert result == "重试后成功"
        assert calls == [0, 1]
        retry_logs = [r for r in caplog.records if r.name == retry_module.logger.name]
        assert len(retry_logs) == 1, "触发 1 次重试应恰好产出 1 条日志"
        text = retry_logs[0].getMessage()

        # 正向：日志必须包含排障所需的分类名与重试进度
        assert "RATE_LIMITED" in text
        assert "1/1" in text
        # 反向：这两条是护栏的全部意义——一旦日志里出现它们，测试必须立刻变红
        assert secret not in text, "重试日志泄露了异常 message 中的密钥"
        assert body not in text, "重试日志泄露了 context.response_body"
        # 兜底：整个日志对象（含 traceback 文本）都不得携带敏感片段
        assert secret not in caplog.text
        assert body not in caplog.text
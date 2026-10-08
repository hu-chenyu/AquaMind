"""预算熔断：token 与成本双维度限流，达阈值快速失败。

一次评测可能因为用例量、prompt 变长或重试放大而在无人察觉的情况下烧掉大量
token 与费用。预算模块的作用是在**调用真正发生之前**就把这条红线钉死：调用方每
完成一次调用就把用量记到 Budget 上，一旦某维度超过上限立即抛BudgetExceeded，
让调用方尽早停下来，而不是等账单出来才发现。

三个关键约定：

1. **None 表示无上限**，而不是「上限为 0」。构造参数里的 ``float("inf")`` 会被
   归一为 ``None``——``inf`` 若原样存下来，它就是成本求和中一个真实参与运算的
   数字，「无上限」语义会被破坏（配置层的预算字段允许写入 ``inf``，所以这里必须
   兜住）。
2. **达阈值快速失败且不部分扣减**：超限时直接抛异常，用量保持触发前的快照。
   若先累加再抛账本就已经错了，调用方无法据异常判断「到底用掉多少」。
3. **并发安全**：check-then-commit 必须是一个原子步骤。多个协程并发 consume 时，
   若「读当前值 → 判断 → 写回」之间被await 打断，就会出现都读到同一个旧值、
   各自通过校验、超出总额。临界区由 asyncio.Lock 保护。
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .exceptions import BudgetError


class BudgetExceeded(BudgetError):
    """预算超限异常。

    继承 ``BudgetError``（而非直接继承基类 ``AquaMindError``）以便调用方用
    一个 ``except BudgetError`` 就能同时兜住「预算超限」与「预算配置非法」
    两类失败；``context`` 完整携带触发时的账本快照与本次请求量，便于把
    「为什么熔断」直接写进报告而不必回溯调用方日志。
    """


def _normalize_token_limit(value: float | None) -> int | None:
    """把 max_tokens 归一为「非负整数或 None（无上限）」。

    Args:
        value: 构造参数原值。类型标注为 ``float`` 以便接受 ``float('inf')``
            表示显式无上限；按 PEP 484 数值塔，int 会被视为合法 float 实参。

    Returns:
        int | None: 归一后的上限；无上限时返回 None。

    Raises:
        ValueError: 类型不合法、含小数、或为负数时抛出。
    """
    if value is None:
        return None
    if isinstance(value, float):
        # inf 归一为 None：无上限是本模块的一等语义，不该以一个参与求和的数存在
        if math.isinf(value):
            return None
        # 整数值浮点（如 100.0）来源常见（比值计算、JSON 解析），接受但取整；
        # 带小数的则是笔误，没有「最多 100.5 个 token」这种东西
        if value.is_integer():
            return int(value)
        raise ValueError(f"max_tokens 必须是非负整数，当前值含小数: {value!r}")
    # 类型与取值合并成一个判断：bool 是 int 的子类，不单独排除会让
    # Budget(True, ...) 静默变成「上限 1 token」
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"max_tokens 必须是非负整数或 None，当前值: {value!r}")
    return value


def _normalize_cost_limit(value: float | None) -> float | None:
    """把 max_cost 归一为「非负有限浮点或 None（无上限）」。

    Args:
        value: 构造参数原值，允许 int/float 或 None（int 按数值塔视为合法）。

    Returns:
        float | None: 归一后的上限；无上限时返回 None。

    Raises:
        ValueError: 类型不合法、NaN 或为负数时抛出。
    """
    if value is None:
        return None
    # 类型与取值合并成一个判断：bool 是 int 的子类，True 会被静默当成 1.0 美元。
    # NaN 一并在此拦：它与任何数比较都是 False，会让超限判断彻底失效。
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        raise ValueError(f"max_cost 必须是非负数或 None（不能是 NaN），当前值: {value!r}")
    amount = float(value)
    # inf 归一为 None，语义与 token 维度一致。必须排在「是否小于 0」之前：
    # -inf 自身就是 inf，同样表示显式无上限，而不是一个负数上限
    if math.isinf(amount):
        return None
    if amount < 0:
        raise ValueError(f"max_cost 不能为负，当前值: {value!r}")
    return amount


def _validate_amount(tokens: int, cost: float) -> float:
    """校验单次 consume 的用量输入。

    Args:
        tokens: 本次消耗的 token 数，必须是非负整数。
        cost: 本次消耗的成本，必须是非负有限数。

    Returns:
        float: 归一后的 cost。

    Raises:
        ValueError: tokens/cost 类型非法、为负，或 cost 为 NaN/inf 时抛出。
    """
    # bool 是 int 的子类：consume(True) 若放行会被当成消耗 1 个 token，
    # 因此类型与取值合并成一个判断
    if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
        raise ValueError(f"tokens 必须是非负整数，当前值: {tokens!r}")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise ValueError(f"cost 必须是非负数，当前值: {cost!r}")
    amount = float(cost)
    # NaN 与 inf 都不放进账本：它们会污染后续每一次累加与比较，
    # 让「是否超限」这个问题再也得不到可靠答案。
    # （负数已在上一步的合并判断里拦掉，这里不重复判断）
    if not math.isfinite(amount):
        raise ValueError(f"cost 必须是有限非负数（不能是 NaN 或 inf），当前值: {cost!r}")
    return amount


class Budget:
    """单次评测运行的预算账本：token 与成本双维度限流。

    典型用法（由 runner / 负载引擎持有，单次运行创建一个）::

        budget = Budget(max_tokens=settings.max_tokens_per_run,
                        max_cost=settings.max_cost_per_run)
        try:
            await budget.consume(usage.total_tokens, cost)
        except BudgetExceeded:
            ...  # 立刻停止发压并出报告

    上限可在运行期读取但不可变：预算一经设定再被改写，会让「本次运行到底按什么
    预算执行」这件事在报告里无法自证。
    """

    def __init__(
        self,
        # 标注为 float 以便接受 float('inf') 表示显式无上限；按 PEP 484 数值塔，
        # int 实参（如 Budget(100, 1.0)）会被视为合法
        max_tokens: float | None = None,
        max_cost: float | None = None,
    ) -> None:
        """初始化预算账本。

        Args:
            max_tokens: token 上限；None 或 ``float('inf')`` 表示无上限。
            max_cost: 成本上限（美元）；None 或 ``float('inf')`` 表示无上限。

        Raises:
            ValueError: 上限类型非法、含小数/NaN、或为负数时抛出。
        """
        self._max_tokens = _normalize_token_limit(max_tokens)
        self._max_cost = _normalize_cost_limit(max_cost)
        self._current_tokens = 0
        self._current_cost = 0.0
        # Python 3.10+ 的 Lock 在构造时不再要求事件循环，首次 await 时才绑定，
        # 因此 Budget 可以在同步代码里构造、交给异步调用方使用
        self._lock = asyncio.Lock()

    @property
    def max_tokens(self) -> int | None:
        """token 上限；None 表示无上限。"""
        return self._max_tokens

    @property
    def max_cost(self) -> float | None:
        """成本上限；None 表示无上限。"""
        return self._max_cost

    @property
    def current_tokens(self) -> int:
        """已消耗的 token 数。"""
        return self._current_tokens

    @property
    def current_cost(self) -> float:
        """已消耗的成本。"""
        return self._current_cost

    @property
    def remaining_tokens(self) -> int | None:
        """剩余 token 额度；None 表示该维度无上限。"""
        if self._max_tokens is None:
            return None
        return self._max_tokens - self._current_tokens

    @property
    def remaining_cost(self) -> float | None:
        """剩余成本额度；None 表示该维度无上限。"""
        if self._max_cost is None:
            return None
        return self._max_cost - self._current_cost

    @property
    def is_exhausted(self) -> bool:
        """是否任一**有限**维度已经用满（再来一次正数消耗即会熔断）。"""
        if self._max_tokens is not None and self._current_tokens >= self._max_tokens:
            return True
        return self._max_cost is not None and self._current_cost >= self._max_cost

    async def consume(self, tokens: int, cost: float = 0.0) -> None:
        """记账一次消耗；超限则快速失败且不部分扣减。

        「超限」定义为**严格大于**上限：恰好用满（``new == max``）不算超限，
        否则预算会被浪费掉最后一点点余量，也让「用满即停」与「超一点就停」
        两种直觉在代码里对不上。

        Args:
            tokens: 本次消耗的 token 数（非负整数）。
            cost: 本次消耗的成本（非负有限数，美元）。

        Raises:
            ValueError: 输入类型非法或为负时抛出（校验在加锁之前完成，非法输入
                不应占用临界区）。
            BudgetExceeded: 累加后任一维度超过上限时抛出，用量保持触发前快照。
        """
        amount = _validate_amount(tokens, cost)
        # check-then-commit 必须在同一个临界区内：读当前值与写回之间一旦被
        # await 打断，并发调用方就会都读到同一个旧值并各自通过校验，造成超额
        async with self._lock:
            new_tokens = self._current_tokens + tokens
            if self._max_tokens is not None and new_tokens > self._max_tokens:
                raise self._exceeded(
                    dimension="token",
                    current_tokens=self._current_tokens,
                    max_tokens=self._max_tokens,
                    current_cost=self._current_cost,
                    max_cost=self._max_cost,
                    requested_tokens=tokens,
                    requested_cost=amount,
                )
            new_cost = self._current_cost + amount
            if self._max_cost is not None and new_cost > self._max_cost:
                raise self._exceeded(
                    dimension="cost",
                    current_tokens=self._current_tokens,
                    max_tokens=self._max_tokens,
                    current_cost=self._current_cost,
                    max_cost=self._max_cost,
                    requested_tokens=tokens,
                    requested_cost=amount,
                )
            # 两项都通过才写回：超限时账本保持触发前快照，不做部分扣减
            self._current_tokens = new_tokens
            self._current_cost = new_cost

    @staticmethod
    def _exceeded(
        dimension: str,
        *,
        current_tokens: int,
        max_tokens: int | None,
        current_cost: float,
        max_cost: float | None,
        requested_tokens: int,
        requested_cost: float,
    ) -> BudgetExceeded:
        """构造带完整账本快照的 BudgetExceeded。

        Args:
            dimension: 超限维度标识（"token" 或 "cost"），用于拼装可读消息。
            current_tokens: 触发前的已用 token 数。
            max_tokens: token 上限，None 表示该维度无上限。
            current_cost: 触发前的已用成本。
            max_cost: 成本上限，None 表示该维度无上限。
            requested_tokens: 本次请求的 token 数。
            requested_cost: 本次请求的成本。

        Returns:
            BudgetExceeded: 已填充 message 与 context 的异常实例。
        """
        # 统一标注为 int | float：token 维度取整、cost 维度取浮点，
        # 直接复用同名局部变量拼消息，避免两个分支各写一遍格式化逻辑
        used: int | float
        limit: int | float | None
        requested: int | float
        unit: str
        if dimension == "token":
            used, limit, requested, unit = current_tokens, max_tokens, requested_tokens, "tokens"
        else:
            used, limit, requested, unit = current_cost, max_cost, requested_cost, "美元"
        message = (
            f"预算超限（{dimension} 维度）：已用 {used} + 本次 {requested} "
            f"> 上限 {limit} {unit}"
        )
        context: dict[str, Any] = {
            "dimension": dimension,
            "current_tokens": current_tokens,
            "max_tokens": max_tokens,
            "current_cost": current_cost,
            "max_cost": max_cost,
            "requested_tokens": requested_tokens,
            "requested_cost": requested_cost,
        }
        return BudgetExceeded(message=message, context=context)
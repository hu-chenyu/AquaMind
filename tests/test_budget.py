"""预算熔断模块单元测试。

覆盖范围：
- 构造与上限归一：None / float('inf') 均视为无上限；非法上限被拒
- 记账：未超限正常累加、恰好用满不算超限、超限抛 BudgetExceeded 且不部分扣减
- 只读视图：max/current/remaining/is_exhausted 在有上限与无上限两种情形下的取值
- 并发安全：asyncio.gather 真正并发下的总额不超额、恰好超限的那次抛异常、
  并与「无锁对照组」比较证明锁确实挡住了 check-then-commit 竞态
- 输入校验：tokens/cost 的类型、非负性与有限性
- 异常契约：继承链、context 字段完整、__str__ 只渲染键名

所有测试不触真实 API（零 key），也不依赖 CWD 下的 .env。
"""

from __future__ import annotations

import asyncio
import math

import pytest

from aquamind.budget import Budget, BudgetExceeded
from aquamind.exceptions import AquaMindError, BudgetError


class TestConstruction:
    """构造与上限存储。"""

    def test_budget_creation_normal(self) -> None:
        """正常构造后两个上限正确存储，用量归零。"""
        budget = Budget(max_tokens=100, max_cost=1.5)
        assert budget.max_tokens == 100
        assert budget.max_cost == 1.5
        # 账本从零开始，不因上限非零而预扣
        assert budget.current_tokens == 0
        assert budget.current_cost == 0.0

    def test_default_construction_is_unlimited(self) -> None:
        """不给参数时两个维度都是无上限，且不处于已耗尽状态。"""
        budget = Budget()
        assert budget.max_tokens is None
        assert budget.max_cost is None
        assert budget.is_exhausted is False

    def test_integral_float_token_limit_accepted(self) -> None:
        """整数值浮点上限（100.0，常见于比值计算/JSON）应被接受并取整。"""
        assert Budget(max_tokens=100.0).max_tokens == 100

    def test_no_shared_state_between_instances(self) -> None:
        """两个 Budget 实例的账本互不影响（不能用类属性存用量）。"""
        first, second = Budget(100, 1.0), Budget(100, 1.0)
        asyncio.run(first.consume(50, 0.5))
        assert first.current_tokens == 50
        assert second.current_tokens == 0


class TestUnlimitedSemantics:
    """None 与 float('inf') 都表示无上限。"""

    async def test_none_limit_unlimited_tokens(self) -> None:
        """max_tokens=None 时大额 consume 不熔断。"""
        budget = Budget(max_tokens=None, max_cost=1.0)
        await budget.consume(10**9, 0.0)
        assert budget.current_tokens == 10**9
        assert budget.remaining_tokens is None

    async def test_none_limit_unlimited_cost(self) -> None:
        """max_cost=None 时大额 consume 不熔断。"""
        budget = Budget(max_tokens=100, max_cost=None)
        await budget.consume(0, 10**9)
        assert budget.current_cost == pytest.approx(10**9)
        assert budget.remaining_cost is None

    async def test_inf_treated_as_none_tokens(self) -> None:
        """float('inf') 上限必须被归一为 None，而不是一个参与运算的数。"""
        budget = Budget(max_tokens=float("inf"), max_cost=1.0)
        # 归一结果本身就得是 None，否则后续比较会被 inf 语义污染
        assert budget.max_tokens is None
        await budget.consume(10**9, 0.1)
        assert budget.current_tokens == 10**9

    async def test_inf_treated_as_none_cost(self) -> None:
        """max_cost=float('inf') 同样归一为 None。"""
        budget = Budget(max_tokens=100, max_cost=float("inf"))
        assert budget.max_cost is None
        await budget.consume(0, 10**9)
        assert budget.current_cost == pytest.approx(10**9)

    async def test_both_inf_allows_large_consume(self) -> None:
        """两个维度都是 inf 时，第5段验收的大额调用必须通过。"""
        budget = Budget(float("inf"), float("inf"))
        await budget.consume(999999, 999999.0)
        assert budget.current_tokens == 999999
        assert budget.current_cost == pytest.approx(999999.0)

    async def test_negative_inf_treated_as_none(self) -> None:
        """-inf 同样表示「无上限」；让它落进比较会在加任何值时立刻误判超限。"""
        assert Budget(max_tokens=-float("inf")).max_tokens is None
        assert Budget(max_cost=-float("inf")).max_cost is None


class TestConsume:
    """记账与熔断。"""

    async def test_consume_within_limit(self) -> None:
        """未超阈值时正常累加，用量与剩余额度都对得上。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        await budget.consume(40, 0.4)
        await budget.consume(30, 0.3)
        assert budget.current_tokens == 70
        assert budget.current_cost == pytest.approx(0.7)
        assert budget.remaining_tokens == 30
        assert budget.remaining_cost == pytest.approx(0.3)

    async def test_consume_zero(self) -> None:
        """consume(0, 0.0) 是合法的空记账，不应报错也不应改变账本。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        await budget.consume(0, 0.0)
        assert budget.current_tokens == 0
        assert budget.current_cost == 0.0

    async def test_consume_default_cost(self) -> None:
        """cost 省略时默认为 0.0（只记 token 的调用是常态）。"""
        budget = Budget(max_tokens=100)
        await budget.consume(10)
        assert budget.current_tokens == 10
        assert budget.current_cost == 0.0

    async def test_exceed_max_tokens_raises(self) -> None:
        """token 维度超限立即熔断。"""
        budget = Budget(max_tokens=100, max_cost=1000.0)
        await budget.consume(60)
        with pytest.raises(BudgetExceeded):
            await budget.consume(41)

    async def test_exceed_max_cost_raises(self) -> None:
        """cost 维度超限立即熔断。"""
        budget = Budget(max_tokens=10**9, max_cost=1.0)
        await budget.consume(0, 0.9)
        with pytest.raises(BudgetExceeded):
            await budget.consume(0, 0.2)

    async def test_exact_limit_not_exceeded(self) -> None:
        """恰好等于上限不算超限（判据是严格大于），否则会白白浪费最后一点额度。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        await budget.consume(100, 1.0)
        assert budget.current_tokens == 100
        assert budget.is_exhausted is True
        # 用满之后再消费任意正数才会熔断
        with pytest.raises(BudgetExceeded):
            await budget.consume(1)

    async def test_exceed_does_not_partial_deduct(self) -> None:
        """超限时账本保持触发前快照，不做部分扣减。"""
        budget = Budget(max_tokens=100, max_cost=1000.0)
        await budget.consume(50, 0.5)
        with pytest.raises(BudgetExceeded):
            await budget.consume(100, 0.5)
        # 关键断言：若先累加再抛账本就已经错了，调用方无法判断到底用掉多少
        assert budget.current_tokens == 50
        assert budget.current_cost == pytest.approx(0.5)

    async def test_failed_cost_deduct_does_not_affect_tokens(self) -> None:
        """cost 超限的那一次，token 也不能被部分扣减。"""
        budget = Budget(max_tokens=1000, max_cost=1.0)
        await budget.consume(10, 0.5)
        with pytest.raises(BudgetExceeded):
            await budget.consume(100, 1.0)
        assert budget.current_tokens == 10
        assert budget.current_cost == pytest.approx(0.5)

    async def test_budget_usable_again_after_partial_series(self) -> None:
        """一次超限后账本未变，小额消费仍可继续（熔断不是把对象作废）。"""
        budget = Budget(max_tokens=100, max_cost=1000.0)
        await budget.consume(90)
        with pytest.raises(BudgetExceeded):
            await budget.consume(50)
        await budget.consume(10)
        assert budget.current_tokens == 100


class TestRemainingViews:
    """只读视图属性。"""

    def test_remaining_calculation(self) -> None:
        """remaining = 上限 - 已用，两个维度独立计算。"""
        budget = Budget(max_tokens=100, max_cost=2.0)
        asyncio.run(budget.consume(30, 0.5))
        assert budget.remaining_tokens == 70
        assert budget.remaining_cost == pytest.approx(1.5)

    def test_remaining_none_when_unlimited(self) -> None:
        """无上限维度返回 None 而不是 float('inf')，避免下游误以为还有额度。"""
        budget = Budget(max_tokens=None, max_cost=None)
        assert budget.remaining_tokens is None
        assert budget.remaining_cost is None

    def test_remaining_can_go_negative_after_breach(self) -> None:
        """账本不会被超限调用改动，因此 remaining 只会反映真实已用量。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        asyncio.run(budget.consume(80, 0.8))
        assert budget.remaining_tokens == 20
        assert budget.remaining_cost == pytest.approx(0.2)

    def test_is_exhausted_false_while_budget_left(self) -> None:
        """尚有余额时不算耗尽。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        asyncio.run(budget.consume(1, 0.1))
        assert budget.is_exhausted is False

    def test_is_exhausted_true_when_tokens_full(self) -> None:
        """token 维度用满即耗尽，即使成本维度还有余额。"""
        budget = Budget(max_tokens=100, max_cost=1000.0)
        asyncio.run(budget.consume(100, 0.1))
        assert budget.is_exhausted is True

    def test_is_exhausted_true_when_cost_full(self) -> None:
        """成本维度用满即耗尽，即使 token 维度还有余额。"""
        budget = Budget(max_tokens=10**9, max_cost=1.0)
        asyncio.run(budget.consume(1, 1.0))
        assert budget.is_exhausted is True

    def test_is_exhausted_false_when_all_unlimited(self) -> None:
        """全无上限时永远不会耗尽。"""
        assert Budget().is_exhausted is False


class TestConcurrency:
    """并发安全：check-then-commit 必须是原子的。"""

    async def test_concurrent_consume_no_overage(self) -> None:
        """并发消费不超额：最终用量恰好等于上限，绝不越过。"""
        budget = Budget(max_tokens=100, max_cost=1.0)

        async def _one() -> None:
            await budget.consume(10, 0.1)

        await asyncio.gather(*[_one() for _ in range(10)])
        # 若竞态存在，这里会大于 100 —— 这是本组测试的核心断言
        assert budget.current_tokens == 100
        assert budget.current_cost == pytest.approx(1.0, abs=1e-9)

    async def test_concurrent_consume_exact_total(self) -> None:
        """并发记账无丢失、无重复：总额等于各次之和。"""
        budget = Budget(max_tokens=10**6, max_cost=10**6)
        amounts = [1, 2, 3, 5, 8, 13, 21, 34]

        async def _one(amount: int) -> None:
            await budget.consume(amount, float(amount))

        await asyncio.gather(*[_one(amount) for amount in amounts])
        assert budget.current_tokens == sum(amounts)
        assert budget.current_cost == pytest.approx(float(sum(amounts)))

    async def test_concurrent_exceed_raises_once(self) -> None:
        """并发消费中恰好越过上限的那些次抛异常，其余正常完成。"""
        budget = Budget(max_tokens=100, max_cost=10**6)
        outcomes: list[str] = []

        async def _one() -> None:
            try:
                await budget.consume(10)
            except BudgetExceeded:
                outcomes.append("exceeded")
            else:
                outcomes.append("ok")

        # 15 次 × 10 token，上限 100：10 次成功，5 次熔断
        await asyncio.gather(*[_one() for _ in range(15)])
        assert outcomes.count("ok") == 10
        assert outcomes.count("exceeded") == 5
        # 账本恰好停在上限，而不是被并发写入推到 150
        assert budget.current_tokens == 100

    async def test_lock_protects_critical_section(self) -> None:
        """与「无锁对照组」比较，证明锁真的挡住了 check-then-commit 竞态。

        对照组刻意在「校验通过」与「写回账本」之间 await 一次让出控制权——
        这正是 asyncio 调度器最常见的插入点。若Budget 不加锁，此时会有多个
        协程同时通过校验并各自写回，造成总额远超上限。
        """

        class _NaiveBudget:
            """故意不加锁的对照组，用于暴露竞态。"""

            def __init__(self, limit: int) -> None:
                self.limit = limit
                self.current = 0

            async def consume(self, amount: int) -> None:
                if self.current + amount > self.limit:
                    raise BudgetExceeded("naive budget exceeded")
                await asyncio.sleep(0)  # 竞态窗口：交出控制权
                self.current += amount

        naive = _NaiveBudget(limit=100)
        guarded = Budget(max_tokens=100)

        async def _consume_naive() -> None:
            try:
                await naive.consume(10)
            except BudgetExceeded:
                pass

        async def _consume_guarded() -> None:
            try:
                await guarded.consume(10)
            except BudgetExceeded:
                pass

        await asyncio.gather(*[_consume_naive() for _ in range(20)])
        await asyncio.gather(*[_consume_guarded() for _ in range(20)])

        # 工作线程数（20×10=200）必须大于上限，否则全部通过校验后总量恰好等于
        # 上限，竞态被掩盖。无锁版本被冲破（200 > 100）；带锁版本精确停在 100
        assert naive.current > 100, "对照组本应复现超额，若未复现说明竞态未被构造出来"
        assert naive.current == 200
        assert guarded.current_tokens == 100


class TestInputValidation:
    """构造参数与 consume 入参的校验。"""

    @pytest.mark.parametrize("bad", [-1, -100])
    def test_negative_max_tokens_rejected(self, bad: int) -> None:
        """负数上限非法。"""
        with pytest.raises(ValueError, match="max_tokens"):
            Budget(max_tokens=bad)

    def test_fractional_max_tokens_rejected(self) -> None:
        """带小数的 token 上限非法（不存在「最多 100.5 个 token」）。"""
        with pytest.raises(ValueError, match="max_tokens"):
            Budget(max_tokens=100.5)

    @pytest.mark.parametrize("bad", ["100", [100], object()])
    def test_non_numeric_max_tokens_rejected(self, bad: object) -> None:
        """非数值类型的上限必须被拒，而不是在后续比较里抛裸 TypeError。"""
        with pytest.raises(ValueError, match="max_tokens"):
            Budget(max_tokens=bad)  # type: ignore[arg-type]

    def test_bool_max_tokens_rejected(self) -> None:
        """bool 是 int 子类，必须显式拒绝，否则 True 会变成「上限 1 token」。"""
        with pytest.raises(ValueError, match="max_tokens"):
            Budget(max_tokens=True)

    def test_nan_max_cost_rejected(self) -> None:
        """NaN 上限必须被拒：它与任何数比较都是 False，会让超限判断彻底失效。"""
        with pytest.raises(ValueError, match="max_cost"):
            Budget(max_cost=math.nan)

    @pytest.mark.parametrize("bad", [-0.1, -1.0])
    def test_negative_max_cost_rejected(self, bad: float) -> None:
        """负数成本上限非法。"""
        with pytest.raises(ValueError, match="max_cost"):
            Budget(max_cost=bad)

    @pytest.mark.parametrize("bad", ["1.0", [1.0]])
    def test_non_numeric_max_cost_rejected(self, bad: object) -> None:
        """非数值类型的成本上限必须被拒。"""
        with pytest.raises(ValueError, match="max_cost"):
            Budget(max_cost=bad)  # type: ignore[arg-type]

    def test_bool_max_cost_rejected(self) -> None:
        """bool 成本上限必须被拒，否则 True 会被当成 1.0 美元。"""
        with pytest.raises(ValueError, match="max_cost"):
            Budget(max_cost=True)

    @pytest.mark.parametrize("bad", [-1, -100])
    async def test_negative_tokens_rejected(self, bad: int) -> None:
        """consume 的负数 token 必须被拒。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        with pytest.raises(ValueError, match="tokens"):
            await budget.consume(bad)

    @pytest.mark.parametrize("bad", ["10", [10], 1.5, True])
    async def test_invalid_tokens_type_rejected(self, bad: object) -> None:
        """token 必须是整数；浮点/字符串/布尔都不接受。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        with pytest.raises(ValueError, match="tokens"):
            await budget.consume(bad)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [-0.01, -1.0])
    async def test_negative_cost_rejected(self, bad: float) -> None:
        """consume 的负数成本必须被拒（负数退款会凭空造出额度）。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        with pytest.raises(ValueError, match="cost"):
            await budget.consume(1, bad)

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    async def test_non_finite_cost_rejected(self, bad: float) -> None:
        """NaN/inf 成本必须被拒：它们一旦进入账本会污染后续每一次累加与比较。"""
        budget = Budget(max_tokens=100, max_cost=1000.0)
        with pytest.raises(ValueError, match="cost"):
            await budget.consume(1, bad)

    @pytest.mark.parametrize("bad", ["0.1", [0.1], True])
    async def test_invalid_cost_type_rejected(self, bad: object) -> None:
        """非数值类型的成本必须被拒。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        with pytest.raises(ValueError, match="cost"):
            await budget.consume(1, bad)  # type: ignore[arg-type]

    async def test_invalid_input_leaves_ledger_untouched(self) -> None:
        """校验失败不应污染账本（校验在加锁之前完成，且先于任何写回）。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        await budget.consume(10, 0.1)
        with pytest.raises(ValueError):
            await budget.consume(-5)
        assert budget.current_tokens == 10
        assert budget.current_cost == pytest.approx(0.1)


class TestExceptionContract:
    """BudgetExceeded 的继承关系、context 与防泄露契约。"""

    async def test_budget_exceeded_inherits_budget_error(self) -> None:
        """继承链为 BudgetExceeded -> BudgetError -> AquaMindError -> Exception。"""
        budget = Budget(max_tokens=1)
        with pytest.raises(BudgetError):
            await budget.consume(2)
        # 调用方用一个 except BudgetError 就能同时兜住超限与配置非法
        assert issubclass(BudgetExceeded, BudgetError)
        assert issubclass(BudgetExceeded, AquaMindError)

    async def test_budget_exceeded_context_complete(self) -> None:
        """context 完整携带账本快照与本次请求量，报告可直接引用。"""
        budget = Budget(max_tokens=100, max_cost=1.0)
        await budget.consume(80, 0.8)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(30, 0.1)
        context = excinfo.value.context
        for key in (
            "current_tokens",
            "max_tokens",
            "current_cost",
            "max_cost",
            "requested_tokens",
            "requested_cost",
        ):
            assert key in context, f"context 缺少 {key}"
        # 快照必须是**触发前**的账本，而不是已累加的结果
        assert context["current_tokens"] == 80
        assert context["max_tokens"] == 100
        assert context["requested_tokens"] == 30
        assert context["current_cost"] == pytest.approx(0.8)
        assert context["max_cost"] == pytest.approx(1.0)
        assert context["requested_cost"] == pytest.approx(0.1)
        assert context["dimension"] == "token"

    async def test_budget_exceeded_context_dimension_cost(self) -> None:
        """成本维度熔断时 context 的 dimension 标记为 cost。"""
        budget = Budget(max_tokens=10**6, max_cost=1.0)
        await budget.consume(0, 0.9)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(0, 0.5)
        assert excinfo.value.context["dimension"] == "cost"
        assert excinfo.value.context["current_cost"] == pytest.approx(0.9)

    async def test_budget_exceeded_message_token_dimension(self) -> None:
        """token 维度消息点明维度、已用、本次与上限，便于直接写进报告。"""
        budget = Budget(max_tokens=100)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(101)
        message = excinfo.value.message
        assert "token" in message
        assert "101" in message
        assert "100" in message

    async def test_budget_exceeded_message_cost_dimension(self) -> None:
        """成本维度消息点明维度与上限。"""
        budget = Budget(max_cost=1.0)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(0, 2.0)
        message = excinfo.value.message
        assert "cost" in message
        assert "1.0" in message

    async def test_budget_exceeded_str_no_values(self) -> None:
        """str(exc) 不得把 context 字典连同取值一起渲染（M1-D01 防泄露契约）。

        注意区分两处数字来源：message 里的用量数字是**有意**写进去的
        （任务要求消息点明维度/当前值/阈值，且预算数值本身不敏感）；
        需要杜绝的是 ``context`` 被整体 repr 出来的泄露通道。
        """
        budget = Budget(max_tokens=100)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(101)
        text = str(excinfo.value)
        # 键名必须保留（排障得知道有哪些上下文可用）
        assert "context keys:" in text
        assert "current_tokens" in text
        # 字典渲染形态不得出现：{...} 或 'key': value 形式
        assert "'current_tokens':" not in text
        assert "{" not in text

    async def test_budget_exceeded_repr_no_values(self) -> None:
        """repr(exc) 同样不渲染 context 取值（M1-P2 修复 6 已加固基类）。"""
        budget = Budget(max_tokens=100)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(101)
        text = repr(excinfo.value)
        assert "'max_tokens':" not in text
        assert "context keys:" in text
        assert "{" not in text

    async def test_budget_exceeded_preserves_chain(self) -> None:
        """异常链从 consume 直接抛出，无需额外包装，__cause__ 允许为 None。"""
        budget = Budget(max_tokens=100)
        with pytest.raises(BudgetExceeded) as excinfo:
            await budget.consume(101)
        assert isinstance(excinfo.value, AquaMindError)
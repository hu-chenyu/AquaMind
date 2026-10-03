"""适配器层单元测试。

覆盖：CallableAdapter 正常调用与 messages 透传、AdapterResponse 结构归一与严格模式
（extra="forbid"）、BaseAdapter 抽象约束（不可实例化 / 子类必须实现 acomplete）、
stream=True 不支持时的 AdapterError 拦截、后端异常包装为 AdapterError 并保留异常链、
非 str 返回导致的归一化失败包装、多层异常链完整保留。
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from aquamind.adapters import AdapterResponse, BaseAdapter, CallableAdapter
from aquamind.exceptions import AdapterError

# 供各测试复用的最小 OpenAI 格式消息列表
_MESSAGES: list[dict[str, str]] = [{"role": "user", "content": "你好"}]


class _IncompleteAdapter(BaseAdapter):
    """仅继承 BaseAdapter 而不实现 acomplete，用于验证抽象方法约束。"""


class _DelegatingAdapter(BaseAdapter):
    """已实现 acomplete 但显式转交基类默认实现，用于验证基类的兜底行为。"""

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        stream: bool = False,
    ) -> AdapterResponse:
        """把调用转交给 BaseAdapter.acomplete 的默认实现（正常子类不应这样写）。"""
        return await super().acomplete(messages, stream)


class TestCallableAdapter:
    """测试本地函数适配器的调用与归一化行为。"""

    def test_returns_adapter_response(self) -> None:
        """CallableAdapter 应把函数返回值归一为 AdapterResponse。"""
        adapter = CallableAdapter(lambda messages: "固定输出")
        response = asyncio.run(adapter.acomplete(_MESSAGES))
        assert isinstance(response, AdapterResponse)
        assert response.content == "固定输出"
        # metadata 标识来源适配器，便于后续报告归因
        assert response.metadata["adapter"] == "callable"

    def test_passes_messages_through(self) -> None:
        """被包装函数收到的 messages 应与传入完全一致。"""
        captured: list[list[dict[str, str]]] = []

        def _recorder(messages: list[dict[str, str]]) -> str:
            """记录入参并返回固定输出。"""
            captured.append(messages)
            return "ok"

        asyncio.run(CallableAdapter(_recorder).acomplete(_MESSAGES))
        assert captured == [_MESSAGES]

    def test_response_structure_normalized(self) -> None:
        """归一结构：仅含 content/raw/metadata 三字段，raw 保存函数原始返回值。"""
        adapter = CallableAdapter(lambda messages: "原始值")
        response = asyncio.run(adapter.acomplete(_MESSAGES))
        assert set(response.model_dump()) == {"content", "raw", "metadata"}
        # CallableAdapter 中 raw 即函数原始返回值，故与 content 相同
        assert response.raw == response.content == "原始值"

    def test_stream_not_supported(self) -> None:
        """stream=True 时应抛 AdapterError，消息含“流式”且 context 标记 stream。"""
        adapter = CallableAdapter(lambda messages: "unused")
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(adapter.acomplete(_MESSAGES, stream=True))
        assert "流式" in str(exc_info.value)
        assert exc_info.value.context["stream"] is True
        assert exc_info.value.context["adapter"] == "callable"
        assert "流式" in exc_info.value.context["reason"]

    def test_backend_exception_wrapped(self) -> None:
        """fn 抛出的异常应被包装为 AdapterError，并保留原始异常链与类型信息。"""

        def _boom(messages: list[dict[str, str]]) -> str:
            """模拟后端故障：无论入参如何都抛 ValueError。"""
            raise ValueError(f"模拟后端故障（入参 {len(messages)} 条消息）")

        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(CallableAdapter(_boom).acomplete(_MESSAGES))
        assert "调用失败" in str(exc_info.value)
        # 异常链保留：__cause__ 应指向原始 ValueError，便于定位真实故障
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert exc_info.value.context["adapter"] == "callable"
        assert exc_info.value.context["error_type"] == "ValueError"

    def test_non_str_return_wrapped(self) -> None:
        """fn 返回非 str（如 dict）时归一化失败，应被包装为 AdapterError。"""

        def _returns_dict(messages: list[dict[str, str]]) -> str:
            """模拟后端返回结构体（与 content: str 契约不符）。"""
            payload = {"result": "hello", "message_count": len(messages)}
            return payload  # type: ignore[return-value]

        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(CallableAdapter(_returns_dict).acomplete(_MESSAGES))
        assert "调用失败" in str(exc_info.value)
        # 异常链保留：__cause__ 应为 pydantic 的 ValidationError（归一化失败）
        assert isinstance(exc_info.value.__cause__, ValidationError)
        assert exc_info.value.context["adapter"] == "callable"
        assert exc_info.value.context["error_type"] == "ValidationError"

    def test_nested_exception_chain_preserved(self) -> None:
        """fn 内部的多层异常链应被完整保留，不因适配层包装而丢失中间层。"""

        def _raises_nested(messages: list[dict[str, str]]) -> str:
            """构造两层异常链：KeyError 被包装为 ConnectionError。"""
            try:
                raise KeyError(f"底层故障（入参 {len(messages)} 条消息）")
            except KeyError as ke:
                raise ConnectionError("上层故障") from ke

        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(CallableAdapter(_raises_nested).acomplete(_MESSAGES))
        # 第一层：适配层包装的原始异常应为 ConnectionError
        assert isinstance(exc_info.value.__cause__, ConnectionError)
        # 第二层：原始异常自身的 cause 链应完整保留（KeyError），便于逐层定位
        assert isinstance(exc_info.value.__cause__.__cause__, KeyError)
        assert exc_info.value.context["error_type"] == "ConnectionError"


class TestAdapterResponseContract:
    """测试 AdapterResponse 的契约约束（严格模式禁止未知字段）。"""

    def test_extra_fields_forbidden(self) -> None:
        """AdapterResponse 传入未知字段时应抛 ValidationError（extra_forbidden）。"""
        with pytest.raises(ValidationError) as exc_info:
            AdapterResponse(content="x", bogus=1)
        # 错误类型须为 extra_forbidden：验证严格模式生效，键名拼写错误不会被静默忽略
        assert any(e["type"] == "extra_forbidden" for e in exc_info.value.errors())


class TestBaseAdapterContract:
    """测试 BaseAdapter 的抽象契约约束。"""

    def test_cannot_instantiate_base_adapter(self) -> None:
        """BaseAdapter 是抽象类，直接实例化应抛 TypeError。"""
        with pytest.raises(TypeError) as exc_info:
            BaseAdapter()  # type: ignore[abstract]
        # ABC 在实例化阶段即拒绝，错误消息应指出具体类名
        assert "BaseAdapter" in str(exc_info.value)

    def test_subclass_must_implement_acomplete(self) -> None:
        """未实现 acomplete 的子类在实例化阶段即被 ABC 拒绝。"""
        with pytest.raises(TypeError) as exc_info:
            _IncompleteAdapter()  # type: ignore[abstract]
        assert "_IncompleteAdapter" in str(exc_info.value)


class TestBaseAdapterDefaultImplementation:
    """测试 BaseAdapter.acomplete 的兜底实现（抽象方法体本身）。"""

    def test_super_acomplete_raises_not_implemented(self) -> None:
        """转交基类的 acomplete 应抛 NotImplementedError，而不是静默返回 None。"""
        adapter = _DelegatingAdapter()
        with pytest.raises(NotImplementedError) as exc_info:
            asyncio.run(adapter.acomplete(_MESSAGES))
        # 基类兜底必须是明确的 NotImplementedError：实现缺失时要当场暴露，
        # 否则上层会拿到 None 并在后续归一化环节报出难以定位的错误
        assert type(exc_info.value) is NotImplementedError

    def test_super_acomplete_raises_for_stream_request(self) -> None:
        """stream=True 走到基类兜底时同样抛 NotImplementedError（基类不做流式分支）。"""
        adapter = _DelegatingAdapter()
        with pytest.raises(NotImplementedError):
            asyncio.run(adapter.acomplete(_MESSAGES, stream=True))

    def test_acomplete_is_abstract_and_async(self) -> None:
        """acomplete 须同时是抽象方法与协程函数，保证子类沿用异步签名。"""
        assert BaseAdapter.acomplete.__isabstractmethod__ is True
        assert asyncio.iscoroutinefunction(BaseAdapter.acomplete)


class TestCallableAdapterConcurrency:
    """测试同步 fn 卸载到线程池、异步 fn 直接 await（P2-5 + P3-1）。

    回归背景：原实现在协程内直接同步调用 ``self.fn(messages)``，会阻塞事件循环，
    使 ``asyncio.gather`` 的并发调用退化为串行——对一个以并发压测为目标的工具
    而言会让压测结果彻底失真。
    """

    def test_sync_fn_does_not_block_event_loop(self) -> None:
        """4 个各 sleep 100ms 的同步 fn 并发执行，总耗时应远小于串行的 400ms。"""
        import time

        def _slow(messages: list[dict[str, str]]) -> str:
            """模拟带延迟的本地 SUT。"""
            time.sleep(0.1)
            return "ok"

        adapter = CallableAdapter(_slow)

        async def _drive() -> None:
            start = time.perf_counter()
            results = await asyncio.gather(
                *(adapter.acomplete(_MESSAGES) for _ in range(4)),
            )
            elapsed = time.perf_counter() - start
            assert all(r.content == "ok" for r in results)
            # 串行约 0.4s，并发约 0.1s；取 0.3s 为界以容忍 CI 抖动
            assert elapsed < 0.3, f"同步 fn 疑似阻塞事件循环，实际耗时 {elapsed:.3f}s"

        asyncio.run(_drive())

    def test_async_fn_is_awaited(self) -> None:
        """传入 async fn 应被直接 await 并正常返回，不再报 ValidationError。"""

        async def _async_sut(messages: list[dict[str, str]]) -> str:
            """异步本地 SUT。"""
            await asyncio.sleep(0)
            return "异步输出"

        adapter = CallableAdapter(_async_sut)  # type: ignore[arg-type]
        response = asyncio.run(adapter.acomplete(_MESSAGES))
        assert response.content == "异步输出"

    def test_async_fn_emits_no_never_awaited_warning(self, recwarn: pytest.WarningsRecorder) -> None:
        """async fn 路径不得产生“协程未 await”的 RuntimeWarning（无协程泄漏）。"""

        async def _async_sut(messages: list[dict[str, str]]) -> str:
            """异步本地 SUT。"""
            return "异步输出"

        adapter = CallableAdapter(_async_sut)  # type: ignore[arg-type]
        asyncio.run(adapter.acomplete(_MESSAGES))
        leaked = [w for w in recwarn.list if "never awaited" in str(w.message)]
        assert leaked == [], f"检测到未 await 的协程: {[str(w.message) for w in leaked]}"

    def test_async_fn_exception_wrapped(self) -> None:
        """async fn 抛出的异常同样应被包装为 AdapterError 并保留异常链。"""

        async def _boom(messages: list[dict[str, str]]) -> str:
            """模拟异步 SUT 故障。"""
            raise ValueError("异步后端故障")

        adapter = CallableAdapter(_boom)  # type: ignore[arg-type]
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(adapter.acomplete(_MESSAGES))
        assert exc_info.value.context["error_type"] == "ValueError"
        assert isinstance(exc_info.value.__cause__, ValueError)
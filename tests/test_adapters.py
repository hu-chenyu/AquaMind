"""适配器层单元测试。

覆盖：CallableAdapter 正常调用与 messages 透传、AdapterResponse 结构归一与严格模式
（extra="forbid"）、BaseAdapter 抽象约束（不可实例化 / 子类必须实现 acomplete）、
stream=True 不支持时的 AdapterError 拦截、后端异常包装为 AdapterError 并保留异常链。
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
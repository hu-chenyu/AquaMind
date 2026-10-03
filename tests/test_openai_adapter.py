"""OpenAI 兼容非流式适配器单元测试。

覆盖：正常调用与响应归一（content/raw/metadata）、鉴权头开关、base_url 末尾斜杠归一、
model 与多轮 messages 透传、timeout 透传、stream 不支持拦截、HTTP 状态码错误
（消息含状态码与响应体片段）、网络异常（连接失败/超时）包装与异常链保留、
响应结构防御性校验（JSON 解析失败/顶层非对象/choices/message/content）。

所有 HTTP 交互均由 httpx.MockTransport 模拟，不触真实 API（零 key）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from aquamind.adapters import OpenAIAdapter
from aquamind.exceptions import AdapterError

_BASE_URL = "https://api.example.com/v1"
_MESSAGES: list[dict[str, str]] = [{"role": "user", "content": "你好"}]
# 低熵测试凭据，非真实密钥
_TEST_API_KEY = "test-key-123"

_SUCCESS_PAYLOAD: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "你好，这是测试输出"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
}


def _status_transport(status_code: int, body: str) -> httpx.MockTransport:
    """构造固定状态码与响应体的 MockTransport。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text=body)

    return httpx.MockTransport(_handler)


def _payload_transport(payload: Any) -> httpx.MockTransport:
    """构造返回指定 JSON 载荷（HTTP 200）的 MockTransport。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(_handler)


def _adapter(transport: httpx.AsyncBaseTransport, **kwargs: Any) -> OpenAIAdapter:
    """构造指向 mock 端点的适配器（其余参数可覆盖）。"""
    return OpenAIAdapter(_BASE_URL, transport=transport, **kwargs)


@pytest.fixture
def recorded_requests() -> list[httpx.Request]:
    """收集 MockTransport 收到的请求，供 URL / headers / body 断言。"""
    return []


@pytest.fixture
def success_transport(recorded_requests: list[httpx.Request]) -> httpx.MockTransport:
    """返回 HTTP 200 + 标准 OpenAI 响应体的 MockTransport。"""

    def _handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        return httpx.Response(200, json=_SUCCESS_PAYLOAD)

    return httpx.MockTransport(_handler)


class TestSuccessfulCompletion:
    """测试正常非流式调用的响应归一。"""

    def test_returns_normalized_response(self, success_transport: httpx.MockTransport) -> None:
        """正常调用应返回 content 归一、raw 完整、metadata 齐备的 AdapterResponse。"""
        response = asyncio.run(_adapter(success_transport).acomplete(_MESSAGES))
        assert response.content == "你好，这是测试输出"
        # raw 保留完整响应 JSON，便于后续录制/回放与排障
        assert response.raw == _SUCCESS_PAYLOAD
        assert set(response.metadata) == {"model", "usage", "latency_ms"}

    def test_metadata_carries_usage_and_model(self, success_transport: httpx.MockTransport) -> None:
        """metadata 应含响应中的 model 与 usage 三项 token 统计。"""
        response = asyncio.run(_adapter(success_transport).acomplete(_MESSAGES))
        assert response.metadata["model"] == "gpt-4o-mini"
        usage = response.metadata["usage"]
        assert usage["prompt_tokens"] == 5
        assert usage["completion_tokens"] == 7
        assert usage["total_tokens"] == 12

    def test_metadata_latency_ms_positive(self, success_transport: httpx.MockTransport) -> None:
        """metadata["latency_ms"] 应为正数（毫秒），供后续性能统计消费。"""
        response = asyncio.run(_adapter(success_transport).acomplete(_MESSAGES))
        latency_ms = response.metadata["latency_ms"]
        assert isinstance(latency_ms, float)
        assert latency_ms > 0


class TestRequestConstruction:
    """测试请求构造：鉴权头、URL 归一、请求体透传、超时透传。"""

    def test_no_api_key_omits_authorization_header(
        self, success_transport: httpx.MockTransport, recorded_requests: list[httpx.Request]
    ) -> None:
        """未配置 api_key 时请求头不应含 Authorization（适配本地无鉴权端点）。"""
        asyncio.run(_adapter(success_transport).acomplete(_MESSAGES))
        assert "authorization" not in recorded_requests[0].headers

    def test_api_key_adds_bearer_authorization_header(
        self, success_transport: httpx.MockTransport, recorded_requests: list[httpx.Request]
    ) -> None:
        """配置 api_key 时请求头应含 Bearer 鉴权。"""
        asyncio.run(_adapter(success_transport, api_key=_TEST_API_KEY).acomplete(_MESSAGES))
        auth_header = recorded_requests[0].headers["authorization"]
        assert auth_header == f"Bearer {_TEST_API_KEY}"

    def test_base_url_trailing_slash_normalized(
        self, success_transport: httpx.MockTransport, recorded_requests: list[httpx.Request]
    ) -> None:
        """base_url 末尾斜杠应被归一，请求 URL 不出现双斜杠。"""
        adapter = OpenAIAdapter(f"{_BASE_URL}/", transport=success_transport)
        asyncio.run(adapter.acomplete(_MESSAGES))
        assert str(recorded_requests[0].url) == f"{_BASE_URL}/chat/completions"

    def test_model_passed_to_request_body(
        self, success_transport: httpx.MockTransport, recorded_requests: list[httpx.Request]
    ) -> None:
        """model 参数应原样进入请求体，且 stream 固定为 False。"""
        asyncio.run(_adapter(success_transport, model="custom-model").acomplete(_MESSAGES))
        body = json.loads(recorded_requests[0].content)
        assert body["model"] == "custom-model"
        assert body["stream"] is False

    def test_messages_passed_through_multiturn(
        self, success_transport: httpx.MockTransport, recorded_requests: list[httpx.Request]
    ) -> None:
        """多轮对话 messages 应原样透传到请求体。"""
        messages = [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "第一轮回答"},
            {"role": "user", "content": "第二轮"},
        ]
        asyncio.run(_adapter(success_transport).acomplete(messages))
        body = json.loads(recorded_requests[0].content)
        assert body["messages"] == messages

    def test_custom_timeout_passed_to_client(
        self, monkeypatch: pytest.MonkeyPatch, success_transport: httpx.MockTransport
    ) -> None:
        """自定义 timeout 应透传给 httpx.AsyncClient。"""
        captured: dict[str, Any] = {}
        real_client = httpx.AsyncClient

        def _spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
            """记录构造参数后交给真实客户端。"""
            captured.update(kwargs)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _spy)
        asyncio.run(_adapter(success_transport, timeout=7.5).acomplete(_MESSAGES))
        assert captured["timeout"] == 7.5


class TestStreamingNotSupported:
    """测试流式调用的拦截（流式由 M1-D11 实现）。"""

    def test_stream_true_raises_adapter_error(self, success_transport: httpx.MockTransport) -> None:
        """stream=True 应抛 AdapterError，消息含“流式”。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(success_transport).acomplete(_MESSAGES, stream=True))
        assert "流式" in str(exc_info.value)
        assert exc_info.value.context["stream"] is True


class TestHttpAndNetworkErrors:
    """测试 HTTP 状态码错误与网络异常的包装。"""

    def test_http_error_status_codes_rejected(self) -> None:
        """401/429/500 应统一抛 AdapterError，消息含状态码。"""
        for status_code, body in (
            (401, '{"error": "invalid api key"}'),
            (429, "rate limit exceeded"),
            (500, "internal server error"),
        ):
            with pytest.raises(AdapterError) as exc_info:
                asyncio.run(_adapter(_status_transport(status_code, body)).acomplete(_MESSAGES))
            assert str(status_code) in str(exc_info.value)
            assert exc_info.value.context["status_code"] == status_code
            # 响应体片段应进入消息，便于定位服务端返回的错误详情
            assert body[:500] in str(exc_info.value)

    def test_connect_error_wrapped_with_cause(self) -> None:
        """连接失败应包装为 AdapterError，并保留原始异常链。"""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连接被拒绝")

        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(httpx.MockTransport(_handler)).acomplete(_MESSAGES))
        assert isinstance(exc_info.value.__cause__, httpx.ConnectError)
        assert exc_info.value.context["error_type"] == "ConnectError"

    def test_timeout_error_wrapped_with_cause(self) -> None:
        """读取超时应包装为 AdapterError（TimeoutException 属 HTTPError 子类）。"""

        def _handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("读取超时")

        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(httpx.MockTransport(_handler)).acomplete(_MESSAGES))
        assert isinstance(exc_info.value.__cause__, httpx.TimeoutException)
        assert exc_info.value.context["error_type"] == "ReadTimeout"


class TestResponseValidation:
    """测试响应结构的防御性校验。"""

    def test_invalid_json_body_rejected(self) -> None:
        """HTTP 200 但响应体不是合法 JSON 时应抛 AdapterError（保留原始异常链）。"""
        transport = _status_transport(200, "<html>not json</html>")
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(transport).acomplete(_MESSAGES))
        assert "JSON" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert exc_info.value.context["error_type"] == "JSONDecodeError"

    def test_non_object_payload_rejected(self) -> None:
        """响应顶层不是 JSON 对象（如数组）时应抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport([1, 2, 3])).acomplete(_MESSAGES))
        assert "顶层" in str(exc_info.value)
        assert exc_info.value.context["actual_type"] == "list"

    def test_missing_choices_rejected(self) -> None:
        """响应缺少 choices 应抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"model": "gpt-4o-mini"})).acomplete(_MESSAGES))
        assert "choices" in str(exc_info.value)

    def test_empty_choices_rejected(self) -> None:
        """choices 为空数组应抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"choices": []})).acomplete(_MESSAGES))
        assert "choices" in str(exc_info.value)

    def test_missing_message_rejected(self) -> None:
        """choices[0] 缺少 message 应抛 AdapterError。"""
        payload = {"choices": [{"index": 0}]}
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport(payload)).acomplete(_MESSAGES))
        assert "message" in str(exc_info.value)

    def test_missing_content_rejected(self) -> None:
        """choices[0].message 缺少 content 应抛 AdapterError，避免校验错误穿透适配层。"""
        payload = {"choices": [{"message": {"role": "assistant"}}]}
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport(payload)).acomplete(_MESSAGES))
        assert "content" in str(exc_info.value)
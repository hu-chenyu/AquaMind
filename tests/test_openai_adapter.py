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
        assert set(response.metadata) == {
            "model",
            "usage",
            "latency_ms",
            "latency_includes_connection",
        }

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
            # 响应体片段只放 context（异常字符串只渲染键名，避免敏感数据进日志），
            # 排障时经 exc.context 显式读取
            assert body[:500] in exc_info.value.context["response_body"]

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


class TestBaseUrlValidation:
    """测试 base_url 的构造期校验与请求期异常包装。

    回归背景（P1-2）：``httpx.InvalidURL`` 直接继承 ``Exception`` 而非
    ``httpx.HTTPError``，原实现的 ``except httpx.HTTPError`` 捕获不到它，
    非法 base_url 会以裸 httpx 异常击穿「调用方只需 except AdapterError」的契约。
    """

    @pytest.mark.parametrize(
        "bad_url",
        ["", "   ", "ht!tp://x", "api.example.com/v1", "ftp://x/v1"],
    )
    def test_invalid_base_url_rejected_at_construction(self, bad_url: str) -> None:
        """空串/空白/缺协议/非 http(s) 协议的 base_url 应在构造期即抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            OpenAIAdapter(bad_url)
        # 错误须直接点名 base_url，便于用户改配置
        assert "base_url" in str(exc_info.value)
        assert exc_info.value.context["base_url"] == bad_url
        assert exc_info.value.context["adapter"] == "openai"

    def test_valid_base_url_still_reaches_request(self) -> None:
        """合法 base_url（含末尾斜杠）不应被新增校验误伤，仍能完成一次正常调用。"""
        adapter = OpenAIAdapter(f"{_BASE_URL}/", transport=_payload_transport(_SUCCESS_PAYLOAD))
        response = asyncio.run(adapter.acomplete(_MESSAGES))
        assert response.content == "你好，这是测试输出"

    def test_invalid_url_wrapped_at_request_time(self) -> None:
        """IPv6 语法非法的 base_url 在请求期抛 InvalidURL，应被包装为 AdapterError。

        该 base_url 以 http:// 开头、能通过构造期校验，错误只在真正建请求时才暴露，
        因此请求期的宽捕获是不可省略的第二道防线。修复前此处抛出裸
        ``httpx.InvalidURL``，``pytest.raises(AdapterError)`` 匹配不到而失败。
        """
        adapter = OpenAIAdapter("http://[::1", transport=_status_transport(200, "{}"))
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(adapter.acomplete(_MESSAGES))
        assert exc_info.value.context["error_type"] == "InvalidURL"
        # 异常链保留原始 httpx 异常
        assert isinstance(exc_info.value.__cause__, httpx.InvalidURL)


class TestHttpErrorBodyRedaction:
    """测试 HTTP 错误消息脱敏。

    回归背景（P1-3）：原实现把响应体片段直接拼进异常消息，而部分网关/反代会
    在错误响应里回显请求体，导致用户 prompt 随异常进入日志与报告。
    """

    def test_error_message_excludes_echoed_prompt(self) -> None:
        """服务端回显请求体时，异常消息不得包含用户 prompt，响应体应转入 context。"""

        def _echo(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8", "replace")
            return httpx.Response(500, text=f'{{"error":"rejected: {body}"}}')

        secret = "用户隐私数据-身份证110101199001011234"
        with pytest.raises(AdapterError) as exc_info:
            adapter = _adapter(httpx.MockTransport(_echo))
            asyncio.run(adapter.acomplete([{"role": "user", "content": secret}]))
        # 消息只保留状态码与响应体长度，不得携带用户输入
        assert secret not in exc_info.value.message
        assert "500" in exc_info.value.message
        # 排障所需的响应体片段仍可通过 context 按需取用
        assert exc_info.value.context["status_code"] == 500
        assert secret in exc_info.value.context["response_body"]

    def test_error_message_reports_body_length(self) -> None:
        """消息应带响应体长度，便于判断片段是否被截断。"""
        body = "x" * 600
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_status_transport(500, body)).acomplete(_MESSAGES))
        assert "600" in exc_info.value.message
        # context 中的响应体片段仍受 _ERROR_BODY_PREVIEW 上限保护
        assert len(exc_info.value.context["response_body"]) == 500

    def test_response_body_absent_from_exception_string(self) -> None:
        """敏感响应体不得出现在 str(exc) 中（依赖 __str__ 只渲染 context 键名）。

        str(exc) 会进入 traceback 与 logging.exception，是数据外泄的实际通道。
        """
        secret = "用户隐私数据-身份证110101199001011234"

        def _echo(request: httpx.Request) -> httpx.Response:
            body = request.content.decode("utf-8", "replace")
            return httpx.Response(500, text=f'{{"error":"rejected: {body}"}}')

        adapter = _adapter(httpx.MockTransport(_echo))
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(adapter.acomplete([{"role": "user", "content": secret}]))
        # 键名可见、取值不可见
        assert "response_body" in str(exc_info.value)
        assert secret not in str(exc_info.value)
        # 取值仍可通过 context 显式取用
        assert secret in exc_info.value.context["response_body"]


class TestChoicesErrorClassification:
    """测试 choices 四种根因的差异化报错（P2-2 + P2-6）。

    回归背景：原实现把「字段缺失 / 显式 null / 类型错误 / 空列表」以及
    「choices[0] 不是对象」合并为少数几条消息，且不带实际类型，排障时无法区分
    端点侧到底是哪种 bug。
    """

    def test_choices_missing(self) -> None:
        """choices 字段缺失：消息点名字段，context 记录 present=False。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"model": "m"})).acomplete(_MESSAGES))
        assert "缺少 choices" in str(exc_info.value)
        assert exc_info.value.context["present"] is False

    def test_choices_null(self) -> None:
        """choices 显式为 null：与「字段缺失」区分开。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"choices": None})).acomplete(_MESSAGES))
        assert "null" in str(exc_info.value)
        assert exc_info.value.context["actual_type"] == "NoneType"

    def test_choices_wrong_type(self) -> None:
        """choices 类型错误（如 dict）：消息与 context 均带实际类型名。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"choices": {}})).acomplete(_MESSAGES))
        assert "类型错误" in str(exc_info.value)
        assert "dict" in str(exc_info.value)
        assert exc_info.value.context["actual_type"] == "dict"

    def test_choices_empty_list(self) -> None:
        """choices 为空列表：单独措辞，可与「类型错误」区分。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"choices": []})).acomplete(_MESSAGES))
        assert "空列表" in str(exc_info.value)
        assert exc_info.value.context["actual_type"] == "list"

    def test_first_choice_not_object(self) -> None:
        """choices[0] 不是对象：应报「不是对象」而非误导性的「缺少 message」。"""
        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_adapter(_payload_transport({"choices": ["oops"]})).acomplete(_MESSAGES))
        message = str(exc_info.value)
        assert "不是对象" in message
        assert "缺少 choices[0].message" not in message
        # 实际类型必须记录，供端点兼容性排查
        assert exc_info.value.context["actual_type"] == "str"

    def test_first_choice_missing_message_distinct_from_not_object(self) -> None:
        """choices[0] 是对象但缺 message：与「不是对象」保持两类区分。"""
        with pytest.raises(AdapterError) as exc_info:
            adapter = _adapter(_payload_transport({"choices": [{"index": 0}]}))
            asyncio.run(adapter.acomplete(_MESSAGES))
        assert "缺少 choices[0].message" in str(exc_info.value)
        assert exc_info.value.context["actual_type"] == "NoneType"


class TestLatencyCalibration:
    """测试延迟口径声明（P2-3 第一步）。"""

    def test_metadata_declares_connection_included(
        self, success_transport: httpx.MockTransport
    ) -> None:
        """metadata 应显式标识 latency_ms 含连接建立开销，避免被误当服务端耗时。"""
        response = asyncio.run(_adapter(success_transport).acomplete(_MESSAGES))
        assert response.metadata["latency_includes_connection"] is True
        assert isinstance(response.metadata["latency_ms"], float)

    def test_latency_calibration_documented(self) -> None:
        """acomplete 的 docstring 必须写明 latency_ms 为端到端口径。"""
        doc = OpenAIAdapter.acomplete.__doc__ or ""
        assert "端到端" in doc
        assert "TLS 握手" in doc
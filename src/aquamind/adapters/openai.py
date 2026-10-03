"""OpenAI 兼容端点适配器（非流式）。

通过 httpx 异步调用 {base_url}/chat/completions，把响应归一为 AdapterResponse；
HTTP 错误与网络异常统一包装为 AdapterError，使调用方只依赖适配器契约。
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..exceptions import AdapterError
from .base import AdapterResponse, BaseAdapter

# OpenAI 兼容端点统一的补全路径
_CHAT_COMPLETIONS_PATH = "/chat/completions"
# 错误消息中响应体片段的最大长度，避免超长正文污染日志
_ERROR_BODY_PREVIEW = 500


class OpenAIAdapter(BaseAdapter):
    """OpenAI 兼容适配器：异步调用 /chat/completions 并归一为 AdapterResponse。

    支持无鉴权的本地兼容端点（如 vLLM / Ollama）：api_key 为空时请求不带
    Authorization 头。HTTP 非 2xx、网络异常、响应结构异常统一抛 AdapterError。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """初始化适配器。

        Args:
            base_url: 端点根地址（如 https://api.openai.com/v1），末尾斜杠会被归一；
                必须非空且以 http:// 或 https:// 开头。
            api_key: API 密钥；None 或空串表示端点无需鉴权。
            model: 模型名，随每次请求发送。
            timeout: 请求超时秒数，默认 60。
            transport: 可选 httpx 传输层（测试注入 MockTransport 用），默认用 httpx 默认传输。

        Raises:
            AdapterError: base_url 为空或协议不受支持时抛出。构造期即失败，
                避免配置错误推迟到请求期并以 httpx 原生异常的形式暴露给调用方。
        """
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise AdapterError(
                message=(
                    "OpenAIAdapter base_url 必须非空且以 http:// 或 https:// 开头，"
                    f"当前值: {base_url!r}"
                ),
                context={"adapter": "openai", "base_url": base_url},
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._transport = transport

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        stream: bool = False,
    ) -> AdapterResponse:
        """异步执行一次非流式补全调用。

        Args:
            messages: OpenAI 格式消息列表。
            stream: 是否流式；本阶段不支持，传 True 直接抛 AdapterError。

        Returns:
            AdapterResponse: content 为模型输出文本，raw 为完整响应 JSON，
                metadata 含 model/usage/latency_ms。

        Raises:
            AdapterError: 请求流式、HTTP 非 2xx、网络异常或响应结构不符合预期时抛出。
        """
        if stream:
            raise AdapterError(
                message="OpenAIAdapter 暂不支持流式调用（M1-D11 实现）",
                context={"adapter": "openai", "stream": True},
            )

        url = f"{self._base_url}{_CHAT_COMPLETIONS_PATH}"
        payload: dict[str, Any] = {"model": self._model, "messages": messages, "stream": False}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                transport=self._transport,
            ) as client:
                response = await client.post(url, json=payload, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # 网络层异常（连接失败/超时等）统一包装，保留原始异常链。
            # httpx.InvalidURL 直接继承 Exception（不是 HTTPError 子类），非法 base_url
            # 会在请求构造期抛出它，必须显式列入才能守住「调用方只需 except
            # AdapterError」的契约；ValueError 覆盖请求构造期的参数类错误
            # （含自定义 transport 下的 URL 解析失败）。
            # 捕获范围刻意限定在这一次 HTTP 调用内；asyncio.CancelledError /
            # KeyboardInterrupt / SystemExit 均继承 BaseException，不会被误捕，
            # 任务取消与中断语义保持原样。
            raise AdapterError(
                message=f"OpenAI API 请求失败: {type(exc).__name__}",
                context={"adapter": "openai", "url": url, "error_type": type(exc).__name__},
            ) from exc
        latency_ms = round((time.perf_counter() - start) * 1000, 3)

        if not 200 <= response.status_code < 300:
            # 消息只保留状态码与响应体长度：部分网关/反代会在错误响应体里回显请求体，
            # 直接把响应体拼进 message 会让用户 prompt 随异常进入日志与报告。
            # 排障所需的响应体片段改放 context.response_body，由调用方按需取用。
            # 已知残留：AquaMindError.__str__ 会渲染 context 的全部取值，故响应体片段
            # 仍会出现在 str(exc) 中。彻底消除需在异常基类做脱敏，属独立改动，
            # 不在本次修复范围内（勿据此认为本分支已完全阻断泄露）。
            raise AdapterError(
                message=(
                    f"OpenAI API 返回 HTTP {response.status_code}"
                    f"（响应体 {len(response.text)} 字符，详见 context.response_body）"
                ),
                context={
                    "adapter": "openai",
                    "url": url,
                    "status_code": response.status_code,
                    "response_body": response.text[:_ERROR_BODY_PREVIEW],
                },
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AdapterError(
                message="OpenAI API 响应不是合法 JSON",
                context={"adapter": "openai", "url": url, "error_type": type(exc).__name__},
            ) from exc

        return self._normalize_response(data, latency_ms)

    def _normalize_response(self, data: Any, latency_ms: float) -> AdapterResponse:
        """把响应 JSON 归一为 AdapterResponse，并做防御性结构校验。

        Args:
            data: 已解析的响应 JSON。
            latency_ms: 本次请求耗时（毫秒）。

        Returns:
            AdapterResponse: 归一化后的响应对象。

        Raises:
            AdapterError: 顶层非对象、choices 缺失或为空、message 或 content 缺失时抛出。
        """
        if not isinstance(data, dict):
            raise AdapterError(
                message="OpenAI API 响应格式异常: 顶层不是 JSON 对象",
                context={"adapter": "openai", "actual_type": type(data).__name__},
            )
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise AdapterError(
                message="OpenAI API 响应缺少 choices 或 choices 为空",
                context={"adapter": "openai"},
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            raise AdapterError(
                message="OpenAI API 响应缺少 choices[0].message",
                context={"adapter": "openai"},
            )
        content = message.get("content")
        if not isinstance(content, str):
            # content 缺失/为 None/结构化返回均属契约不符，避免 ValidationError 穿透适配层
            raise AdapterError(
                message="OpenAI API 响应缺少 choices[0].message.content（或非字符串）",
                context={"adapter": "openai", "content_type": type(content).__name__},
            )
        reported_model = data.get("model")
        metadata: dict[str, Any] = {
            "model": reported_model if isinstance(reported_model, str) else self._model,
            "usage": data.get("usage"),
            "latency_ms": latency_ms,
        }
        return AdapterResponse(content=content, raw=data, metadata=metadata)
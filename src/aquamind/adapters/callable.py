"""本地函数适配器：把同步可调用对象接入统一适配器契约。

用于零成本本地验证（CI 零 key 场景）：把任意 fn(messages) -> str 包装为
异步 acomplete，使执行器在不触真实 API 的前提下跑通全链路。
"""

from __future__ import annotations

from collections.abc import Callable

from ..exceptions import AdapterError
from .base import AdapterResponse, BaseAdapter


class CallableAdapter(BaseAdapter):
    """本地函数适配器：包装同步可调用对象，归一为 AdapterResponse。

    Attributes:
        fn: 被包装的本地函数，接收 OpenAI 格式 messages 并返回输出字符串。
    """

    def __init__(self, fn: Callable[[list[dict[str, str]]], str]) -> None:
        """初始化适配器。

        Args:
            fn: 本地可调用对象，签名为 (messages: list[dict[str, str]]) -> str。
        """
        self.fn: Callable[[list[dict[str, str]]], str] = fn

    async def acomplete(
        self,
        messages: list[dict[str, str]],
        stream: bool = False,
    ) -> AdapterResponse:
        """调用本地函数并归一化返回。

        Args:
            messages: OpenAI 格式消息列表，原样透传给 fn。
            stream: 是否流式；本地函数适配器暂不支持。

        Returns:
            AdapterResponse: content 与 raw 均为 fn 的返回值。

        Raises:
            AdapterError: stream=True 时抛出（暂不支持流式调用）。
        """
        if stream:
            raise AdapterError(
                message="CallableAdapter 暂不支持流式调用",
                context={
                    "stream": True,
                    "adapter": "callable",
                    "reason": "CallableAdapter 暂不支持流式",
                },
            )
        result = self.fn(messages)
        return AdapterResponse(content=result, raw=result, metadata={"adapter": "callable"})
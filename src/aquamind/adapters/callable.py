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
            AdapterError: stream=True 时抛出（暂不支持流式调用）；
                fn 抛出的任意异常都会被包装为 AdapterError，原始异常经 from 保留在 __cause__。
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
        try:
            result = self.fn(messages)
            # 归一化同样纳入 try：fn 返回非 str 时 pydantic 校验失败，须一并包装
            return AdapterResponse(content=result, raw=result, metadata={"adapter": "callable"})
        except Exception as e:
            # 后端异常统一包装为契约异常，调用方只需 except AdapterError 即可捕获全部故障
            raise AdapterError(
                message=f"适配器调用失败: {type(e).__name__}",
                context={"adapter": "callable", "error_type": type(e).__name__},
            ) from e
"""适配器层契约：统一响应模型 AdapterResponse + 抽象基类 BaseAdapter。

适配器负责把不同后端（本地函数、OpenAI 兼容端点、cassette 回放）的调用
归一为 AdapterResponse，使上层执行器与评分器无需感知具体后端差异。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field

from ..models.score import validated_model_copy


class AdapterResponse(BaseModel):
  """适配器统一响应：所有适配器 acomplete 的返回类型。

  Attributes:
    content: 模型输出文本（归一后的核心字段，评分器直接消费）
    raw: 原始响应（本地函数适配器中为函数原始返回值；远端适配器中为原始 JSON）
    metadata: 元数据（如 model / latency_ms / usage 等）
  """

  # 严格模式：禁止未知字段，防止元数据键名拼写错误被静默忽略
  model_config = ConfigDict(extra="forbid")

  content: str = Field(description="模型输出文本（归一后的核心字段，评分器直接消费）")
  raw: Any = Field(default=None, description="原始响应，未提供时为 None")
  metadata: dict[str, Any] = Field(
    default_factory=dict,
    description="元数据，如 model/latency_ms/usage",
  )

  def model_copy(
    self,
    *,
    update: Mapping[str, Any] | None = None,
    deep: bool = False,
  ) -> Self:
    """复制实例；带 update 时重新走完整校验，防止绕过契约。

    pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**（未知字段还会被
    静默丢弃）。本模型是执行器与评分器拿到的第一手响应，bad 副本会让「评的是哪段
    输出」与报告对不上。收口逻辑见 ``models.score.validated_model_copy``。

    Args:
      update: 要覆盖的字段（None 或空字典时按纯复制处理）。
      deep: 是否深拷贝嵌套对象（沿用官方语义）。

    Returns:
      Self: 副本；带 update 时是**重新校验过**的副本。

    Raises:
      ValidationError: update 后的字段违反契约（未知字段、类型不符等）时抛出。
    """
    return validated_model_copy(self, update=update, deep=deep)


class BaseAdapter(ABC):
  """适配器抽象基类：定义所有后端适配器的统一异步调用契约。

  子类必须实现 acomplete。messages 统一采用 OpenAI 消息格式
  （[{"role": "user", "content": "..."}]），返回值统一为 AdapterResponse，
  从而让执行器与评分器只依赖契约、不依赖具体后端。
  """

  @abstractmethod
  async def acomplete(
    self,
    messages: list[dict[str, str]],
    stream: bool = False,
  ) -> AdapterResponse:
    """异步执行一次补全调用并归一化返回。

    Args:
      messages: OpenAI 格式消息列表。
      stream: 是否启用流式；本阶段各适配器均暂不支持流式。

    Returns:
      AdapterResponse: 归一化后的响应。

    Raises:
      AdapterError: 调用失败，或请求了当前暂不支持的能力（如 stream=True）时抛出。
    """
    raise NotImplementedError
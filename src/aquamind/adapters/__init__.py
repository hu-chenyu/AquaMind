"""适配器层：统一各后端调用的接口契约与响应模型。

对外导出 BaseAdapter（抽象基类）、CallableAdapter（本地函数适配器）、
OpenAIAdapter（OpenAI 兼容端点适配器）与 AdapterResponse（统一响应模型）。
"""

from .base import AdapterResponse, BaseAdapter
from .callable import CallableAdapter
from .openai import OpenAIAdapter

__all__ = ["AdapterResponse", "BaseAdapter", "CallableAdapter", "OpenAIAdapter"]
"""AquaMind 异常体系：统一基类 + 分层子类。

所有异常均携带 context 字典，便于调试和错误归因。
异常层级：AquaMindError → 各模块异常 → 具体场景异常
"""

from __future__ import annotations

from typing import Any


class AquaMindError(Exception):
    """AquaMind 所有异常的基类。

    Attributes:
        message: 人类可读的错误描述
        context: 结构化上下文信息，包含错误相关的变量、参数、状态等
    """

    def __init__(
        self,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        """初始化异常。

        Args:
            message: 错误描述
            context: 结构化上下文，默认为空字典
        """
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context or {}

    def __str__(self) -> str:
        """返回带上下文**键名**的错误描述（不渲染 context 的取值）。

        context 的取值可能包含响应体、完整 URL 等敏感或冗长内容（服务端的错误
        响应会回显用户 prompt）。一旦渲染进异常字符串，就会随 traceback、
        logging.exception 一并进入日志与报告，构成数据泄露通道。

        因此这里只输出键名以保留「有哪些上下文可用」这一排障信息，取值一律通过
        ``exc.context["key"]`` 按需显式读取。

        Returns:
            str: 形如 ``"消息 (context keys: [k1, k2])"`` 的描述；context 为空时
                直接返回消息本身。
        """
        if self.context:
            keys = ", ".join(self.context.keys())
            return f"{self.message} (context keys: [{keys}])"
        return self.message

    def __repr__(self) -> str:
        """返回可调试的异常表示。"""
        return f"{self.__class__.__name__}(message={self.message!r}, context={self.context!r})"


class ConfigError(AquaMindError):
    """配置相关错误。

    触发场景：配置加载失败、环境变量非法值、配置文件格式错误等。
    """


class LoaderError(AquaMindError):
    """用例加载相关错误。

    触发场景：YAML/JSON解析失败、文件不存在、用例格式不合法、必填字段缺失等。
    """


class AdapterError(AquaMindError):
    """适配器相关错误。

    触发场景：API调用失败、响应格式异常、认证失败、端点不可达等。
    子类按错误类型进一步细分（在 M1-D07 重试模块中定义）。
    """


class ReplayError(AquaMindError):
    """VCR回放相关错误。

    触发场景：cassette不存在、hash不匹配、回放失败、录制失败等。
    """


class BudgetError(AquaMindError):
    """预算熔断相关错误。

    触发场景：token消耗超限、费用超限、预算配置非法等。
    """

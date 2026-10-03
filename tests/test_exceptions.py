"""异常体系单元测试。

覆盖：基类初始化、context携带、__str__、__repr__、子类继承关系。
"""

from __future__ import annotations

import pytest

from aquamind.exceptions import (
    AdapterError,
    AquaMindError,
    BudgetError,
    ConfigError,
    LoaderError,
    ReplayError,
)


class TestAquaMindErrorBase:
    """测试异常基类。"""

    def test_init_with_message_only(self) -> None:
        e = AquaMindError("something went wrong")
        assert e.message == "something went wrong"
        assert e.context == {}

    def test_init_with_context(self) -> None:
        ctx = {"file": "test.yaml", "line": 42}
        e = AquaMindError("parse failed", context=ctx)
        assert e.message == "parse failed"
        assert e.context == ctx

    def test_context_default_empty(self) -> None:
        e = AquaMindError("test")
        assert isinstance(e.context, dict)
        assert len(e.context) == 0

    def test_str_without_context(self) -> None:
        e = AquaMindError("simple error")
        assert str(e) == "simple error"

    def test_str_with_context(self) -> None:
        """__str__ 应渲染消息与 context 的键名，但不渲染取值。"""
        e = AquaMindError("error with ctx", context={"key": "value"})
        s = str(e)
        assert "error with ctx" in s
        # 键名可见：排障时需要知道「有哪些上下文可用」
        assert "key" in s

    def test_repr_contains_class_name(self) -> None:
        e = AquaMindError("test")
        r = repr(e)
        assert "AquaMindError" in r
        assert "test" in r

    def test_is_exception(self) -> None:
        e = AquaMindError("test")
        assert isinstance(e, Exception)

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(AquaMindError):
            raise AquaMindError("catch me")


class TestContextValueRedaction:
    """测试 __str__ 不渲染 context 取值（架构级修复）。

    背景：context 中可能存放响应体、完整 URL 等敏感内容。异常字符串会随
    traceback、logging.exception 进入日志与报告，因此 __str__ 只输出键名，
    取值必须经 exc.context 显式读取。
    """

    def test_str_excludes_context_values(self) -> None:
        """str(exc) 不得包含 context 的取值，且须保留键名。"""
        secret = "用户隐私数据-身份证110101199001011234"
        e = LoaderError("加载失败", context={"response_body": secret, "status_code": 500})
        s = str(e)
        assert secret not in s
        # 键名仍可见，调用方能据此知道该去哪里取值
        assert "response_body" in s
        assert "status_code" in s

    def test_context_value_still_accessible(self) -> None:
        """脱敏只影响字符串呈现，context 取值本身不得丢失。"""
        secret = "Bearer sk-test-000000"
        e = AdapterError("鉴权失败", context={"authorization": secret})
        assert e.context["authorization"] == secret
        assert secret not in str(e)

    def test_str_without_context_has_no_suffix(self) -> None:
        """context 为空时不应追加键名后缀，保持输出简洁。"""
        assert str(AquaMindError("plain")) == "plain"

    def test_nested_subclasses_inherit_redaction(self) -> None:
        """各子类共享基类 __str__，不得回退到渲染取值。"""
        secret = "病历全文"
        for exc_class in (ConfigError, LoaderError, AdapterError, ReplayError, BudgetError):
            e = exc_class("失败", context={"payload": secret})
            assert secret not in str(e), f"{exc_class.__name__} 仍在渲染 context 取值"
            assert "payload" in str(e), f"{exc_class.__name__} 丢失了 context 键名"


class TestSubclasses:
    """测试各子类。"""

    @pytest.mark.parametrize(
        "exc_class",
        [ConfigError, LoaderError, AdapterError, ReplayError, BudgetError],
    )
    def test_subclass_inherits_base(self, exc_class: type[AquaMindError]) -> None:
        assert issubclass(exc_class, AquaMindError)

    @pytest.mark.parametrize(
        "exc_class",
        [ConfigError, LoaderError, AdapterError, ReplayError, BudgetError],
    )
    def test_subclass_init_with_message(self, exc_class: type[AquaMindError]) -> None:
        e = exc_class("test error")
        assert e.message == "test error"
        assert isinstance(e, AquaMindError)

    @pytest.mark.parametrize(
        "exc_class",
        [ConfigError, LoaderError, AdapterError, ReplayError, BudgetError],
    )
    def test_subclass_init_with_context(self, exc_class: type[AquaMindError]) -> None:
        ctx = {"detail": "info"}
        e = exc_class("test", context=ctx)
        assert e.context == ctx

    def test_config_error_caught_as_base(self) -> None:
        with pytest.raises(AquaMindError):
            raise ConfigError("config bad")

    def test_loader_error_caught_as_base(self) -> None:
        with pytest.raises(AquaMindError):
            raise LoaderError("load failed")

    def test_adapter_error_caught_as_base(self) -> None:
        with pytest.raises(AquaMindError):
            raise AdapterError("api failed")

    def test_replay_error_caught_as_base(self) -> None:
        with pytest.raises(AquaMindError):
            raise ReplayError("replay failed")

    def test_budget_error_caught_as_base(self) -> None:
        with pytest.raises(AquaMindError):
            raise BudgetError("budget exceeded")


class TestExceptionHierarchy:
    """测试异常层级关系。"""

    def test_all_subclasses_distinct(self) -> None:
        classes = [ConfigError, LoaderError, AdapterError, ReplayError, BudgetError]
        for i, c1 in enumerate(classes):
            for j, c2 in enumerate(classes):
                if i != j:
                    assert not issubclass(c1, c2), (
                        f"{c1.__name__} should not be subclass of {c2.__name__}"
                    )

    def test_base_not_subclass_of_subclass(self) -> None:
        assert not issubclass(AquaMindError, ConfigError)

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
        e = AquaMindError("error with ctx", context={"key": "value"})
        s = str(e)
        assert "error with ctx" in s
        assert "key=value" in s

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

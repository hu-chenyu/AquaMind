"""配置模块单元测试。

覆盖：默认值、环境变量覆盖、非法值拒绝、路径展开、日志级别大写化。
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

from aquamind.config import Settings, load_config
from aquamind.exceptions import ConfigError


class TestSettingsDefaults:
    """测试默认值是否正确。"""

    def test_default_log_level(self) -> None:
        s = Settings()
        assert s.log_level == "INFO"

    def test_default_timeout(self) -> None:
        s = Settings()
        assert s.default_timeout == 60.0

    def test_default_max_retries(self) -> None:
        s = Settings()
        assert s.max_retries == 3

    def test_default_retry_base_delay(self) -> None:
        s = Settings()
        assert s.retry_base_delay == 1.0

    def test_default_max_tokens_none(self) -> None:
        s = Settings()
        assert s.max_tokens_per_run is None

    def test_default_max_cost_none(self) -> None:
        s = Settings()
        assert s.max_cost_per_run is None

    def test_default_replay_mode(self) -> None:
        s = Settings()
        assert s.replay_mode == "auto"

    def test_default_replay_dir(self) -> None:
        s = Settings()
        assert s.replay_dir.is_absolute()
        assert s.replay_dir.name == ".aquamind_cache"

    def test_default_replay_strict_hash(self) -> None:
        s = Settings()
        assert s.replay_strict_hash is True

    def test_default_concurrency(self) -> None:
        s = Settings()
        assert s.default_concurrency == 10


class TestEnvVarOverride:
    """测试环境变量覆盖。"""

    def test_log_level_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_LOG_LEVEL", "DEBUG")
        s = Settings()
        assert s.log_level == "DEBUG"

    def test_log_level_lowercase_uppercase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_LOG_LEVEL", "warning")
        s = Settings()
        assert s.log_level == "WARNING"

    def test_timeout_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_DEFAULT_TIMEOUT", "30")
        s = Settings()
        assert s.default_timeout == 30.0

    def test_max_retries_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_MAX_RETRIES", "5")
        s = Settings()
        assert s.max_retries == 5

    def test_replay_mode_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_REPLAY_MODE", "record")
        s = Settings()
        assert s.replay_mode == "record"

    def test_concurrency_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_DEFAULT_CONCURRENCY", "50")
        s = Settings()
        assert s.default_concurrency == 50

    def test_max_tokens_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_MAX_TOKENS_PER_RUN", "100000")
        s = Settings()
        assert s.max_tokens_per_run == 100000

    def test_max_cost_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_MAX_COST_PER_RUN", "5.0")
        s = Settings()
        assert s.max_cost_per_run == 5.0


class TestInvalidValues:
    """测试非法值被拒绝。"""

    def test_invalid_log_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_LOG_LEVEL", "INVALID")
        with pytest.raises(ValidationError):
            Settings()

    def test_timeout_too_small(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_DEFAULT_TIMEOUT", "0.5")
        with pytest.raises(ValidationError):
            Settings()

    def test_timeout_too_large(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_DEFAULT_TIMEOUT", "1000")
        with pytest.raises(ValidationError):
            Settings()

    def test_max_retries_negative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_MAX_RETRIES", "-1")
        with pytest.raises(ValidationError):
            Settings()

    def test_concurrency_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_DEFAULT_CONCURRENCY", "0")
        with pytest.raises(ValidationError):
            Settings()

    def test_invalid_replay_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_REPLAY_MODE", "invalid_mode")
        with pytest.raises(ValidationError):
            Settings()

    def test_non_numeric_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_DEFAULT_TIMEOUT", "not_a_number")
        with pytest.raises(ValidationError):
            Settings()


class TestReplayDir:
    """测试回放目录路径处理。"""

    def test_relative_path_expanded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_REPLAY_DIR", "./my_cache")
        s = Settings()
        assert s.replay_dir.is_absolute()
        assert s.replay_dir.name == "my_cache"

    def test_custom_absolute_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("AQ_REPLAY_DIR", str(tmp_path / "cassettes"))
        s = Settings()
        assert s.replay_dir == tmp_path / "cassettes"


class TestLoadConfig:
    """测试 load_config 函数。"""

    def test_load_config_returns_settings(self) -> None:
        s = load_config()
        assert isinstance(s, Settings)

    def test_load_config_invalid_raises_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_LOG_LEVEL", "INVALID")
        with pytest.raises(ConfigError):
            load_config()

    def test_load_config_error_has_context(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AQ_LOG_LEVEL", "INVALID")
        try:
            load_config()
            pytest.fail("should have raised ConfigError")
        except ConfigError as e:
            assert "error_type" in e.context


class TestLogLevelValidator:
    """测试 log_level 校验器：字符串统一大写，非字符串原样透传给 pydantic 判定。"""

    def test_non_str_log_level_deferred_to_pydantic(self) -> None:
        """非 str 的 log_level 不在校验器内被改写，应由 pydantic 报字段级类型错误。

        若校验器对任意输入都调用 .upper()，此处抛出的会是 AttributeError
        而不是可定位到 log_level 字段的契约错误。
        """
        with pytest.raises(ValidationError) as exc_info:
            Settings(log_level=123)  # type: ignore[arg-type]
        assert exc_info.value.errors()[0]["loc"] == ("log_level",)

    def test_none_log_level_rejected(self) -> None:
        """None 同样原样透传给 pydantic，报错定位到 log_level 字段。"""
        with pytest.raises(ValidationError) as exc_info:
            Settings(log_level=None)  # type: ignore[arg-type]
        assert exc_info.value.errors()[0]["loc"] == ("log_level",)

    def test_validator_returns_non_str_unchanged(self) -> None:
        """校验器对非 str 输入原样返回同一对象，不做任何类型推断或转换。"""
        sentinel: object = object()
        assert Settings._uppercase_log_level(sentinel) is sentinel  # type: ignore[arg-type]
        assert Settings._uppercase_log_level(123) == 123  # type: ignore[arg-type]
        assert Settings._uppercase_log_level(None) is None  # type: ignore[arg-type]

    def test_validator_uppercases_str(self) -> None:
        """校验器对 str 输入统一转大写，保留既有归一语义。"""
        assert Settings._uppercase_log_level("warning") == "WARNING"
        assert Settings._uppercase_log_level("DeBuG") == "DEBUG"


class TestEnvFileCwdDocumented:
    """测试 .env 解析口径已在文档中显式声明（P2-4）。

    回归背景：``env_file=".env"`` 相对进程 CWD 解析，配置结果取决于执行目录。
    本次不改变该默认行为（会破坏既有 .env 用法），改为在文档中明确声明。
    """

    def test_settings_docstring_declares_cwd_resolution(self) -> None:
        """Settings 类 docstring 须说明 .env 按当前工作目录解析。"""
        doc = inspect.getdoc(Settings) or ""
        assert "当前工作目录" in doc
        assert ".env" in doc

    def test_load_config_docstring_declares_cwd_resolution(self) -> None:
        """load_config 的 docstring 同样须声明该口径。"""
        doc = inspect.getdoc(load_config) or ""
        assert "当前工作目录" in doc

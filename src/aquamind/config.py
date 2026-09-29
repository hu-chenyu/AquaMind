"""AquaMind 配置体系：环境变量驱动的 pydantic-settings 配置加载。

配置优先级：环境变量 > 配置文件 > 默认值。
所有环境变量以 AQ_ 为前缀，避免与其他项目冲突。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """AquaMind 全局配置。

    所有字段均有默认值，可通过 AQ_ 前缀环境变量覆盖。
    示例：AQ_LOG_LEVEL=DEBUG、AQ_DEFAULT_TIMEOUT=30
    """

    model_config = SettingsConfigDict(
        env_prefix="AQ_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── 基础配置 ──────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="日志级别",
    )

    default_timeout: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description="单次请求默认超时时间（秒），范围1-600",
    )

    max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="请求最大重试次数，范围0-10",
    )

    retry_base_delay: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description="重试退避基础延迟（秒），范围0.1-60",
    )

    # ── 预算配置 ──────────────────────────────────────────────
    max_tokens_per_run: int | None = Field(
        default=None,
        ge=1,
        description="单次运行最大token消耗，None表示不限制",
    )

    max_cost_per_run: float | None = Field(
        default=None,
        ge=0.0,
        description="单次运行最大费用（美元），None表示不限制",
    )

    # ── 回放配置 ──────────────────────────────────────────────
    replay_mode: Literal["auto", "record", "play", "off"] = Field(
        default="auto",
        description="VCR回放模式：auto=有cassette则回放否则录制，record=强制录制，play=强制回放（无匹配则报错），off=关闭回放",
    )

    replay_dir: Path = Field(
        default=Path("./.aquamind_cache"),
        description="cassette存储目录",
    )

    replay_strict_hash: bool = Field(
        default=True,
        description="cassette hash不匹配时是否报错（True=报错，False=警告后继续）",
    )

    # ── 并发配置 ──────────────────────────────────────────────
    default_concurrency: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="默认并发数，范围1-1000",
    )

    # ── 校验器 ────────────────────────────────────────────────
    @field_validator("replay_dir")
    @classmethod
    def _expand_replay_dir(cls, v: Path) -> Path:
        """展开 ~ 和相对路径为绝对路径。"""
        return v.expanduser().resolve()

    @field_validator("log_level", mode="before")
    @classmethod
    def _uppercase_log_level(cls, v: str) -> str:
        """日志级别统一转大写。"""
        if isinstance(v, str):
            return v.upper()
        return v


def load_config() -> Settings:
    """加载全局配置。

    读取顺序：环境变量（AQ_前缀）> .env文件 > 默认值。

    Returns:
        Settings: 加载并校验后的配置对象

    Raises:
        ConfigError: 配置校验失败时抛出
    """
    from .exceptions import ConfigError

    try:
        return Settings()
    except Exception as e:
        raise ConfigError(
            message=f"配置加载失败: {e}",
            context={"error_type": type(e).__name__},
        ) from e

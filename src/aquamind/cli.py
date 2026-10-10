"""AquaMind 命令行入口。

本文件提供 typer app 与已挂载的子命令：

    aquamind --help                显示帮助（exit code 0）
    aquamind version               显示当前包版本号
    aquamind run CONFIG_PATH       发起一次流式补全调用并实时输出（M1-D12 挂载）

``run`` 的运行配置为 JSON 文件，字段口径见 ``RunSpec``。``report`` 与门禁类
子命令随 M4 里程碑提供（见 docs/ROADMAP.md，选型决策见 docs/adr/0006-typer-cli.md）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .exceptions import AdapterError, AquaMindError, ConfigError
from .sse import iter_stream_deltas

# OpenAI 兼容端点统一的补全路径（与 adapters.openai 保持同一口径）
_CHAT_COMPLETIONS_PATH = "/chat/completions"
# 失败输出的统一前缀，便于脚本区分「这是 AquaMind 报的错」而非端点回显
_ERROR_PREFIX = "错误"


class RunSpec(BaseModel):
    """``run`` 子命令的运行配置（JSON 配置文件反序列化目标）。

    配置文件示例::

        {
          "base_url": "https://api.example.com/v1",
          "model": "gpt-4o-mini",
          "api_key": "<your-key>",
          "prompt": "你好",
          "timeout": 60.0
        }

    Attributes:
        base_url: 端点根地址，必须以 http:// 或 https:// 开头，末尾斜杠会被归一。
        model: 模型名，随每次请求发送。
        api_key: API 密钥；None 或空串表示端点无需鉴权（如本地 vLLM / Ollama）。
        prompt: 作为单条 user 消息发送的提示词。
        timeout: 单次请求超时秒数，必须大于 0。
    """

    # 多余字段直接报错：配置里写错的键名若被静默忽略，用户会以为参数已生效
    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str = "gpt-4o-mini"
    api_key: str | None = None
    prompt: str = ""
    timeout: float = Field(default=60.0, gt=0)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """校验端点根地址的协议，构造期即失败而不推迟到请求期。

        Args:
            value: 用户填写的端点根地址。

        Returns:
            str: 校验通过的原始值（不做改写，末尾斜杠由请求构造时归一）。

        Raises:
            ValueError: 地址为空或协议不受支持时抛出，由 pydantic 收敛为字段错误。
        """
        if not value.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        return value


def load_run_spec(config_path: Path) -> RunSpec:
    """读取并校验 ``run`` 子命令的 JSON 运行配置。

    Args:
        config_path: 配置文件路径。

    Returns:
        RunSpec: 校验通过的运行配置。

    Raises:
        ConfigError: 文件不可读、不是合法 JSON、或字段校验失败时抛出；
            context 只记录出错字段名等键级信息，不记录取值。
    """
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        # 文件不存在、路径是目录、无读权限都落在这里，统一收敛为配置错误
        raise ConfigError(
            message=f"配置文件不可读: {config_path}",
            context={"error_type": type(exc).__name__},
        ) from exc

    try:
        payload: Any = json.loads(raw_text)
    except ValueError as exc:
        raise ConfigError(
            message=f"配置文件不是合法 JSON: {config_path}",
            context={"error_type": type(exc).__name__},
        ) from exc

    try:
        return RunSpec.model_validate(payload)
    except ValidationError as exc:
        # 只取 loc（字段名）与错误条数：ValidationError 的默认渲染会带上输入值，
        # 而 api_key 这类字段的取值不应随错误信息外泄
        locs = {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
        raise ConfigError(
            message=f"配置文件字段校验失败: {config_path}",
            context={"error_count": exc.error_count(), "fields": sorted(locs)},
        ) from exc


def _validate_speed(value: int) -> int:
    """``--speed`` 的参数校验回调：拒绝负数。

    Args:
        value: click 转换后的整数值。

    Returns:
        int: 校验通过的速度值。

    Raises:
        typer.BadParameter: 速度为负数时抛出，click 以用法错误呈现（退出码 2）。
    """
    if value < 0:
        raise typer.BadParameter("--speed 不能为负数（0 表示全速输出）")
    return value


def _build_client(spec: RunSpec) -> httpx.AsyncClient:
    """构造发起流式请求用的异步 HTTP 客户端。

    单独抽成函数，是为了让测试能用注入 ``httpx.MockTransport`` 的版本替换它，
    从而在不触真实 API（零 key）的前提下走完真实的 httpx 流式链路。

    Args:
        spec: 已校验的运行配置。

    Returns:
        httpx.AsyncClient: 带超时设置的异步客户端，由调用方负责关闭。
    """
    return httpx.AsyncClient(timeout=spec.timeout)


async def _iter_run_deltas(spec: RunSpec) -> AsyncIterator[str]:
    """发起流式补全请求，逐个产出响应中的 delta 文本。

    Args:
        spec: 已校验的运行配置。

    Yields:
        str: 每个 chunk 的 delta 文本，顺序与响应一致。

    Raises:
        AdapterError: 网络层异常时抛出。httpx 原生异常在此收敛，
            使 CLI 只需处理 AquaMind 异常族即可。
    """
    headers = {"Content-Type": "application/json"}
    if spec.api_key:
        # 未配置 api_key 时不带 Authorization：本地无鉴权端点会直接拒绝该头
        headers["Authorization"] = f"Bearer {spec.api_key}"
    payload = {
        "model": spec.model,
        "stream": True,
        "messages": [{"role": "user", "content": spec.prompt}],
    }
    url = f"{spec.base_url.rstrip('/')}{_CHAT_COMPLETIONS_PATH}"
    try:
        async with _build_client(spec) as client, client.stream(
            "POST", url, json=payload, headers=headers
        ) as response:
            async for delta in iter_stream_deltas(response):
                yield delta
    except httpx.HTTPError as exc:
        raise AdapterError(
            message=f"流式请求失败: {type(exc).__name__}",
            context={"error_type": type(exc).__name__, "url": url},
        ) from exc


async def _stream_to_stdout(spec: RunSpec, speed: int) -> None:
    """消费流式响应，把每个 delta 实时输出到 stdout。

    Args:
        spec: 已校验的运行配置。
        speed: 输出速度上限，单位字符/秒（CPS）；0 表示全速输出，不做任何等待。
    """
    # 每字符耗时 = 1/speed：长度 len(delta) 的一段文本恰好耗时 len(delta)/speed 秒，
    # 于是整体速率稳定在 speed 字符/秒
    delay_per_char = 0.0 if speed <= 0 else 1.0 / speed
    async for delta in _iter_run_deltas(spec):
        # nl=False：delta 之间不换行，保持模型输出的原始排版
        typer.echo(delta, nl=False)
        if delay_per_char:
            await asyncio.sleep(delay_per_char * len(delta))
    typer.echo()


# 命令行程序实例。骨架阶段即创建并注册，避免入口点悬空导致已发布包
# 出现 ImportError；子命令通过 @app.command() 挂载（run 于 M1-D12 挂载，
# 门禁/报告命令于 M4 起挂载）。
app = typer.Typer(
    name="aquamind",
    help=(
        "AquaMind —— LLM 应用在并发阶梯负载下的质量变化判定工具"
        "（run 子命令用于单次流式调用；报告与门禁子命令将在 M4 里程碑提供）"
    ),
    no_args_is_help=True,
)


# 显式 callback 让 typer 保持"命令组 + 子命令"结构：
# 若 app 下只有一个 command，typer 会把该命令提升为根命令，
# 导致 `aquamind version` 被当作多余参数拒绝。
@app.callback()
def main() -> None:
    """AquaMind 命令行入口。"""


@app.command()
def version() -> None:
    """显示当前安装的 AquaMind 版本号。"""
    # 延迟导入版本号，使本模块不依赖包初始化顺序。
    from aquamind import __version__

    typer.echo(__version__)


@app.command()
def run(
    config_path: Annotated[Path, typer.Argument(help="运行配置文件（JSON）的路径")],
    speed: Annotated[
        int,
        typer.Option(
            "--speed",
            callback=_validate_speed,
            help="输出速度上限（字符/秒）；0 表示全速输出，默认 0",
        ),
    ] = 0,
) -> None:
    """发起一次流式补全调用，把模型响应实时输出到终端。

    配置文件为 JSON，字段口径见 ``aquamind.cli.RunSpec``；``--speed`` 控制
    输出速度，单位为字符/秒（CPS），默认 0 表示全速输出（不等待）。

    Args:
        config_path: 运行配置文件路径。
        speed: 输出速度上限，字符/秒；0 为全速，N>0 表示每秒输出 N 个字符。

    Raises:
        typer.Exit: 配置不可读/非法、请求失败、SSE 解析失败或未预期异常时，
            以退出码 1 结束；错误消息写入 stderr，不向用户暴露异常堆栈。
    """
    try:
        spec = load_run_spec(config_path)
        asyncio.run(_stream_to_stdout(spec, speed))
    except AquaMindError as exc:
        # 领域异常（配置/适配器）：AquaMindError 的 str 只渲染消息与 context 键名，
        # 不会把响应体、端点回显等取值带进终端
        typer.echo(f"{_ERROR_PREFIX}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        # 兜底：未预期异常同样只给一行提示，堆栈留给开发者的 traceback 而非终端
        typer.echo(f"{_ERROR_PREFIX}: 未预期异常 {type(exc).__name__}", err=True)
        raise typer.Exit(code=1) from exc


if __name__ == "__main__":
    # 支持 ``python -m aquamind.cli`` 方式直接调用。
    app()

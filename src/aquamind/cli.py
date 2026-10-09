"""AquaMind 命令行入口（骨架版本）。

本文件当前提供最小可用的 typer app，保证 pyproject.toml 中注册的入口点
``aquamind = "aquamind.cli:app"`` 在任何已发布版本上都可解析、可执行：

    aquamind --help     显示帮助（exit code 0）
    aquamind version    显示当前包版本号

完整的命令行程序（``run`` / ``report`` / ``cache`` 等子命令）将在 M1-D12
（CLI `run` 集成，AM-Day14，2026-10-12）起逐步落地，`report` 子命令随 M4-D09
（报告渲染能力）提供（见 docs/ROADMAP.md，选型决策见
docs/adr/0006-typer-cli.md）。
"""

from __future__ import annotations

import typer

# 命令行程序实例。骨架阶段即创建并注册，避免入口点悬空导致已发布包
# 出现 ImportError；后续子命令通过 @app.command() 挂载（run 于 M1-D12 挂载，
# 门禁/报告命令于 M4 起挂载）。
app = typer.Typer(
    name="aquamind",
    help=(
        "AquaMind —— LLM 应用在并发阶梯负载下的质量变化判定工具"
        "（run 子命令将在 M1 里程碑提供，报告与门禁子命令将在 M4 里程碑提供，当前为骨架版本）"
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


if __name__ == "__main__":
    # 支持 ``python -m aquamind.cli`` 方式直接调用。
    app()

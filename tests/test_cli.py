"""命令行入口单元测试（typer app 骨架）。

覆盖：app 元信息与命令注册结构（显式 callback 防止命令被提升为根命令）、
version 子命令输出、--help / 无参数 / 未知命令三类参数分支的退出码与输出，
以及 ``python -m aquamind.cli`` 的 __main__ 入口路径。
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import runpy
import sys

import pytest
import typer
from typer.testing import CliRunner

import aquamind
from aquamind.cli import app, main, version


def _run_as_module(args: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """以 __main__ 身份执行 cli 模块，返回（退出码, 标准输出）。

    用 runpy 在当前进程内复现 ``python -m aquamind.cli`` 的执行路径：模块顶层
    代码会重跑一次，从而真实触发 ``if __name__ == "__main__": app()`` 分支。
    typer 独立模式在命令结束后以 SystemExit 结束进程，故在此捕获退出码；
    异常直接向上传播给 pytest，不做静默吞掉。
    """
    spec = importlib.util.find_spec("aquamind.cli")
    assert spec is not None and spec.origin is not None
    monkeypatch.setattr(sys, "argv", ["aquamind", *args])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        try:
            runpy.run_path(spec.origin, run_name="__main__")
        except SystemExit as exc:
            # SystemExit.code 可能为 None（非显式退出）或整数退出码
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
            return code, buffer.getvalue()
    raise AssertionError("模块入口未以 SystemExit 结束，命令行解析链路可能已失效")


class TestCliAppContract:
    """测试 typer app 的注册结构与元信息。"""

    def test_app_metadata(self) -> None:
        """app 应为 Typer 实例，程序名为 aquamind，且无子命令时打印帮助。"""
        assert isinstance(app, typer.Typer)
        assert app.info.name == "aquamind"
        assert app.info.no_args_is_help is True

    def test_version_registered_as_subcommand(self) -> None:
        """version 应注册为子命令：若被提升为根命令，``aquamind version`` 会被当作多余参数。"""
        command_names = [command.name or command.callback.__name__ for command in app.registered_commands]
        assert command_names == ["version"]

    def test_callback_is_silent_noop(self, capsys: pytest.CaptureFixture[str]) -> None:
        """显式 callback 只维持“命令组 + 子命令”结构，本身不产出任何输出。"""
        assert main() is None
        assert capsys.readouterr().out == ""


class TestCliCommandBehavior:
    """测试 version 子命令与三类参数分支的退出码/输出。"""

    def test_version_command_prints_package_version(self) -> None:
        """``aquamind version`` 应以退出码 0 输出当前包版本号。"""
        result = CliRunner().invoke(app, ["version"])
        assert result.exit_code == 0
        assert result.stdout.strip() == aquamind.__version__

    def test_version_function_echoes_version(self, capsys: pytest.CaptureFixture[str]) -> None:
        """直接调用命令函数也应把版本号写到 stdout（typer.echo 语义）。"""
        version()
        assert capsys.readouterr().out.strip() == aquamind.__version__

    def test_help_exits_zero_and_lists_version(self) -> None:
        """``--help`` 应以退出码 0 打印用法，并列出 version 子命令。"""
        result = CliRunner().invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.stdout
        assert "version" in result.stdout

    def test_no_args_shows_help_with_nonzero_exit(self) -> None:
        """no_args_is_help=True：缺省不带子命令时打印用法并以非 0 退出。"""
        result = CliRunner().invoke(app, [])
        assert result.exit_code != 0
        assert "Usage" in result.stdout

    def test_unknown_command_is_rejected(self) -> None:
        """未知子命令应被 typer 以用法错误（退出码 2）拒绝，且不污染 stdout。"""
        result = CliRunner().invoke(app, ["not-a-command"])
        assert result.exit_code == 2
        assert result.stdout == ""


class TestCliModuleEntryPoint:
    """测试 ``python -m aquamind.cli`` 入口路径（__main__ 分支）。"""

    def test_module_entry_prints_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模块入口执行 version 子命令：退出码 0 且输出版本号。"""
        code, output = _run_as_module(["version"], monkeypatch)
        assert code == 0
        assert output.strip() == aquamind.__version__

    def test_module_entry_rejects_unknown_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """模块入口同样具备参数校验：未知子命令以退出码 2 结束。"""
        code, output = _run_as_module(["not-a-command"], monkeypatch)
        assert code == 2
        assert output.strip() == ""

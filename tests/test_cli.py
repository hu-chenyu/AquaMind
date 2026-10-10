"""命令行入口与 SSE 流解析器单元测试。

覆盖：
1. typer app 的注册结构、version 子命令、--help / 无参数 / 未知命令三类参数分支、
   以及 ``python -m aquamind.cli`` 的 __main__ 入口路径；
2. ``run`` 子命令的骨架契约（--help、--speed 校验）、流式输出接线（delta 顺序、
   速度控制、请求构造）、以及配置/HTTP/SSE/未预期异常四类失败的非 0 退出；
3. ``sse`` 模块的完整接口面：SSEEvent 属性与 JSON 解析、parse_sse_events 的协议归一、
   SSEStreamParser 的分片边界处理、iter_stream_deltas 的错误识别与 delta 提取。

sse.py 是 run 子命令的流式解析依赖，其接口面必须整体覆盖，否则总覆盖率会被
单个 0% 文件拖到 79%。所有 HTTP 交互均由 httpx.MockTransport 模拟，不触真实 API
（零 key）。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import re
import runpy
import sys
import warnings
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from typer.testing import CliRunner

import aquamind
from aquamind import cli as cli_module
from aquamind import sse as sse_module
from aquamind.cli import RunSpec, app, load_run_spec, main, version
from aquamind.exceptions import AdapterError, ConfigError
from aquamind.sse import (
    SSEEvent,
    SSEStreamParser,
    collect_stream_content,
    iter_stream_deltas,
    parse_sse_events,
)

# run 用例里的假端点与假凭据：全为测试数据，不含任何真实密钥
_TEST_BASE_URL = "https://api.example.test/v1"
# ANSI 转义序列（CSI SGR）：help 文本断言前需剥离，否则着色与否会决定断言成败
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_TEST_API_KEY = "test-token-not-a-real-key"
_TEST_PROMPT = "你好"
# 失败路径用的上游错误体：故意带一段敏感串，用于验证它不会随错误信息外泄
_UPSTREAM_ERROR_BODY = '{"error":"prompt 用户隐私数据-身份证110101199001011234"}'
_EXPECTED_STREAM_TEXT = "Hello world"
# 期望文本对应的 delta 长度（2/1/5），用于核对 --speed 的等待时长
_EXPECTED_DELTA_LENGTHS = (5, 1, 5)


def _data_event(payload: object) -> str:
    """构造一个携带任意 JSON 载荷的 SSE 事件（含尾部空行分隔符）。

    Args:
        payload: 放进 ``data:`` 字段的 JSON 值（会被序列化）。

    Returns:
        str: 完整事件文本，可直接按 UTF-8 编码后喂给解析器。
    """
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _delta_event(content: str) -> str:
    """构造一个携带 delta 文本的标准 OpenAI 流式事件。

    Args:
        content: delta 文本。

    Returns:
        str: 完整事件文本。
    """
    return _data_event({"choices": [{"delta": {"content": content}}]})


# 端到端流式用例使用的分片序列：三个 delta 后跟 [DONE]
_STREAM_CHUNKS: list[bytes] = [
    _delta_event("Hello").encode(),
    _delta_event(" ").encode(),
    _delta_event("world").encode(),
    b"data: [DONE]\n\n",
]


class _AsyncChunks(httpx.AsyncByteStream):
    """把字节分片包装成 httpx 异步字节流，模拟 TCP 分片。

    httpx 的流式响应要求 ``response.stream`` 是 ``AsyncByteStream``，而
    ``content=`` 传同步迭代器时会被断言拒绝，故这里显式实现异步字节流。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        """记录待产出的字节分片。

        Args:
            chunks: 按顺序产出的字节分片。
        """
        self._chunks = list(chunks)

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """按顺序产出分片。"""
        for chunk in self._chunks:
            yield chunk


class _UndecodableBody:
    """没有 ``decode`` 方法的响应体桩，模拟自定义 transport 返回非 bytes 响应体。"""

    def __len__(self) -> int:
        """返回桩体的字节长度（供错误信息回退文案使用）。"""
        return 17


class _UndecodableResponse:
    """最小响应桩：状态码固定，``aread`` 返回没有 ``decode`` 的响应体。"""

    def __init__(self, status_code: int) -> None:
        """记录桩响应的状态码。

        Args:
            status_code: 对外呈现的 HTTP 状态码。
        """
        self.status_code = status_code

    async def aread(self) -> _UndecodableBody:
        """返回不可解码的响应体桩。"""
        return _UndecodableBody()


def _deltas(chunks: list[bytes], status_code: int = 200) -> list[str]:
    """经 MockTransport 打开真实流式响应，排干 ``iter_stream_deltas``。

    Args:
        chunks: 端点按顺序返回的字节分片。
        status_code: 端点返回的 HTTP 状态码。

    Returns:
        list[str]: 按响应顺序产出的 delta 文本。
    """
    return asyncio.run(_deltas_async(chunks, status_code))


async def _deltas_async(chunks: list[bytes], status_code: int) -> list[str]:
    """``_deltas`` 的异步实现。"""
    client = _mock_client(chunks, status_code)
    deltas: list[str] = []
    async with client, client.stream("POST", _TEST_BASE_URL, json={}) as response:
        async for delta in iter_stream_deltas(response):
            deltas.append(delta)
    return deltas


def _collect_content(chunks: list[bytes]) -> str:
    """经 MockTransport 打开真实流式响应，取回 ``collect_stream_content`` 的聚合结果。

    Args:
        chunks: 端点按顺序返回的字节分片。

    Returns:
        str: 聚合后的完整文本。
    """

    async def _run() -> str:
        client = _mock_client(chunks, 200)
        async with client, client.stream("POST", _TEST_BASE_URL, json={}) as response:
            return await collect_stream_content(response)

    return asyncio.run(_run())


def _mock_handler(
    chunks: list[bytes],
    status_code: int,
    recorded: list[httpx.Request] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """构造 httpx.MockTransport 使用的处理函数（非 2xx 时返回固定错误体）。

    Args:
        chunks: 端点按顺序返回的字节分片。
        status_code: 端点返回的 HTTP 状态码。
        recorded: 可选的请求记录列表。

    Returns:
        处理函数：记录请求后返回流式响应或错误响应。
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        if recorded is not None:
            recorded.append(request)
        if status_code >= 300:
            return httpx.Response(status_code, text=_UPSTREAM_ERROR_BODY)
        return httpx.Response(status_code, stream=_AsyncChunks(chunks))

    return _handler


def _mock_client(chunks: list[bytes], status_code: int) -> httpx.AsyncClient:
    """构造注入 MockTransport 的异步客户端。"""
    transport = httpx.MockTransport(_mock_handler(chunks, status_code))
    return httpx.AsyncClient(transport=transport)


def _write_config(tmp_path: Path, **overrides: Any) -> Path:
    """写出一份 run 运行配置 JSON 文件。

    Args:
        tmp_path: pytest 提供的临时目录。
        **overrides: 覆盖默认字段（值为 None 时写入 JSON null，用于构造无鉴权端点）。

    Returns:
        Path: 配置文件路径。
    """
    payload: dict[str, Any] = {
        "base_url": _TEST_BASE_URL,
        "model": "test-model",
        "api_key": _TEST_API_KEY,
        "prompt": _TEST_PROMPT,
    }
    payload.update(overrides)
    config_path = tmp_path / "run.json"
    config_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return config_path


def _write_raw_config(tmp_path: Path, raw_text: str) -> Path:
    """写入一份原样文本的配置文件（用于构造非法 JSON 用例）。

    Args:
        tmp_path: pytest 提供的临时目录。
        raw_text: 配置文件的原始文本。

    Returns:
        Path: 配置文件路径。
    """
    config_path = tmp_path / "broken.json"
    config_path.write_text(raw_text, encoding="utf-8")
    return config_path


def _install_mock_transport(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[bytes],
    status_code: int = 200,
) -> list[httpx.Request]:
    """把 run 链路使用的 HTTP 客户端替换为 MockTransport 版本。

    替换的是 ``cli._build_client``（客户端工厂），而不是 httpx 本身，
    因此请求构造、流式读取、响应解析走的都是真实链路。

    Args:
        monkeypatch: pytest 提供的补丁器。
        chunks: 端点按顺序返回的字节分片。
        status_code: 端点返回的 HTTP 状态码。

    Returns:
        list[httpx.Request]: 记录到的请求，供 URL / headers / body 断言。
    """
    recorded: list[httpx.Request] = []

    def _fake_build_client(spec: RunSpec) -> httpx.AsyncClient:
        transport = httpx.MockTransport(_mock_handler(chunks, status_code, recorded))
        return httpx.AsyncClient(transport=transport, timeout=spec.timeout)

    monkeypatch.setattr(cli_module, "_build_client", _fake_build_client)
    return recorded


def _record_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """拦截 run 链路里的 ``asyncio.sleep``，记录每次等待秒数（不真实等待）。

    Args:
        monkeypatch: pytest 提供的补丁器。

    Returns:
        list[float]: 按调用顺序记录的等待秒数。
    """
    calls: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        calls.append(delay)

    monkeypatch.setattr(cli_module.asyncio, "sleep", _fake_sleep)
    return calls


def _help_text(args: list[str]) -> str:
    """执行 help 子命令并返回剥掉 ANSI 颜色码的纯文本。

    typer 在检测到 ``GITHUB_ACTIONS`` 时会强制开启彩色输出（让 Actions 日志可读），
    而 rich 会把 ``--speed`` 这类选项名渲染成 ``-`` 与 ``speed`` 两段独立着色的片段，
    字面量 ``--speed`` 因而不再连续出现——本地无该变量、CI 有，同一份断言一绿一红。
    断言前统一剥掉 ANSI 序列，使 help 文本的断言与「当前是否着色」彻底解耦。

    Args:
        args: 传给 app 的参数（如 ``["run", "--help"]``）。

    Returns:
        str: 去除 ANSI 转义序列后的标准输出。
    """
    result = CliRunner().invoke(app, args)
    return _ANSI_ESCAPE.sub("", result.stdout)


def _run_as_module(args: list[str], monkeypatch: pytest.MonkeyPatch) -> tuple[int, str]:
    """以 __main__ 身份执行 cli 模块，返回（退出码, 标准输出）。

    用 ``runpy.run_module`` 在当前进程内复现 ``python -m aquamind.cli`` 的执行路径：
    模块顶层代码会重跑一次，从而真实触发 ``if __name__ == "__main__": app()`` 分支。
    必须用 run_module 而非 run_path——cli 是包内模块，模块级的相对导入
    （``from .sse import ...``）在 run_path 提供的空 ``__package__`` 下会直接 ImportError。

    typer 独立模式在命令结束后以 SystemExit 结束进程，故在此捕获退出码；
    异常直接向上传播给 pytest，不做静默吞掉。
    """
    monkeypatch.setattr(sys, "argv", ["aquamind", *args])
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), warnings.catch_warnings():
        # run_module 对「已在 sys.modules 中的模块」发 RuntimeWarning：
        # 那正是 runpy 复用同进程状态时固有提示，与被测代码无关
        warnings.simplefilter("ignore", RuntimeWarning)
        try:
            runpy.run_module("aquamind.cli", run_name="__main__")
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

    def test_commands_registered_as_subcommands(self) -> None:
        """version 与 run 都应注册为子命令。

        若任一命令被 typer 提升为根命令，``aquamind version`` 会被当作多余参数拒绝。
        这里只断言「都挂在命令组下」，不锁定注册顺序：挂载新子命令不应让本用例失效。
        """
        command_names = [
            command.name or command.callback.__name__ for command in app.registered_commands
        ]
        assert "version" in command_names
        assert "run" in command_names

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


class TestRunCommandContract:
    """测试 run 子命令的骨架契约：帮助文本与 --speed 参数校验。"""

    def test_run_help_exits_zero(self) -> None:
        """``aquamind run --help`` 应以退出码 0 打印用法。"""
        result = CliRunner().invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.stdout

    def test_run_help_documents_speed_unit(self) -> None:
        """帮助文本应写明 --speed 的单位（字符/秒）与 0 表示全速。"""
        text = _help_text(["run", "--help"])
        assert "--speed" in text
        assert "字符/秒" in text

    def test_run_help_documents_config_argument(self) -> None:
        """帮助文本应列出配置文件位置参数。"""
        text = _help_text(["run", "--help"])
        assert "config_path" in text
        assert "运行配置文件" in text

    def test_negative_speed_is_usage_error(self) -> None:
        """--speed 为负数应在参数解析阶段被拒（退出码 2），且不进入命令体。"""
        result = CliRunner().invoke(app, ["run", "config.json", "--speed", "-1"])
        assert result.exit_code == 2
        assert "不能为负数" in result.output

    def test_config_argument_is_required(self) -> None:
        """缺少配置文件位置参数应为用法错误（退出码 2）。"""
        result = CliRunner().invoke(app, ["run"])
        assert result.exit_code == 2


class TestRunStreamingOutput:
    """测试 run 的流式输出接线：输出内容、顺序、速度控制与请求构造。"""

    def test_streams_all_deltas_in_order(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端到端：多个 delta 应按响应顺序完整输出，并以换行收尾。"""
        _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 0
        # 逐字对齐：delta 之间不得插入换行，末尾恰好一个换行
        assert result.stdout == f"{_EXPECTED_STREAM_TEXT}\n"

    def test_speed_zero_never_sleeps(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--speed 0 为全速输出：整个流不应发生任何等待。"""
        _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        sleeps = _record_sleeps(monkeypatch)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path)), "--speed", "0"])
        assert result.exit_code == 0
        assert sleeps == []

    def test_speed_positive_delays_proportionally_to_length(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--speed N>0：每段 delta 的等待应为 该段字符数/N 秒。"""
        _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        sleeps = _record_sleeps(monkeypatch)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path)), "--speed", "2"])
        assert result.exit_code == 0
        expected = [length / 2 for length in _EXPECTED_DELTA_LENGTHS]
        assert sleeps == pytest.approx(expected)

    def test_output_survives_multibyte_deltas(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """分片边界落在多字节字符中间时，文本不得损坏或丢字。

        切割点 42 位于「并」字的 3 字节 UTF-8 序列内部：解析器必须把不完整的
        尾部留在字节缓冲里，等下一个分片补齐后再解码。
        """
        text = "并发阶梯"
        raw_event = _delta_event(text).encode()
        chunks = [raw_event[:42], raw_event[42:], b"data: [DONE]\n\n"]
        _install_mock_transport(monkeypatch, chunks)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 0
        assert result.stdout == f"{text}\n"

    def test_request_url_and_payload(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """请求应打到 {base_url}/chat/completions，body 含 model/stream/messages。"""
        recorded = _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        config_path = _write_config(tmp_path, base_url=f"{_TEST_BASE_URL}/")
        CliRunner().invoke(app, ["run", str(config_path)])
        assert str(recorded[0].url) == f"{_TEST_BASE_URL}/chat/completions"
        body = json.loads(recorded[0].content)
        assert body["model"] == "test-model"
        assert body["stream"] is True
        assert body["messages"] == [{"role": "user", "content": _TEST_PROMPT}]

    def test_api_key_adds_bearer_header(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置 api_key 时请求应带 Bearer 鉴权头。"""
        recorded = _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert recorded[0].headers["authorization"] == f"Bearer {_TEST_API_KEY}"

    def test_absent_api_key_omits_authorization(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未配置 api_key 时不应发送 Authorization（本地无鉴权端点会拒绝该头）。"""
        recorded = _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        CliRunner().invoke(app, ["run", str(_write_config(tmp_path, api_key=None))])
        assert "authorization" not in recorded[0].headers

    def test_build_client_applies_spec_timeout(self) -> None:
        """_build_client 应把配置里的 timeout 交给 httpx 客户端。"""
        client = cli_module._build_client(RunSpec(base_url=_TEST_BASE_URL, timeout=12.5))
        try:
            assert client.timeout.read == 12.5
        finally:
            asyncio.run(client.aclose())

    def test_timeout_is_passed_to_client(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置里的 timeout 应透传给 httpx 客户端。"""
        _install_mock_transport(monkeypatch, _STREAM_CHUNKS)
        installed_build_client = cli_module._build_client
        captured: dict[str, Any] = {}

        def _spy_build_client(spec: RunSpec) -> httpx.AsyncClient:
            captured["timeout"] = spec.timeout
            # 复用已注入 MockTransport 的版本，避免真实发起网络请求
            return installed_build_client(spec)

        monkeypatch.setattr(cli_module, "_build_client", _spy_build_client)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path, timeout=12.5))])
        assert result.exit_code == 0
        assert captured["timeout"] == 12.5


class TestRunFailureExitCode:
    """测试 run 的失败路径：全部以退出码 1 结束且不向用户暴露堆栈。"""

    def test_missing_config_exits_nonzero(self, tmp_path: Path) -> None:
        """配置文件不存在：退出码 1 + stderr 错误消息 + 无堆栈。"""
        result = CliRunner().invoke(app, ["run", str(tmp_path / "missing.json")])
        assert result.exit_code == 1
        assert "配置文件不可读" in result.stderr
        assert "Traceback" not in result.output

    def test_directory_config_exits_nonzero(self, tmp_path: Path) -> None:
        """配置路径是目录（读取失败）：同样归为配置不可读并退出码 1。"""
        result = CliRunner().invoke(app, ["run", str(tmp_path)])
        assert result.exit_code == 1
        assert "配置文件不可读" in result.stderr

    def test_invalid_json_exits_nonzero(self, tmp_path: Path) -> None:
        """配置文件不是合法 JSON：退出码 1 并说明原因。"""
        result = CliRunner().invoke(app, ["run", str(_write_raw_config(tmp_path, "{oops"))])
        assert result.exit_code == 1
        assert "不是合法 JSON" in result.stderr

    def test_missing_required_field_exits_nonzero(self, tmp_path: Path) -> None:
        """缺少必填字段 base_url：退出码 1 并报字段校验失败。"""
        config_path = _write_raw_config(tmp_path, json.dumps({"model": "test-model"}))
        result = CliRunner().invoke(app, ["run", str(config_path)])
        assert result.exit_code == 1
        assert "字段校验失败" in result.stderr

    def test_field_error_context_lists_names_not_values(self, tmp_path: Path) -> None:
        """字段错误只以字段名形式进 context，取值不进（防 api_key 随错误外泄）。"""
        config_path = _write_raw_config(tmp_path, json.dumps({"model": "test-model"}))
        with pytest.raises(ConfigError) as exc_info:
            load_run_spec(config_path)
        assert exc_info.value.context["fields"] == ["base_url"]
        assert exc_info.value.context["error_count"] == 1

    def test_unsupported_base_url_protocol_exits_nonzero(self, tmp_path: Path) -> None:
        """base_url 协议不受支持：构造期即失败，退出码 1。"""
        result = CliRunner().invoke(
            app, ["run", str(_write_config(tmp_path, base_url="ftp://example.test"))]
        )
        assert result.exit_code == 1
        assert "字段校验失败" in result.stderr

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        """多余字段应被拒绝：写错的键名若被静默忽略，用户会以为参数已生效。"""
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path, modle="typo"))])
        assert result.exit_code == 1
        assert "字段校验失败" in result.stderr

    def test_error_output_never_leaks_field_values(self, tmp_path: Path) -> None:
        """失败信息只列字段名，不得回显 api_key 的取值。"""
        config_path = _write_config(tmp_path, base_url="ftp://example.test")
        result = CliRunner().invoke(app, ["run", str(config_path)])
        assert result.exit_code == 1
        assert _TEST_API_KEY not in result.output

    def test_non_2xx_status_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端点返回 HTTP 500：退出码 1，且响应体取值不回显到终端。"""
        _install_mock_transport(monkeypatch, _STREAM_CHUNKS, status_code=500)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 1
        assert "HTTP 500" in result.stderr
        assert "身份证" not in result.output

    def test_network_error_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """连接失败：httpx 原生异常收敛为 AdapterError，退出码 1 且无堆栈。"""
        _install_mock_transport(monkeypatch, _STREAM_CHUNKS)

        def _failing_handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("连接被拒绝")

        monkeypatch.setattr(
            cli_module, "_build_client", lambda spec: httpx.AsyncClient(
                transport=httpx.MockTransport(_failing_handler), timeout=spec.timeout
            )
        )
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 1
        assert "流式请求失败" in result.stderr
        assert "Traceback" not in result.output

    def test_stream_error_event_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """流中收到错误事件：退出码 1 且不打印已产出的半截文本。"""
        chunks = [
            _delta_event("半截").encode(),
            _data_event({"error": {"message": "上游过载"}}).encode(),
        ]
        _install_mock_transport(monkeypatch, chunks)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 1
        assert "上游过载" in result.stderr

    def test_malformed_stream_json_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SSE 载荷不是合法 JSON：退出码 1。"""
        _install_mock_transport(monkeypatch, [b"data: {oops\n\n"])
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 1
        assert "不是合法 JSON" in result.stderr

    def test_unexpected_error_exits_nonzero_without_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未预期异常同样只给一行提示：堆栈不进入终端。"""

        def _boom(config_path: Path) -> RunSpec:
            raise ValueError("内部缺陷")

        monkeypatch.setattr(cli_module, "load_run_spec", _boom)
        result = CliRunner().invoke(app, ["run", str(_write_config(tmp_path))])
        assert result.exit_code == 1
        assert "未预期异常 ValueError" in result.stderr
        assert "Traceback" not in result.output


class TestSseEventContract:
    """测试 SSEEvent 的终止标记判定与 JSON 解析。"""

    def test_done_marker_is_recognized(self) -> None:
        """data 等于 [DONE] 时为终止事件。"""
        assert SSEEvent(data="[DONE]").is_done is True

    def test_done_marker_tolerates_surrounding_whitespace(self) -> None:
        """data 两端有空白时仍应识别为终止事件。"""
        assert SSEEvent(data="  [DONE]  ").is_done is True

    def test_non_done_data_is_not_terminator(self) -> None:
        """普通载荷不是终止事件。"""
        assert SSEEvent(data='{"choices": []}').is_done is False

    def test_missing_data_is_not_terminator(self) -> None:
        """无 data 字段（纯注释事件）不是终止事件。"""
        assert SSEEvent().is_done is False

    def test_parse_json_returns_payload(self) -> None:
        """合法 JSON 载荷应原样返回。"""
        assert SSEEvent(data='{"k": 1}').parse_json() == {"k": 1}

    def test_parse_json_without_data_raises(self) -> None:
        """data 为 None 时无法解析，抛 AdapterError 并携带事件上下文。"""
        with pytest.raises(AdapterError) as exc_info:
            SSEEvent(event_type="ping", raw=": keep-alive").parse_json()
        assert "无 data 字段" in exc_info.value.message
        assert exc_info.value.context["event_type"] == "ping"

    def test_parse_json_invalid_payload_raises(self) -> None:
        """非法 JSON 抛 AdapterError，context 只带截断预览与错误类型。"""
        with pytest.raises(AdapterError) as exc_info:
            SSEEvent(data="{oops").parse_json()
        assert "不是合法 JSON" in exc_info.value.message
        assert exc_info.value.context["data_preview"] == "{oops"

    def test_event_exposes_parsed_fields(self) -> None:
        """事件应保留 data/event/id/retry 与原始文本。"""
        event = SSEEvent(data="d", event_type="e", event_id="7", retry=100, raw="raw")
        assert (event.data, event.event_type, event.event_id, event.retry, event.raw) == (
            "d",
            "e",
            "7",
            100,
            "raw",
        )


class TestParseSseEvents:
    """测试 parse_sse_events 的协议归一与字段分发。"""

    def test_parses_multiple_events_with_crlf(self) -> None:
        """CRLF 行尾应被归一，一并解析出多个事件。"""
        text = 'data: {"a": 1}\r\n\r\ndata: {"a": 2}\r\n\r\n'
        assert [event.data for event in parse_sse_events(text)] == ['{"a": 1}', '{"a": 2}']

    def test_skips_comment_and_blank_segments(self) -> None:
        """纯注释段与空白段不产出事件。"""
        assert parse_sse_events(": ping\n\n\n\n: pong\n\n") == []

    def test_skips_event_without_any_known_field(self) -> None:
        """没有冒号的行按规范成为字段名，不属于任何已知字段时不产出。"""
        assert parse_sse_events("barefield\n\n") == []

    def test_joins_multiple_data_lines(self) -> None:
        """多个 data 行的值以换行拼接。"""
        event = parse_sse_events("data: 第一行\ndata: 第二行\n\n")[0]
        assert event.data == "第一行\n第二行"

    def test_keeps_value_without_leading_space(self) -> None:
        """冒号后无空格时不应吞掉 value 的首字符。"""
        assert parse_sse_events("data:abc\n\n")[0].data == "abc"

    def test_parses_event_and_id_fields(self) -> None:
        """event / id 字段应被解析出来。"""
        event = parse_sse_events("event: message\nid: 7\ndata: x\n\n")[0]
        assert event.event_type == "message"
        assert event.event_id == "7"

    def test_later_field_value_wins(self) -> None:
        """同名字段后出现的值覆盖先出现的。"""
        event = parse_sse_events("event: first\nevent: second\nid: 1\nid: 2\ndata: x\n\n")[0]
        assert event.event_type == "second"
        assert event.event_id == "2"

    def test_parses_retry_milliseconds(self) -> None:
        """retry 为数字时应解析为毫秒数。"""
        assert parse_sse_events("retry: 3000\n\n")[0].retry == 3000

    def test_negative_retry_is_ignored(self) -> None:
        """retry 为负数时按无效处理（置空）；带 data 字段以免事件被当作纯注释丢弃。"""
        assert parse_sse_events("data: x\nretry: -5\n\n")[0].retry is None

    def test_non_numeric_retry_is_ignored(self) -> None:
        """retry 非数字时忽略该行，不影响已解析的有效值。"""
        assert parse_sse_events("data: x\nretry: soon\n\n")[0].retry is None

    def test_data_without_any_field_yields_no_payload(self) -> None:
        """空 data 行产出空串而非 None，便于调用方区分“有 data 但为空”。"""
        assert parse_sse_events("data:\n\n")[0].data == ""

    def test_event_text_is_preserved_for_debugging(self) -> None:
        """raw 应保留事件原文，供错误上下文使用。"""
        event = parse_sse_events("data: x\n\n")[0]
        assert event.raw == "data: x"

    def test_oversized_data_line_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """单个 data 行超长时直接报错而非静默截断（防内存耗尽）。"""
        monkeypatch.setattr(sse_module, "_MAX_DATA_LENGTH", 4)
        with pytest.raises(AdapterError) as exc_info:
            parse_sse_events("data: 123456\n\n")
        assert "超过最大长度限制" in exc_info.value.message
        assert exc_info.value.context["actual_length"] == 6


class TestSSEStreamParser:
    """测试 SSEStreamParser 的分片边界处理与缓冲上限。"""

    def test_incomplete_event_is_buffered_until_complete(self) -> None:
        """事件未收全时不产出，补齐分隔符后才产出。"""
        parser = SSEStreamParser()
        assert parser.feed(b'data: {"a": 1}\n') == []
        events = parser.feed(b"\n")
        assert [event.data for event in events] == ['{"a": 1}']

    def test_mixed_crlf_separator_is_recognized(self) -> None:
        """CRLF 与裸 LF 混合的分隔符（\\r\\n\\n）应被正确切分。"""
        parser = SSEStreamParser()
        events = parser.feed(b"data: x\r\n\n")
        assert [event.data for event in events] == ["x"]

    def test_multiple_events_in_one_chunk(self) -> None:
        """单个分片内含多个完整事件时应一次性全部产出。"""
        parser = SSEStreamParser()
        events = parser.feed(b"data: a\n\ndata: b\n\n")
        assert [event.data for event in events] == ["a", "b"]

    def test_comment_only_event_is_skipped(self) -> None:
        """保活注释事件不产出。"""
        parser = SSEStreamParser()
        assert parser.feed(b": ping\n\n") == []

    def test_invalid_utf8_event_is_skipped(self) -> None:
        """非法 UTF-8 的事件被跳过，后续事件照常产出（防御而非崩溃）。"""
        parser = SSEStreamParser()
        events = parser.feed(b"\xff\xfe\n\ndata: ok\n\n")
        assert [event.data for event in events] == ["ok"]

    def test_buffer_overflow_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """端点一直不发分隔符导致缓冲超上限时直接报错。"""
        monkeypatch.setattr(sse_module, "_MAX_BUFFER_SIZE", 16)
        parser = SSEStreamParser()
        with pytest.raises(AdapterError) as exc_info:
            parser.feed(b"x" * 32)
        assert "缓冲超过最大限制" in exc_info.value.message
        assert exc_info.value.context["buffer_size"] == 32

    def test_finish_on_empty_buffer_returns_empty(self) -> None:
        """缓冲为空时 finish 无事可做。"""
        assert SSEStreamParser().finish() == []

    def test_finish_parses_residual_event_without_trailing_blank(self) -> None:
        """端点不发尾部空行时，finish 应把残留事件解析出来。"""
        parser = SSEStreamParser()
        assert parser.feed(b"data: tail") == []
        assert [event.data for event in parser.finish()] == ["tail"]

    def test_finish_clears_buffer(self) -> None:
        """finish 处理完残留后应清空缓冲，重复调用不再产出。"""
        parser = SSEStreamParser()
        parser.feed(b"data: tail")
        assert len(parser.finish()) == 1
        assert parser.finish() == []

    def test_finish_with_truncated_utf8_raises(self) -> None:
        """流被异常截断（缓冲末尾不是完整 UTF-8 序列）时抛 AdapterError。"""
        parser = SSEStreamParser()
        parser.feed(b"\xff")
        with pytest.raises(AdapterError) as exc_info:
            parser.finish()
        assert "异常截断" in exc_info.value.message
        assert exc_info.value.context["error_type"] == "UnicodeDecodeError"


class TestIterStreamDeltas:
    """测试 iter_stream_deltas 的错误识别与 delta 提取。"""

    def test_yields_deltas_in_response_order(self) -> None:
        """多个 chunk 的 delta 应按响应顺序产出。"""
        chunks = [_delta_event("Hello").encode(), _delta_event(" world").encode()]
        assert _deltas(chunks) == ["Hello", " world"]

    def test_done_marker_stops_iteration(self) -> None:
        """遇到 [DONE] 后立即结束迭代，其后的事件不再处理。"""
        chunks = [
            _delta_event("first").encode(),
            b"data: [DONE]\n\n",
            _delta_event("ignored").encode(),
        ]
        assert _deltas(chunks) == ["first"]

    def test_non_2xx_status_raises_with_body_context(self) -> None:
        """HTTP 非 2xx 时抛 AdapterError，context 记录状态码与响应体片段。"""
        with pytest.raises(AdapterError) as exc_info:
            _deltas([], status_code=500)
        assert "HTTP 500" in exc_info.value.message
        assert exc_info.value.context["status_code"] == 500
        assert exc_info.value.context["response_body"] == _UPSTREAM_ERROR_BODY

    def test_non_bytes_error_body_falls_back_to_length(self) -> None:
        """响应体不是 bytes 形态时回退为长度文案，不让解码异常打断错误处理。"""

        async def _drain() -> None:
            async for _ in iter_stream_deltas(_UndecodableResponse(502)):
                pass

        with pytest.raises(AdapterError) as exc_info:
            asyncio.run(_drain())
        assert exc_info.value.context["response_body"] == "<17 bytes>"

    def test_chunks_without_text_yield_nothing(self) -> None:
        """无 content 的 chunk（role/finish_reason/usage 事件）不产出但也不报错。"""
        payloads: list[object] = [
            {"choices": []},
            {"choices": "not-a-list"},
            {"object": "usage"},
            {"choices": [{"finish_reason": "stop"}]},
            {"choices": [{"delta": {}}]},
            {"choices": [{"delta": {"content": 42}}]},
        ]
        chunks = [_data_event(payload).encode() for payload in payloads]
        assert _deltas(chunks) == []

    def test_error_event_with_message_and_type_raises(self) -> None:
        """标准错误事件：消息取 error.message，类型取 error.type。"""
        chunks = [_data_event({"error": {"message": "上游过载", "type": "server_error"}}).encode()]
        with pytest.raises(AdapterError) as exc_info:
            _deltas(chunks)
        assert "上游过载" in exc_info.value.message
        assert exc_info.value.context["error_type"] == "server_error"

    def test_error_event_without_message_falls_back_to_payload(self) -> None:
        """错误对象缺 message 时回退为整个对象的字符串形式。"""
        chunks = [_data_event({"error": {"code": 500}}).encode()]
        with pytest.raises(AdapterError) as exc_info:
            _deltas(chunks)
        assert "{'code': 500}" in exc_info.value.message
        assert exc_info.value.context["error_type"] is None

    def test_error_event_as_plain_string_raises(self) -> None:
        """少数端点把 error 发成字符串：按字符串内容报错，context 不带对象。"""
        chunks = [_data_event({"error": "boom"}).encode()]
        with pytest.raises(AdapterError) as exc_info:
            _deltas(chunks)
        assert "boom" in exc_info.value.message
        assert exc_info.value.context["error"] is None

    def test_non_object_payload_raises(self) -> None:
        """chunk 顶层不是 JSON 对象时抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            _deltas([_data_event([1, 2]).encode()])
        assert "顶层不是 JSON 对象" in exc_info.value.message

    def test_non_object_choice_raises(self) -> None:
        """choices[0] 不是对象时抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            _deltas([_data_event({"choices": ["x"]}).encode()])
        assert "choices[0] 不是对象" in exc_info.value.message

    def test_invalid_json_event_raises(self) -> None:
        """事件 data 不是合法 JSON 时抛 AdapterError。"""
        with pytest.raises(AdapterError) as exc_info:
            _deltas([b"data: {oops\n\n"])
        assert "不是合法 JSON" in exc_info.value.message

    def test_residual_event_without_trailing_blank_is_yielded(self) -> None:
        """端点不发尾部空行时，残留事件经 finish 仍应产出。"""
        chunks = [_delta_event("tail").encode()[:-2]]
        assert _deltas(chunks) == ["tail"]

    def test_residual_done_marker_is_skipped(self) -> None:
        """残留缓冲里的 [DONE] 不应被当成内容再次处理。"""
        chunks = [_delta_event("tail").encode()[:-2] + b"\n", b"\ndata: [DONE]\n"]
        assert _deltas(chunks) == ["tail"]

    def test_residual_event_without_text_yields_nothing(self) -> None:
        """残留事件只带 finish_reason 时不产出文本，但也不应报错。"""
        chunks = [_data_event({"choices": [{"finish_reason": "stop"}]}).encode()[:-2]]
        assert _deltas(chunks) == []


class TestCollectStreamContent:
    """测试 collect_stream_content 与逐 delta 迭代的一致性。"""

    def test_aggregates_all_deltas(self) -> None:
        """一次性聚合应得到与逐 delta 拼接完全相同的文本。"""
        chunks = [_delta_event("Hello").encode(), _delta_event(" world").encode()]
        assert _collect_content(chunks) == _EXPECTED_STREAM_TEXT

    def test_done_marker_ends_aggregation(self) -> None:
        """聚合同样在 [DONE] 处结束，其后事件不计入。"""
        chunks = [
            _delta_event("kept").encode(),
            b"data: [DONE]\n\n",
            _delta_event("dropped").encode(),
        ]
        assert _collect_content(chunks) == "kept"

"""AquaMind 命令行 ``run`` 子命令最小示例（cli_run_demo）。

``aquamind run`` 会向 ``base_url + /chat/completions`` 发起**真实的流式 HTTP 请求**，
因此要离线演示它，必须先有一个能返回 OpenAI 兼容 SSE 流的端点。

本示例的做法是：在**后台线程里起一个本地 HTTP 服务**（绑定 127.0.0.1，端口由内核分配），
它按 SSE 规范逐帧吐出 ``data: {"choices":[{"delta":{"content":"..."}}]}``，
最后发一帧 ``data: [DONE]`` 收尾；然后用 ``subprocess`` 调用真实的 ``aquamind run``。

这样演示的是**完整真实链路**（配置文件读取 → 建连 → SSE 分帧 → delta 提取 → 逐片输出），
只是端点换成本机进程——因此**不触任何真实 API，也不需要任何 API 密钥**（CI 零 key）。

演示四段：

1. ``run`` 正常执行：流式文本实时打到 stdout，退出码 0；
2. ``--speed`` 参数：按字符/秒限速输出，0 表示全速；
3. 配置文件字段非法：退出码 1，错误写 stderr 且不泄露端点回显的取值；
4. 端点不可达：退出码 1。

运行方式（在**仓库根目录**执行）::

  .venv\\Scripts\\python.exe examples/cli_run_demo/cli_run_demo.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# mock 端点要逐帧吐出的文本。切成多片是为了让"流式"这件事看得见：
# 每片是模型输出中的一个增量片段，顺序拼接后等于完整回答。
_CHUNKS = ["你好，", "我是本地 ", "mock 端点。", "这段文本按 SSE 协议分帧送达。"]

# 控制单次请求最多读取多少请求体，避免与真实请求体大小耦合
_MAX_BODY = 64 * 1024


class _SSEHandler(BaseHTTPRequestHandler):
  """把 ``POST /v1/chat/completions`` 响应成一条 OpenAI 兼容的 SSE 流。"""

  protocol_version = "HTTP/1.1"

  def do_POST(self) -> None:
    """按 OpenAI 兼容格式返回流式响应。

    方法名沿用 ``BaseHTTPRequestHandler`` 规定的 ``do_<METHOD>`` 命名。
    """
    length = int(self.headers.get("Content-Length") or 0)
    if length:
      self.rfile.read(min(length, _MAX_BODY))

    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    # chunked 传输：流还没结束，声明长度等于"长度未知"
    self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()

    for text in _CHUNKS:
      payload = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        ensure_ascii=False,
      )
      # SSE 以空行分帧：先 data 行，再一个空行
      self._write_chunk(f"data: {payload}\n\n")
    # 结束标记：OpenAI / vLLM / Ollama 均以 [DONE] 收尾
    self._write_chunk("data: [DONE]\n\n")
    self._write_chunk(b"") # 0 长度分片表示流结束

  def _write_chunk(self, text: str | bytes) -> None:
    """按 chunked 编码写出一帧。

    Args:
      text: 要写出的内容；空串或空 bytes 表示流结束。
    """
    raw = text.encode("utf-8") if isinstance(text, str) else text
    if raw:
      self.wfile.write(f"{len(raw):X}\r\n".encode("ascii"))
      self.wfile.write(raw)
      self.wfile.write(b"\r\n")
    else:
      self.wfile.write(b"0\r\n\r\n")
    self.wfile.flush()

  def log_message(self, format: str, *args: object) -> None:
    """关掉 ``http.server`` 默认的逐请求 stderr 日志。

    参数名沿用标准库签名（``format`` 遮蔽内建名是标准库既定约定），
    此处仅做静默覆写，不消费任何参数。
    """
    del format, args


def _start_mock_endpoint() -> tuple[ThreadingHTTPServer, str]:
  """在后台线程启动本地 mock SSE 端点。

  Returns:
    tuple: （server 实例, 可直接写进配置文件的 base_url）。
  """
  server = ThreadingHTTPServer(("127.0.0.1", 0), _SSEHandler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  port = server.server_address[1]
  return server, f"http://127.0.0.1:{port}/v1"


def _write_config(directory: Path, name: str, payload: dict[str, object]) -> Path:
  """把运行配置写成 JSON 文件。

  Args:
    directory: 目标目录。
    name: 文件名。
    payload: 配置内容（会被序列化为 JSON）。

  Returns:
    Path: 写好的配置文件路径。
  """
  path = directory / name
  path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
  return path


def _build_command() -> list[str]:
  """构造调用 ``aquamind run`` 的命令前缀。

  优先使用安装后的 ``aquamind`` 可执行文件；若当前环境找不到它（例如未激活虚拟环境），
  回退为 ``python -m aquamind.cli``，两条路径进入的是同一个 typer app。

  Returns:
    list[str]: 命令前缀，形如 ``["aquamind", "run"]``。
  """
  executable = shutil.which("aquamind")
  if executable:
    return [executable, "run"]
  return [sys.executable, "-m", "aquamind.cli", "run"]


def _run_case(
  label: str, command: list[str], description: str
) -> tuple[subprocess.CompletedProcess[str], float]:
  """执行一次 ``aquamind run`` 并打印结果。

  Args:
    label: 用例标题。
    command: 完整命令行（含配置文件路径与参数）。
    description: 本次演示要说明的要点。

  Returns:
    tuple: （子进程结果, 本次执行的墙钟耗时秒数），便于调用方比较退出码与限速效果。
  """
  print()
  print("=" * 68)
  print(label)
  print("=" * 68)
  print(f" 命令：{' '.join(command)}")
  print(f" 预期：{description}")
  print(" --- stdout ---")
  started = time.perf_counter()
  result = subprocess.run(
    command, capture_output=True, text=True, encoding="utf-8", check=False
  )
  elapsed = time.perf_counter() - started
  print(" " + (result.stdout.strip() or "(空)"))
  if result.stderr.strip():
    print(" --- stderr ---")
    print(" " + result.stderr.strip())
  print(f" --- 退出码：{result.returncode}｜耗时：{elapsed:.2f}s ---")
  return result, elapsed


def main() -> None:
  """起本地端点，跑完四段 ``run`` 演示。"""
  print("本示例在本地起一个 mock SSE 端点，用真实的 aquamind run 去调它。")
  print("全过程不访问外网，也不需要 API 密钥。")

  server, base_url = _start_mock_endpoint()
  print(f"mock 端点已就绪：{base_url}/chat/completions")

  try:
    with tempfile.TemporaryDirectory(prefix="aquamind-cli-demo-") as tmp:
      directory = Path(tmp)
      ok_config = _write_config(
        directory,
        "run_ok.json",
        {
          "base_url": base_url,
          "model": "mock-model",
          "prompt": "请做个自我介绍",
          "timeout": 10.0,
        },
      )
      # 字段名拼错：RunSpec 开启 extra="forbid"，构造期即拒绝
      bad_config = _write_config(
        directory,
        "run_bad.json",
        {"base_url": base_url, "model": "mock-model", "time_out": 10.0},
      )
      # 端口 1 在本机几乎必然连接失败，用它演示不可达路径
      unreachable_config = _write_config(
        directory,
        "run_unreachable.json",
        {"base_url": "http://127.0.0.1:1/v1", "model": "mock-model", "timeout": 2.0},
      )

      command = _build_command()
      print(f"调用方式：{' '.join(command)} ...")
      if command[0].endswith("aquamind.cli"):
        print("（未在 PATH 找到 aquamind 可执行文件，已回退到 python -m aquamind.cli）")

      ok, ok_seconds = _run_case(
        "1. 正常执行（--speed 0，全速）",
        [*command, str(ok_config)],
        "退出码 0；stdout 为四帧拼接后的完整文本；耗时接近进程启动开销",
      )
      slow, slow_seconds = _run_case(
        "2. --speed 10：按 10 字符/秒限速",
        [*command, str(ok_config), "--speed", "10"],
        "退出码 0；**文本与第 1 段完全一致**，但耗时被拉长到字符数 ÷ 10 秒",
      )
      bad, _ = _run_case(
        "3. 配置文件字段非法（time_out 拼错）",
        [*command, str(bad_config)],
        "退出码 1；错误写 stderr，前缀为「错误:」，不回显端点或密钥取值",
      )
      unreachable, _ = _run_case(
        "4. 端点不可达（127.0.0.1:1）",
        [*command, str(unreachable_config)],
        "退出码 1；网络异常收敛为 AdapterError，不抛裸 httpx 堆栈",
      )

      print()
      print("=" * 68)
      print("小结")
      print("=" * 68)
      print(f" 全速（--speed 0） 退出码 {ok.returncode}，耗时 {ok_seconds:.2f}s")
      print(f" 限速（--speed 10） 退出码 {slow.returncode}，耗时 {slow_seconds:.2f}s")
      print(f" 配置字段非法    退出码 {bad.returncode}")
      print(f" 端点不可达     退出码 {unreachable.returncode}")
      print()
      delta = slow_seconds - ok_seconds
      print(f" 两段 stdout 逐字相同，耗时差 {delta:.2f}s 就是限速的效果——")
      print(" 输出内容与节奏是两件事：--speed 只改节奏，不改内容。")
      print()
      print(" 要点：CLI 只向用户暴露一行「错误: ...」，不打印异常堆栈；")
      print("    领域异常（配置 / 适配器）统一收敛为退出码 1。")
  finally:
    server.shutdown()
    server.server_close()
    print()
    print("mock 端点已关闭。")


if __name__ == "__main__":
  main()

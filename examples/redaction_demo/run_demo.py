"""cassette 录制期脱敏演示（M1-D15）：敏感字段落盘前自动 REDACTED，密钥零泄漏。

运行（在仓库根目录）：
  .venv\\Scripts\\python.exe examples/redaction_demo/run_demo.py

演示流程：
  1. 构造一个含敏感字段的示例请求（请求体 api_key、请求头 Authorization）
    与响应（响应体 access_token、响应头 Set-Cookie）；
  2. 调用 record() 落盘到系统临时目录；
  3. 打印脱敏前后对比：原始请求体 vs 落盘后的 cassette 内容；
  4. 调用 scripts/scan_cassette_secrets.py 复扫该目录，确认无泄漏
    （固定复跑命令见本目录 README）。

本脚本不发起任何真实网络请求，也不含真实密钥（演示用假密钥均为明显的占位串）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from aquamind.replay import RequestInfo, ResponseInfo, record

# 演示用假密钥：刻意短于 16 字符（"sk-" 后仅 9 位），不会命中仓库密钥扫描规则；
# 它的作用是证明「这里本来该有一个密钥、落盘后变成了 REDACTED」
DEMO_API_KEY = "sk-demo-0001"
# 演示用假 token（JWT 形态占位，无 sk-/pypi- 等前缀）
DEMO_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.demo-access-token"

# 示例请求体：api_key 是唯一的敏感字段，其余字段应原样保留
REQUEST_BODY = json.dumps(
  {
    "model": "demo-model",
    "messages": [{"role": "user", "content": "你好，请用一句话介绍你自己"}],
    "temperature": 0.7,
    "api_key": DEMO_API_KEY,
  },
  ensure_ascii=False,
  sort_keys=True,
)

# 示例响应体：access_token 是唯一的敏感字段
RESPONSE_BODY = json.dumps(
  {
    "id": "chatcmpl-demo-redact",
    "object": "chat.completion",
    "model": "demo-model",
    "choices": [
      {"index": 0, "message": {"role": "assistant", "content": "我是一个演示用的模型。"}}
    ],
    "access_token": DEMO_ACCESS_TOKEN,
  },
  ensure_ascii=False,
  sort_keys=True,
)

REQUEST = RequestInfo(
  method="POST",
  url="http://127.0.0.1:8000/v1/chat/completions",
  headers={
    "Content-Type": "application/json",
    "Authorization": "Bearer demo-token-0001",
  },
  body=REQUEST_BODY,
)

RESPONSE = ResponseInfo(
  status_code=200,
  headers={
    "Content-Type": "application/json",
    "Set-Cookie": "session=abc123; HttpOnly",
  },
  body=RESPONSE_BODY,
)


def main() -> int:
  """运行脱敏演示：录制 → 对比 → 复扫。

  Returns:
    int: 进程退出码；0 表示脱敏与扫描全部通过，1 表示发现明文泄漏。
  """
  cassette_dir = Path(tempfile.mkdtemp(prefix="aquamind-redaction-demo-"))
  print(f"[0] 写入目录  : {cassette_dir}（系统临时目录，退出后仍保留）")

  # ── 1. 录制：脱敏发生在 record() 边界，调用方无需做任何事 ────────────
  path = Path(record(REQUEST, RESPONSE, cassette_dir))
  payload = json.loads(path.read_text(encoding="utf-8"))
  print(f"[1] 落盘文件  : {path.name}（{path.stat().st_size} 字节）")

  print("\n── 脱敏前后对比 ──────────────────────────────────────")
  print(f"[2] 录制前请求体: {REQUEST_BODY}")
  print(f"[3] 落盘请求体 : {payload['request_body']}")

  # ── 2. 四个通道逐项核对 ───────────────────────────────────────────
  request_body = json.loads(payload["request_body"])
  response_body = json.loads(payload["response_body"])
  print("\n── 四个通道的落盘形态 ────────────────────────────────")
  print(f"[4] 请求头 Authorization: {payload['request_headers'].get('authorization')}")
  print("  （sha256: 短摘要——请求头参与指纹，必须保留「换密钥即换指纹」的区分度）")
  print(f"[5] 请求体 api_key   : {request_body['api_key']}")
  print(f"[6] 响应头 Set-Cookie  : {payload['response_headers']['Set-Cookie']}")
  print(f"[7] 响应体 access_token : {response_body['access_token']}")
  print(f"[8] 非敏感字段保留示例 : model={request_body['model']!r}，"
     f"temperature={request_body['temperature']}（未被改动）")

  # ── 3. 明文自查：四项演示密钥形态都不得出现在文件文本里 ──────────────
  content = path.read_text(encoding="utf-8")
  secrets_to_check = {
    "请求体 api_key": DEMO_API_KEY,
    "请求头 token": "demo-token-0001",
    "响应体 token": DEMO_ACCESS_TOKEN,
    "响应头 cookie": "session=abc123",
  }
  leaked = [name for name, value in secrets_to_check.items() if value in content]
  if leaked:
    print(f"[9] 明文自查  : 泄漏！{leaked}")
  else:
    print(f"[9] 明文自查  : {len(secrets_to_check)} 项演示密钥均不在 cassette 文本中")

  # ── 4. 扫描脚本复扫（固定命令见同目录 README） ─────────────────────
  script = Path(__file__).resolve().parents[2] / "scripts" / "scan_cassette_secrets.py"
  env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
  result = subprocess.run(
    [sys.executable, str(script), "--dir", str(cassette_dir)],
    capture_output=True,
    text=True,
    encoding="utf-8",
    env=env,
    check=False,
  )
  print("\n── cassette 扫描脚本复扫 ─────────────────────────────")
  print(f"[10] 扫描输出  : {result.stdout.strip()}")
  print(f"[11] 扫描退出码 : {result.returncode}")

  ok = not leaked and result.returncode == 0
  if ok:
    print("\n结论：录制期脱敏生效，四项密钥零落盘，扫描通过（退出码 0）。")
  else:
    print("\n结论：存在明文泄漏，演示失败。")
  return 0 if ok else 1


if __name__ == "__main__":
  raise SystemExit(main())
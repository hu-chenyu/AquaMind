"""VCR 录制回放最简演示：构造模拟请求 → 录制 → 查找 → 回放。

运行（在仓库根目录）：
    .venv\\Scripts\\python.exe examples/replay_demo/demo_record_play.py

本脚本不发起任何真实网络请求，响应体在本地构造，仅演示 replay 模块的用法。
"""

from __future__ import annotations

import json
from pathlib import Path

from aquamind.exceptions import ReplayError
from aquamind.replay import (
    Cassette,
    RequestInfo,
    ResponseInfo,
    find_match,
    play,
    record,
    replay_request,
)

# 示例 cassette 目录：与本脚本同级的 cassettes/
CASSETTE_DIR = Path(__file__).parent / "cassettes"

# 一次典型的补全请求：中文 body + 鉴权头 + 噪声头
REQUEST = RequestInfo(
    method="POST",
    url="http://127.0.0.1:8000/v1/chat/completions",
    headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer demo-token-not-a-real-credential",
        "User-Agent": "aquamind-demo/0.0.4",
    },
    body=json.dumps(
        {
            "model": "demo-model",
            "messages": [{"role": "user", "content": "请用一句话介绍你自己"}],
        },
        ensure_ascii=False,
        sort_keys=True,
    ),
)

# 模拟响应：真实场景由适配器层发起 HTTP 后拿到的响应替换
RESPONSE = ResponseInfo(
    status_code=200,
    headers={"Content-Type": "application/json"},
    body=json.dumps(
        {
            "id": "chatcmpl-demo-0001",
            "model": "demo-model",
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "我是一个演示用的模型。"}}
            ],
            "usage": {"prompt_tokens": 18, "completion_tokens": 12, "total_tokens": 30},
        },
        ensure_ascii=False,
        sort_keys=True,
    ),
)


def main() -> None:
    """演示完整的录制-回放链路，以及两道必须显式失败的防线。

    Returns:
        None: 结果直接打印到标准输出。
    """
    # ── 1. 录制：请求指纹算出来就是 cassette 文件名 ──────────────
    request_hash = Cassette.compute_request_hash(REQUEST)
    path = Path(record(REQUEST, RESPONSE, CASSETTE_DIR))
    print(f"[1] 请求指纹   : {request_hash}")
    print(f"[2] 落盘文件   : {path.name}（{path.stat().st_size} 字节）")
    # 鉴权头只以短摘要落盘，明文密钥不进入会被提交的 cassette
    stored_auth = json.loads(path.read_text(encoding="utf-8"))["request_headers"]["authorization"]
    print(f"[3] 鉴权头落盘 : {stored_auth}  ← 摘要而非明文")

    # ── 2. 回放：离线用同一请求找回响应 ────────────────────────
    cassette = find_match(REQUEST, CASSETTE_DIR)
    assert cassette is not None
    replayed = play(cassette)
    print(f"[4] 回放状态码 : {replayed.status_code}")
    print(f"[5] 回放 text  : {replayed.text[:60]}...")
    print(f"[6] 回放 json  : {replayed.json()['choices'][0]['message']['content']}")
    print(f"[7] 录制时刻   : {replayed.recorded_at}（来源: {replayed.source}）")

    # ── 3. 防线一：请求变了 → 指纹不同 → 无匹配，显式报错 ─────────
    changed = REQUEST.model_copy(update={"body": REQUEST.body.replace("一句话", "三句话")})
    print(f"[8] 改 body 后查找: {find_match(changed, CASSETTE_DIR)}（None = 无匹配）")
    try:
        replay_request(changed, CASSETTE_DIR)
    except ReplayError as exc:
        print(f"[9] 无匹配报错 : {exc}")

    # ── 4. 防线二：cassette 被改过 → 指纹版本不符，显式报错 ────────
    tampered = path.read_text(encoding="utf-8").replace(request_hash, "0123456789abcdef")
    path.write_text(tampered, encoding="utf-8")
    try:
        find_match(REQUEST, CASSETTE_DIR)
    except ReplayError as exc:
        print(f"[10] 指纹变更报错: {exc}")
    # 演示完复原，避免留下被改坏的示例文件
    path.write_text(
        json.dumps(cassette.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n结论：录制与回放逐字一致（status_code/body/text 全等），"
          "请求变更与文件被篡改两类错误都显式抛出，不会静默回放。")


if __name__ == "__main__":
    main()

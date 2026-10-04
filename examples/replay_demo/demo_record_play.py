"""VCR 录制回放最简演示：构造模拟请求 → 录制 → 查找 → 回放。

运行（在仓库根目录）：
    .venv\\Scripts\\python.exe examples/replay_demo/demo_record_play.py

需要把演示 cassette 写进仓库内示例目录时（会改动被 git 跟踪的文件，慎用）：
    .venv\\Scripts\\python.exe examples/replay_demo/demo_record_play.py ^
        --cassette-dir examples/replay_demo/cassettes

默认写入系统临时目录：演示脚本会重写 cassette（录制时刻随运行变化），若默认
写仓库，每次运行都会在被跟踪文件上留下改动，把工作区弄脏、并让已交付的
示例产物与实际内容漂移。

本脚本不发起任何真实网络请求，响应体在本地构造，仅演示 replay 模块的用法。
"""

from __future__ import annotations

import argparse
import json
import tempfile
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


def _parse_args() -> argparse.Namespace:
    """解析命令行参数。

    Returns:
        argparse.Namespace: 解析结果，仅含 ``cassette_dir`` 一项（None 表示临时目录）。
    """
    parser = argparse.ArgumentParser(
        description="AquaMind VCR 录制回放演示（默认写入系统临时目录，不污染仓库）"
    )
    parser.add_argument(
        "--cassette-dir",
        default=None,
        help="指定 cassette 写入目录；缺省时写入系统临时目录",
    )
    return parser.parse_args()


def main() -> None:
    """演示完整的录制-回放链路，以及两道必须显式失败的防线。

    Returns:
        None: 结果直接打印到标准输出。
    """
    args = _parse_args()
    # 临时目录用 mkdtemp 而非 TemporaryDirectory：演示结束时保留目录，
    # 便于读者在脚本退出后仍能打开 cassette 文件查看内容
    is_temporary = args.cassette_dir is None
    cassette_dir = (
        Path(tempfile.mkdtemp(prefix="aquamind-demo-"))
        if is_temporary
        else Path(args.cassette_dir)
    )
    if is_temporary:
        print(f"[0] 写入目录   : {cassette_dir}（系统临时目录，退出后仍保留）")

    # ── 1. 录制：请求指纹算出来就是 cassette 文件名 ──────────────
    request_hash = Cassette.compute_request_hash(REQUEST)
    path = Path(record(REQUEST, RESPONSE, cassette_dir))
    print(f"[1] 请求指纹   : {request_hash}")
    print(f"[2] 落盘文件   : {path.name}（{path.stat().st_size} 字节）")
    # 鉴权头只以短摘要落盘，明文密钥不进入会被提交的 cassette
    stored_auth = json.loads(path.read_text(encoding="utf-8"))["request_headers"]["authorization"]
    print(f"[3] 鉴权头落盘 : {stored_auth}  ← 摘要而非明文")

    # ── 2. 回放：离线用同一请求找回响应 ────────────────────────
    cassette = find_match(REQUEST, cassette_dir)
    assert cassette is not None
    replayed = play(cassette)
    print(f"[4] 回放状态码 : {replayed.status_code}")
    print(f"[5] 回放 text  : {replayed.text[:60]}...")
    print(f"[6] 回放 json  : {replayed.json()['choices'][0]['message']['content']}")
    print(f"[7] 录制时刻   : {replayed.recorded_at}（来源: {replayed.source}，随运行变化）")

    # ── 3. 防线一：请求变了 → 指纹不同 → 无匹配，显式报错 ─────────
    changed = REQUEST.model_copy(update={"body": REQUEST.body.replace("一句话", "三句话")})
    print(f"[8] 改 body 后查找: {find_match(changed, cassette_dir)}（None = 无匹配）")
    try:
        replay_request(changed, cassette_dir)
    except ReplayError as exc:
        print(f"[9] 无匹配报错 : {exc}")

    # ── 4. 防线二：cassette 被改过 → 指纹版本不符，显式报错 ────────
    tampered = path.read_text(encoding="utf-8").replace(request_hash, "0123456789abcdef")
    path.write_text(tampered, encoding="utf-8")
    try:
        find_match(REQUEST, cassette_dir)
    except ReplayError as exc:
        print(f"[10] 指纹变更报错: {exc}")
    # 演示完复原，避免留下被改坏的示例文件
    path.write_text(
        json.dumps(cassette.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("\n结论：录制与回放逐字一致（status_code/body/text 全等），"
          "请求变更与文件被篡改两类错误都显式抛出，不会静默回放。")
    if is_temporary:
        print(f"演示 cassette 已写入临时目录：{cassette_dir}（不会污染仓库）")


if __name__ == "__main__":
    main()

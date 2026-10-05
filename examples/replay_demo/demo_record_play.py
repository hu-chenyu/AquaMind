"""VCR 录制回放演示：构造模拟请求 → 录制 → 查找 → 回放（含 chunk 时序录制）。

运行（在仓库根目录）：
    .venv\\Scripts\\python.exe examples/replay_demo/demo_record_play.py

需要把演示 cassette 写进仓库内示例目录时（会改动被 git 跟踪的文件，慎用）：
    .venv\\Scripts\\python.exe examples/replay_demo/demo_record_play.py ^
        --cassette-dir examples/replay_demo/cassettes

默认写入系统临时目录：演示脚本会重写 cassette（录制时刻随运行变化），若默认
写仓库，每次运行都会在被跟踪文件上留下改动，把工作区弄脏、并让已交付的
示例产物与实际内容漂移。

两段演示：
    1. 文本录制回放 + 两道防线（请求变更、文件被篡改）；
    2. 流式响应的 chunk 到达时序录制（M1-D06b），以及 format_version 升到 2
       之后「v2 读 v1」兼容、「v1 读 v2」报版本过新的双向口径。

本脚本不发起任何真实网络请求，响应体在本地构造，仅演示 replay 模块的用法。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from aquamind.exceptions import ReplayError
from aquamind.replay import (
    CURRENT_FORMAT_VERSION,
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


# 模拟一次流式响应：(到达时刻毫秒, chunk 增量文本)
# 第二个 chunk 的时刻故意给 5 位小数（100.23456），用于演示 arrival_ms 保留 3 位
STREAM_CHUNKS: list[tuple[float, str]] = [
    (0.0, "你好"),
    (100.23456, "，我是"),
    (250.0, "流式"),
    (400.0, "模型。"),
]
# 流式请求：URL 与 body 都与非流式请求不同，否则会落到同一个 cassette 文件
STREAM_REQUEST = RequestInfo(
    method="POST",
    url="http://127.0.0.1:8000/v1/chat/completions?stream=1",
    headers={"Content-Type": "application/json"},
    body=json.dumps(
        {
            "model": "demo-model",
            "messages": [{"role": "user", "content": "请用一句话介绍你自己"}],
            "stream": True,
        },
        ensure_ascii=False,
        sort_keys=True,
    ),
)
# 流式响应体：content 等于各 chunk 增量拼接，用于验证「录的内容没串片」
STREAM_RESPONSE = ResponseInfo(
    status_code=200,
    headers={"Content-Type": "application/json"},
    body=json.dumps(
        {
            "id": "chatcmpl-demo-0002",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "".join(text for _, text in STREAM_CHUNKS),
                    },
                }
            ],
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


def _demo_timing(cassette_dir: Path) -> None:
    """演示流式响应的 chunk 时序录制，以及 format_version 双向兼容口径。

    演示 4 件事：

    1. ``record(chunks=...)`` 把每个 chunk 的相对到达时刻与文本一起落盘，
       第一个 chunk 自动归一化为 0.0（首包延迟不进时序）；
    2. 落盘的 JSON 与 ``find_match`` 读回的 ``timing`` 逐字段一致（往返无损）；
    3. D6b 边界：``play()`` 仍一次性返回完整响应体，不按录下的时刻分块输出；
    4. 版本口径：新代码读旧 v1 cassette（无 timing）→ ``timing is None``；
       旧代码读 v2 cassette → 报「版本过新」而不是「文件损坏」。

    Args:
        cassette_dir: cassette 写入目录（临时目录或显式指定的示例目录）。
    """
    print("\n── 时序录制（M1-D06b）──────────────────────────────")
    stream_path = Path(record(STREAM_REQUEST, STREAM_RESPONSE, cassette_dir, chunks=STREAM_CHUNKS))
    payload = json.loads(stream_path.read_text(encoding="utf-8"))
    print(f"[11] 时序落盘   : {stream_path.name}（{stream_path.stat().st_size} 字节，"
          f"format_version={payload['format_version']}）")

    cassette = find_match(STREAM_REQUEST, cassette_dir)
    assert cassette is not None and cassette.timing is not None
    arrivals = " / ".join(f"{chunk.arrival_ms:.3f}" for chunk in cassette.timing)
    print(f"[12] 时序明细   : {len(cassette.timing)} 个 chunk，到达时刻(ms) {arrivals}")
    print("[13] 增量文本   : " + " + ".join(repr(chunk.text) for chunk in cassette.timing))
    # 首片归一化：录制方给的是绝对到达时刻，replay 侧以第一个 chunk 为原点平移
    print(f"[14] 首片归一化 : {cassette.timing[0].arrival_ms}（原始首包时刻不进时序）")

    # 往返无损：磁盘 JSON 与契约模型读出的 timing 必须逐字段相同
    on_disk = [(item["index"], item["arrival_ms"], item["text"]) for item in payload["timing"]]
    in_memory = [(c.index, c.arrival_ms, c.text) for c in cassette.timing]
    print(f"[15] JSON 往返  : {on_disk == in_memory}（index/arrival_ms/text 逐字段一致）")

    # D6b 边界：时序被录下但回放不分块（时序调度属于 M1-D06c）
    replayed = play(cassette)
    content = str(replayed.json()["choices"][0]["message"]["content"])
    print(f"[16] 回放全文   : {content}（{len(replayed.text)} 字符，未按 chunk 切分）")
    print(f"[17] 拼接一致   : {content == ''.join(c.text for c in cassette.timing)}"
          "（各 chunk 增量拼接 == 响应体 content）")

    # 版本口径一：新代码读旧 v1 cassette（无 timing 字段）→ 按缺省 None 处理
    v1_payload = {**payload, "format_version": 1}
    del v1_payload["timing"]
    stream_path.write_text(
        json.dumps(v1_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    legacy = find_match(STREAM_REQUEST, cassette_dir)
    assert legacy is not None
    print(f"[18] 读旧 v1    : format_version={legacy.format_version}，timing={legacy.timing}"
          "（无 timing 字段 → None，按瞬时回放）")

    # 版本口径二：文件声明的版本高于代码支持上限 → 报「版本过新」
    stream_path.write_text(
        json.dumps({**payload, "format_version": CURRENT_FORMAT_VERSION + 1}, ensure_ascii=False,
                   indent=2),
        encoding="utf-8",
    )
    try:
        find_match(STREAM_REQUEST, cassette_dir)
    except ReplayError as exc:
        print(f"[19] 版本闸门   : {exc}")

    # 演示完复原，避免留下被改坏的示例文件
    stream_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
    _demo_timing(cassette_dir)
    print("\n结论（时序）：chunk 到达时刻可录可还原且往返无损；D6b 只录不播，"
          "回放不分块、倍率调度留给 M1-D06c。")
    if is_temporary:
        print(f"演示 cassette 已写入临时目录：{cassette_dir}（不会污染仓库）")


if __name__ == "__main__":
    main()

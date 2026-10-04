"""生成 scripts/replay_baseline.json：VCR 文本录制回放的一致率基线。

运行（在仓库根目录）：
    .venv\\Scripts\\python.exe examples/replay_demo/generate_baseline.py

口径说明：
    本基线只覆盖 M1-D06a 的**文本层面**还原精度（状态码/响应体/text 逐字一致率
    与指纹计算开销）。chunk 到达时间（timing）与时序调度回放的偏差指标由
    M1-D06b / M1-D06c 完成后补充，本文件届时追加对应字段。

复现性：除 generated_at 与指纹计算耗时外，所有字段均为确定性结果——
    一致率类字段重跑必然相同，耗时类字段随机器波动属正常。
"""

from __future__ import annotations

import json
import platform
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aquamind.replay import (
    Cassette,
    RequestInfo,
    ResponseInfo,
    find_match,
    play,
    record,
)

# 仓库根目录：本脚本位于 <repo>/examples/replay_demo/generate_baseline.py
REPO_ROOT = Path(__file__).resolve().parents[2]
# 基线数据落盘位置
BASELINE_PATH = REPO_ROOT / "scripts" / "replay_baseline.json"
# 指纹耗时的重复次数：单次调用在微秒级，需多次平均才稳定
HASH_ITERATIONS = 100
# URL 前缀（本地假端点，本脚本不发起任何真实网络请求）
_BASE_URL = "http://127.0.0.1:8000/v1/chat/completions"


@dataclass(frozen=True)
class Scenario:
    """一组录制-回放场景：一个请求 + 它应当被还原出的响应。

    Attributes:
        label: 场景中文名，仅用于人工阅读报告。
        request: 请求信息。
        response: 响应信息（本地构造，不经网络）。
    """

    label: str
    request: RequestInfo
    response: ResponseInfo
    # 期望落盘的响应体文本：bytes 在录制边界按 UTF-8 解码、None 归一为空串
    expected_body: str = field(default="")

    def __post_init__(self) -> None:
        """按录制侧的归一规则推导期望响应体文本，避免脚本里另写一份归一逻辑。"""
        body = self.response.body
        if body is None:
            object.__setattr__(self, "expected_body", "")
        elif isinstance(body, bytes):
            object.__setattr__(self, "expected_body", body.decode("utf-8"))
        else:
            object.__setattr__(self, "expected_body", body)


def _build_scenarios() -> list[Scenario]:
    """构造覆盖多种形态的录制-回放场景集。

    覆盖维度：GET/POST/DELETE 三种方法、中文与英文 body、中文 + emoji、
    含鉴权头（authorization / x-api-key）与不含鉴权头、带非关键噪声头
    （User-Agent）、bytes 响应体、无响应体（204）、非 2xx 响应（503）。

    Returns:
        list[Scenario]: 场景列表。
    """
    json_headers = {"Content-Type": "application/json", "Accept": "application/json"}

    def _payload(model: str, content: str, **extra: Any) -> str:
        """构造与录制侧口径一致的 JSON 请求体（键排序，保证指纹稳定）。"""
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
        }
        body.update(extra)
        return json.dumps(body, ensure_ascii=False, sort_keys=True)

    scenarios = [
        Scenario(
            label="GET 无请求体",
            request=RequestInfo(
                method="GET", url=f"{_BASE_URL}?q=ping", headers={"Accept": "application/json"}
            ),
            response=ResponseInfo(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body='{"pong": true}',
            ),
        ),
        Scenario(
            label="POST 中文 body",
            request=RequestInfo(
                method="POST", url=_BASE_URL, headers=json_headers, body=_payload("demo-model", "你好")
            ),
            response=ResponseInfo(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body='{"choices": [{"message": {"content": "你好，有什么可以帮你？"}}]}',
            ),
        ),
        Scenario(
            label="POST 英文 body",
            request=RequestInfo(
                method="POST",
                url=_BASE_URL,
                headers=json_headers | {"User-Agent": "aquamind-baseline/0.0.4"},
                body=_payload("demo-model", "Give a one-line self intro"),
            ),
            response=ResponseInfo(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body='{"choices": [{"message": {"content": "I am a demo model."}}]}',
            ),
        ),
        Scenario(
            label="POST 中文 + emoji body（带 authorization）",
            request=RequestInfo(
                method="POST",
                url=f"{_BASE_URL}?tenant=acme",
                headers=json_headers | {"Authorization": "Bearer baseline-token-not-a-real-cred"},
                body=_payload("demo-model", "带中文标点与 emoji：你好 🐟", stream=False),
            ),
            response=ResponseInfo(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body='{"choices": [{"message": {"content": "已收到 🐟"}}]}',
            ),
        ),
        Scenario(
            label="POST 大 token 预算（带 x-api-key）",
            request=RequestInfo(
                method="POST",
                url=f"{_BASE_URL}?variant=long",
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": "baseline-key-not-a-real-cred",
                },
                body=_payload("demo-model-long", ""),
            ),
            response=ResponseInfo(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body='{"choices": [{"message": {"content": "长上下文场景"}}]}',
            ),
        ),
        Scenario(
            label="GET 中文查询串（bytes 响应体）",
            request=RequestInfo(
                method="GET",
                url=f"{_BASE_URL}?health=%E5%81%A5%E5%BA%B7",
                headers={"Accept": "application/json"},
            ),
            response=ResponseInfo(
                status_code=200,
                headers={"Content-Type": "application/json"},
                body="字节响应体（bytes）".encode(),
            ),
        ),
        Scenario(
            label="DELETE 无响应体",
            request=RequestInfo(
                method="DELETE",
                url=f"{_BASE_URL}?session=s-001",
                headers={"Accept": "application/json"},
            ),
            response=ResponseInfo(status_code=204, headers={}, body=None),
        ),
        Scenario(
            label="POST 非 2xx 响应",
            request=RequestInfo(
                method="POST",
                url=f"{_BASE_URL}?mode=overload",
                headers=json_headers,
                body=_payload("demo-model", "压测一下"),
            ),
            response=ResponseInfo(
                status_code=503,
                headers={"Content-Type": "application/json"},
                body='{"error": {"message": "服务当前繁忙，请稍后重试"}}',
            ),
        ),
    ]
    return scenarios


def main() -> None:
    """跑完录制-回放全流程并写出基线数据。

    Returns:
        None: 统计结果写入 ``scripts/replay_baseline.json`` 并打印摘要。
    """
    scenarios = _build_scenarios()

    status_hits = 0
    body_hits = 0
    text_hits = 0
    hash_stable = 0
    replayed_ok = 0
    hash_ms_samples: list[float] = []
    per_case: list[dict[str, Any]] = []
    # 留一个实测指纹样本用于报告，避免把 hash 长度在脚本里再写一遍常量
    sample_hash = ""

    # 基线统计用独立临时目录，避免污染示例 cassette 目录。
    # 用上下文管理器而非 mkdtemp：后者在脚本退出后不清理，每次运行都在 %TEMP%
    # 下留一个 aquamind-baseline-* 目录（内含 8 个 cassette 文件）持续累积
    with tempfile.TemporaryDirectory(prefix="aquamind-baseline-") as tmpdir:
        cassette_dir = Path(tmpdir)
        for index, scenario in enumerate(scenarios, start=1):
            record(scenario.request, scenario.response, cassette_dir)

            # 指纹稳定性：同一请求算两次必须一致
            first_hash = Cassette.compute_request_hash(scenario.request)
            second_hash = Cassette.compute_request_hash(scenario.request)
            hash_stable += int(first_hash == second_hash)
            sample_hash = first_hash

            # 指纹计算耗时：重复 HASH_ITERATIONS 次取平均
            start = time.perf_counter()
            for _ in range(HASH_ITERATIONS):
                Cassette.compute_request_hash(scenario.request)
            elapsed_ms = (time.perf_counter() - start) * 1000 / HASH_ITERATIONS
            hash_ms_samples.append(elapsed_ms)

            cassette = find_match(scenario.request, cassette_dir)
            if cassette is None:
                per_case.append(
                    {
                        "index": index,
                        "label": scenario.label,
                        "method": scenario.request.method,
                        "matched": False,
                        "request_hash": first_hash,
                        "hash_compute_ms": round(elapsed_ms, 4),
                    }
                )
                continue

            replayed = play(cassette)
            replayed_ok += 1
            # 三个一致率：状态码、响应体原文、响应体文本
            # （bytes 已在录制边界归一为文本，故 text 期望值与 body 期望值同源）
            status_hit = replayed.status_code == scenario.response.status_code
            body_hit = replayed.body == scenario.expected_body
            text_hit = replayed.text == scenario.expected_body
            status_hits += int(status_hit)
            body_hits += int(body_hit)
            text_hits += int(text_hit)
            per_case.append(
                {
                    "index": index,
                    "label": scenario.label,
                    "method": scenario.request.method,
                    "matched": True,
                    "request_hash": first_hash,
                    "recorded_status_code": scenario.response.status_code,
                    "replayed_status_code": replayed.status_code,
                    "status_code_match": status_hit,
                    "body_match": body_hit,
                    "text_match": text_hit,
                    "hash_compute_ms": round(elapsed_ms, 4),
                }
            )

    total = len(scenarios)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "task": "M1-D06a VCR 文本录制回放一致率基线",
        "scope": "文本层面（请求指纹/响应体还原精度）；timing 时序指标待 M1-D06b/D6c 补充",
        "environment": {"python": platform.python_version(), "platform": platform.system()},
        "summary": {
            "recorded_requests": total,
            "replay_succeeded": replayed_ok,
            "unmatched_requests": total - replayed_ok,
            "status_code_match_rate": round(status_hits / total, 4),
            "body_match_rate": round(body_hits / total, 4),
            "text_match_rate": round(text_hits / total, 4),
            "request_hash_stability_rate": round(hash_stable / total, 4),
            "hash_length": len(sample_hash),
        },
        "hash_cost": {
            "iterations_per_case": HASH_ITERATIONS,
            "total_measured_calls": total * HASH_ITERATIONS,
            "avg_compute_ms": round(statistics.fmean(hash_ms_samples), 4),
            "max_compute_ms": round(max(hash_ms_samples), 4),
        },
        "cases": per_case,
    }

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summary = report["summary"]
    print(f"基线已写入: {BASELINE_PATH.relative_to(REPO_ROOT)}")
    print(
        f"  录制 {summary['recorded_requests']} 条 / 回放成功 {summary['replay_succeeded']} 条"
        f" / 无匹配 {summary['unmatched_requests']} 条"
    )
    print(
        f"  status_code 一致率 {summary['status_code_match_rate']:.2%}"
        f" / body {summary['body_match_rate']:.2%}"
        f" / text {summary['text_match_rate']:.2%}"
    )
    print(f"  平均指纹计算耗时 {report['hash_cost']['avg_compute_ms']} ms")


if __name__ == "__main__":
    main()

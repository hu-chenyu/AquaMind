"""生成 scripts/replay_baseline.json：VCR 录制回放的一致率与时序录制基线。

运行（在仓库根目录）：
    .venv\\Scripts\\python.exe examples/replay_demo/generate_baseline.py

口径说明：
    本基线覆盖两层——

    1. **文本层面**（M1-D06a）：状态码/响应体/text 逐字一致率与指纹计算开销；
    2. **时序层面**（M1-D06b）：流式响应的 chunk 到达时刻录制精度（归一化、
       单调性、3 位小数精度、JSON 往返一致性）。D6b 只录不播，故**不含**
       「回放调度偏差」类指标——那属于 M1-D06c（时序调度回放与倍率）。

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
from itertools import pairwise
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
# arrival_ms 保留的小数位（与 replay 侧口径一致，用于精度校验）
ARRIVAL_DECIMALS = 3
# URL 前缀（本地假端点，本脚本不发起任何真实网络请求）
_BASE_URL = "http://127.0.0.1:8000/v1/chat/completions"


@dataclass(frozen=True)
class Scenario:
    """一组录制-回放场景：一个请求 + 它应当被还原出的响应。

    Attributes:
        label: 场景中文名，仅用于人工阅读报告。
        request: 请求信息。
        response: 响应信息（本地构造，不经网络）。
        chunks: 流式响应的 chunk 列表，每个元素为 (到达时刻毫秒, 增量文本)；
            None 表示非流式响应。
    """

    label: str
    request: RequestInfo
    response: ResponseInfo
    # 流式响应的 chunk 列表：(到达时刻毫秒, 增量文本)；None = 非流式响应
    chunks: list[tuple[float, str]] | None = None
    # 期望落盘的响应体文本：bytes 在录制边界按 UTF-8 解码、None 归一为空串
    expected_body: str = field(default="")
    # 期望的时序元组（index, 归一化 arrival_ms, text）；非流式场景为空列表
    expected_timing: list[tuple[int, float, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        """按录制侧的归一规则推导期望值，避免脚本里另写一份归一/解码逻辑。"""
        body = self.response.body
        if body is None:
            object.__setattr__(self, "expected_body", "")
        elif isinstance(body, bytes):
            object.__setattr__(self, "expected_body", body.decode("utf-8"))
        else:
            object.__setattr__(self, "expected_body", body)
        if self.chunks is None:
            return
        # 独立复刻一遍归一化规则（以第一个 chunk 为原点、保留 3 位小数），
        # 作为「录制侧输出 == 期望」的交叉校验依据
        first_arrival = self.chunks[0][0]
        object.__setattr__(
            self,
            "expected_timing",
            [
                (index, round(arrival - first_arrival, ARRIVAL_DECIMALS), text)
                for index, (arrival, text) in enumerate(self.chunks)
            ],
        )

    @property
    def streamed_content(self) -> str:
        """流式响应各 chunk 增量文本的拼接结果（应等于响应体里的 content）。"""
        return "".join(text for _, text in (self.chunks or []))


def _streamed_response(content: str) -> ResponseInfo:
    """按非流式响应体的结构包一层 content，用于流式场景（形态与真实补全一致）。

    Args:
        content: 完整回复文本（等于各 chunk 增量拼接）。

    Returns:
        ResponseInfo: 响应信息。
    """
    return ResponseInfo(
        status_code=200,
        headers={"Content-Type": "application/json"},
        body=json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": content}}]},
            ensure_ascii=False,
            sort_keys=True,
        ),
    )


def _build_scenarios() -> list[Scenario]:
    """构造覆盖多种形态的录制-回放场景集。

    覆盖维度：GET/POST/DELETE 三种方法、中文与英文 body、中文 + emoji、
    含鉴权头（authorization / x-api-key）与不含鉴权头、带非关键噪声头
    （User-Agent）、bytes 响应体、无响应体（204）、非 2xx 响应（503），
    以及三组流式响应（4 chunk 起点为 0、3 chunk 起点非 0、5 chunk 含
    亚毫秒时刻与中英混排）。

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
        # ── M1-D06b：流式响应场景（chunk 到达时刻录制）──────────────
        Scenario(
            label="POST 流式响应（4 chunk，起点 0ms）",
            request=RequestInfo(
                method="POST",
                url=f"{_BASE_URL}?stream=1",
                headers=json_headers,
                body=_payload("demo-model", "用一句话打个招呼", stream=True),
            ),
            # 逐字匀速的 4 片：最典型的流式形态
            chunks=[(0.0, "你"), (100.0, "好"), (250.0, "，我是"), (400.0, "流式模型。")],
            response=_streamed_response("你好，我是流式模型。"),
        ),
        Scenario(
            label="POST 流式响应（3 chunk，起点非 0ms）",
            request=RequestInfo(
                method="POST",
                url=f"{_BASE_URL}?stream=2",
                headers=json_headers,
                body=_payload("demo-model", "分析一下这次失败的原因", stream=True),
            ),
            # 首包延迟（120ms）不应出现在 timing 里：归一化后首片恒为 0.0
            chunks=[(120.0, "分析结果"), (370.0, "：首包"), (920.0, "延迟已归一化。")],
            response=_streamed_response("分析结果：首包延迟已归一化。"),
        ),
        Scenario(
            label="POST 流式响应（5 chunk，亚毫秒时刻 + 中英混排）",
            request=RequestInfo(
                method="POST",
                url=f"{_BASE_URL}?stream=3",
                headers=json_headers | {"x-api-key": "baseline-stream-key-not-a-real-cred"},
                body=_payload("demo-model-long", "分步骤说明", stream=True),
            ),
            # 5 位小数的到达时刻：验证 3 位小数精度的截断/进位行为
            chunks=[
                (0.0, "Step"),
                (75.5, " 1"),
                (150.256789, " done"),
                (300.0, "，Step"),
                (412.125678, " 2 done"),
            ],
            response=_streamed_response("Step 1 done，Step 2 done"),
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

    # M1-D06b 时序录制统计：命中计数按「有时序的场景数」为分母，
    # 非流式场景不参与（对它们断言首片为 0.0 毫无意义）
    timing_scenarios = 0
    total_chunks = 0
    chunk_char_samples: list[int] = []
    first_zero_hits = 0
    monotonic_hits = 0
    precision_hits = 0
    roundtrip_hits = 0
    concat_hits = 0
    max_decimals_seen = 0

    # 基线统计用独立临时目录，避免污染示例 cassette 目录。
    # 用上下文管理器而非 mkdtemp：后者在脚本退出后不清理，每次运行都在 %TEMP%
    # 下留一个 aquamind-baseline-* 目录（内含 11 个 cassette 文件）持续累积
    with tempfile.TemporaryDirectory(prefix="aquamind-baseline-") as tmpdir:
        cassette_dir = Path(tmpdir)
        for index, scenario in enumerate(scenarios, start=1):
            record(scenario.request, scenario.response, cassette_dir, chunks=scenario.chunks)

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

            # 时序校验：仅对流式场景做，非流式场景 timing 必为 None
            if scenario.chunks is None:
                timing_fields: dict[str, Any] = {
                    "streaming": False,
                    "timing_is_none": cassette.timing is None,
                }
            else:
                timing_scenarios += 1
                observed = cassette.timing or []
                arrivals = [chunk.arrival_ms for chunk in observed]
                actual = [(chunk.index, chunk.arrival_ms, chunk.text) for chunk in observed]
                total_chunks += len(observed)
                chunk_char_samples.extend(len(chunk.text) for chunk in observed)
                # 实际小数位数上界：留 3 位即微秒精度，不应出现第 4 位
                case_decimals = max(
                    (len(repr(value).partition(".")[2]) for value in arrivals), default=0
                )
                max_decimals_seen = max(max_decimals_seen, case_decimals)
                # ① 首片归一化为 0.0（首包延迟属链路特性，不应留在时序里）
                first_zero_hits += int(bool(arrivals) and arrivals[0] == 0.0)
                # ② 到达时刻单调不减（正常流式响应逐片下发）
                is_monotonic = all(e <= later for e, later in pairwise(arrivals))
                monotonic_hits += int(is_monotonic)
                # ③ 全部时刻保留 3 位小数，浮点尾差已在落盘前抹掉
                precision_hits += int(
                    all(value == round(value, ARRIVAL_DECIMALS) for value in arrivals)
                )
                # ④ 落盘再加载后逐字段与期望一致（JSON 往返无损）
                roundtrip_hits += int(actual == scenario.expected_timing)
                # ⑤ chunk 增量拼接 == 响应体 content（录制的内容本身没串片）
                replayed_content = str(replayed.json()["choices"][0]["message"]["content"])
                concat_hits += int(replayed_content == scenario.streamed_content)
                timing_fields = {
                    "streaming": True,
                    "chunk_count": len(observed),
                    "first_arrival_ms": arrivals[0] if arrivals else None,
                    "last_arrival_ms": arrivals[-1] if arrivals else None,
                    "arrival_monotonic": is_monotonic,
                    "max_decimals": case_decimals,
                    "timing_round_trip_ok": actual == scenario.expected_timing,
                }

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
                    **timing_fields,
                }
            )

    total = len(scenarios)
    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "task": "M1-D06a/b VCR 录制回放一致率与时序录制基线",
        "scope": (
            "文本层面（请求指纹/响应体还原精度）+ 时序层面（chunk 到达时刻录制精度）；"
            "D6b 只录不播，回放调度偏差指标待 M1-D06c 补充"
        ),
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
        "timing_recording": {
            "streaming_scenarios": timing_scenarios,
            "non_streaming_scenarios": total - timing_scenarios,
            "total_chunks": total_chunks,
            "avg_chunk_chars": round(statistics.fmean(chunk_char_samples), 2),
            "min_chunk_chars": min(chunk_char_samples),
            "max_chunk_chars": max(chunk_char_samples),
            "first_chunk_zero_rate": round(first_zero_hits / timing_scenarios, 4),
            "arrival_monotonic_rate": round(monotonic_hits / timing_scenarios, 4),
            "three_decimal_precision_rate": round(precision_hits / timing_scenarios, 4),
            "max_decimals_observed": max_decimals_seen,
            "json_round_trip_rate": round(roundtrip_hits / timing_scenarios, 4),
            "chunks_concat_matches_content_rate": round(concat_hits / timing_scenarios, 4),
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
    timing_report = report["timing_recording"]
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
    print(
        f"  时序场景 {timing_report['streaming_scenarios']} 组 / 共录制"
        f" {timing_report['total_chunks']} 个 chunk / 平均每片"
        f" {timing_report['avg_chunk_chars']} 字"
    )
    print(
        f"  首片归零 {timing_report['first_chunk_zero_rate']:.2%}"
        f" / 单调递增 {timing_report['arrival_monotonic_rate']:.2%}"
        f" / 3 位小数 {timing_report['three_decimal_precision_rate']:.2%}"
        f"（实测最大小数位 {timing_report['max_decimals_observed']}）"
    )
    print(
        f"  timing JSON 往返一致 {timing_report['json_round_trip_rate']:.2%}"
        f" / 增量拼接对齐响应体 {timing_report['chunks_concat_matches_content_rate']:.2%}"
    )


if __name__ == "__main__":
    main()

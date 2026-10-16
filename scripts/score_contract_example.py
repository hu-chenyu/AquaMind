"""D19 评分输出契约示例生成：输出 Score / ScoreTrace 的示例 JSON（可复现）。

用途：
    按 M2-D01「数据契约 Score / ScoreTrace」的定义，生成三个典型场景的示例 JSON
    （通过 / 失败 / 边界），作为后续评分器（M2-D03 精确匹配、M2-D05 rubric、
    M2-D07 Judge）输出形态的样板核对物，也是 ``examples/score_contract/`` 演示
    所依据的同一份事实源。

运行（在仓库根目录）::

    .venv\\Scripts\\python.exe scripts/score_contract_example.py

输出：单个 JSON 对象（含 ``schema`` 版本号与三个场景）写到 stdout。同一条命令可反复
执行且输出逐字节一致——场景里的时间戳是**固定值**而非当前时钟：示例产物一旦随运行
变化，就无法被当作比对基线，"生成命令可复现"这句话也随之落空。

Windows 备注：本脚本输出 UTF-8 文本。若要重定向到文件，先执行
``$env:PYTHONIOENCODING="utf-8"``，否则默认代码页（cp936）会把中文写成另一种编码，
下游按 UTF-8 读取时解析失败。

本脚本不发起任何网络请求，不需要任何 API 密钥（CI 零 key）。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

from aquamind.models import Score, ScoreStep, ScoreTrace

# 产物 schema 版本：JSON 形态变更时递增，供下游按版本号分支解析
SCHEMA_VERSION = "score-contract.v1"

# 固定时刻（UTC）：见模块 docstring——示例产物必须与运行时间无关
FIXED_TIMESTAMP = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _build_pass_example() -> tuple[Score, ScoreTrace]:
    """构造「通过」场景：精确匹配命中，取分数上界 1.0。

    Returns:
        tuple[Score, ScoreTrace]: 该场景的评分结果与追溯链路（一步）。
    """
    step = ScoreStep(
        scorer="exact",
        input_digest="期望=4",
        output_digest="4",
        partial_score=1.0,
        reasoning="模型输出与期望值逐字相等",
    )
    score = Score(
        score=1.0,
        reason="模型输出与期望值逐字相等",
        scorer="exact",
        passed=True,
        matched_tags=["accuracy"],
    )
    trace = ScoreTrace(
        case_id="demo-exact-pass",
        steps=[step],
        final_score=1.0,
        scorer_version="exact-1.0.0",
        timestamp=FIXED_TIMESTAMP,
    )
    return score, trace


def _build_fail_example() -> tuple[Score, ScoreTrace]:
    """构造「失败」场景：两步追溯链（子串检查失败 → 裁判复核），取分数下界 0.0。

    两步是刻意保留的形态样板：M2-D07 的 Judge 适配器会在自动断言之后追加一次
    裁判复核，链上多一步时"最终分"仍必须等于链尾的中间分。

    Returns:
        tuple[Score, ScoreTrace]: 该场景的评分结果与追溯链路（两步）。
    """
    first = ScoreStep(
        scorer="contains",
        input_digest="期望子串=因为",
        output_digest="4（无因果说明）",
        partial_score=0.0,
        reasoning="期望子串未出现在模型输出中",
    )
    second = ScoreStep(
        scorer="judge-demo",
        input_digest="问题=为什么 2+2=4；输出=4",
        output_digest="判定=缺少因果说明",
        partial_score=0.0,
        reasoning="裁判复核确认缺少因果说明，维持 0 分",
    )
    score = Score(
        score=0.0,
        reason="期望子串未命中，且裁判复核确认缺少因果说明",
        scorer="judge-demo",
        passed=False,
        matched_tags=["accuracy", "reasoning"],
        metadata={"judge_samples": 1, "fallback_from": "contains"},
    )
    trace = ScoreTrace(
        case_id="demo-judge-fail",
        steps=[first, second],
        final_score=0.0,
        scorer_version="judge-demo-1.0.0",
        timestamp=FIXED_TIMESTAMP,
    )
    return score, trace


def _build_boundary_example() -> tuple[Score, ScoreTrace]:
    """构造「边界」场景：两步 rubric 链路，链尾 0.5 即最终分。

    这个场景演示两条边界：其一，分数是归一化后的中间值（既非全中也非全不中）；
    其二，链上出现"字段级打分 + 聚合"两步时，聚合结果必须显式登记为链尾步骤，
    否则 ``ScoreTrace`` 的"最终分等于链尾"不变式会当场失败。

    Returns:
        tuple[Score, ScoreTrace]: 该场景的评分结果与追溯链路（两步）。
    """
    field_step = ScoreStep(
        scorer="rubric",
        input_digest="字段=结论(权重 0.5)",
        output_digest="结论正确",
        partial_score=1.0,
        reasoning="结论字段与参考答案一致，该字段得满分",
    )
    aggregate_step = ScoreStep(
        scorer="rubric",
        input_digest="字段级得分={结论:1.0, 理由:0.0}，权重={结论:0.5, 理由:0.5}",
        output_digest="加权聚合=0.5",
        partial_score=0.5,
        reasoning="理由字段缺因果说明得 0 分，按权重聚合得 0.5",
    )
    score = Score(
        score=0.5,
        reason="结论字段正确、理由字段缺因果说明，加权聚合得 0.5",
        scorer="rubric",
        passed=False,
        matched_tags=["accuracy"],
        metadata={"missing_fields": ["reasoning"]},
    )
    trace = ScoreTrace(
        case_id="demo-rubric-boundary",
        steps=[field_step, aggregate_step],
        final_score=0.5,
        scorer_version="rubric-1.0.0",
        timestamp=FIXED_TIMESTAMP,
    )
    return score, trace


def build_examples() -> dict[str, dict[str, Any]]:
    """构造三个示例场景的 JSON 载荷（通过 / 失败 / 边界）。

    每个场景的载荷形如 ``{"score": {...}, "trace": {...}}``；
    构造过程本身就是一次契约校验（``Score`` / ``ScoreTrace`` 是严格模型），
    因此本函数能返回即意味着示例数据在契约层是合法的。

    Returns:
        dict[str, dict[str, Any]]: 场景名 → 该场景的 JSON 兼容载荷。

    Raises:
        AssertionError: 产物无法被契约回读（JSON 往返断链）时抛出——示例一旦
            不可回读，就失去了"供下游照抄的形态样板"这一唯一用途。
    """
    builders = {
        "pass": _build_pass_example,
        "fail": _build_fail_example,
        "boundary": _build_boundary_example,
    }
    examples: dict[str, dict[str, Any]] = {}
    for name, builder in builders.items():
        score, trace = builder()
        # mode="json"：把 datetime 等类型转成 JSON 兼容形态，直接可被 json.dumps 序列化
        payload = {"score": score.model_dump(mode="json"), "trace": trace.model_dump(mode="json")}
        # 自证：产物必须能被契约原样回读（分数契约的 JSON 往返无损），
        # 否则示例会误导下游——「照着它写」应当总是安全的
        assert Score.model_validate(payload["score"]) == score, f"{name} 场景的 score 无法回读"
        assert ScoreTrace.model_validate(payload["trace"]) == trace, f"{name} 场景的 trace 无法回读"
        examples[name] = payload
    return examples


def main() -> int:
    """把示例载荷写进 stdout（UTF-8 JSON，缩进 2，键有序）。

    ``sort_keys=True`` 让同一载荷无论字典书写顺序如何都产出同一串字节，
    这是"生成命令可复现"的执行层保证（另一半是固定的时间戳）。

    Returns:
        int: 恒为 0；示例生成本身没有失败路径（契约不合法会在构造期抛异常）。
    """
    payload = {"schema": SCHEMA_VERSION, "examples": build_examples()}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

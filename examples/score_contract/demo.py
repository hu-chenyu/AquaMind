"""AquaMind 评分契约最小示例（score_contract）。

本示例演示 D19 数据契约（Score / ScoreStep / ScoreTrace）的四件事：

1. **构造**：从一条 ``TestCase`` 出发，模拟一次两步评分（字段级打分 + 加权聚合），
  产出 ``Score`` 与 ``ScoreTrace``——「结论 + 证据」成对出现；
2. **校验**：坏数据在构造期就被拦住——分数越界、理由全空白、未知字段、追溯链终值
  与最终分不一致、时间戳缺时区，逐条演示异常定位到哪个字段；
3. **不可变**：评分结果构造后即为终态（``frozen=True``），赋值会被拒绝——
  否则「报告里的分数」与「追溯链里的分数」会无声地分叉；
4. **序列化**：``model_dump_json`` / ``model_validate_json`` 往返无损，
  评分产物可直接落盘当作证据，且回读后与原对象逐字段相等。

本示例不发起任何网络请求，不需要任何 API 密钥（CI 零 key）；时间戳是**固定值**，
因此每次运行的输出逐字相同——示例输出本身就可以当比对基线。

运行方式（在**仓库根目录**执行）::

  .venv\\Scripts\\python.exe examples/score_contract/demo.py
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from aquamind.models import Score, ScoreStep, ScoreTrace, TestCase

# 固定时刻（UTC）：示例输出必须与运行时间无关，否则"每次输出一致"无法成立
_FIXED_TS = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _build_case() -> TestCase:
  """构造一条演示用例（输入侧契约）。

  Returns:
    TestCase: 期望「4」的精确匹配用例，带两个评分标签。
  """
  return TestCase(
    input="2+2=?",
    expected={"type": "exact", "value": "4"},
    score_tags=[{"name": "accuracy", "weight": 1.0}, {"name": "reasoning", "weight": 0.5}],
  )


def _build_score_and_trace(case: TestCase) -> tuple[Score, ScoreTrace]:
  """模拟一次两步评分（字段级打分 → 加权聚合），产出结论与追溯链。

  Args:
    case: 待评分的用例（提供期望值与标签）。

  Returns:
    tuple[Score, ScoreTrace]: 评分结果与该次评分的追溯链路。
  """
  field_step = ScoreStep(
    scorer=case.expected.type,
    input_digest=f"期望={case.expected.value}",
    output_digest="4",
    partial_score=1.0,
    reasoning="结论字段与期望值逐字相等",
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
    reason="结论正确但缺因果说明，加权聚合得 0.5",
    scorer="rubric",
    passed=False,
    matched_tags=["accuracy"],
    metadata={"missing_fields": ["reasoning"]},
  )
  trace = ScoreTrace(
    case_id="demo-2plus2",
    steps=[field_step, aggregate_step],
    final_score=score.score,
    scorer_version="rubric-1.0.0",
    timestamp=_FIXED_TS,
  )
  return score, trace


def _demo_construct() -> tuple[Score, ScoreTrace]:
  """演示第一步：构造 Score / ScoreTrace，并打印序列化后的结论。

  Returns:
    tuple[Score, ScoreTrace]: 供后续小节复用的评分结果与追溯链。
  """
  print("=" * 68)
  print("1. 构造：一条用例 → 两步评分 → Score（结论）+ ScoreTrace（证据）")
  print("=" * 68)
  case = _build_case()
  score, trace = _build_score_and_trace(case)
  print(f" 用例        : input={case.input!r} expected={case.expected.value!r}")
  print(f" 评分结果      : score={score.score} passed={score.passed}")
  print(f" 命中标签      : {score.matched_tags}")
  print(f" 追溯链步骤数    : {len(trace.steps)}")
  print(f" 链尾中间分/最终分 : {trace.steps[-1].partial_score} / {trace.final_score}")
  print(" 说明：final_score 必须等于链尾的 partial_score——聚合动作本身也是链上一步，")
  print("    因此「分数怎么来的」在任何时候都能沿链路复算。")
  print(" score 的 JSON 表示：")
  print("  " + json.dumps(score.model_dump(mode="json"), ensure_ascii=False))
  return score, trace


def _demo_validation(trace: ScoreTrace) -> None:
  """演示第二步：坏数据在构造期被拦，异常定位到具体字段。

  Args:
    trace: 用于构造「坏链路」的合法追溯链（取其首步作为链尾）。
  """
  print()
  print("=" * 68)
  print("2. 校验：坏数据在构造期被拦住，错误定位到具体字段")
  print("=" * 68)
  bad_scores: list[tuple[str, dict[str, Any]]] = [
    ("分数越界 1.5", {"score": 1.5, "reason": "越界样例", "scorer": "exact", "passed": True}),
    ("理由全空白", {"score": 1.0, "reason": "  ", "scorer": "exact", "passed": True}),
    ("未知字段 scorrer", {"score": 1.0, "reason": "r", "scorrer": "exact", "passed": True}),
  ]
  for label, payload in bad_scores:
    try:
      Score(**payload)
    except ValidationError as exc:
      fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
      types = sorted({e["type"] for e in exc.errors()})
      print(f" {label:16s} -> 被拒，定位字段={fields}，错误类型={types}")

  # 追溯链终值与最终分不一致：报告结论不得与追溯证据打架
  try:
    ScoreTrace(
      case_id="demo-bad-chain",
      steps=[trace.steps[0]],
      final_score=0.9,
      scorer_version="rubric-1.0.0",
      timestamp=_FIXED_TS,
    )
  except ValidationError as exc:
    print(f" 最终分≠链尾   -> 被拒：{exc.errors()[0]['msg']}")

  # naive datetime：跨时区比对时会静默错位，必须在入口拦住。
  # 这里刻意构造无时区时间（它就是被拒场景的输入），故对 DTZ001 定向豁免
  naive_ts = datetime(2026, 10, 17, 9, 0) # noqa: DTZ001
  try:
    ScoreTrace(
      case_id="demo-naive-ts",
      steps=[trace.steps[0]],
      final_score=1.0,
      scorer_version="rubric-1.0.0",
      timestamp=naive_ts,
    )
  except ValidationError as exc:
    print(f" 时间戳缺时区   -> 被拒，定位字段={exc.errors()[0]['loc']}")


def _demo_immutability(score: Score) -> None:
  """演示第三步：评分结果不可变（frozen=True），构造后赋值被拒。

  Args:
    score: 已构造的评分结果。
  """
  print()
  print("=" * 68)
  print("3. 不可变：评分结果是终态产物，构造后赋值一律被拒")
  print("=" * 68)
  try:
    score.score = 0.9
  except ValidationError as exc:
    print(f" 改写 score.score -> 被拒，字段={exc.errors()[0]['loc']}，类型="
       f"{exc.errors()[0]['type']}")
    print(f" 原值未被改动   : score.score={score.score}")
  print(" 说明：如果评分结果可改，一次「顺手修正」就会让报告与追溯链分叉，")
  print("    而这类不一致没有任何报错。要改结论，就重新评分并产出新的 Score。")


def _demo_serialization(trace: ScoreTrace) -> None:
  """演示第四步：JSON 往返无损，产物可直接落盘当证据。

  Args:
    trace: 待往返的追溯链。
  """
  print()
  print("=" * 68)
  print("4. 序列化：model_dump_json → model_validate_json 往返无损")
  print("=" * 68)
  text = trace.model_dump_json()
  restored = ScoreTrace.model_validate_json(text)
  print(f" 往返前后相等   : {restored == trace}")
  print(f" 时间戳保留时区  : {restored.timestamp.isoformat()}")
  print(" ScoreTrace 的 JSON 表示（缩进 2）：")
  print(json.dumps(json.loads(text), ensure_ascii=False, indent=2))
  print()
  print("小结：Score 回答「多少分、为什么」，ScoreTrace 回答「怎么算出来的、用哪个版本算的」。")
  print("   两者一起，才让一个分数在事后仍然可复算、可追责。")


def main() -> None:
  """脚本入口：按顺序跑完全部演示段落（恒以退出码 0 结束）。"""
  score, trace = _demo_construct()
  _demo_validation(trace)
  _demo_immutability(score)
  _demo_serialization(trace)


if __name__ == "__main__":
  main()

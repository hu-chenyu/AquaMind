"""评分输出契约模型（Score / ScoreStep / ScoreTrace）单元测试。

覆盖范围（对应 D19 的验收口径）：
    1. 合法构造与默认值：Score（最小 / 完整）、ScoreStep、ScoreTrace（两步链路）；
    2. 分数边界与越界拦截：0.0 与 1.0 两端合法，<0、>1、NaN 逐条被拒并定位到字段；
    3. 非空与非空白校验：空字符串（min_length）与全空白（自定义校验器）各拦一次；
    4. 严格性与不可变：未知字段被拒（extra="forbid"）、构造后赋值被拒（frozen=True）；
    5. 追溯链自洽：步骤为空被拒、最终分与链尾不一致被拒、naive 时间戳被拒；
    6. JSON 往返无损：Score / ScoreTrace 的 model_dump_json → model_validate_json；
    7. 坏数据拦截的字段定位：缺必填字段、类型错误（异常消息定位到具体字段名）；
    8. 与输入侧契约衔接：TestCase（含 ScoreTag）→ 模拟评分 → Score/ScoreTrace，
       标签与分数沿链路可对应；
    9. 三个一产物：scripts/score_contract_example.py 按同一条命令可复跑，
       输出合法 JSON 且各场景产物与追溯链自洽（子进程真实 CLI 口径）。

全部用例为纯数据契约检查，不触真实 API（CI 零 key）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from aquamind.models import Score, ScoreStep, ScoreTag, ScoreTrace, TestCase

# 仓库根按测试文件位置反推，不依赖运行目录（pytest 可能从任意 CWD 启动）
_REPO_ROOT = Path(__file__).resolve().parents[1]
# 示例生成脚本：三个一产物之一，用子进程按真实 CLI 口径复跑
_EXAMPLE_SCRIPT = _REPO_ROOT / "scripts" / "score_contract_example.py"
# 固定时刻（UTC）：契约测试不依赖当前时间，断言可逐字复现
_FIXED_TS = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


def _score_payload(**overrides: Any) -> dict[str, Any]:
    """构造一份合法的 Score 字段载荷（用 overrides 定向破坏其中的某一项）。

    Args:
        **overrides: 需要覆盖的字段（如 ``score=-1.0``）。

    Returns:
        dict[str, Any]: 可直接展开给 ``Score(**payload)`` 的字典。
    """
    payload: dict[str, Any] = {
        "score": 1.0,
        "reason": "模型输出与期望值逐字相等",
        "scorer": "exact",
        "passed": True,
    }
    payload.update(overrides)
    return payload


def _step_payload(**overrides: Any) -> dict[str, Any]:
    """构造一份合法的 ScoreStep 字段载荷。

    Args:
        **overrides: 需要覆盖的字段（如 ``partial_score=1.5``）。

    Returns:
        dict[str, Any]: 可直接展开给 ``ScoreStep(**payload)`` 的字典。
    """
    payload: dict[str, Any] = {
        "scorer": "exact",
        "input_digest": "期望=4",
        "output_digest": "4",
        "partial_score": 1.0,
        "reasoning": "模型输出与期望值逐字相等",
    }
    payload.update(overrides)
    return payload


def _trace_payload(**overrides: Any) -> dict[str, Any]:
    """构造一份合法的 ScoreTrace 字段载荷（默认两步链路：字段级打分 + 加权聚合）。

    Args:
        **overrides: 需要覆盖的字段（如 ``steps=[]``）。

    Returns:
        dict[str, Any]: 可直接展开给 ``ScoreTrace(**payload)`` 的字典。
    """
    payload: dict[str, Any] = {
        "case_id": "case-2plus2",
        "steps": [
            _step_payload(scorer="rubric", partial_score=1.0, reasoning="结论字段与参考答案一致"),
            _step_payload(
                scorer="rubric",
                input_digest="字段级得分={结论:1.0, 理由:0.0}",
                output_digest="加权聚合=0.5",
                partial_score=0.5,
                reasoning="理由字段缺因果说明得 0 分，加权聚合得 0.5",
            ),
        ],
        "final_score": 0.5,
        "scorer_version": "rubric-1.0.0",
        "timestamp": _FIXED_TS,
    }
    payload.update(overrides)
    return payload


class TestScoreConstruction:
    """Score 的合法构造与可选字段默认值。"""

    def test_minimal_and_full_score_construct(self) -> None:
        """最小字段可构造且默认值正确；完整构造逐字段可读。"""
        minimal = Score(**_score_payload())
        assert minimal.score == 1.0
        assert minimal.reason == "模型输出与期望值逐字相等"
        assert minimal.scorer == "exact"
        assert minimal.passed is True
        # 可选字段默认值：标签为空列表、元数据为空字典
        assert minimal.matched_tags == []
        assert minimal.metadata == {}

        full = Score(
            **_score_payload(
                score=0.25,
                passed=False,
                matched_tags=["accuracy", "reasoning"],
                metadata={"judge_samples": 3},
            )
        )
        assert full.score == 0.25
        assert full.passed is False
        assert full.matched_tags == ["accuracy", "reasoning"]
        assert full.metadata == {"judge_samples": 3}

        # 默认值必须是每次新建的独立对象：共享同一个 list/dict 会让一条用例的写入串到另一条
        assert Score(**_score_payload()).matched_tags is not minimal.matched_tags


class TestScoreRange:
    """分数范围：两端合法、越界与 NaN 被拦。"""

    def test_range_bounds_accepted(self) -> None:
        """0.0 与 1.0 是归一化闭区间的两端，均合法。"""
        assert Score(**_score_payload(score=0.0)).score == 0.0
        assert Score(**_score_payload(score=1.0)).score == 1.0

    def test_below_range_rejected(self) -> None:
        """低于 0.0 被拒，错误定位到 score 字段。"""
        with pytest.raises(ValidationError) as exc_info:
            Score(**_score_payload(score=-0.01))
        # 元组集合断言：不依赖 pydantic 错误顺序，同时保住嵌套字段父路径
        assert ("score",) in {e["loc"] for e in exc_info.value.errors()}
        assert {e["type"] for e in exc_info.value.errors()} == {"greater_than_equal"}

    def test_above_range_rejected(self) -> None:
        """高于 1.0 被拒，错误定位到 score 字段。"""
        with pytest.raises(ValidationError) as exc_info:
            Score(**_score_payload(score=1.01))
        assert ("score",) in {e["loc"] for e in exc_info.value.errors()}
        assert {e["type"] for e in exc_info.value.errors()} == {"less_than_equal"}

    def test_nan_rejected(self) -> None:
        """NaN 必须被拒：它会让 ge/le 全部失效，且落盘时被写成 null、往返不再自洽。"""
        with pytest.raises(ValidationError) as exc_info:
            Score(**_score_payload(score=float("nan")))
        assert ("score",) in {e["loc"] for e in exc_info.value.errors()}
        # allow_inf_nan=False 先于区间约束生效，故错误类型是 finite_number 而非区间越界
        assert {e["type"] for e in exc_info.value.errors()} == {"finite_number"}

        # 同理 +inf 也被拒（否则 JSON 往返会得到 Infinity 字面量）
        with pytest.raises(ValidationError) as inf_exc:
            Score(**_score_payload(score=float("inf")))
        assert ("score",) in {e["loc"] for e in inf_exc.value.errors()}


class TestScoreStringValidation:
    """理由与标识的非空、非空白校验。"""

    def test_empty_or_blank_strings_rejected(self) -> None:
        """空串（min_length）与全空白（自定义校验器）都必须被拦，并定位到字段。"""
        with pytest.raises(ValidationError) as empty_exc:
            Score(**_score_payload(reason=""))
        assert ("reason",) in {e["loc"] for e in empty_exc.value.errors()}
        assert {e["type"] for e in empty_exc.value.errors()} == {"string_too_short"}

        # '   ' 能过 min_length，必须由自定义校验器拦下，且给出可读原因
        with pytest.raises(ValidationError) as blank_exc:
            Score(**_score_payload(reason="   "))
        assert ("reason",) in {e["loc"] for e in blank_exc.value.errors()}
        assert any("空白" in e["msg"] for e in blank_exc.value.errors())

        with pytest.raises(ValidationError) as scorer_exc:
            Score(**_score_payload(scorer=""))
        assert ("scorer",) in {e["loc"] for e in scorer_exc.value.errors()}


class TestScoreStrictness:
    """严格模式（extra="forbid"）与不可变（frozen=True）。"""

    def test_extra_field_forbidden(self) -> None:
        """未知字段（拼错的 reaosn）被拒：字段名拼写错误不得被静默丢弃。"""
        with pytest.raises(ValidationError) as exc_info:
            Score(**_score_payload(reaosn="拼错的字段名"))
        assert ("reaosn",) in {e["loc"] for e in exc_info.value.errors()}
        assert any(e["type"] == "extra_forbidden" for e in exc_info.value.errors())

    def test_frozen_assignment_rejected(self) -> None:
        """构造后赋值被拒：评分结果一旦生成即为终态，不得被下游就地改写。"""
        score = Score(**_score_payload())
        with pytest.raises(ValidationError) as exc_info:
            score.score = 0.5
        assert ("score",) in {e["loc"] for e in exc_info.value.errors()}
        assert any(e["type"] == "frozen_instance" for e in exc_info.value.errors())
        # 赋值被拒后原值不受影响（不是"改了一半才报错"）
        assert score.score == 1.0


class TestScoreSerialization:
    """Score 的 JSON 往返无损。"""

    def test_json_round_trip_is_lossless(self) -> None:
        """model_dump_json → model_validate_json 必须逐字段还原（含中文与元数据）。"""
        original = Score(
            **_score_payload(
                score=0.75,
                matched_tags=["accuracy"],
                metadata={"note": "中文元数据", "judge_samples": 2},
            )
        )
        restored = Score.model_validate_json(original.model_dump_json())
        assert restored == original
        # 再逐字段核一遍：防止将来某字段被加 exclude/alias 后"相等"掩盖了丢字段
        assert restored.model_dump() == original.model_dump()
        assert restored.model_dump_json() == original.model_dump_json()


class TestScoreStep:
    """ScoreStep 的构造与字段校验。"""

    def test_construct_and_field_validation(self) -> None:
        """合法构造逐字段可读；越界中间分与缺失必填字段都被拦并定位。"""
        step = ScoreStep(**_step_payload())
        assert step.scorer == "exact"
        assert step.input_digest == "期望=4"
        assert step.output_digest == "4"
        assert step.partial_score == 1.0
        assert step.reasoning == "模型输出与期望值逐字相等"

        with pytest.raises(ValidationError) as range_exc:
            ScoreStep(**_step_payload(partial_score=1.5))
        assert ("partial_score",) in {e["loc"] for e in range_exc.value.errors()}

        incomplete = _step_payload()
        del incomplete["reasoning"]
        with pytest.raises(ValidationError) as missing_exc:
            ScoreStep(**incomplete)
        assert ("reasoning",) in {e["loc"] for e in missing_exc.value.errors()}
        assert any(e["type"] == "missing" for e in missing_exc.value.errors())


class TestScoreTrace:
    """ScoreTrace 追溯链的构造、自洽约束与序列化。"""

    def test_construct_two_step_trace(self) -> None:
        """两步链路可构造：步骤被强制为该类型、最终分等于链尾、时间戳带时区。"""
        trace = ScoreTrace(**_trace_payload())
        assert trace.case_id == "case-2plus2"
        assert len(trace.steps) == 2
        # 传入的是 dict，构造后必须是 ScoreStep（嵌套契约同样走校验）
        assert all(isinstance(step, ScoreStep) for step in trace.steps)
        assert trace.final_score == trace.steps[-1].partial_score
        assert trace.scorer_version == "rubric-1.0.0"
        assert trace.timestamp.tzinfo is not None
        assert trace.timestamp == _FIXED_TS

    def test_empty_steps_rejected(self) -> None:
        """空链路被拒：没有步骤的追溯链无法解释分数从何而来。"""
        with pytest.raises(ValidationError) as exc_info:
            ScoreTrace(**_trace_payload(steps=[]))
        assert ("steps",) in {e["loc"] for e in exc_info.value.errors()}
        assert {e["type"] for e in exc_info.value.errors()} == {"too_short"}

    def test_naive_timestamp_rejected(self) -> None:
        """无时区时间戳被拒；带非 UTC 偏移的时区感知时间必须被接受（不强制 UTC）。"""
        # 用字符串给无时区时间，而不是构造 naive datetime：这正是真实入参形态
        # （裁判与落库侧给的时间戳本来就是字符串）。另一个原因是不必写规则豁免：
        # 本地 ruff 启用 DTZ001、CI 侧未启用，写下的定向豁免会在 CI 触发 RUF100
        # （未使用的豁免指令）而判红——本地绿、CI 红的经典分歧
        with pytest.raises(ValidationError) as exc_info:
            ScoreTrace(**_trace_payload(timestamp="2026-10-17T09:00:00"))
        assert ("timestamp",) in {e["loc"] for e in exc_info.value.errors()}
        assert {e["type"] for e in exc_info.value.errors()} == {"timezone_aware"}

        # 东八区时间：同一时刻的另一种合法表示，必须原样保留偏移
        offset_trace = ScoreTrace(
            **_trace_payload(timestamp=_FIXED_TS.astimezone(timezone(timedelta(hours=8))))
        )
        assert offset_trace.timestamp.utcoffset() == timedelta(hours=8)

    def test_final_score_must_match_last_step(self) -> None:
        """最终分与链尾不一致时被拒：报告结论不得与追溯证据打架。"""
        with pytest.raises(ValidationError) as exc_info:
            ScoreTrace(**_trace_payload(final_score=0.9))
        # 模型级校验器的错误 loc 为空元组，故断言消息内容：须含字段名与两个取值
        message = str(exc_info.value)
        assert "final_score" in message
        # 消息里同时给出两个取值：final_score=0.9 与链尾 steps[-1].partial_score=0.5
        assert "0.9" in message
        assert "0.5" in message

    def test_json_round_trip_is_lossless(self) -> None:
        """追溯链的 JSON 往返无损：步骤列表、时间戳与最终分全部原样还原。"""
        original = ScoreTrace(**_trace_payload())
        restored = ScoreTrace.model_validate_json(original.model_dump_json())
        assert restored == original
        assert restored.timestamp == original.timestamp
        assert restored.model_dump_json() == original.model_dump_json()


class TestBadDataInterception:
    """坏数据拦截：缺必填字段与类型错误都要定位到具体字段。"""

    def test_missing_field_and_wrong_type_rejected(self) -> None:
        """异常消息里必须能看到具体字段名，避免"某个字段错了"式排障。"""
        # 缺必填字段：Score 少 scorer
        missing_score = _score_payload()
        del missing_score["scorer"]
        with pytest.raises(ValidationError) as score_exc:
            Score(**missing_score)
        assert ("scorer",) in {e["loc"] for e in score_exc.value.errors()}
        assert any(e["type"] == "missing" for e in score_exc.value.errors())

        # 类型错误：passed 传字符串（bool 解析失败）
        with pytest.raises(ValidationError) as type_exc:
            Score(**_score_payload(passed="也许"))
        assert ("passed",) in {e["loc"] for e in type_exc.value.errors()}
        assert any(e["type"] == "bool_parsing" for e in type_exc.value.errors())

        # 缺必填字段：ScoreTrace 少 case_id
        missing_trace = _trace_payload()
        del missing_trace["case_id"]
        with pytest.raises(ValidationError) as trace_exc:
            ScoreTrace(**missing_trace)
        assert ("case_id",) in {e["loc"] for e in trace_exc.value.errors()}


class TestCaseScoreLink:
    """输出侧契约与输入侧契约的衔接。"""

    def test_case_to_score_and_trace_link_tags(self) -> None:
        """TestCase（含 ScoreTag）→ 模拟评分 → Score/ScoreTrace，标签与分数可对应。"""
        case = TestCase(
            input="2+2=?",
            expected={"type": "exact", "value": "4"},
            score_tags=[
                {"name": "accuracy", "weight": 1.0},
                {"name": "format", "weight": 0.5},
            ],
        )
        tag_names = [tag.name for tag in case.score_tags]
        assert tag_names == ["accuracy", "format"]  # 输入侧标签来自同一次构造

        # 模拟一次精确匹配评分：输出与期望逐字相等 → 1.0，命中全部标签
        step = ScoreStep(
            scorer=case.expected.type,
            input_digest=f"期望={case.expected.value}",
            output_digest="4",
            partial_score=1.0,
            reasoning="模型输出与期望值逐字相等",
        )
        score = Score(
            score=1.0,
            reason="模型输出与期望值逐字相等",
            scorer=case.expected.type,
            passed=True,
            matched_tags=tag_names,
        )
        trace = ScoreTrace(
            case_id="case-2plus2",
            steps=[step],
            final_score=score.score,
            scorer_version="exact-1.0.0",
            timestamp=_FIXED_TS,
        )

        # 命中标签与用例声明的标签集合一致（顺序无关，但集合必须相等）
        assert set(score.matched_tags) == {tag.name for tag in case.score_tags}
        # 分数沿链路一致：Score.score = 链尾中间分 = Trace.final_score
        assert trace.final_score == score.score == trace.steps[-1].partial_score
        # 评分器标识与用例的 ExpectedSpec.type 对齐：输入侧声明什么类型，输出侧就报什么评分器
        assert score.scorer == trace.steps[0].scorer == "exact"
        assert isinstance(case.score_tags[0], ScoreTag)


class TestExampleScript:
    """三个一产物：示例生成脚本可复跑且输出合法 JSON。"""

    def _run_script(self) -> subprocess.CompletedProcess[str]:
        """以子进程按 README 的固定命令运行示例脚本。

        Returns:
            subprocess.CompletedProcess[str]: 含 returncode/stdout/stderr 的进程结果。
        """
        # PYTHONIOENCODING 固定子进程输出为 UTF-8：Windows 管道默认走本地代码页（cp936），
        # 与父进程按 UTF-8 解码的约定不一致会直接炸在解码步骤上
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        return subprocess.run(
            [sys.executable, str(_EXAMPLE_SCRIPT)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=_REPO_ROOT,
            env=env,
            check=False,
        )

    def test_example_script_emits_valid_json_reproducibly(self) -> None:
        """退出码 0、stdout 是合法 JSON、三场景齐备，且连跑两次输出逐字节一致。"""
        first = self._run_script()
        assert first.returncode == 0, first.stderr

        payload = json.loads(first.stdout)
        assert payload["schema"] == "score-contract.v1"

        examples = payload["examples"]
        # 通过 / 失败 / 边界三场景齐备，且覆盖分数上界 1.0、下界 0.0 与中间值 0.5
        assert set(examples) == {"pass", "fail", "boundary"}
        assert {item["score"]["score"] for item in examples.values()} == {0.0, 0.5, 1.0}

        for name, item in examples.items():
            assert item["trace"]["steps"], f"{name} 场景的追溯链为空"
            # 产物自身满足追溯链不变式：最终分 = 链尾中间分 = Score.score
            assert item["trace"]["final_score"] == item["trace"]["steps"][-1]["partial_score"], name
            assert item["trace"]["final_score"] == item["score"]["score"], name
            # 时区感知落到 JSON 上必须是带偏移/UTC 标记的时间戳
            stamp = item["trace"]["timestamp"]
            assert stamp.endswith("Z") or "+" in stamp, name

        # 可复现：时间戳固定，同一条命令两次运行必须逐字节一致（否则无法作为比对基线）
        assert self._run_script().stdout == first.stdout

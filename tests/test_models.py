"""用例契约模型单元测试。

覆盖：合法最小/完整用例构造、必填字段缺失报错定位、未知字段（extra）拒绝、
可选字段默认值、weights 值非负、ScoreTag.weight 非负、tolerance 非负。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aquamind.models import ExpectedSpec, ScoreTag, TestCase


def _minimal_case_data() -> dict[str, object]:
    """构造合法最小用例的字段载荷（仅必填字段）。"""
    return {
        "input": "你好",
        "expected": {"type": "exact", "value": "你好"},
    }


class TestValidConstruction:
    """测试合法用例构造路径。"""

    def test_minimal_case_constructs(self) -> None:
        case = TestCase(**_minimal_case_data())
        assert case.input == "你好"
        assert case.expected.type == "exact"
        assert case.expected.value == "你好"

    def test_full_case_constructs(self) -> None:
        case = TestCase(
            input="1+1=?",
            context={"lang": "zh"},
            expected={"type": "contains", "value": "2", "tolerance": 0.1},
            score_tags=[{"name": "accuracy", "weight": 0.9}],
            weights={"accuracy": 0.8},
        )
        # 完整用例的所有字段应可访问且取值正确
        assert case.context == {"lang": "zh"}
        assert case.expected.tolerance == 0.1
        assert len(case.score_tags) == 1
        assert case.score_tags[0].name == "accuracy"
        assert case.score_tags[0].weight == 0.9
        assert case.weights["accuracy"] == 0.8


class TestRequiredFieldErrors:
    """测试必填字段缺失时 ValidationError 的字段定位。"""

    def test_missing_input_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TestCase(expected={"type": "exact", "value": "你好"})
        # 元组集合断言：不依赖 pydantic 错误顺序，同时保住嵌套字段父路径
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("input",) in error_locs

    def test_missing_expected_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TestCase(input="你好")
        # 元组集合断言：不依赖 pydantic 错误顺序，同时保住嵌套字段父路径
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("expected",) in error_locs

    def test_expected_missing_type_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TestCase(input="你好", expected={"value": "你好"})
        # 元组集合断言：不依赖 pydantic 错误顺序，同时保住嵌套字段父路径
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("expected", "type") in error_locs

    def test_expected_missing_value_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TestCase(input="你好", expected={"type": "exact"})
        # 元组集合断言：不依赖 pydantic 错误顺序，同时保住嵌套字段父路径
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("expected", "value") in error_locs


class TestDefaultValues:
    """测试可选字段的默认值。"""

    def test_context_defaults_to_empty_dict(self) -> None:
        case = TestCase(**_minimal_case_data())
        assert case.context == {}

    def test_score_tags_defaults_to_empty_list(self) -> None:
        case = TestCase(**_minimal_case_data())
        assert case.score_tags == []

    def test_weights_defaults_to_empty_dict(self) -> None:
        case = TestCase(**_minimal_case_data())
        assert case.weights == {}

    def test_expected_spec_tolerance_default(self) -> None:
        """ExpectedSpec 不传 tolerance 时默认应为 0.0。"""
        spec = ExpectedSpec(type="exact", value="hello")
        assert spec.tolerance == 0.0

    def test_score_tag_weight_default(self) -> None:
        """ScoreTag 不传 weight 时默认应为 1.0。"""
        tag = ScoreTag(name="accuracy")
        assert tag.weight == 1.0


class TestNonNegativeValidation:
    """测试三处非负校验：weights 值、ScoreTag.weight、tolerance。"""

    def test_negative_weight_value_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            TestCase(**_minimal_case_data(), weights={"accuracy": -0.5})
        # 元组集合断言：不依赖 pydantic 错误顺序
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("weights",) in error_locs
        # 自定义校验器的错误消息应含违规维度名：元组断言只覆盖字段定位，消息内容需单独验证
        assert any("accuracy" in e["msg"] for e in exc_info.value.errors())

    def test_score_tag_negative_weight_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ScoreTag(name="accuracy", weight=-1.0)
        # 元组集合断言：不依赖 pydantic 错误顺序
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("weight",) in error_locs

    def test_negative_tolerance_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            ExpectedSpec(type="exact", value="你好", tolerance=-0.1)
        # 元组集合断言：不依赖 pydantic 错误顺序
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("tolerance",) in error_locs


class TestExtraFieldsForbidden:
    """测试未知字段（extra）被严格拒绝，防止拼写错误被静默忽略。"""

    def test_extra_fields_forbidden(self) -> None:
        """TestCase 传入未知字段（拼写错误的 weigths）时应抛 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            TestCase(
                input="1+1=?",
                expected={"type": "exact", "value": "2"},
                weigths={"accuracy": 1.0},
            )
        # 元组集合断言：不依赖 pydantic 错误顺序，精确定位到拼写错误的字段名
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("weigths",) in error_locs
        # 错误类型须为 extra_forbidden：验证报错类型维度，与字段定位是不同维度
        assert any(e["type"] == "extra_forbidden" for e in exc_info.value.errors())

    def test_expected_spec_extra_forbidden(self) -> None:
        """ExpectedSpec 传入未知字段（拼写错误的 tol）时应抛 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            ExpectedSpec(type="exact", value="hello", tol=0.1)
        # 元组集合断言：精确定位到未知字段 tol，保证 ExpectedSpec 的 extra="forbid" 生效
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("tol",) in error_locs

    def test_score_tag_extra_forbidden(self) -> None:
        """ScoreTag 传入未知字段（拼写错误的 wight）时应抛 ValidationError。"""
        with pytest.raises(ValidationError) as exc_info:
            ScoreTag(name="accuracy", wight=0.5)
        # 元组集合断言：精确定位到未知字段 wight，保证 ScoreTag 的 extra="forbid" 生效
        error_locs = {e["loc"] for e in exc_info.value.errors()}
        assert ("wight",) in error_locs
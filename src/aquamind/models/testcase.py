"""用例契约模型：TestCase / ExpectedSpec / ScoreTag。

三个模型构成 AquaMind 全链路（加载器 → 批量执行器 → 评分器）的数据底座：
用例在进入系统内部的第一刻即完成校验，坏数据在边界处被拦截。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExpectedSpec(BaseModel):
    """期望输出规范：描述一条用例对模型输出的判定条件。

    Attributes:
        type: 期望类型（如 exact/regex/contains/judge），决定 value 的解释方式
        value: 期望值，具体类型由 type 决定
        tolerance: 数值容差，须非负；仅对数值型期望有意义
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略
    model_config = ConfigDict(extra="forbid")

    type: str = Field(description="期望类型，如 exact/regex/contains/judge")
    value: Any = Field(description="期望值，具体类型由 type 决定")
    tolerance: float = Field(default=0.0, ge=0.0, description="数值容差，须非负")


class ScoreTag(BaseModel):
    """评分标签：标记用例应命中的评分维度及其权重。

    Attributes:
        name: 标签名（如 accuracy/consistency）
        weight: 标签权重，须非负
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略
    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="标签名，如 accuracy/consistency")
    weight: float = Field(default=1.0, ge=0.0, description="标签权重，须非负")


class TestCase(BaseModel):
    """测试用例契约：一次评测的最小输入单元。

    Attributes:
        input: 被测输入（必填）
        context: 运行时上下文（可选，默认空 dict）
        expected: 期望输出规范（必填）
        score_tags: 应命中的评分标签列表（可选，默认空 list）
        weights: 各评分维度权重（可选，默认空 dict，值须非负）
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略
    model_config = ConfigDict(extra="forbid")

    # 告知 pytest：本类不是测试类（名称以 Test 开头，避免被误收集产生 PytestCollectionWarning）
    __test__ = False

    input: str = Field(description="被测输入（必填）")
    context: dict[str, Any] = Field(default_factory=dict, description="运行时上下文（可选）")
    expected: ExpectedSpec = Field(description="期望输出规范（必填）")
    score_tags: list[ScoreTag] = Field(default_factory=list, description="应命中的评分标签（可选）")
    weights: dict[str, float] = Field(default_factory=dict, description="各评分维度权重（可选，值须非负）")

    @field_validator("weights")
    @classmethod
    def _check_weights_non_negative(cls, v: dict[str, float]) -> dict[str, float]:
        """校验权重字典的所有值必须非负。

        Args:
            v: 待校验的权重字典。

        Returns:
            校验通过的权重字典（原样返回）。

        Raises:
            ValueError: 存在负权重时抛出，消息中包含违规的维度名。
        """
        negative_keys = [key for key, weight in v.items() if weight < 0]
        if negative_keys:
            raise ValueError(f"权重值不能为负: {negative_keys}")
        return v
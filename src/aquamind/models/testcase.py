"""用例契约模型：TestCase / ExpectedSpec / ScoreTag。

三个模型构成 AquaMind 全链路（加载器 → 批量执行器 → 评分器）的数据底座：
用例在进入系统内部的第一刻即完成校验，坏数据在边界处被拦截。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 期望类型的合法取值集合。用 Literal 而非裸 str：type 决定 value 的解释方式，
# 拼错（如 "excat"）若放过，会一路飘到 M2 打分器静默产出错误分数且沿途无任何报错。
# 与 config.Settings.log_level 用同一套 Literal 写法保持项目风格一致；
# M2 若需新增类型，只需在此处追加一个成员。
ExpectedType = Literal["exact", "regex", "contains", "judge"]


class ExpectedSpec(BaseModel):
    """期望输出规范：描述一条用例对模型输出的判定条件。

    Attributes:
        type: 期望类型（exact/regex/contains/judge），决定 value 的解释方式
        value: 期望值，具体类型由 type 决定
        tolerance: 数值容差，须非负；仅对数值型期望有意义
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
    # validate_assignment：让不变量在构造后的赋值路径上同样成立
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    type: ExpectedType = Field(description="期望类型，如 exact/regex/contains/judge")
    value: Any = Field(description="期望值，具体类型由 type 决定")
    tolerance: float = Field(default=0.0, ge=0.0, description="数值容差，须非负")


class ScoreTag(BaseModel):
    """评分标签：标记用例应命中的评分维度及其权重。

    Attributes:
        name: 标签名（如 accuracy/consistency）
        weight: 标签权重，须非负
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
    # validate_assignment：让非负约束在构造后的赋值路径上同样成立
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(description="标签名，如 accuracy/consistency")
    weight: float = Field(default=1.0, ge=0.0, description="标签权重，须非负")


class TestCase(BaseModel):
    """测试用例契约：一次评测的最小输入单元。

    Attributes:
        input: 被测输入（必填）
        context: 运行时上下文（可选，默认空 dict）
        expected: 期望输出规范（必填）
        score_tags: 应命中的评分标签列表（可选，默认空 list）
        weights: 各评分维度权重（可选，默认空 dict，值须非负且为有限值）
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
    # validate_assignment：让权重约束在构造后的赋值路径上同样成立
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    # 告知 pytest：本类不是测试类（名称以 Test 开头，避免被误收集产生 PytestCollectionWarning）
    __test__ = False

    input: str = Field(description="被测输入（必填）")
    context: dict[str, Any] = Field(default_factory=dict, description="运行时上下文（可选）")
    expected: ExpectedSpec = Field(description="期望输出规范（必填）")
    score_tags: list[ScoreTag] = Field(default_factory=list, description="应命中的评分标签（可选）")
    # 权重值额外要求有限（allow_inf_nan=False）：ge=0.0 拦得住负数与 NaN
    # （NaN 与任何数比较都是 False），但拦不住 +inf；更关键的是 NaN 一旦混进
    # 加权聚合会让 sum 变成 NaN，而 JSON 序列化会把它写成 null，
    # 导致「构造成功 → model_dump_json → model_validate_json」这条自洽往返断掉。
    # 用 Annotated 把约束下沉到 dict 的**值**类型上，声明式即可，无需在校验器里手写。
    weights: dict[str, Annotated[float, Field(allow_inf_nan=False)]] = Field(
        default_factory=dict, description="各评分维度权重（可选，值须为非负有限数）"
    )

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
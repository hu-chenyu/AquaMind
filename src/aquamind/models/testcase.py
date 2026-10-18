"""用例契约模型：TestCase / ExpectedSpec / ScoreTag。

三个模型构成 AquaMind 全链路（加载器 → 批量执行器 → 评分器）的数据底座：
用例在进入系统内部的第一刻即完成校验，坏数据在边界处被拦截。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 输入侧复用输出侧的非空字符串约束（score 只依赖 pydantic，不反向导入本模块，
# 故不存在循环导入）：空串/纯空白标签名在两侧应给出同一条拒绝理由。
from .score import NonBlankStr

# 期望类型的合法取值集合。用 Literal 而非裸 str：type 决定 value 的解释方式，
# 拼错（如 "excat"）若放过，会一路飘到 M2 打分器静默产出错误分数且沿途无任何报错。
# 与 config.Settings.log_level 用同一套 Literal 写法保持项目风格一致；
# M2 若需新增类型，只需在此处追加一个成员。
#
# 七类中 exact/regex/contains/excludes/keywords/json 由 scoring.py 的确定性匹配器
# 实现；judge 需真实模型调用（M2-D07），不属确定性评分器，故由 judge 适配器承接。
ExpectedType = Literal["exact", "regex", "contains", "judge", "excludes", "keywords", "json"]


class ExpectedSpec(BaseModel):
  """期望输出规范：描述一条用例对模型输出的判定条件。

  Attributes:
    type: 期望类型（exact/regex/contains/judge/excludes/keywords/json），
      决定 value 的解释方式
    value: 期望值，具体类型由 type 决定
    tolerance: 数值容差，须非负；仅对数值型期望有意义
  """

  # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
  # validate_assignment：让不变量在构造后的赋值路径上同样成立
  model_config = ConfigDict(extra="forbid", validate_assignment=True)

  type: ExpectedType = Field(
    description="期望类型：exact/regex/contains/judge/excludes/keywords/json"
  )
  value: Any = Field(description="期望值，具体类型由 type 决定")
  tolerance: float = Field(default=0.0, ge=0.0, description="数值容差，须非负")

  def model_copy(
    self,
    *,
    update: Mapping[str, Any] | None = None,
    deep: bool = False,
  ) -> Self:
    """复制实例；带 update 时重新走完整校验，防止绕过契约。

    pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**，于是
    ``spec.model_copy(update={"type": "excat"})`` 能产出非法期望类型、
    ``update={"tolerance": -1.0}`` 能产出负容差，全程无报错——坏用例会一路飘到
    评分器静默产出错误结论。故带 update 时改走 ``model_validate``；不带 update
    时保持官方语义（纯复制，不做无谓的重新校验）。

    Args:
      update: 要覆盖的字段（None 或空字典时按纯复制处理）。
      deep: 是否深拷贝嵌套对象（沿用官方语义）。

    Returns:
      Self: 副本；带 update 时是**重新校验过**的副本。

    Raises:
      ValidationError: update 后的字段违反契约（非法 type、负容差、未知字段等）时抛出。
    """
    copied = super().model_copy(update=update, deep=deep)
    if update:
      # 把 update 的原样取值并回副本字段值再走完整校验（Literal 枚举、ge=0.0 与
      # extra="forbid" 全部重跑）。不能只校验副本：model_copy 会先把「不属于本模型
      # 的键」静默丢掉，不并回的话拼错的字段名会被吞掉，与契约口径相悖
      candidate: dict[str, Any] = {**copied.__dict__, **update}
      return self.__class__.model_validate(candidate)
    return copied


class ScoreTag(BaseModel):
  """评分标签：标记用例应命中的评分维度及其权重。

  Attributes:
    name: 标签名（如 accuracy/consistency）
    weight: 标签权重，须非负
  """

  # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
  # validate_assignment：让非负约束在构造后的赋值路径上同样成立
  model_config = ConfigDict(extra="forbid", validate_assignment=True)

  # 复用输出侧 Score.matched_tags 的 NonBlankStr（min_length=1 + strip 后非空）：
  # 空串与纯空白串标签名既无法命中任何评分维度，也会让「按标签聚合」的键退化成
  # 空键，属于坏输入。输入输出两侧共用同一条约束，避免"看起来对齐、实则不等价"。
  name: NonBlankStr = Field(description="标签名，如 accuracy/consistency")
  weight: float = Field(default=1.0, ge=0.0, description="标签权重，须非负")

  def model_copy(
    self,
    *,
    update: Mapping[str, Any] | None = None,
    deep: bool = False,
  ) -> Self:
    """复制实例；带 update 时重新走完整校验，防止绕过契约。

    pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**，于是
    ``tag.model_copy(update={"name": "  ", "weight": -1.0})`` 能产出一个空白标签名
    与负权重，全程无报错——按标签聚合时这类键会退化成空键，且负权重会污染加权
    总分。故带 update 时改走 ``model_validate``；不带 update 时保持官方语义。

    Args:
      update: 要覆盖的字段（None 或空字典时按纯复制处理）。
      deep: 是否深拷贝嵌套对象（沿用官方语义）。

    Returns:
      Self: 副本；带 update 时是**重新校验过**的副本。

    Raises:
      ValidationError: update 后的字段违反契约（空白标签名、负权重、未知字段等）时抛出。
    """
    copied = super().model_copy(update=update, deep=deep)
    if update:
      # 把 update 的原样取值并回副本字段值再走完整校验：非空标签名（min_length +
      # AfterValidator）与 ge=0.0 全部重跑，与直接构造等价；并回也是为了让未知键
      # 落到 model_validate 的 extra="forbid" 上（model_copy 会静默丢弃未知键）
      candidate: dict[str, Any] = {**copied.__dict__, **update}
      return self.__class__.model_validate(candidate)
    return copied


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

  def model_copy(
    self,
    *,
    update: Mapping[str, Any] | None = None,
    deep: bool = False,
  ) -> Self:
    """复制实例；带 update 时重新走完整校验，防止绕过契约。

    pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**，于是
    ``case.model_copy(update={"weights": {"accuracy": -1.0}})`` 能产出一条负权重
    用例、``update={"expected": {"type": "excat"}}`` 能产出非法期望类型、拼错字段名
    还会被静默丢弃，全程无报错。本类是整条评测链路的输入起点，一旦被这样改写，
    后续执行与评分全部建立在坏数据上。故带 update 时改走 ``model_validate``；
    不带 update 时保持官方语义（纯复制，不做无谓的重新校验）。

    Args:
      update: 要覆盖的字段（None 或空字典时按纯复制处理）。
      deep: 是否深拷贝嵌套对象（沿用官方语义）。

    Returns:
      Self: 副本；带 update 时是**重新校验过**的副本。

    Raises:
      ValidationError: update 后的字段违反契约（负/NaN 权重、非法期望类型、
        未知字段等）时抛出。
    """
    copied = super().model_copy(update=update, deep=deep)
    if update:
      # 把 update 的原样取值并回副本字段值再走完整校验：嵌套的 ExpectedSpec /
      # ScoreTag 在此重新校验为契约模型，权重非负与 allow_inf_nan=False 一并重跑；
      # 并回也是为了让未知键落到 model_validate 的 extra="forbid" 上
      candidate: dict[str, Any] = {**copied.__dict__, **update}
      return self.__class__.model_validate(candidate)
    return copied
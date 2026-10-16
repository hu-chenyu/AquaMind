"""评分输出契约模型：Score / ScoreStep / ScoreTrace。

本模块是 ``models/testcase.py``（输入侧契约）的**输出侧对应物**：一条用例经评分器打分后，
产出 ``Score``（单条评分结果）与 ``ScoreTrace``（评分追溯链路）。两者是「结论 + 证据」的关系——
``ScoreTrace.steps`` 逐步记录评分器、输入/输出摘要与中间分，且
``ScoreTrace.final_score`` 恒等于追溯链最后一步的中间分，于是任何一个分数都能沿链路复算，
而不只是一个孤立的数字。

三条设计取向（完整论证见 docs/adr/0012-score-data-contract.md）：

1. **分数一律归一化到 0.0-1.0**：这是各评分器之间唯一的可比尺度（0-100、五级量表、
   Judge 的 1-5 分都先归一到这条尺度再入库）；
2. **产出即终态（frozen=True）**：评分结果生成后不应被就地改写（例如某处顺手
   ``score.score = 0.9``），否则「报告里的分数」与「追溯链里的分数」会不一致，
   而这类不一致没有任何报错、极难发现；
3. **严格模式（extra="forbid"）**：字段名拼错当场报错，不允许静默丢弃——与输入侧
   契约 ``ExpectedSpec`` / ``ScoreTag`` / ``TestCase`` 同一口径。

本模块**只定义契约、不含任何评分逻辑**：精确匹配（M2-D03）、结构化 rubric（M2-D05）、
Judge 适配器（M2-D07）各自实现打分，输出统一收敛为本模块的模型；多评分器聚合
（M2-D06/D10）的结果同样以本模块表达。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Self

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, model_validator


def _ensure_non_blank(value: str) -> str:
    """校验字符串去掉首尾空白后仍非空。

    防止「看起来有值」的空串混进契约：``'   '`` 能通过 ``min_length=1``，但一个
    全空白的评分理由或评分器标识没有任何信息量，等同于字段缺失。

    Args:
        value: 待校验的字符串。

    Returns:
        str: 原样返回入参（不做 strip，保留调用方原文，保证 JSON 往返逐字一致）。

    Raises:
        ValueError: 字符串仅由空白字符组成时抛出。
    """
    if not value.strip():
        raise ValueError("字段不能为空或仅含空白字符")
    return value


# 归一化分数：所有评分器的统一输出尺度，闭区间 [0.0, 1.0]。
# allow_inf_nan=False 与 testcase.py 的 weights 同口径：nan 与任何数比较都为 False
# （ge/le 拦不住），而 nan 一旦落盘会被 JSON 序列化成 null，往返回来不再是原值——
# 分数的 JSON 往返一旦不自洽，追溯链就失去证据价值。
ScoreValue = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]

# 非空字符串：min_length 拦空串，「仅含空白」由 _ensure_non_blank 兜底。
# 用 Annotated 而非逐字段写 field_validator，让这条约束在三个模型上声明式地保持一致。
NonBlankStr = Annotated[str, Field(min_length=1), AfterValidator(_ensure_non_blank)]


class ScoreStep(BaseModel):
    """评分追溯链路中的单一步骤：一次评分动作的完整留痕。

    Attributes:
        scorer: 本步骤使用的评分器标识（如 exact/regex/contains/judge-xxx）
        input_digest: 输入摘要（截断后的可读摘要，不落原始全文）
        output_digest: 输出摘要（同上，便于人工核对「评的是哪段输出」）
        partial_score: 本步骤产出的中间分，范围 0.0-1.0（与最终分同一尺度）
        reasoning: 本步骤的推理说明（人类可读的评分依据）
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
    # frozen：追溯链上的每一步都是既成事实，不允许事后逐字段改写
    model_config = ConfigDict(extra="forbid", frozen=True)

    scorer: NonBlankStr = Field(description="本步骤使用的评分器标识，如 exact/judge-gpt-4o-mini")
    input_digest: NonBlankStr = Field(description="输入摘要（截断后的可读文本）")
    output_digest: NonBlankStr = Field(description="输出摘要（截断后的可读文本）")
    partial_score: ScoreValue = Field(description="本步骤产出的中间分，0.0-1.0")
    reasoning: NonBlankStr = Field(description="本步骤的推理说明（人类可读）")

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """复制实例；带 update 时重新走完整校验，防止绕过契约。

        pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**：它把新值直接
        塞进副本，于是 ``step.model_copy(update={"partial_score": 5.0})`` 能产出一个
        越界中间分、``update={"reasoning": ""}`` 能产出空推理说明，全程无报错。
        契约的可信度不能依赖调用方自觉，故带 update 时改走 ``model_validate``；
        不带 update 时保持官方语义（纯复制，不做无谓的重新校验）。

        Args:
            update: 要覆盖的字段（None 或空字典时按纯复制处理）。
            deep: 是否深拷贝嵌套对象（沿用官方语义）。

        Returns:
            Self: 副本；带 update 时是**重新校验过**的副本。

        Raises:
            ValidationError: update 后的字段违反契约（越界、空值、未知字段等）时抛出。
        """
        copied = super().model_copy(update=update, deep=deep)
        if update:
            # 把 update 的原样取值并回副本字段值再走完整校验（field_validator /
            # AfterValidator / extra="forbid" 全部重跑，与直接构造等价）。
            # 为什么不能只校验副本：model_copy 会先把「不属于本模型的键」静默丢掉，
            # 不并回的话 extra="forbid" 就形同虚设（拼错的字段名被吞掉，与契约口径相悖）
            candidate: dict[str, Any] = {**copied.__dict__, **update}
            return self.__class__.model_validate(candidate)
        return copied


class ScoreTrace(BaseModel):
    """评分追溯链路：一条用例从输入到最终分的完整评分过程。

    不变式：``final_score`` 必须等于 ``steps`` 最后一步的 ``partial_score``。
    这条约束把「追溯链」和「最终结论」焊死——若未来出现多评分器聚合
    （M2-D06/D10），聚合动作本身应作为链上的一个显式步骤登记，其
    ``partial_score`` 即聚合结果，本条不变式依然成立。

    Attributes:
        case_id: 用例标识（用于把分数关联回具体用例）
        steps: 评分步骤序列，至少一步（空链无法解释分数从何而来）
        final_score: 最终分数，范围 0.0-1.0，必须等于最后一步的中间分
        scorer_version: 评分器版本，用于可复算（同输入 + 同版本应得同分）
        timestamp: 评分发生时刻，必须带时区（naive 时间在跨时区比对时会静默错位）
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
    # frozen：评分链路是既成事实，报告消费它时不应有人回填/篡改其中一步
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: NonBlankStr = Field(description="用例标识，用于把评分结果关联回具体用例")
    steps: list[ScoreStep] = Field(min_length=1, description="评分步骤序列（至少一步）")
    final_score: ScoreValue = Field(description="最终分数，0.0-1.0，等于最后一步的中间分")
    scorer_version: NonBlankStr = Field(description="评分器版本，用于可复算")
    # 不设默认值：时间戳必须由调用方显式给出，避免在测试与复算链里冒出
    # 「看似相等实则不同源」的隐式当前时间；AwareDatetime 直接拒绝 naive datetime
    timestamp: AwareDatetime = Field(description="评分发生时刻（须带时区）")

    @model_validator(mode="after")
    def _check_final_score_matches_last_step(self) -> ScoreTrace:
        """校验追溯链自洽：最终分必须等于最后一步的中间分。

        Returns:
            ScoreTrace: 校验通过的模型自身。

        Raises:
            ValueError: 最终分与追溯链终点不一致时抛出，消息中同时给出两个取值。
        """
        # 精确相等而非容差比较：最终分本就是链终点的原值，允许容差等于允许
        # 「报告结论」与「追溯证据」不一致——那正是本契约要防住的事
        last_step_score = self.steps[-1].partial_score
        if self.final_score != last_step_score:
            raise ValueError(
                "final_score 必须等于最后一步的 partial_score："
                f"final_score={self.final_score}, steps[-1].partial_score={last_step_score}"
            )
        return self

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """复制实例；带 update 时重新走完整校验，防止绕过追溯链不变式。

        pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**，因此
        ``trace.model_copy(update={"final_score": 0.9})`` 能造出一个
        ``final_score`` 与链尾 ``partial_score`` 不一致的 ``ScoreTrace``——决策四
        「把结论与证据焊死」这条核心不变式会被官方 API 一行撕开。故带 update 时
        改走 ``model_validate``（``model_validator`` 随之重跑）；不带 update 时
        保持官方语义。

        Args:
            update: 要覆盖的字段（None 或空字典时按纯复制处理）。
            deep: 是否深拷贝嵌套对象（沿用官方语义）。

        Returns:
            Self: 副本；带 update 时是**重新校验过**的副本。

        Raises:
            ValidationError: update 后的字段违反契约（含破坏追溯链不变式）时抛出。
        """
        copied = super().model_copy(update=update, deep=deep)
        if update:
            # 把 update 的原样取值并回副本字段值再走完整校验：步骤列表在此重新校验为
            # ScoreStep，追溯链不变式（final_score = 链尾中间分）在此重新成立；
            # 并回也是为了让未知键落到 model_validate 的 extra="forbid" 上
            candidate: dict[str, Any] = {**copied.__dict__, **update}
            return self.__class__.model_validate(candidate)
        return copied


class Score(BaseModel):
    """单条评分结果：一条用例经某个评分器评分后的结论产物。

    Attributes:
        score: 分数，范围 0.0-1.0（归一化，各评分器之间的唯一可比尺度）
        reason: 人类可读的评分依据（非空）
        scorer: 评分器标识（与 ExpectedSpec.type 对齐或扩展，如 judge-gpt-4o-mini）
        passed: 是否通过（由评分器按自身阈值判定，契约不强制它与 score 的数值绑定）
        matched_tags: 命中的评分标签名列表（对应 TestCase.score_tags 的 name）
        metadata: 可扩展元数据（评分器自定义附加信息，如 Judge 的采样次数）
    """

    # 严格模式：禁止未知字段，防止字段名拼写错误被静默忽略；
    # frozen：评分结果是终态产物，报告与追溯链读到的必须是同一份结论
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: ScoreValue = Field(description="分数，0.0-1.0（归一化）")
    reason: NonBlankStr = Field(description="人类可读的评分依据（非空）")
    # 评分器标识用裸 str 而非 Literal：M2-D11 的评分器注册机制是运行时注册，
    # 且 judge 类标识带模型名后缀（judge-gpt-4o-mini），Literal 枚举会把扩展
    # 权限锁死在契约层（与 ExpectedSpec.type 用 Literal 的分工不同——那里
    # type 决定 value 的解释方式，取值集合必须封闭）
    scorer: NonBlankStr = Field(description="评分器标识，如 exact/regex/judge-gpt-4o-mini")
    passed: bool = Field(description="是否通过（由评分器按自身口径的阈值判定）")
    matched_tags: list[NonBlankStr] = Field(
        default_factory=list, description="命中的评分标签名（对应 TestCase.score_tags 的 name）"
    )
    # metadata 是刻意保留的扩展口袋：评分器的个性化附加信息（采样次数、原始终端输出等）
    # 不该反过来污染本契约的固定字段；extra="forbid" 管的是「未知的顶层字段」，
    # 不与此处「已知字段承载自由内容」冲突
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="可扩展元数据（评分器自定义附加信息）"
    )

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> Self:
        """复制实例；带 update 时重新走完整校验，防止绕过契约。

        pydantic v2 的 ``model_copy(update=...)`` **默认不跑任何校验**，于是
        ``score.model_copy(update={"score": 5.0, "reason": ""})`` 能产出一个越界
        分数 + 空理由的 ``Score``，全程无报错——这正是决策三「产出即终态」要防的
        分叉入口。故带 update 时改走 ``model_validate``；不带 update 时保持官方
        语义（纯复制，不做无谓的重新校验）。

        Args:
            update: 要覆盖的字段（None 或空字典时按纯复制处理）。
            deep: 是否深拷贝嵌套对象（沿用官方语义）。

        Returns:
            Self: 副本；带 update 时是**重新校验过**的副本。

        Raises:
            ValidationError: update 后的字段违反契约（越界分数、空理由、未知字段等）时抛出。
        """
        copied = super().model_copy(update=update, deep=deep)
        if update:
            # 把 update 的原样取值并回副本字段值再走完整校验：分数范围、非空白理由、
            # extra="forbid" 全部重跑，与直接构造等价；并回也是为了让未知键落到
            # model_validate 的 extra="forbid" 上（model_copy 会静默丢弃未知键）
            candidate: dict[str, Any] = {**copied.__dict__, **update}
            return self.__class__.model_validate(candidate)
        return copied

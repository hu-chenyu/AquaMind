"""Judge 适配器：调用异族（不同厂商）模型作为裁判，对被测输出做结构化 JSON 评分。

本模块处在「评分器」层，与 ``scoring.py`` 的确定性匹配器并列，但性质不同：确定性匹配器
不调用任何模型（可复算、零 key），Judge 必须调用一次真实模型，因此它的结论**不保证可复算**
——只能靠输出结构约束、异族约束与契约校验把不确定性收在入口处（完整论证见
``docs/adr/0013-judge-adapter.md`` 与 ``docs/adr/0005-judge-structured-output.md``）。

三条硬约束：

1. **强制异族**：裁判模型与被测模型同族（同厂商前缀）时，构造期即抛 ``JudgeError``。
   同族自评会让「裁判」与被测方共享同一套训练偏好与失效模式，判定失去独立性——这是
   LLM-as-Judge 最常见的作废原因；
2. **固定 JSON schema**：裁判只能输出 ``{"passed": bool, "score": float, "reason": str}``，
   解析后由 ``JudgeOutput`` 做强校验（``extra="forbid"``、``score`` 闭区间 0.0-1.0、
   ``passed`` 严格布尔、拒绝数字型字符串）；
3. **不可判不等于不通过**：解析失败、字段缺失、类型不符、分数越界一律抛 ``JudgeError``
   （UNJUDGEABLE 口径）。把它压成 ``passed=False`` 会让通过率虚低，压成 ``True`` 会让它
   虚高——两者都会让报告上的数字「看起来正常但含义是错的」。

与适配器层的关系：``JudgeAdapter`` **不继承** ``BaseAdapter``——它不是通用后端适配器，
返回值是判定结论（``JudgeOutput``）而非 ``AdapterResponse``；内部复用已配置好的
``BaseAdapter``（生产环境是 ``OpenAIAdapter``，测试与 CI 中是 ``CallableAdapter``）完成
HTTP 调用，不重复造轮子。裁判 API 的 429 / 超时 / 网络故障由 ``OpenAIAdapter`` 包装为
``AdapterError``，本模块**不重试**，只在边界处转成 ``JudgeError`` 并保留原始异常链
（``__cause__``）：这一次判定拿不到结论，属「不可判」而非「被测应用退化」。

异常放置说明：``JudgeError`` 有意定义在本模块内而非 ``exceptions.py``，原因是紧邻唯一使用处
（``JudgeAdapter`` 的构造期与 ``aevaluate`` 的四处 raise），且 ``JudgeError`` 不被其他模块
import（测试从 ``aquamind.judge`` 导入是测试侧惯例）。``exceptions.py`` 的统一体系覆盖的是
**跨模块共享**异常（Config / Loader / Adapter / Replay / Budget），``JudgeError`` 属模块内
私有异常，故不破坏该体系「统一基类 + 分层子类」的宣称；若将来出现第二个模块需要 catch
``JudgeError``，再迁入 ``exceptions.py`` 并改 import（登记见 ADR 0013 边界八）。
"""

from __future__ import annotations

import json
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from .adapters.base import BaseAdapter
from .exceptions import AdapterError, AquaMindError

# 模型族前缀：命中同一前缀即视为同族（同厂商），用于强制异族校验。
# 这是一个可扩展的**数据**而不是散落在 if 里的判断，新增厂商只需在此追加一行。
_MODEL_FAMILY_PREFIXES: list[str] = [
  "gpt-",
  "claude-",
  "qwen-",
  "deepseek-",
  "llama-",
  "mistral-",
  "gemini-",
  "grok-",
]

# 裁判 system prompt：把输出结构写成不可协商的契约。schema 说明必须与下方
# JudgeOutput 的字段逐字对齐，否则裁判按 prompt 输出却过不了模型校验。
_JUDGE_SYSTEM_PROMPT = (
  "你是格式严格的质量裁判。给定一条用例描述与被测输出，判断被测输出是否满足用例要求。\n"
  "只输出一个 JSON 对象：不要输出解释、前后缀文字，也不要使用 Markdown 代码块。\n"
  "该 JSON 必须且只能包含三个字段：\n"
  '- "passed"：布尔值，true 表示满足用例要求，false 表示不满足；\n'
  '- "score"：0.0 到 1.0 之间的数字（可以是小数），0.0 表示完全不满足，1.0 表示完全满足；\n'
  '- "reason"：中文字符串，说明判定理由，必须能独立解释上面的分数。\n'
  "字段缺失、类型不符或 score 越界都会被判为不可判，不要尝试用别的字段表达结论。"
)

# 从前缀文本中提取首个 JSON 对象用的复用解码器。
# 用 raw_decode 而不是「首个 { 到最后一个 }」的切片：后者在输出里出现第二个花括号
# （例如理由文本中提到 {说明}）或字符串内含花括号时会切出错位的片段，raw_decode 按
# JSON 语法本身推进，天然处理嵌套与字符串转义。
_JSON_DECODER = json.JSONDecoder()

# 解析失败的哨兵：``"null"`` 是合法 JSON（解析结果为 None），不能用 None 表示失败，
# 否则「输出为 null」会被误判成「需要从前缀文本里提取」
_PARSE_FAILED = object()


class JudgeError(AquaMindError):
  """Judge 适配器错误：异族拒绝、裁判输出不可解析、JSON 契约不符。

  沿用 ``AquaMindError`` 的约定——message 面向人读，context 带结构化归因，且
  ``str()`` / ``repr()`` 只渲染 context 的键名，不渲染取值（裁判输出可能含用例原文）。

  触发场景：
    - 构造期：``sut_model`` / ``judge_model`` 为空，或两者同族；
    - 调用期：裁判 API 失败（原始 ``AdapterError`` 保留在 ``__cause__``）、输出不是
      合法 JSON、JSON 顶层不是对象、缺字段 / 类型错 / score 越界 / reason 为空白。
  """


class JudgeOutput(BaseModel):
  """裁判的结构化判定结论：通过与否 + 归一化分数 + 中文理由。

  字段契约固定为三个，``extra="forbid"`` 让裁判多给的字段当场报错，而不是被静默丢弃
  ——静默丢弃会让「prompt 里要求的 schema」与「实际被接受的结构」悄悄分叉。

  Attributes:
    passed: 布尔判定结论（严格布尔，``1`` / ``"true"`` 不被接受）。
    score: 归一化分数，闭区间 [0.0, 1.0]，拒绝 NaN / inf。
    reason: 中文判定理由，非空且 strip 后仍非空（空证据不承载任何复核信息）。
  """

  model_config = ConfigDict(extra="forbid")

  passed: StrictBool = Field(description="布尔判定结论，严格布尔")
  score: float = Field(
    ge=0.0,
    le=1.0,
    allow_inf_nan=False,
    description="归一化分数，闭区间 0.0-1.0",
  )
  reason: str = Field(min_length=1, description="中文判定理由")

  @field_validator("score", mode="before")
  @classmethod
  def _require_numeric_score(cls, value: object) -> object:
    """拦下「看起来像数字」的非数字取值（``"0.8"``、``true``）后再交给 pydantic。

    pydantic 的 lax 模式会把数字型字符串 ``"0.8"`` 与布尔 ``true`` 静默转成浮点，
    于是「类型错误」这一类契约违约会变成「合法分数」——裁判实际违反 schema 却被计入
    通过率，正是本模块要防的无声错误。pydantic v2 的字段校验器必须抛 ``ValueError``
    才会被收成 ``ValidationError``（抛 ``TypeError`` 会直接穿透校验层），故此处用
    守卫子句的写法而非在 ``isinstance`` 分支里 raise。

    Args:
      value: 原始字段取值（任意类型）。

    Returns:
      object: 原样返回 int / float 取值，交由 pydantic 做区间校验。

    Raises:
      ValueError: 取值既不是 int 也不是 float，或是布尔时抛出。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
      return value
    raise ValueError(f"score 必须是数字（int 或 float），实际类型: {type(value).__name__}")

  @field_validator("reason", mode="after")
  @classmethod
  def _reject_blank_reason(cls, value: str) -> str:
    """拒绝空串与「全空白」的理由。

    ``min_length=1`` 只拦空串：``"   "`` 长度足够却没有任何复核价值——一个全空白的理由
    与字段缺失等价，都让分数无法被人工核对（ADR 0013 决策三把 reason 定位为「证据」）。
    因此这里再拦一次 strip 后为空，口径与输入契约的 ``ScoreTag.name``（``NonBlankStr``）
    一致：既有长度约束，也要求剥掉空白后仍有内容。

    Args:
      value: 已通过长度校验的理由字符串。

    Returns:
      str: 原样返回（不做 strip，保留调用方原文，保证 JSON 往返逐字一致）。

    Raises:
      ValueError: 字符串仅由空白字符组成时抛出。
    """
    if not value.strip():
      raise ValueError("reason 不能为空白字符串")
    return value


def _model_family(model: str) -> str | None:
  """返回模型名命中的族前缀；未命中任何已知前缀时返回 None。

  判定只看**前缀**（同厂商的模型名前缀一致），并以最后一段为准：兼容端点常写成
  ``openai/gpt-4o``、``meta-llama/llama-3`` 这类「厂商/模型」形态，若整串做前缀匹配，
  带厂商前缀的同族模型会被误判成异族，强制异族校验当场失效。

  Args:
    model: 模型名（大小写与首尾空白不敏感）。

  Returns:
    str | None: 命中的族前缀（小写）；未命中时为 None。
  """
  segment = model.strip().lower().rsplit("/", 1)[-1]
  for prefix in _MODEL_FAMILY_PREFIXES:
    if segment.startswith(prefix):
      return prefix
  return None


def _is_same_family(sut_model: str, judge_model: str) -> bool:
  """判定被测模型与裁判模型是否同族（同厂商）。

  规则：
    - 两者都命中已知前缀（且前缀不同）→ 异族；前缀相同 → 同族；
    - 只有一方命中已知前缀 → 族不同，视为异族（无法证明同源，按异族放行）；
    - 两者都不命中已知前缀 → 按完整字符串比较：完全相同视为同族，不同视为异族。
      这是**保守策略**：未知厂商无法证明其独立性，同名字段按同族拒，宁拒勿漏。

  Args:
    sut_model: 被测模型名。
    judge_model: 裁判模型名。

  Returns:
    bool: 同族返回 True（调用方应拒绝构造），异族返回 False。
  """
  sut_family = _model_family(sut_model)
  judge_family = _model_family(judge_model)
  if sut_family is None and judge_family is None:
    return sut_model.strip().lower() == judge_model.strip().lower()
  return sut_family == judge_family


def _parse_judge_json(content: str) -> dict[str, Any]:
  """把裁判输出解析成 JSON 对象，允许前后缀文本。

  两段式：先按整串解析（裁判守规矩时的快路径）；失败则定位首个 ``{`` 并用
  ``raw_decode`` 从该处解出一个完整的 JSON 对象——裁判常在 JSON 前后加一句
  「判定如下：」，这属可容忍的格式噪音，但**结论本身仍必须是结构化 JSON**，
  解析不出来的部分绝不退回自由文本猜测。

  Args:
    content: 裁判模型返回的原始文本。

  Returns:
    dict[str, Any]: 解析出的 JSON 对象。

  Raises:
    JudgeError: 整串与提取段都解析失败、输出中不存在 ``{``、或 JSON 顶层不是对象时抛出。
  """
  text = content.strip()
  # 哨兵而非 None：``"null"`` 是合法 JSON，解析结果为 None，不能与「解析失败」混淆
  parsed: Any = _PARSE_FAILED
  try:
    parsed = json.loads(text)
  except (json.JSONDecodeError, RecursionError):
    # RecursionError：深嵌套输出会击穿 json 的递归下降。它继承 RuntimeError 而非
    # ValueError，不在此显式收口就会以裸异常穿透「调用方只需 except JudgeError」的契约。
    parsed = _PARSE_FAILED
  if parsed is _PARSE_FAILED:
    start = text.find("{")
    if start == -1:
      raise JudgeError(
        message="裁判输出不是合法 JSON：未找到 { 起始的 JSON 对象",
        context={"adapter": "judge", "stage": "parse", "content_length": len(text)},
      )
    try:
      parsed = _JSON_DECODER.raw_decode(text, start)[0]
    except (json.JSONDecodeError, RecursionError) as exc:
      raise JudgeError(
        message=f"裁判输出不是合法 JSON：提取段解析失败（{type(exc).__name__}）",
        context={
          "adapter": "judge",
          "stage": "parse",
          "error_type": type(exc).__name__,
          "content_length": len(text),
        },
      ) from exc
  if not isinstance(parsed, dict):
    raise JudgeError(
      message=f"裁判输出 JSON 顶层不是对象，实际类型: {type(parsed).__name__}",
      context={"adapter": "judge", "stage": "parse", "actual_type": type(parsed).__name__},
    )
  return cast("dict[str, Any]", parsed)


class JudgeAdapter:
  """裁判适配器：异族校验 + prompt 构造 + JSON 解析 + 契约校验。

  生命周期只有两段：构造期做静态校验（模型名非空、强制异族），调用期做一次裁判调用
  与结论校验。**不做重试、不做并发调用、不写库**——这些都属后续任务的范围
  （降级与 κ 标定见 M2-D08），本类的职责边界在此收住，见 ADR 0013 的【边界】。

  Attributes:
    sut_model: 被测模型名（构造期与裁判模型做异族校验）。
    judge_model: 裁判模型名。
  """

  def __init__(self, judge_adapter: BaseAdapter, sut_model: str, judge_model: str) -> None:
    """初始化裁判适配器并在构造期完成异族校验。

    Args:
      judge_adapter: 已配置好的裁判后端适配器（生产用 ``OpenAIAdapter``，
        测试与 CI 用 ``CallableAdapter``）。本类只依赖其 ``acomplete`` 契约。
      sut_model: 被测模型名，如 ``gpt-4o``。
      judge_model: 裁判模型名，如 ``claude-3-opus``；必须与被测模型异族。

    Raises:
      JudgeError: 任一模型名为空/全空白，或两者同族时抛出。构造期即失败，
        避免「同族自评」这种作废的测量方式跑完一整轮负载才发现。
    """
    if not sut_model.strip() or not judge_model.strip():
      raise JudgeError(
        message="JudgeAdapter 的 sut_model 与 judge_model 都必须为非空字符串",
        context={
          "adapter": "judge",
          "sut_model": sut_model,
          "judge_model": judge_model,
          "stage": "construct",
        },
      )
    if _is_same_family(sut_model, judge_model):
      raise JudgeError(
        message=(
          "裁判模型与被测模型同族，强制异族校验失败："
          f"sut_model={sut_model} judge_model={judge_model} "
          f"（家族={_model_family(judge_model) or '未知前缀'}）"
        ),
        context={
          "adapter": "judge",
          "sut_model": sut_model,
          "judge_model": judge_model,
          "sut_family": _model_family(sut_model),
          "judge_family": _model_family(judge_model),
          "stage": "construct",
        },
      )
    self._judge_adapter = judge_adapter
    self.sut_model = sut_model
    self.judge_model = judge_model

  def _build_prompt(self, case_description: str, sut_output: str) -> list[dict[str, str]]:
    """构造裁判 prompt：system 说明角色与 JSON schema，user 给出待判内容。

    两段分离而不是拼成一条：schema 约束属于「角色指令」，待判内容属于「数据」。
    混在同一条消息里时，被测输出中的指令性文本更容易被裁判当成新的任务（注入面更大）。

    Args:
      case_description: 用例描述（要让裁判知道「满足什么算通过」）。
      sut_output: 被测应用的实际输出。

    Returns:
      list[dict[str, str]]: OpenAI 格式的两条消息（system + user）。
    """
    user_content = (
      "【用例描述】\n"
      f"{case_description}\n\n"
      "【被测输出】\n"
      f"{sut_output}\n\n"
      "请按 system 指定的 JSON schema 输出判定结果，只输出 JSON。"
    )
    return [
      {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
      {"role": "user", "content": user_content},
    ]

  async def aevaluate(self, case_description: str, sut_output: str) -> JudgeOutput:
    """调用裁判模型对一条输出做判定，返回结构化结论。

    流程：构造 prompt → ``judge_adapter.acomplete`` → 解析 JSON → ``JudgeOutput`` 校验。
    任一环节失败都抛 ``JudgeError``，**不返回**任何默认结论：不可判必须有独立出口，
    否则它会以「通过」或「不通过」的身份混进通过率。

    Args:
      case_description: 用例描述。
      sut_output: 被测应用的实际输出。

    Returns:
      JudgeOutput: 经契约校验的判定结论。

    Raises:
      JudgeError: 裁判调用失败（原始 ``AdapterError`` 在 ``__cause__`` 中）、输出不是
        合法 JSON、JSON 顶层不是对象、字段缺失 / 类型不符 / score 越界时抛出。
    """
    messages = self._build_prompt(case_description, sut_output)
    try:
      response = await self._judge_adapter.acomplete(messages)
    except AdapterError as exc:
      # 传输层故障（429 / 超时 / 网络）不重试：本次判定拿不到结论，属不可判。
      # 原始异常保留在 __cause__ 中，归因（HTTP 状态码等）不丢失。
      raise JudgeError(
        message=f"裁判模型调用失败，本次判定不可判: {type(exc).__name__}",
        context={
          "adapter": "judge",
          "stage": "complete",
          "error_type": type(exc).__name__,
          "judge_model": self.judge_model,
        },
      ) from exc
    payload = _parse_judge_json(response.content)
    try:
      return JudgeOutput.model_validate(payload)
    except ValidationError as exc:
      # 消息里必须点名字段：契约失败时「哪个字段不合法」是唯一有用的信息，
      # 只报「校验失败」会让排障回到逐条猜 schema 的起点
      fields = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
      raise JudgeError(
        message=f"裁判输出未通过 JSON 契约校验，不合法字段: {'、'.join(fields)}",
        context={
          "adapter": "judge",
          "stage": "contract",
          "fields": fields,
          "error_types": [str(err["type"]) for err in exc.errors()],
          "payload_keys": sorted(payload),
        },
      ) from exc

"""Judge 适配器单元测试（M2-D25）。

覆盖范围：
- 强制异族校验：同族构造期被拒（含大小写/厂商前缀分段形态与「完全相同字符串」），
  异族构造成功；两方都不命中已知前缀时按完整字符串比较（相同=同族、不同=异族）；
- 构造期空模型名拦截；
- 结构化 JSON：合法输出返回 ``JudgeOutput``；带前后缀文本（含 json 代码块围栏）时提取
  首个 ``{`` 起的 JSON 对象；``raw_decode`` 支持字符串内含花括号与嵌套对象；
- 契约校验：缺字段（passed / score / reason 各一）、类型错（passed 非严格布尔、
  score 为数字型字符串、score 为布尔、reason 非字符串）、score 越界（下界以下 / 上界以上 /
  NaN / Infinity 这类非有限值）、reason 为空白、JSON 顶层不是对象，全部抛 ``JudgeError``
  且消息里点名具体字段；
- 坏 JSON：完全不是 JSON、提取段解析失败、深嵌套触发 ``RecursionError``；
- 裁判后端故障：``AdapterError`` 在 Judge 边界转成 ``JudgeError``，原始异常保留在
  ``__cause__``（本次判定不可判，而非被测应用退化）；
- prompt 构造：system 说明 JSON schema、user 携带用例描述与被测输出，两段分离；
- 异常契约：``JudgeError`` 继承 ``AquaMindError`` 并携带 context（``str()`` 只渲染键名）；
- 降级口径（D08）：``aevaluate_safe``（实例方法与模块级自由函数）成功返回
  ``JudgeOutput``，失败返回 ``Unjudgeable`` 并按 ``stage``（complete / parse / contract）
  与失败字段归因，且不把裁判原文带进结果对象；
- 解析可追溯性（D08）：parse 阶段的 ``JudgeError.context`` 带 ``content_head`` /
  ``content_tail`` 截断片段（各 200 字符，短输出保留全文），正文不含原文；200 / 250 /
  400 三个长度边界各钉一次（全量 / 重叠 / 相接）；
- 归因不可双降解（D08 热修）：model 级契约错误（``loc=()``）时 ``fields`` 为空但
  ``error_type`` 顶替承载归因、消息不悬挂冒号；模块外把 ``fields`` 写成字符串时按
  「无字段」处理，不逐字符拆。

全部用例用 ``CallableAdapter`` 注入裁判响应，不发起任何网络调用（CI 零 key），
也不读取任何真实密钥或环境变量。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import ValidationError

from aquamind.adapters import CallableAdapter
from aquamind.exceptions import AdapterError, AquaMindError
from aquamind.judge import (
  JudgeAdapter,
  JudgeError,
  JudgeOutput,
  Unjudgeable,
  _is_same_family,
  aevaluate_safe,
)

# 合法裁判输出：三字段齐全、类型正确、分数落在闭区间内
_VALID_JSON = '{"passed": true, "score": 0.9, "reason": "答案正确且给出了推导过程"}'

# 同族模型对：应全部在构造期被拒。既含同前缀的不同模型，也含完全相同的字符串，
# 以及「厂商/模型」分段形态——后者若不做分段归一，同族会被误判成异族
_SAME_FAMILY_PAIRS = [
  ("gpt-4o", "gpt-4o-mini"),
  ("claude-3-opus", "claude-3-sonnet"),
  ("deepseek-chat", "deepseek-reasoner"),
  ("qwen-max", "qwen-plus"),
  ("gpt-4o", "openai/gpt-4o-mini"),
  ("acme-internal-7b", "acme-internal-7b"),
]

# 异族模型对：应全部构造成功。末两组覆盖「一方命中前缀、另一方未知」与
# 「两方都未知但字符串不同」这两种放行路径
_DIFFERENT_FAMILY_PAIRS = [
  ("gpt-4o", "claude-3-opus"),
  ("qwen-max", "deepseek-chat"),
  ("gpt-4o", "acme-unknown-7b"),
  ("acme-alpha", "acme-beta"),
  ("openai/gpt-4o", "anthropic/claude-3-opus"),
]


def _raise_adapter_error(messages: list[dict[str, str]]) -> str:
  """模拟裁判后端故障：抛异常后由 ``CallableAdapter`` 包装成 ``AdapterError``。

  Args:
    messages: OpenAI 格式消息列表（本函数不使用，仅为满足可调用对象签名）。

  Returns:
    str: 正常不会返回（异常路径专用）。

  Raises:
    RuntimeError: 恒定抛出，用于验证传输层故障在 Judge 边界的转换。
  """
  raise RuntimeError("模拟裁判端点不可达")


@pytest.fixture
def mock_judge_adapter() -> Callable[[str], JudgeAdapter]:
  """返回一个工厂：给定裁判输出文本，产出已通过异族校验的 ``JudgeAdapter``。

  所有用例共用该工厂注入裁判响应——``CallableAdapter`` 只做本地函数调用，
  因此整套测试零 key、零网络。

  Returns:
    Callable[[str], JudgeAdapter]: 参数为裁判输出文本的构造工厂。
  """

  def factory(content: str) -> JudgeAdapter:
    return JudgeAdapter(
      CallableAdapter(lambda messages: content),
      sut_model="gpt-4o",
      judge_model="claude-3-opus",
    )

  return factory


class TestCrossFamilyGuard:
  """强制异族校验：同族拒绝、异族放行、未知前缀按完整字符串比较。"""

  @pytest.mark.parametrize(("sut_model", "judge_model"), _SAME_FAMILY_PAIRS)
  def test_same_family_rejected_at_construction(
    self, sut_model: str, judge_model: str
  ) -> None:
    """同族模型对必须在构造期抛 JudgeError（不等到调用期才暴露）。"""
    with pytest.raises(JudgeError) as excinfo:
      JudgeAdapter(CallableAdapter(lambda messages: _VALID_JSON), sut_model, judge_model)
    # 消息里要能看出是「同族」这一条被触发，而不是别的构造期校验
    assert "同族" in str(excinfo.value)
    assert excinfo.value.context["stage"] == "construct"

  @pytest.mark.parametrize(("sut_model", "judge_model"), _DIFFERENT_FAMILY_PAIRS)
  def test_different_family_accepted(self, sut_model: str, judge_model: str) -> None:
    """异族模型对构造成功，且模型名被原样保留供后续报告归因。"""
    adapter = JudgeAdapter(
      CallableAdapter(lambda messages: _VALID_JSON), sut_model, judge_model
    )
    assert adapter.sut_model == sut_model
    assert adapter.judge_model == judge_model

  def test_unknown_prefix_same_string_is_same_family(self) -> None:
    """两方都不命中已知前缀时，完全相同视为同族（保守策略：宁拒勿漏）。"""
    assert _is_same_family("acme-model", "acme-model") is True
    with pytest.raises(JudgeError):
      JudgeAdapter(
        CallableAdapter(lambda messages: _VALID_JSON), "acme-model", "acme-model"
      )

  def test_unknown_prefix_different_string_is_different_family(self) -> None:
    """两方都不命中已知前缀但字符串不同时，视为异族（无法证明同源，放行）。"""
    assert _is_same_family("acme-model", "acme-other") is False

  def test_case_and_whitespace_insensitive(self) -> None:
    """族判定对大小写与首尾空白不敏感，避免大小写差异绕过强制异族。"""
    assert _is_same_family(" GPT-4o ", "gpt-4o-mini") is True

  @pytest.mark.parametrize(
    ("sut_model", "judge_model"),
    [("", "claude-3-opus"), ("gpt-4o", "   ")],
  )
  def test_blank_model_name_rejected(self, sut_model: str, judge_model: str) -> None:
    """空/全空白模型名在构造期被拒：无法判定族归属，不能放行。"""
    with pytest.raises(JudgeError, match="非空"):
      JudgeAdapter(CallableAdapter(lambda messages: _VALID_JSON), sut_model, judge_model)


class TestJsonParsing:
  """裁判输出的 JSON 解析：快路径、带前后缀提取、嵌套与花括号。"""

  async def test_valid_json_returns_judge_output(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """合法 JSON 端到端返回三字段正确的 JudgeOutput。"""
    output = await mock_judge_adapter(_VALID_JSON).aevaluate("2+2=?", "4")
    assert isinstance(output, JudgeOutput)
    assert output.passed is True
    assert output.score == 0.9
    assert output.reason == "答案正确且给出了推导过程"

  async def test_extracts_first_json_object_from_surrounding_text(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """裁判在 JSON 前后加了说明文字时，仍能提取并解析首个 JSON 对象。"""
    content = (
      "判定如下：\n"
      '{"passed": false, "score": 0.2, "reason": "未给出推导过程"}\n'
      "以上为本次判定。"
    )
    output = await mock_judge_adapter(content).aevaluate("2+2=?", "4")
    assert output.passed is False
    assert output.score == 0.2

  async def test_braces_inside_string_are_not_mistaken_for_object_end(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """理由文本里含花括号时，仍按 JSON 语法正确提取（不当成对象结束）。

    这一条盯住「首个 { 到最后一个 }」式切片解法的失效场景：切片会把字符串里的
    花括号也计入嵌套层级，导致一个结构完全合法的裁判输出被判成不可判。
    """
    content = (
      "判定如下（不含其他花括号）："
      '{"passed": true, "score": 1.0, "reason": "输出包含 {推导} 段落"}'
    )
    output = await mock_judge_adapter(content).aevaluate("2+2=?", "4")
    assert output.passed is True
    assert output.reason == "输出包含 {推导} 段落"

  async def test_leading_non_json_brace_is_unjudgeable(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """首个 ``{`` 起的片段不是 JSON 时判不可判，而不是顺延去找后面的对象。

    这是刻意的口径：JSON 之前的说明文字一旦也带花括号，说明裁判没有按 schema 输出，
    继续往后猜只会把「猜对的那次」记成通过，让通过率虚高。
    """
    content = '参考 {格式说明} 如下：{"passed": true, "score": 1.0, "reason": "理由"}'
    with pytest.raises(JudgeError, match="提取段解析失败"):
      await mock_judge_adapter(content).aevaluate("2+2=?", "4")

  async def test_markdown_fence_extracted(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """裁判用 json 代码块围栏（三反引号 + json）包裹输出时仍能提取。

    这是真实裁判最常见的格式噪音：整串不是 JSON，但 ``find("{")`` + ``raw_decode``
    能定位到被围栏包住的那个对象。system prompt 明令禁止围栏，但禁止不等于不会发生，
    解析层必须兜住——兜不住就会把一整批正常判定记成不可判。
    """
    content = '```json\n{"passed": true, "score": 0.9, "reason": "围栏测试"}\n```'
    output = await mock_judge_adapter(content).aevaluate("2+2=?", "4")
    assert output.passed is True
    assert output.score == 0.9

  async def test_plain_text_is_unjudgeable(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """完全不是 JSON 的自由文本不被猜测成结论，直接判不可判。"""
    with pytest.raises(JudgeError, match="不是合法 JSON"):
      await mock_judge_adapter("我觉得这个回答挺好的，给 0.9 分吧").aevaluate("x", "y")

  async def test_unclosed_object_is_unjudgeable(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """有 ``{`` 但截断成不合法 JSON 时抛 JudgeError，而非回退到文本判定。"""
    with pytest.raises(JudgeError, match="提取段解析失败"):
      await mock_judge_adapter('{"passed": true, "score": 0.9').aevaluate("x", "y")

  async def test_recursion_error_is_unjudgeable(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    """``json.loads`` 递归溢出必须收口为 JudgeError，不得裸逃逸。

    用 monkeypatch 固定异常形态而非构造真实深嵌套：3.13+ 的 C 递归上限与 3.11 不同，
    真实深嵌套在哪个深度触发 RecursionError 会随版本与平台栈大小漂移（D20 实测 3.13/
    3.14 的 2000 层可解析成功），断言一旦绑上触发机制就变成版本敏感。裸 RecursionError
    会绕过「调用方只需 except JudgeError」的契约，这才是要钉住的点。
    """

    def _boom(_raw: str) -> object:
      """模拟 json.loads 递归溢出。"""
      raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("aquamind.judge.json.loads", _boom)
    with pytest.raises(JudgeError, match="不是合法 JSON"):
      await mock_judge_adapter("深嵌套输出（不含花括号）").aevaluate("x", "y")

  async def test_real_deep_nesting_never_leaks_recursion_error(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """≥2000 层真实嵌套：任何版本都不得把裸 RecursionError 抛给调用方。

    只钉「契约不击穿」，不钉触发机制：该版本触发递归上限时解析阶段判不可判，
    解析成功时顶层不是对象、同样判不可判——两种出口都合法，具体消息随 json 的
    递归实现差异而不同（3.11 走前者，3.13/3.14 视嵌套深度与平台栈大小而定）。
    """
    deeply_nested = "[" * 2000 + "]" * 2000
    with pytest.raises(JudgeError) as excinfo:
      await mock_judge_adapter(deeply_nested).aevaluate("x", "y")
    # 两条出口都发生在解析阶段，用 stage 统一断言，避免绑定到某一条消息上
    assert excinfo.value.context["stage"] == "parse"

  async def test_top_level_array_is_unjudgeable(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """合法 JSON 但顶层是数组时同样判不可判：结论必须是对象。"""
    with pytest.raises(JudgeError, match="顶层不是对象"):
      await mock_judge_adapter('[{"passed": true}]').aevaluate("x", "y")


class TestContractValidation:
  """JSON 契约校验：缺字段 / 类型错 / 分数越界，均须点名具体字段。"""

  @pytest.mark.parametrize(
    ("payload", "field"),
    [
      ('{"score": 0.9, "reason": "理由"}', "passed"),
      ('{"passed": true, "reason": "理由"}', "score"),
      ('{"passed": true, "score": 0.9}', "reason"),
    ],
  )
  async def test_missing_field_rejected(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    payload: str,
    field: str,
  ) -> None:
    """缺任一字段都抛 JudgeError，且消息里点名缺的是哪个字段。"""
    with pytest.raises(JudgeError, match="契约校验") as excinfo:
      await mock_judge_adapter(payload).aevaluate("x", "y")
    assert field in str(excinfo.value)
    assert excinfo.value.context["fields"] == [field]

  @pytest.mark.parametrize(
    ("payload", "field", "note"),
    [
      # 严格布尔：字符串 "true" 不被接受（否则裁判违反 schema 也被计入通过率）
      ('{"passed": "true", "score": 0.9, "reason": "理由"}', "passed", "passed 非布尔"),
      # 数字型字符串：lax 模式会静默转成 0.8，故由前置校验器拦下
      ('{"passed": true, "score": "0.8", "reason": "理由"}', "score", "score 为字符串"),
      # 布尔不是数字：True 也会被 lax 模式当成 1.0
      ('{"passed": true, "score": true, "reason": "理由"}', "score", "score 为布尔"),
      ('{"passed": true, "score": 0.9, "reason": 42}', "reason", "reason 非字符串"),
    ],
  )
  async def test_type_error_rejected(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    payload: str,
    field: str,
    note: str,
  ) -> None:
    """类型不符一律抛 JudgeError（不可判），不静默转换成合法取值。"""
    with pytest.raises(JudgeError, match="契约校验") as excinfo:
      await mock_judge_adapter(payload).aevaluate("x", "y")
    assert field in str(excinfo.value), note
    assert excinfo.value.context["fields"] == [field]

  @pytest.mark.parametrize(
    ("payload", "note"),
    [
      ('{"passed": false, "score": -0.1, "reason": "理由"}', "低于下界"),
      ('{"passed": true, "score": 1.5, "reason": "理由"}', "高于上界"),
    ],
  )
  async def test_score_out_of_range_rejected(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    payload: str,
    note: str,
  ) -> None:
    """score 越界抛 JudgeError：越界分不能进聚合公式（会被归一化假设吞掉）。"""
    with pytest.raises(JudgeError, match="score") as excinfo:
      await mock_judge_adapter(payload).aevaluate("x", "y")
    assert excinfo.value.context["stage"] == "contract", note

  async def test_extra_field_rejected(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """多余字段同样判不可判：静默丢弃会让实际接受的 schema 悄悄变宽。"""
    payload = '{"passed": true, "score": 0.9, "reason": "理由", "confidence": 0.7}'
    with pytest.raises(JudgeError, match="confidence"):
      await mock_judge_adapter(payload).aevaluate("x", "y")

  @pytest.mark.parametrize(
    ("payload", "note"),
    [
      ('{"passed": true, "score": 0.9, "reason": ""}', "空串"),
      # 全空白长度足够，min_length 拦不住，由 strip 后的校验器拦下
      ('{"passed": true, "score": 0.9, "reason": "   "}', "全空白"),
    ],
  )
  async def test_blank_reason_rejected(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    payload: str,
    note: str,
  ) -> None:
    """空证据不被契约接受：空串与全空白理由都判不可判，且点名 reason。"""
    with pytest.raises(JudgeError, match="reason") as excinfo:
      await mock_judge_adapter(payload).aevaluate("x", "y")
    assert excinfo.value.context["stage"] == "contract", note
    assert excinfo.value.context["fields"] == ["reason"]

  @pytest.mark.parametrize(
    ("payload", "note"),
    [
      # NaN / Infinity 是 Python json.loads 接受的**扩展字面量**（标准 JSON 没有），
      # 因此它们能穿过解析层到达契约层——由 allow_inf_nan=False 拦下
      ('{"passed": true, "score": NaN, "reason": "理由"}', "NaN"),
      ('{"passed": true, "score": Infinity, "reason": "理由"}', "Infinity"),
    ],
  )
  async def test_non_finite_score_rejected(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    payload: str,
    note: str,
  ) -> None:
    """非有限分数判不可判：NaN 与任何数比较都为假，会让聚合整体变成 NaN。"""
    with pytest.raises(JudgeError, match="score") as excinfo:
      await mock_judge_adapter(payload).aevaluate("x", "y")
    assert excinfo.value.context["stage"] == "contract", note
    assert excinfo.value.context["fields"] == ["score"]


class TestPromptAndIntegration:
  """prompt 构造与端到端集成（CallableAdapter 注入裁判）。"""

  async def test_build_prompt_has_system_and_user(self) -> None:
    """prompt 为 system + user 两条，system 含 JSON schema 三字段说明。"""
    adapter = JudgeAdapter(
      CallableAdapter(lambda messages: _VALID_JSON), "gpt-4o", "claude-3-opus"
    )
    messages = adapter._build_prompt("2+2=?", "4")
    assert [m["role"] for m in messages] == ["system", "user"]
    system = messages[0]["content"]
    assert "JSON" in system
    for field in ("passed", "score", "reason"):
      assert field in system, f"system prompt 未声明字段 {field}"
    # 用例描述与被测输出都落在 user 消息里，且与 schema 说明分属两条消息
    assert "2+2=?" in messages[1]["content"]
    assert "4" in messages[1]["content"]

  async def test_end_to_end_with_callable_adapter(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """mock 裁判端到端跑通：判决结果、消息条数与字段取值全部可核对。"""
    adapter = mock_judge_adapter(_VALID_JSON)
    output = await adapter.aevaluate("2+2=?", "4")
    assert output.model_dump() == {
      "passed": True,
      "score": 0.9,
      "reason": "答案正确且给出了推导过程",
    }

  async def test_transport_error_becomes_unjudgeable(self) -> None:
    """裁判后端故障转成 JudgeError，原始 AdapterError 保留在 __cause__ 中。"""
    adapter = JudgeAdapter(
      CallableAdapter(_raise_adapter_error), "gpt-4o", "claude-3-opus"
    )
    with pytest.raises(JudgeError, match="不可判") as excinfo:
      await adapter.aevaluate("x", "y")
    assert excinfo.value.context["stage"] == "complete"
    assert isinstance(excinfo.value.__cause__, AdapterError)


class TestExceptionContract:
  """异常契约：JudgeError 继承 AquaMindError 并携带结构化 context。"""

  def test_judge_error_inherits_aqua_mind_error(self) -> None:
    """JudgeError 必须能被 except AquaMindError 接住（统一异常体系）。"""
    with pytest.raises(AquaMindError) as excinfo:
      JudgeAdapter(
        CallableAdapter(lambda messages: _VALID_JSON), "gpt-4o", "gpt-4o-mini"
      )
    assert isinstance(excinfo.value, JudgeError)
    # 归因信息在 context 里结构化可取；str() 只额外渲染键名，正文不进日志
    assert excinfo.value.context["sut_family"] == "gpt-"
    assert excinfo.value.context["judge_family"] == "gpt-"
    assert "context keys" in str(excinfo.value)


class TestSafeDegradation:
  """降级口径：失败不抛异常，返回可计数的 Unjudgeable（D08）。"""

  async def test_success_returns_judge_output(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """成功路径返回与 aevaluate 同一个 JudgeOutput，而不是第二种结论类型。"""
    result = await mock_judge_adapter(_VALID_JSON).aevaluate_safe("2+2=?", "4")
    assert isinstance(result, JudgeOutput)
    assert result.model_dump() == {
      "passed": True,
      "score": 0.9,
      "reason": "答案正确且给出了推导过程",
    }

  async def test_parse_failure_returns_parse_stage(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """坏 JSON 不抛异常：返回 stage=parse 的 Unjudgeable，且不携带原文。"""
    result = await mock_judge_adapter("我觉得这个回答挺好的").aevaluate_safe("x", "y")
    assert isinstance(result, Unjudgeable)
    assert result.stage == "parse"
    assert result.fields == ()
    assert "不是合法 JSON" in result.message
    # 原文只留在 JudgeError.context 的截断片段里，不进结果对象（会被计数、进报告）
    assert "我觉得" not in result.message

  async def test_parse_failure_keeps_error_type(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """提取段解析失败时 error_type 一并带上，报告层可按阶段再细分归因。"""
    result = await mock_judge_adapter('{"passed": true, "score": 0.9').aevaluate_safe(
      "x", "y"
    )
    assert isinstance(result, Unjudgeable)
    assert result.stage == "parse"
    assert result.error_type == "JSONDecodeError"

  async def test_contract_failure_returns_fields(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """契约失败返回 stage=contract，并带出不合法字段（此处缺 reason）。"""
    result = await mock_judge_adapter('{"passed": true, "score": 0.9}').aevaluate_safe(
      "x", "y"
    )
    assert isinstance(result, Unjudgeable)
    assert result.stage == "contract"
    assert result.fields == ("reason",)
    # 契约阶段的类型错由 fields 表达，error_type 留空（不伪造一个异常类型）
    assert result.error_type is None

  async def test_complete_failure_returns_complete_stage(self) -> None:
    """裁判调用失败返回 stage=complete 与原始异常类型名，不抛异常。"""
    adapter = JudgeAdapter(
      CallableAdapter(_raise_adapter_error), "gpt-4o", "claude-3-opus"
    )
    result = await adapter.aevaluate_safe("x", "y")
    assert isinstance(result, Unjudgeable)
    assert result.stage == "complete"
    assert result.error_type == "AdapterError"
    assert "不可判" in result.message

  async def test_module_level_function_forwards(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """模块级自由函数与实例方法同口径：成功/失败的形状完全一致。"""
    ok = await aevaluate_safe(mock_judge_adapter(_VALID_JSON), "2+2=?", "4")
    assert isinstance(ok, JudgeOutput)
    bad = await aevaluate_safe(mock_judge_adapter("[1, 2]"), "x", "y")
    assert isinstance(bad, Unjudgeable)
    assert bad.stage == "parse"

  async def test_unjudgeable_countable_by_stage(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """报告层用法：不捕获异常也能得到各阶段的不可判条数。"""
    counter: dict[str, int] = {"complete": 0, "parse": 0, "contract": 0}
    payloads = [_VALID_JSON, "不是 JSON", '{"passed": true}']
    for payload in payloads:
      result = await mock_judge_adapter(payload).aevaluate_safe("x", "y")
      if isinstance(result, Unjudgeable):
        counter[result.stage] += 1
    assert counter == {"complete": 0, "parse": 1, "contract": 1}


class TestParseErrorTraceability:
  """解析失败的可追溯性：context 带原文首尾片段，正文不带原文（D08）。"""

  async def test_long_output_keeps_head_and_tail(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """超长输出只留首尾各 200 字符：足以判断「被截断」还是「整段跑偏」。"""
    content = "开头说明" + "。中间的无效内容" * 60 + "结尾说明"
    assert len(content) > 400
    with pytest.raises(JudgeError) as excinfo:
      await mock_judge_adapter(content).aevaluate("x", "y")
    context = excinfo.value.context
    assert context["stage"] == "parse"
    assert context["content_head"] == content[:200]
    assert context["content_tail"] == content[-200:]
    assert len(context["content_head"]) == 200
    assert len(context["content_tail"]) == 200
    # 正文只报「没找到 JSON 对象」，原文不外泄到 message
    assert "开头说明" not in str(excinfo.value)

  async def test_short_output_kept_in_full(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """短输出（<200 字符）首尾片段都等于全文：不为截断丢信息。"""
    content = "裁判只回了一句话，没有 JSON"
    with pytest.raises(JudgeError) as excinfo:
      await mock_judge_adapter(content).aevaluate("x", "y")
    context = excinfo.value.context
    assert context["content_head"] == content
    assert context["content_tail"] == content

  async def test_top_level_mismatch_also_carries_snippets(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter]
  ) -> None:
    """顶层不是对象时同样带片段：这一路径的原文最容易被误当成「解析成功」。"""
    content = '[{"passed": true, "score": 1.0, "reason": "理由"}]'
    with pytest.raises(JudgeError) as excinfo:
      await mock_judge_adapter(content).aevaluate("x", "y")
    context = excinfo.value.context
    assert context["actual_type"] == "list"
    assert context["content_head"] == content
    assert context["content_tail"] == content

  @pytest.mark.parametrize("length", [200, 250, 400])
  async def test_snippet_boundary_lengths(
    self, mock_judge_adapter: Callable[[str], JudgeAdapter], length: int
  ) -> None:
    """200 / 250 / 400 三个边界：以内两段均为全文、之间重叠、恰好 400 相接。

    200 是「一段刚好装下全文」的临界点，250 落在两段必然重叠的区间，400 是
    「两段刚好铺满全文」的临界点。三种形态都只靠端点判断会各错一种，故逐一钉住。
    """
    content = "".join(chr(0x4E00 + index) for index in range(length))
    with pytest.raises(JudgeError) as excinfo:
      await mock_judge_adapter(content).aevaluate("x", "y")
    context = excinfo.value.context
    assert context["content_head"] == content[:200]
    assert context["content_tail"] == content[-200:]
    if length == 200:
      assert context["content_head"] == content
      assert context["content_tail"] == content
    if length == 250:
      # 下标 50-199 的 150 个字符同时落在两段里：中段没有被截掉
      overlap = content[50:200]
      assert overlap in context["content_head"]
      assert overlap in context["content_tail"]
    if length == 400:
      # 恰好相接：两段拼起来就是全文，既不丢字符也不重复
      assert context["content_head"] + context["content_tail"] == content


class TestUnjudgeableEdgeCases:
  """model 级契约错误与非序列 fields：归因不得双双降解（D08 热修）。"""

  @staticmethod
  def _model_level_error() -> ValidationError:
    """构造一个 ``loc=()`` 的 ValidationError，模拟 model 级校验抛错。

    pydantic 只在 model_validator / root model 场景产出 loc 为空的错误，本模块的
    字段校验不会走到这里；但一旦出现，``fields`` 会为空，正是归因全丢的入口。
    """
    return ValidationError.from_exception_data(
      "JudgeOutput",
      [
        {
          "type": "value_error",
          "loc": (),
          "input": {},
          "ctx": {"error": ValueError("模型级校验失败")},
        }
      ],
    )

  async def test_model_level_error_keeps_attribution(
    self,
    mock_judge_adapter: Callable[[str], JudgeAdapter],
    monkeypatch: pytest.MonkeyPatch,
  ) -> None:
    """只产出 model 级错误时：消息不悬挂冒号，error_type 顶替 fields 承载归因。"""
    error = self._model_level_error()

    def _boom(_payload: object) -> JudgeOutput:
      raise error

    monkeypatch.setattr(JudgeOutput, "model_validate", staticmethod(_boom))
    with pytest.raises(JudgeError) as excinfo:
      await mock_judge_adapter(_VALID_JSON).aevaluate("x", "y")
    context = excinfo.value.context
    assert context["fields"] == []
    assert context["error_type"] == "value_error"
    assert "不合法输出格式" in str(excinfo.value)
    # 悬挂冒号是旧实现的症状：消息以「不合法字段: 」结尾而后面什么都没有
    assert not str(excinfo.value).rstrip().endswith("字段:")

    result = await mock_judge_adapter(_VALID_JSON).aevaluate_safe("x", "y")
    assert isinstance(result, Unjudgeable)
    assert result.stage == "contract"
    assert result.fields == ()
    # fields 与 error_type 至少一个有值：这里是 error_type 顶上
    assert result.error_type == "value_error"

  def test_non_sequence_fields_treated_as_empty(self) -> None:
    """模块外把 fields 写成字符串时按「无字段」处理，不逐字符炸成单字母字段名。"""
    exc = JudgeError(
      message="外部构造的契约失败",
      context={"stage": "contract", "fields": "reason"},
    )
    result = Unjudgeable.from_error(exc)
    assert result.fields == ()
    assert result.stage == "contract"

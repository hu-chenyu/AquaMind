"""精确匹配评分器单元测试。

覆盖范围：
- 六类基础匹配器（exact / regex / contains / excludes / keywords / json）的通过与失败
  分支，以及「用例写错」（空子串、非法正则、非法 JSON、期望值类型不对）时必须抛
  ``ValueError`` 而非返回 False；
- 边界加固（D21 收尾）：None 期望值、空正则、灾难性回溯预检、JSON 路径键非字符串、
  深嵌套 JSON 的 ``RecursionError`` 收口；
- ``match_expected`` 按 type 分发、未注册类型的防御性报错、``tolerance`` 契约预留；
- 输入侧三模型（TestCase / ExpectedSpec / ScoreTag）的 ``model_copy(update=...)``
  收口：带 update 走完整校验，越界值/未知字段/非法嵌套一律被拒；
- 契约层 ``ExpectedType`` 的七类合法取值与非法取值拦截；
- D22 新增两类（``all`` / ``agent``）：组合断言的**首个失败定位**与不可判传播、
  Agent 工具调用断言的参数层（``assert_args``）判定。顺序层 / 工具层顺延 D31，
  本文件只固化「``order`` / ``tools`` 出现即忽略」这一现状；
- D23 新增 ``score_response``：判定结论到 ``Score`` 的包装、字段级权重归一化、
  缺字段（声明但未消费的维度）策略、``matched_tags`` 的业务层登记口径，以及
  匹配器异常的原样传播。

全部用例纯本地判定，不发起任何网络调用（零 key）；Agent 用例的模型输出是硬编码
JSON 字符串，不调用任何真实模型。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from aquamind.models.testcase import ExpectedSpec, ExpectedType, ScoreTag, TestCase
from aquamind.scoring import (
  AgentMatcher,
  AllMatcher,
  ContainsMatcher,
  ExactMatcher,
  ExcludesMatcher,
  JsonMatcher,
  KeywordsMatcher,
  RegexMatcher,
  match_expected,
  score_response,
)


def _spec(type_name: ExpectedType, value: object) -> ExpectedSpec:
  """构造一条期望规范，省去各用例重复写 type/value。"""
  return ExpectedSpec(type=type_name, value=value)


def _case(**overrides: object) -> TestCase:
  """构造一条最小合法用例，供 model_copy 收口测试复用。"""
  payload: dict[str, object] = {
    "input": "x",
    "expected": {"type": "exact", "value": "y"},
  }
  payload.update(overrides)
  return TestCase(**payload)  # type: ignore[arg-type]


def _tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
  """构造一条 OpenAI 兼容的工具调用记录：``arguments`` 是 JSON 字符串。

  Args:
    name: 工具名。
    arguments: 参数对象，会被序列化成 JSON 字符串（与真实模型输出一致）。

  Returns:
    dict[str, Any]: ``{"function": {"name": ..., "arguments": ...}}``。
  """
  return {"function": {"name": name, "arguments": json.dumps(arguments)}}


def _agent_output(*calls: dict[str, Any]) -> str:
  """把若干工具调用打包成 Agent 输出文本（硬编码 mock 输出，零 key）。

  Args:
    *calls: ``_tool_call`` 构造的调用记录，按输出顺序传入。

  Returns:
    str: 形如 ``{"tool_calls": [...]}`` 的 JSON 文本。
  """
  return json.dumps({"tool_calls": list(calls)})


def _agent_spec(*assertions: tuple[str, dict[str, Any]], **extra: Any) -> ExpectedSpec:
  """构造 agent 断言规范；``extra`` 用于验证 order / tools 等预留键被忽略。

  Args:
    *assertions: ``(工具名, 期望参数)`` 二元组，按 assert_args 顺序展开。
    **extra: 额外写进 value 的键（如 order / tools）。

  Returns:
    ExpectedSpec: ``type="agent"`` 的期望规范。
  """
  value: dict[str, Any] = {
    "assert_args": [{"tool": tool, "arguments": args} for tool, args in assertions]
  }
  value.update(extra)
  return _spec("agent", value)


class TestExactMatcher:
  """exact：逐字符相等才通过。"""

  def test_equal_passes(self) -> None:
    """完全相等应通过，且说明固定为「通过」。"""
    assert ExactMatcher().match("2", _spec("exact", "2")) == (True, "通过")

  def test_not_equal_fails_with_reason(self) -> None:
    """不相等失败，原因里必须同时给出期望与实际，便于定位差在哪。"""
    passed, reason = ExactMatcher().match("2 摄氏度", _spec("exact", "2"))
    assert passed is False
    assert "期望='2'" in reason
    assert "实际='2 摄氏度'" in reason

  def test_non_string_value_is_compared_as_text(self) -> None:
    """用例里写 ``value: 100``（YAML 数字）应与输出 '100' 判为相等。"""
    assert ExactMatcher().match("100", _spec("exact", 100)) == (True, "通过")

  def test_empty_expected_matches_empty_response(self) -> None:
    """空串 exact 是合法断言（「输出确实为空」是可判定的确定事实）。

    与 contains/excludes 拦空串的口径不同：那两类的空串断言恒真/恒假、不含被测
    信号；而空输出本身是一条有信息量的期望。
    """
    assert ExactMatcher().match("", _spec("exact", "")) == (True, "通过")


class TestRegexMatcher:
  """regex：re.search 命中即通过；非法模式属用例错误，抛 ValueError。"""

  def test_search_hit_passes(self) -> None:
    """不要求整串匹配：前后带文字也应命中。"""
    assert RegexMatcher().match("答案是 42 度", _spec("regex", r"\d+")) == (True, "通过")

  def test_no_match_fails_with_reason(self) -> None:
    """未命中失败，原因里给出模式与实际输出。"""
    passed, reason = RegexMatcher().match("没有数字", _spec("regex", r"\d+"))
    assert passed is False
    assert "未匹配正则" in reason
    assert r"模式='\\d+'" in reason

  def test_invalid_pattern_raises_value_error(self) -> None:
    """非法正则必须抛 ValueError（用例写错，不能计成「被测应用不通过」）。"""
    with pytest.raises(ValueError) as excinfo:
      RegexMatcher().match("anything", _spec("regex", "(["))
    assert "非法正则表达式" in str(excinfo.value)


class TestContainsMatcher:
  """contains：子串包含即通过，大小写敏感。"""

  def test_substring_present_passes(self) -> None:
    """包含子串应通过。"""
    assert ContainsMatcher().match("沸点是 100 度", _spec("contains", "100")) == (True, "通过")

  def test_substring_absent_fails_with_reason(self) -> None:
    """不包含失败，原因标注大小写敏感口径。"""
    passed, reason = ContainsMatcher().match("沸点是 90 度", _spec("contains", "100"))
    assert passed is False
    assert "未包含期望子串" in reason
    assert "大小写敏感" in reason

  def test_case_sensitive(self) -> None:
    """大小写不同即视为不包含：不做隐式折叠。"""
    passed, _ = ContainsMatcher().match("Hello World", _spec("contains", "hello"))
    assert passed is False

  def test_empty_expected_rejected(self) -> None:
    """空串恒被包含，判定无信息量，必须按用例错误拦下。"""
    with pytest.raises(ValueError) as excinfo:
      ContainsMatcher().match("任意输出", _spec("contains", ""))
    assert "不能为空字符串" in str(excinfo.value)


class TestExcludesMatcher:
  """excludes：命中被排除子串即失败。"""

  def test_absent_passes(self) -> None:
    """未出现禁用子串应通过。"""
    passed, _ = ExcludesMatcher().match("建议多喝水", _spec("excludes", "诊断"))
    assert passed is True

  def test_hit_fails_with_forbidden_token(self) -> None:
    """命中即失败，原因里点名命中的子串。"""
    passed, reason = ExcludesMatcher().match("初步诊断：感冒", _spec("excludes", "诊断"))
    assert passed is False
    assert "命中排除子串" in reason
    assert "禁用='诊断'" in reason

  def test_empty_forbidden_rejected(self) -> None:
    """空禁用串恒被命中，属用例错误。"""
    with pytest.raises(ValueError) as excinfo:
      ExcludesMatcher().match("任意输出", _spec("excludes", ""))
    assert "不能为空字符串" in str(excinfo.value)


class TestKeywordsMatcher:
  """keywords：多关键词全部命中才通过（AND 语义）。"""

  def test_all_keywords_present(self) -> None:
    """列表写法的关键词全部出现应通过。"""
    passed, _ = KeywordsMatcher().match("alpha 与 beta 都在", _spec("keywords", ["alpha", "beta"]))
    assert passed is True

  def test_missing_one_keyword_fails(self) -> None:
    """缺一个关键词即失败，原因列出缺失项与总数。"""
    passed, reason = KeywordsMatcher().match(
      "alpha 与 beta 都在", _spec("keywords", ["alpha", "beta", "gamma"])
    )
    assert passed is False
    assert "['gamma']" in reason
    assert "共 3 个" in reason

  def test_comma_separated_string_parsed(self) -> None:
    """逗号分隔字符串应被解析为多关键词，两侧空白被去掉。"""
    passed, _ = KeywordsMatcher().match("alpha beta", _spec("keywords", "alpha, beta"))
    assert passed is True

  def test_tuple_keywords_accepted(self) -> None:
    """元组形态与列表同义（程序内构造用例时可能给出 tuple）。"""
    assert KeywordsMatcher().match("alpha beta", _spec("keywords", ("alpha", "beta"))) == (
      True,
      "通过",
    )

  def test_invalid_value_type_rejected(self) -> None:
    """既不是列表也不是字符串的期望值属传参类型错误（TypeError，非 ValueError）。"""
    with pytest.raises(TypeError) as excinfo:
      KeywordsMatcher().match("alpha", _spec("keywords", 42))
    assert "关键词列表或逗号分隔字符串" in str(excinfo.value)

  @pytest.mark.parametrize("value", [[], "alpha,,beta", "   "])
  def test_empty_or_blank_keywords_rejected(self, value: object) -> None:
    """空列表与含空白项（如逗号写多了）都应报错，不能被静默忽略。"""
    with pytest.raises(ValueError) as excinfo:
      KeywordsMatcher().match("alpha", _spec("keywords", value))
    assert "不能为空或含空白项" in str(excinfo.value)


class TestJsonMatcher:
  """json：解析输出后按「路径 → 期望值」断言，全部相等才通过。"""

  def test_nested_path_passes(self) -> None:
    """点号路径能逐层进入对象。"""
    response = '{"user": {"name": "aqua", "level": 3}}'
    passed, _ = JsonMatcher().match(response, _spec("json", {"user.name": "aqua"}))
    assert passed is True

  def test_list_index_path_passes(self) -> None:
    """纯数字段表示列表下标，可与其它路径组合。"""
    response = '{"items": [{"id": 1}, {"id": 2}]}'
    passed, _ = JsonMatcher().match(response, _spec("json", {"items.1.id": 2}))
    assert passed is True

  def test_missing_path_fails(self) -> None:
    """路径不存在时失败并点名该路径。"""
    passed, reason = JsonMatcher().match('{"user": {}}', _spec("json", {"user.name": "aqua"}))
    assert passed is False
    assert "路径 'user.name' 不存在" in reason

  def test_path_through_scalar_fails(self) -> None:
    """中途碰到标量同样算路径不存在，而不是抛 TypeError。"""
    passed, reason = JsonMatcher().match('{"a": 1}', _spec("json", {"a.b": 1}))
    assert passed is False
    assert "路径 'a.b' 不存在" in reason

  def test_value_mismatch_fails(self) -> None:
    """路径存在但取值不等时失败，原因同时给出期望与实际。"""
    passed, reason = JsonMatcher().match('{"level": 2}', _spec("json", {"level": 3}))
    assert passed is False
    assert "路径 'level' 不匹配" in reason
    assert "期望=3" in reason
    assert "实际=2" in reason

  def test_invalid_json_raises_value_error(self) -> None:
    """非法 JSON 输出属用例执行结果不可解析，按错误抛出而非静默不通过。"""
    with pytest.raises(ValueError) as excinfo:
      JsonMatcher().match("不是 JSON", _spec("json", {"a": 1}))
    assert "输出不是合法 JSON" in str(excinfo.value)

  @pytest.mark.parametrize("value", [{}, [("a", 1)], "a"])
  def test_invalid_assertion_mapping_rejected(self, value: object) -> None:
    """断言集必须是非空映射；空映射与其它形态都属用例错误。"""
    with pytest.raises(ValueError) as excinfo:
      JsonMatcher().match('{"a": 1}', _spec("json", value))
    assert "非空映射" in str(excinfo.value)


class TestMatchExpectedDispatch:
  """match_expected：按 type 分发到对应匹配器。"""

  @pytest.mark.parametrize(
    ("type_name", "response", "value"),
    [
      ("exact", "ok", "ok"),
      ("regex", "abc123", r"\d+"),
      ("contains", "hello world", "world"),
      ("excludes", "hello world", "goodbye"),
      ("keywords", "alpha beta", "alpha,beta"),
      ("json", '{"a": 1}', {"a": 1}),
    ],
  )
  def test_dispatch_to_matching_matcher(
    self, type_name: ExpectedType, response: str, value: object
  ) -> None:
    """六类已注册类型都必须真的分到自己那类匹配器并给出通过结论。"""
    assert match_expected(response, _spec(type_name, value)) == (True, "通过")

  def test_failure_propagates_reason(self) -> None:
    """失败分支的说明必须原样透出，不能被入口吞掉。"""
    passed, reason = match_expected("nope", _spec("contains", "yes"))
    assert passed is False
    assert "未包含期望子串" in reason

  def test_judge_not_registered(self) -> None:
    """judge 需真实模型调用（M2-D07），确定性入口应明确报未注册。"""
    with pytest.raises(ValueError) as excinfo:
      match_expected("任意输出", _spec("judge", "任意期望"))
    assert "未注册的期望类型" in str(excinfo.value)

  def test_unknown_type_bypassing_contract_rejected(self) -> None:
    """防御性兜底：绕过 pydantic 构造出的未知类型也必须报错，不得静默不通过。"""
    bogus = ExpectedSpec.model_construct(type="semantic", value="x")
    with pytest.raises(ValueError) as excinfo:
      match_expected("x", bogus)
    assert "未注册的期望类型" in str(excinfo.value)


class TestModelCopyGuard:
  """三模型 model_copy 收口：带 update 走完整校验，堵住官方后门。"""

  def test_expected_spec_copy_without_update_unchanged(self) -> None:
    """不带 update（含空字典）时保持官方纯复制语义：值相同、对象不同。"""
    spec = _spec("contains", "100")
    for copied in (spec.model_copy(), spec.model_copy(update={})):
      assert copied == spec
      assert copied is not spec

  def test_expected_spec_copy_rejects_invalid_type(self) -> None:
    """update 出非法 type 必须被 Literal 拦下（修复前 model_copy 不跑校验）。"""
    with pytest.raises(ValidationError) as excinfo:
      _spec("exact", "y").model_copy(update={"type": "excat"})
    assert "type" in str(excinfo.value)

  def test_expected_spec_copy_rejects_negative_tolerance(self) -> None:
    """update 出负容差必须被 ge=0.0 拦下。"""
    with pytest.raises(ValidationError):
      _spec("exact", "y").model_copy(update={"tolerance": -1.0})

  def test_expected_spec_copy_rejects_unknown_field(self) -> None:
    """拼错的字段名不得被 model_copy 静默丢弃（extra="forbid" 必须生效）。"""
    with pytest.raises(ValidationError) as excinfo:
      _spec("exact", "y").model_copy(update={"valeu": "z"})
    assert "valeu" in str(excinfo.value)

  def test_expected_spec_copy_with_legal_update(self) -> None:
    """合法 update 应通过且真的生效。"""
    copied = _spec("exact", "y").model_copy(update={"value": "z", "tolerance": 0.5})
    assert copied.value == "z"
    assert copied.tolerance == 0.5

  def test_score_tag_copy_rejects_blank_name_and_negative_weight(self) -> None:
    """空标签名与负权重都不可经 model_copy 绕过。"""
    tag = ScoreTag(name="accuracy", weight=1.0)
    with pytest.raises(ValidationError):
      tag.model_copy(update={"name": "  "})
    with pytest.raises(ValidationError):
      tag.model_copy(update={"weight": -0.5})

  def test_score_tag_copy_with_legal_update(self) -> None:
    """合法 update 应生效；不带 update 时行为与官方一致。"""
    tag = ScoreTag(name="accuracy", weight=1.0)
    assert tag.model_copy(update={"weight": 2.0}).weight == 2.0
    assert tag.model_copy() == tag

  def test_test_case_copy_rejects_negative_weight(self) -> None:
    """用例权重经 model_copy 改为负值必须被拒。"""
    with pytest.raises(ValidationError):
      _case().model_copy(update={"weights": {"accuracy": -1.0}})

  def test_test_case_copy_rejects_invalid_nested_expected(self) -> None:
    """嵌套的 expected 会在 update 后重新校验，非法 type 不得蒙混过关。"""
    with pytest.raises(ValidationError):
      _case().model_copy(update={"expected": {"type": "excat", "value": "y"}})

  def test_test_case_copy_rejects_unknown_field(self) -> None:
    """未知字段必须报错而不是被静默丢弃。"""
    with pytest.raises(ValidationError) as excinfo:
      _case().model_copy(update={"expect": {}})
    assert "expect" in str(excinfo.value)

  def test_test_case_copy_with_legal_update(self) -> None:
    """合法 update 应生效；不带 update 时行为与官方一致。"""
    case = _case()
    assert case.model_copy(update={"input": "y"}).input == "y"
    assert case.model_copy() == case


class TestExpectedTypeEnum:
  """ExpectedType 七类：合法值全部可构造，第八类被 pydantic 拒绝。"""

  @pytest.mark.parametrize(
    "type_name",
    ["exact", "regex", "contains", "judge", "excludes", "keywords", "json"],
  )
  def test_legal_types_accepted(self, type_name: str) -> None:
    """七类合法取值逐一构造（含本日新增的 excludes/keywords/json）。"""
    assert _spec(type_name, "x").type == type_name  # type: ignore[arg-type]

  @pytest.mark.parametrize("bad_type", ["semantic", "exclude", "JSON", ""])
  def test_illegal_types_rejected(self, bad_type: str) -> None:
    """拼错、单复数写错、大小写不符都必须被 Literal 拦下。"""
    with pytest.raises(ValidationError) as excinfo:
      _spec(bad_type, "x")  # type: ignore[arg-type]
    assert "type" in str(excinfo.value)


class TestNoneAndEmptyValueGuard:
  """None / 空正则：曾静默变成有意义的字面量或被放过。"""

  @pytest.mark.parametrize("type_name", ["exact", "contains"])
  def test_none_value_raises_value_error(self, type_name: ExpectedType) -> None:
    """``value: null`` 曾让 str(None) 变成字面量 "None"：输出含 None 即假通过。"""
    with pytest.raises(ValueError) as excinfo:
      match_expected("return None", _spec(type_name, None))
    assert "不能为 None" in str(excinfo.value)

  def test_keywords_none_value_raises_value_error(self) -> None:
    """keywords 的 None 归入 ValueError（用例漏写），而不是 TypeError（类型拼错）。"""
    with pytest.raises(ValueError) as excinfo:
      KeywordsMatcher().match("alpha", _spec("keywords", None))
    assert "不能为 None" in str(excinfo.value)

  def test_regex_empty_pattern_raises_value_error(self) -> None:
    """空正则恒命中任一输出，与 contains/excludes 拦空串的口径对齐。"""
    with pytest.raises(ValueError) as excinfo:
      RegexMatcher().match("任意输出", _spec("regex", ""))
    assert "不能为空" in str(excinfo.value)


class TestRegexRedos:
  """灾难性回溯预检：危险模式编译前拦下，安全模式不得被误伤。"""

  @pytest.mark.parametrize(
    "pattern",
    [
      r"(a+)+b",
      r"(a*)*",
      r"((a+))+",
      r"(?:a+)+",
      r"(x|a+)+$",
      # 交替冗余：两条分支消耗相同字符，零宽保护剪不掉（复核 B 实测
      # (a|a?)+b 在 24 个 a 上耗时 20s，呈 2^n 增长）
      r"(a|a?)+",
      r"(a|ab?)+",
      # 交替冗余的嵌套形态：同一机制隔一层括号（实测 ((a|a?))+b 在 20 个 a 上
      # 0.73s，呈 2^n）；不能靠把分支二回退成 [*+?] 来补，否则误伤 ((a?))+
      r"((a|a?))+",
    ],
  )
  def test_regex_catastrophic_backtracking_detected(self, pattern: str) -> None:
    """组内可多种等长拆分 + 组后被无上界重复：必须在用例执行前拒绝。"""
    with pytest.raises(ValueError) as excinfo:
      RegexMatcher().match("a" * 24, _spec("regex", pattern))
    assert "灾难性回溯" in str(excinfo.value)

  @pytest.mark.parametrize(
    "pattern",
    [
      r"\d+",
      r"^(yes|no)$",
      r"\(a+\)+",
      r"([*])+",
      r"(ab)+",
      r"a+?b",
      r"(\w+\s?){1,3}",
      # 以下三条是「组内只有 ?（有界量词）」的线性模式：复核 A 实测曾被误拒。
      # ? 每次迭代最多消耗 1 个字符，全部收缩路径是线性的，不构成指数回溯。
      r"(ab?c)+",
      r"(\d\s?)+",
      r"(a?){2,}",
      # 嵌套纯 ? 形态：这两条专门锁定第四分支的「| + 量词」**联合**条件。
      # 若把第四分支退化成「子组含量词即拦」（丢掉 | 条件），它们会被误拒——
      # 平面安全列表进不了分支四，故必须有嵌套形态的参数才能捕获该变异（M4 逃逸）。
      r"((a?))+",
      r"((ab?c))+",
    ],
  )
  def test_regex_safe_pattern_accepted(self, pattern: str) -> None:
    """普通量词、有界重复、转义括号、字符类量词、有界 ? 都不得被预检误伤。"""
    passed, _ = RegexMatcher().match("(123) aab", _spec("regex", pattern))
    assert isinstance(passed, bool)


class TestJsonEdgeCases:
  """json 断言的边界：路径键类型、深嵌套不可判、顶层数组下标。"""

  def test_json_non_string_path_key_raises_value_error(self) -> None:
    """YAML 的 ``{1: x}`` 会解析出 int 键：必须报 ValueError，不能漏裸 AttributeError。"""
    with pytest.raises(ValueError) as excinfo:
      JsonMatcher().match('["x"]', _spec("json", {1: "x"}))
    assert "路径必须为字符串" in str(excinfo.value)
    assert "int" in str(excinfo.value)

  def test_json_deep_nesting_raises_value_error(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """深嵌套输出的 RecursionError 必须包装为 ValueError（不可判），不得裸逃逸。

    用 monkeypatch 固定异常形态而非真实深嵌套：3.13/3.14 的 json 解析器不受
    ``sys.setrecursionlimit`` 约束（D20 实测 2000 层仍能解析成功），真实深嵌套会让
    断言变成版本敏感；本地 3.11 已实测 ``setrecursionlimit(100)`` 确实触发该异常，
    故这里只钉「异常被收口」这一契约，不钉触发机制。
    """

    def _boom(_raw: str) -> object:
      """模拟 json.loads 递归溢出。"""
      raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("aquamind.scoring.json.loads", _boom)
    with pytest.raises(ValueError) as excinfo:
      JsonMatcher().match("[[]]", _spec("json", {"a": 1}))
    assert "嵌套过深" in str(excinfo.value)
    # 原始异常保留在 __cause__ 里：上层排障仍能看出根因是递归溢出
    assert isinstance(excinfo.value.__cause__, RecursionError)

  def test_json_top_level_array_index(self) -> None:
    """顶层数组按数字段下标断言（固化既有行为，避免将来被改成只支持对象）。"""
    assert JsonMatcher().match("[10, 20]", _spec("json", {"1": 20})) == (True, "通过")


class TestModelCopyNestedInstance:
  """嵌套**实例**穿透：``revalidate_instances='never'`` 下坏实例曾被原样收下。"""

  def test_model_copy_nested_bad_instance_rejected(self) -> None:
    """model_construct 造的非法 ExpectedSpec 实例经 update 注入必须被拒。"""
    bad = ExpectedSpec.model_construct(type="excat", value="y")
    with pytest.raises(ValidationError):
      _case().model_copy(update={"expected": bad})

  def test_model_copy_nested_bad_instance_in_list_rejected(self) -> None:
    """列表内的模型实例同样要重校验（score_tags 是 list[ScoreTag]）。"""
    bad_tag = ScoreTag.model_construct(name="   ")
    with pytest.raises(ValidationError):
      _case().model_copy(update={"score_tags": [bad_tag]})

  def test_model_copy_legal_nested_instance_accepted(self) -> None:
    """合法实例经 update 注入仍应通过，且被重建为契约模型。"""
    copied = _case().model_copy(update={"expected": ExpectedSpec(type="contains", value="100")})
    assert copied.expected.type == "contains"
    assert isinstance(copied.expected, ExpectedSpec)

  def test_model_copy_deep_copy_keeps_official_semantics(self) -> None:
    """deep=True 的纯复制路径不受收口影响：值相等、嵌套对象不共享。"""
    case = _case(score_tags=[{"name": "accuracy"}])
    copied = case.model_copy(deep=True)
    assert copied == case
    assert copied.expected is not case.expected

  def test_model_copy_deep_with_update_still_validates(self) -> None:
    """deep=True 且带 update 时同样走完整校验（不能借 deep 绕过后门）。"""
    with pytest.raises(ValidationError):
      _case().model_copy(update={"weights": {"accuracy": -1.0}}, deep=True)


class TestToleranceContract:
  """tolerance 只作契约预留：可传入、语义写在字段描述里、不改变当前判定。"""

  def test_tolerance_field_accepted_and_documented(self) -> None:
    """容差当前不参与判定：41.6 与 42 仍判不相等，但字段须写明用途与接线点。"""
    spec = _spec("exact", "42")
    spec.tolerance = 0.5
    assert spec.tolerance == 0.5
    description = ExpectedSpec.model_fields["tolerance"].description or ""
    assert "预留用于数值类断言" in description
    assert "D22" in description
    assert match_expected("41.6", spec)[0] is False


class TestAllMatcher:
  """all：全部子断言通过才通过；首个失败即停；不可判必须向上传播。"""

  def test_all_children_pass(self) -> None:
    """三条子断言（contains / json / regex）全部成立时组合通过。"""
    spec = _spec(
      "all",
      [
        {"type": "contains", "value": "北京"},
        {"type": "json", "value": {"city": "北京"}},
        {"type": "regex", "value": r"\d+"},
      ],
    )
    assert match_expected('{"city": "北京", "temp": 21}', spec) == (True, "通过")

  def test_first_failure_short_circuits(self) -> None:
    """首条失败立即返回，后续子断言不再执行。

    第二条子断言是非法正则（一旦执行就抛 ValueError）：若没有被短路，本用例会以
    异常崩掉而不是返回 ``(False, ...)``，故「没有抛异常且定位到第 1 条」本身就证明
    后续子断言未被执行。
    """
    spec = _spec(
      "all",
      [
        {"type": "exact", "value": "期望输出"},
        {"type": "regex", "value": "(["},
        {"type": "contains", "value": "关键词"},
      ],
    )
    passed, reason = match_expected("实际输出", spec)
    assert passed is False
    assert "组合断言第 1 条失败" in reason
    assert "输出与期望不相等" in reason

  def test_middle_failure_reports_first_failing_index(self) -> None:
    """中间条失败时定位到首个失败（第 2 条），而不是最后一条（第 3 条）。"""
    spec = _spec(
      "all",
      [
        {"type": "contains", "value": "北京"},
        {"type": "contains", "value": "湿度"},
        {"type": "contains", "value": "气压"},
      ],
    )
    passed, reason = match_expected('{"city": "北京"}', spec)
    assert passed is False
    assert "组合断言第 2 条失败" in reason
    assert "湿度" in reason
    # 第 3 条也必然失败，但组合必须在首个失败处停下，不能汇总到「最后一条」
    assert "第 3 条" not in reason

  def test_empty_list_rejected(self) -> None:
    """空组合恒通过、不含任何被测信号，必须按用例写错拦下。"""
    with pytest.raises(ValueError) as excinfo:
      match_expected("任意输出", _spec("all", []))
    assert "不能为空" in str(excinfo.value)

  def test_non_list_value_rejected(self) -> None:
    """value 是映射/字符串/数字时都属用例写错，不能被当成单条子断言收下。"""
    for value in ({"type": "contains", "value": "北京"}, "contains", 42):
      with pytest.raises(ValueError) as excinfo:
        match_expected("任意输出", _spec("all", value))
      assert "子断言列表" in str(excinfo.value)

  def test_json_child_invalid_output_propagates(self) -> None:
    """子断言遇非法 JSON 时整组判不可判：ValueError 必须传播，不能压成不通过。

    压成 False 会让通过率虚低，还会掩盖「输出压根解析不了」这个更该优先暴露的问题。
    """
    spec = _spec(
      "all",
      [
        {"type": "contains", "value": "北京"},
        {"type": "json", "value": {"city": "北京"}},
      ],
    )
    # 第一条 contains 先通过，确保真正走到 json 子断言上；直接调匹配器本身，
    # 证明「不捕获子断言异常」是 AllMatcher 自己的行为，而不是入口层的巧合
    with pytest.raises(ValueError) as excinfo:
      AllMatcher().match("北京但后面不是 JSON", spec)
    assert "输出不是合法 JSON" in str(excinfo.value)

  def test_nested_all_works(self) -> None:
    """all 里再套 all：递归走 match_expected，内层结论能原样透出到外层原因里。"""
    spec = _spec(
      "all",
      [
        {"type": "all", "value": [{"type": "contains", "value": "北京"}]},
        {"type": "contains", "value": "21"},
      ],
    )
    assert match_expected("北京 晴 21 度", spec) == (True, "通过")
    inner_fail = _spec("all", [{"type": "all", "value": [{"type": "contains", "value": "雪"}]}])
    passed, reason = match_expected("北京 晴 21 度", inner_fail)
    assert passed is False
    assert "组合断言第 1 条失败" in reason
    assert "雪" in reason

  def test_unregistered_child_type_propagates(self) -> None:
    """judge 子断言未注册（需真实模型调用）：ValueError 必须传播，不得压成不通过。"""
    spec = _spec("all", [{"type": "judge", "value": "回答是否礼貌"}])
    with pytest.raises(ValueError) as excinfo:
      AllMatcher().match("任意输出", spec)
    assert "未注册的期望类型" in str(excinfo.value)


class TestAgentMatcher:
  """agent：参数层断言（assert_args）——工具被调用且 arguments 完全相等才通过。"""

  def test_single_tool_call_matches(self) -> None:
    """单次工具调用、参数完全相等时通过。"""
    output = _agent_output(_tool_call("get_weather", {"city": "北京"}))
    assert match_expected(output, _agent_spec(("get_weather", {"city": "北京"}))) == (
      True,
      "通过",
    )

  def test_multiple_tool_calls_all_match(self) -> None:
    """多条调用、多条断言全部命中时通过；参数按键值比较，键序不影响结论。"""
    output = _agent_output(
      _tool_call("get_weather", {"city": "北京"}),
      _tool_call("get_air_quality", {"city": "北京", "unit": "aqi"}),
    )
    spec = _agent_spec(
      ("get_air_quality", {"unit": "aqi", "city": "北京"}),
      ("get_weather", {"city": "北京"}),
    )
    assert match_expected(output, spec) == (True, "通过")

  def test_argument_mismatch_reports_expected_and_actual(self) -> None:
    """参数不完全相等即不通过，原因同时给出期望与实际（定位传错哪个值）。"""
    output = _agent_output(_tool_call("get_weather", {"city": "上海"}))
    passed, reason = match_expected(output, _agent_spec(("get_weather", {"city": "北京"})))
    assert passed is False
    assert "参数不匹配" in reason
    assert "期望={'city': '北京'}" in reason
    assert "上海" in reason

  def test_uncalled_tool_fails_with_tool_name(self) -> None:
    """断言指定的工具没被调用时不通过，原因点名该工具与实际调用清单。"""
    output = _agent_output(_tool_call("get_weather", {"city": "北京"}))
    passed, reason = match_expected(output, _agent_spec(("search_hotel", {"city": "北京"})))
    assert passed is False
    assert "未调用工具 'search_hotel'" in reason
    assert "get_weather" in reason

  def test_unparseable_output_raises_value_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
    """输出无法解释为 Agent 结果时一律 ValueError（不可判），不得压成不通过。

    覆盖四种不可解析形态：JSON 语法非法、JSON 顶层不是对象、tool_calls 条目缺
    ``function`` 对象、深嵌套触发 json 递归溢出（与 JsonMatcher 同口径收口）。
    """
    spec = _agent_spec(("get_weather", {"city": "北京"}))
    cases = [
      ("这不是 JSON", "不是合法 JSON"),
      ("[1, 2]", "不是合法 JSON"),
      ('{"tool_calls": ["不是对象"]}', "缺少 function 对象"),
    ]
    for bad_output, fragment in cases:
      with pytest.raises(ValueError) as excinfo:
        AgentMatcher().match(bad_output, spec)
      assert fragment in str(excinfo.value)

    def _boom(_raw: str) -> object:
      """模拟 json.loads 递归溢出。"""
      raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("aquamind.scoring.json.loads", _boom)
    with pytest.raises(ValueError) as excinfo:
      AgentMatcher().match('{"tool_calls": []}', spec)
    assert "嵌套过深" in str(excinfo.value)

  def test_invalid_arguments_json_raises_value_error(self) -> None:
    """arguments 不是合法 JSON 字符串时属不可判（ValueError），不是「参数不匹配」。"""
    output = json.dumps(
      {"tool_calls": [{"function": {"name": "get_weather", "arguments": "{不是 JSON"}}]}
    )
    with pytest.raises(ValueError) as excinfo:
      match_expected(output, _agent_spec(("get_weather", {"city": "北京"})))
    assert "arguments 不是合法 JSON" in str(excinfo.value)

  def test_missing_tool_calls_fails(self) -> None:
    """输出没有 tool_calls 键 = 没调用任何工具：判不通过，而不是不可判。"""
    passed, reason = match_expected('{"messages": []}', _agent_spec(("get_weather", {})))
    assert passed is False
    assert "未调用工具 'get_weather'" in reason
    assert "实际调用=[]" in reason

  def test_empty_assert_args_rejected(self) -> None:
    """assert_args 为空列表 = 没有参数断言，恒通过的空断言必须按用例写错拦下。"""
    with pytest.raises(ValueError) as excinfo:
      match_expected(_agent_output(), _spec("agent", {"assert_args": []}))
    assert "非空 assert_args" in str(excinfo.value)

  def test_invalid_value_shape_rejected(self) -> None:
    """value 形态非法一律 ValueError：非映射、缺键、条目缺 tool 或缺 arguments。"""
    output = _agent_output(_tool_call("get_weather", {"city": "北京"}))
    cases = [
      (["get_weather"], "非空 assert_args"),
      ({"order": ["get_weather"]}, "非空 assert_args"),
      ({"assert_args": ["get_weather"]}, "必须是映射"),
      ({"assert_args": [{"arguments": {"city": "北京"}}]}, "tool 必须是非空字符串"),
      ({"assert_args": [{"tool": "get_weather"}]}, "缺少 arguments 键"),
    ]
    for value, fragment in cases:
      with pytest.raises(ValueError) as excinfo:
        match_expected(output, _spec("agent", value))
      assert fragment in str(excinfo.value)

  def test_order_and_tools_keys_ignored(self) -> None:
    """order / tools 两个预留键今天被忽略：参数层判定不受影响（两层顺延 D31）。"""
    output = _agent_output(
      _tool_call("get_weather", {"city": "北京"}),
      _tool_call("get_air_quality", {"city": "北京"}),
    )
    spec = _agent_spec(
      ("get_weather", {"city": "北京"}),
      # 与实际调用顺序相反、且点名一个根本没调用的工具：今天都不校验
      order=["get_air_quality", "get_weather"],
      tools=["search_hotel"],
    )
    assert match_expected(output, spec) == (True, "通过")

  def test_deeply_nested_arguments_raises_value_error(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    """arguments 二次解析遇递归溢出必须收口为 ValueError（不可判），不得裸逃逸。

    arguments 是**第二次** ``json.loads``（顶层输出解析之外的第二条路径）：深嵌套的
    参数串同样会触发 ``RecursionError``，D21 在 JsonMatcher 收过这条漏洞，这里锁住
    Agent 侧的同一形态。用 monkeypatch 固定异常形态而非真实深嵌套——3.13/3.14 的 C
    解析器不受 ``sys.setrecursionlimit`` 约束（D20 已实测 2000 层可解析成功），真实
    深嵌套会让断言变成版本敏感。

    只让**第二次**解析溢出：第一次是顶层输出（`_parse_agent_output`，已有同族用例），
    若让第一次就溢出，本用例会在顶层就收口、根本走不到 arguments 那条路径。
    """
    real_loads = json.loads
    calls = {"count": 0}

    def _deep_on_second(raw: object) -> object:
      """顶层解析正常返回；arguments 二次解析时才模拟递归溢出。"""
      calls["count"] += 1
      if calls["count"] == 1:
        return real_loads(raw)  # type: ignore[arg-type]
      raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr("aquamind.scoring.json.loads", _deep_on_second)
    with pytest.raises(ValueError, match="嵌套过深") as excinfo:
      AgentMatcher().match(
        '{"tool_calls": [{"function": {"name": "get_weather", "arguments": "{}"}}]}',
        _agent_spec(("get_weather", {"city": "北京"})),
      )
    # 必须定位到 arguments 那一次解析，而不是顶层输出的同族错误
    assert "arguments" in str(excinfo.value)

  def test_extra_arguments_keys_fail(self) -> None:
    """参数比较是**完全相等**：实际比期望多出一个键也必须判不通过。

    这条锁死「不做部分匹配」的承诺——若把判据退化成子集匹配（期望的键都在实际里就算
    通过），「多传了参数」这个工具调用出错的主要形态会被静默放过，本用例会变绿。
    """
    output = _agent_output(_tool_call("get_weather", {"city": "北京", "unit": "aqi"}))
    passed, reason = match_expected(output, _agent_spec(("get_weather", {"city": "北京"})))
    assert passed is False
    assert "参数不匹配" in reason
    assert "期望={'city': '北京'}" in reason
    # 多出来的键必须出现在「实际」一侧，便于判断是参数写多了而不是写错了
    assert "unit" in reason

  def test_same_tool_called_twice_any_match_passes(self) -> None:
    """同一工具被调用多次时，任一次参数相等即通过（不校验调用序与调用次数）。

    锁死 docstring 的口径：调用序 / 次数属顺序层（顺延 D31），参数层只问「有没有一次
    参数完全对得上」。若判据退化成「所有同名调用都必须匹配」，本用例会从通过变不通过。
    """
    output = _agent_output(
      _tool_call("get_weather", {"city": "上海"}),
      _tool_call("get_weather", {"city": "北京"}),
    )
    assert match_expected(output, _agent_spec(("get_weather", {"city": "北京"}))) == (
      True,
      "通过",
    )


# 与 rubric_case 的 exact 期望逐字符相等：包装层不应改动判定结论
_PASSING_RESPONSE = "水面温度 21.5 摄氏度"


@pytest.fixture(name="rubric_case")
def fixture_rubric_case() -> TestCase:
  """最小 exact 用例（无条件权重、无标签），作为 rubric 包装测试的公共起点。"""
  return TestCase(
    input="现在的水温是多少？",
    expected=ExpectedSpec(type="exact", value=_PASSING_RESPONSE),
  )


class TestScoreResponse:
  """score_response：判定结论 → 完整 ``Score`` 的包装、权重归一化与缺字段策略。

  本类只锁「包装」相关的行为：判定语义本身由各匹配器类负责，故这里不重复覆盖
  exact / contains / regex 的判定细节，只验证 scorer 标识随之正确、异常不被吞。
  """

  def test_pass_yields_full_score(self, rubric_case: TestCase) -> None:
    """单条断言通过：score=1.0、passed=True、scorer 取 expected.type。"""
    score = score_response(_PASSING_RESPONSE, rubric_case)
    assert score.passed is True
    assert score.score == 1.0
    assert score.scorer == "exact"
    # 通过时说明沿用匹配器的固定口径，包装层不改写
    assert score.reason == "通过"

  def test_fail_yields_zero_score(self, rubric_case: TestCase) -> None:
    """单条断言不通过：score=0.0，reason 保留匹配器的差异定位。"""
    score = score_response("21.5", rubric_case)
    assert score.passed is False
    assert score.score == 0.0
    # 失败原因必须能回答「差在哪」，不能被包装层替换成通用文案
    assert "期望='" in score.reason
    assert "实际=" in score.reason

  def test_empty_weights_defaults_to_equal_weight(self, rubric_case: TestCase) -> None:
    """weights 为空 dict：走默认等权，且归一化结果为空映射（没有可分配的维度）。"""
    assert rubric_case.weights == {}
    metadata = score_response(_PASSING_RESPONSE, rubric_case).metadata
    assert metadata["default_equal_weight"] is True
    assert metadata["normalized_weights"] == {}

  def test_weights_normalized_to_unit_sum(self, rubric_case: TestCase) -> None:
    """2:1 的权重归一化为 2/3 : 1/3，原始比例可由元数据反推。"""
    case = rubric_case.model_copy(
      update={"weights": {"accuracy": 2.0, "completeness": 1.0}},
      deep=True,
    )
    metadata = score_response(_PASSING_RESPONSE, case).metadata
    assert metadata["default_equal_weight"] is False
    normalized = metadata["normalized_weights"]
    assert normalized["accuracy"] == pytest.approx(2.0 / 3.0)
    assert normalized["completeness"] == pytest.approx(1.0 / 3.0)
    assert sum(normalized.values()) == pytest.approx(1.0)

  def test_all_zero_weights_fall_back_to_equal_weight(self, rubric_case: TestCase) -> None:
    """全零权重无法给出相对比例：退回等权，而不是产出 0/0 或全零份额。"""
    case = rubric_case.model_copy(update={"weights": {"a": 0.0, "b": 0.0}}, deep=True)
    metadata = score_response(_PASSING_RESPONSE, case).metadata
    assert metadata["default_equal_weight"] is True
    assert metadata["normalized_weights"] == {"a": 0.5, "b": 0.5}

  def test_partial_zero_weight_is_not_default(self, rubric_case: TestCase) -> None:
    """显式声明的 0 权重在总和 > 0 时被保留：0 是配置，不等于「未声明」。"""
    case = rubric_case.model_copy(
      update={"weights": {"accuracy": 0.0, "completeness": 2.0}}, deep=True
    )
    metadata = score_response(_PASSING_RESPONSE, case).metadata
    assert metadata["default_equal_weight"] is False
    assert metadata["normalized_weights"] == {"accuracy": 0.0, "completeness": 1.0}

  def test_unclaimed_weights_records_declared_but_unconsumed(
    self, rubric_case: TestCase
  ) -> None:
    """缺字段策略：单条断言下所有声明维度都未被消费，须记录而非报错。"""
    case = rubric_case.model_copy(
      update={"weights": {"accuracy": 1.0, "completeness": 0.5}}, deep=True
    )
    # 不抛异常是策略的一部分：这是配置诊断，不是用例错误
    score = score_response(_PASSING_RESPONSE, case)
    # 按名字排序，便于与用例文件逐条比对
    assert score.metadata["unclaimed_weights"] == ["accuracy", "completeness"]

  def test_matched_tags_registry_recorded(self, rubric_case: TestCase) -> None:
    """标签口径：expected_tags 记录已注册标签，matched_tags 当前恒为空。"""
    case = rubric_case.model_copy(
      update={"score_tags": [ScoreTag(name="accuracy", weight=1.0)]}, deep=True
    )
    score = score_response(_PASSING_RESPONSE, case)
    assert score.metadata["expected_tags"] == ["accuracy"]
    # 匹配器不产出标签：命中清单为空是现状，不是漏记
    assert score.matched_tags == []
    assert score.metadata["matched_tags"] == []

  def test_value_error_propagates(self, rubric_case: TestCase) -> None:
    """匹配器的 ValueError（未注册类型 / 用例写错）必须原样上抛，不被吞成 0 分。"""
    # judge 是契约层合法取值、但尚无确定性匹配器：走 match_expected 的未注册分支
    unregistered = rubric_case.model_copy(
      update={"expected": {"type": "judge", "value": "随便"}}, deep=True
    )
    with pytest.raises(ValueError, match="未注册的期望类型"):
      score_response("随便", unregistered)

    # 用例侧错误（空正则）同属不可判，同样不得压成分数
    broken = rubric_case.model_copy(
      update={"expected": {"type": "regex", "value": ""}}, deep=True
    )
    with pytest.raises(ValueError, match="正则"):
      score_response("随便", broken)

  def test_score_object_is_complete(self, rubric_case: TestCase) -> None:
    """返回的 Score 六字段齐备，metadata 至少含三个约定键。"""
    case = rubric_case.model_copy(
      update={
        "weights": {"accuracy": 1.0},
        "score_tags": [ScoreTag(name="accuracy")],
      },
      deep=True,
    )
    score = score_response(_PASSING_RESPONSE, case)
    # Score 是 frozen + extra=forbid：这里既能读全字段，也证明构造走了完整校验
    assert score.scorer == "exact"
    assert score.reason != ""
    assert score.passed is True
    assert 0.0 <= score.score <= 1.0
    assert {"normalized_weights", "unclaimed_weights", "expected_tags"} <= set(score.metadata)

  @pytest.mark.parametrize(
    ("type_name", "value", "response"),
    [
      ("exact", "21.5", "21.5"),
      ("contains", "21.5", "当前水温 21.5 摄氏度"),
      ("regex", r"21\.\d", "当前水温 21.5 摄氏度"),
    ],
  )
  def test_scorer_follows_expected_type(
    self, type_name: ExpectedType, value: str, response: str
  ) -> None:
    """三类断言各自通过时 scorer 均等于 expected.type（与 Score.scorer 描述对齐）。"""
    case = TestCase(input="x", expected=ExpectedSpec(type=type_name, value=value))
    score = score_response(response, case)
    assert score.passed is True
    assert score.scorer == type_name

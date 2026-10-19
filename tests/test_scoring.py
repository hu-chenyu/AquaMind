"""精确匹配评分器单元测试。

覆盖范围：
- 六类匹配器（exact / regex / contains / excludes / keywords / json）的通过与失败
  分支，以及「用例写错」（空子串、非法正则、非法 JSON、期望值类型不对）时必须抛
  ``ValueError`` 而非返回 False；
- 边界加固（D21 收尾）：None 期望值、空正则、灾难性回溯预检、JSON 路径键非字符串、
  深嵌套 JSON 的 ``RecursionError`` 收口；
- ``match_expected`` 按 type 分发、未注册类型的防御性报错、``tolerance`` 契约预留；
- 输入侧三模型（TestCase / ExpectedSpec / ScoreTag）的 ``model_copy(update=...)``
  收口：带 update 走完整校验，越界值/未知字段/非法嵌套一律被拒；
- 契约层 ``ExpectedType`` 的七类合法取值与非法取值拦截。

全部用例纯本地判定，不发起任何网络调用（零 key）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aquamind.models.testcase import ExpectedSpec, ExpectedType, ScoreTag, TestCase
from aquamind.scoring import (
  ContainsMatcher,
  ExactMatcher,
  ExcludesMatcher,
  JsonMatcher,
  KeywordsMatcher,
  RegexMatcher,
  match_expected,
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
    [r"(a+)+b", r"(a*)*", r"((a+))+", r"(?:a+)+", r"(x|a+)+$"],
  )
  def test_regex_catastrophic_backtracking_detected(self, pattern: str) -> None:
    """组内含 * / + 且组后被无上界重复：必须在用例执行前拒绝。"""
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

"""精确匹配评分器：把 ``ExpectedSpec`` 翻译成对模型输出的一次确定性判定。

本模块只做**确定性断言**——判定规则与期望值都随用例预注册在文件里，评分过程不含
任何模型调用，故同一输入必然得到同一结论（可复算、零 key）。判定结果统一为
``(是否通过, 说明)``：通过时说明固定为「通过」，失败时说明必须能回答「差在哪」。

与 ``models/testcase.py`` 的分工：契约层只用 ``Literal`` 声明 type 的合法取值集合，
本模块负责每种 type 的判定语义。两层都不能省——契约层管「用例文件能不能读进来」，
本模块管「读进来的期望怎么判」。

失败原因与用例写错是两件事，本模块严格区分：
- 判定不通过 → 返回 ``(False, 原因)``，属于**被测应用的表现**；
- 期望值本身非法（空子串、非法正则、非法 JSON 路径映射等）→ 抛 ``ValueError``，
  属于**用例写错了**；期望值的**类型**就不对（如 keywords 传了数字）→ 抛
  ``TypeError``。若把这两类混进「不通过」，通过率统计会被用例错误污染。

``judge`` 类型需真实模型调用（M2-D07），不属确定性评分器，故未注册；``match_expected``
遇到它会明确报「未注册」，而不是静默给出一个假分数。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .models.testcase import ExpectedSpec, ExpectedType

# 通过时的固定说明：调用方无需区分「哪种匹配器通过」，需要细节时应看失败分支
_PASS = "通过"


def _require_non_empty_text(value: object, type_name: str) -> str:
  """把期望值转成非空字符串，空串视为非法用例。

  ``contains`` 的空串断言恒为真、``excludes`` 的空串断言恒为假——两者都不含任何
  被测应用的信息，只会让通过率虚高或虚低，故在判定前就拦下。

  Args:
    value: 用例里给出的期望值（任意类型，统一 str 化后比较）。
    type_name: 期望类型名，仅用于错误消息里指出是哪一类断言写错了。

  Returns:
    str: 期望值转成的非空字符串（不做 strip，保留原文）。

  Raises:
    ValueError: 期望值为空字符串时抛出。
  """
  text = str(value)
  if not text:
    raise ValueError(f"{type_name} 的期望值不能为空字符串：空串断言不含任何被测信号")
  return text


def _keyword_list(value: object) -> list[str]:
  """把 ``keywords`` 的期望值归一为关键词列表。

  支持两种写法：关键词列表（``["a", "b"]``）与逗号分隔字符串（``"a,b"``）——后者
  是为了让 YAML 用例不必为几个词写出嵌套结构。逗号两侧空白会被去掉，词内空白保留
  （「关键词」按原样匹配，不做分词或折叠）。

  Args:
    value: 用例里给出的期望值。

  Returns:
    list[str]: 去空白后的关键词列表，顺序与用例一致。

  Raises:
    TypeError: 期望值的**类型**既不是列表/元组也不是字符串。
    ValueError: 归一后为空列表，或含空白项（内容非法）。
  """
  if isinstance(value, str):
    keywords = [item.strip() for item in value.split(",")]
  elif isinstance(value, (list, tuple)):
    keywords = [str(item).strip() for item in value]
  else:
    # 类型不对抛 TypeError（Python 惯例）：调用方拿到 dict/数字当关键词属于传参错误，
    # 与「关键词内容为空」的 ValueError 分开，便于定位是 schema 写错还是用例漏写
    raise TypeError(f"keywords 的期望值必须是关键词列表或逗号分隔字符串: {value!r}")
  if not keywords or any(not keyword for keyword in keywords):
    # 空列表无法表达任何断言；含空白项多半是逗号写多了（"a,,b"），静默忽略会让
    # 「写了三个词、实际只查两个」这种情况无人发现
    raise ValueError(f"keywords 的关键词列表不能为空或含空白项: {value!r}")
  return keywords


def _resolve_path(document: Any, path: str) -> tuple[bool, Any]:
  """按点号路径在已解析的 JSON 文档中取值。

  路径语法刻意保持最小：``a.b.c`` 逐层进入对象，``items.0.name`` 中的纯数字段表示
  列表下标（只接受非负下标，负下标在 YAML/JSON 用例里易与「倒数第几个」混淆，
  不做隐式支持）。路径走不通（键不存在、下标越界、中途碰到标量）一律返回
  ``(False, None)``，由调用方汇总为「路径不存在」而不是抛异常。

  Args:
    document: ``json.loads`` 解析后的文档根节点。
    path: 点号分隔的路径。

  Returns:
    tuple[bool, Any]: ``(True, 取到的值)`` 或 ``(False, None)``。
  """
  current: Any = document
  for segment in path.split("."):
    if isinstance(current, Mapping):
      if segment not in current:
        return False, None
      current = current[segment]
      continue
    if isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
      current = current[int(segment)]
      continue
    return False, None
  return True, current


class ExactMatcher:
  """精确相等匹配器：输出与期望值逐字符相等才通过。

  语义：比较前把 ``spec.value`` str 化（用例里 ``value: 100`` 与 ``value: "100"``
  视为同一期望），但不做 strip、不做大小写折叠——「精确」就是一个字符都不许差。
  失败时同时给出期望与实际输出，便于判断是模型多说了半句还是整段跑偏。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """判定输出是否与期望完全相等。

    Args:
      response: 被测应用返回的原始输出文本。
      spec: 期望规范，``spec.value`` 为期望输出。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 失败原因)``。
    """
    expected = str(spec.value)
    if response == expected:
      return True, _PASS
    return False, f"输出与期望不相等: 期望={expected!r}, 实际={response!r}"


class RegexMatcher:
  """正则匹配器：``re.search`` 在输出中命中即通过（不要求整串匹配）。

  语义：用 ``re.search`` 而非 ``re.fullmatch``——断言的是「输出里是否出现了符合
  模式的内容」，前后带别的文字不影响结论；需要整串匹配时把模式写成 ``^...$``。
  失败原因：未命中给出模式与实际输出；模式本身非法则抛 ``ValueError``（用例写错，
  不是被测应用不通过，必须与判定失败分开，否则通过率会被用例错误污染）。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """用期望正则搜索输出。

    Args:
      response: 被测应用返回的原始输出文本。
      spec: 期望规范，``spec.value`` 为正则表达式字符串。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 失败原因)``。

    Raises:
      ValueError: ``spec.value`` 无法编译为正则表达式时抛出。
    """
    pattern_text = str(spec.value)
    try:
      pattern = re.compile(pattern_text)
    except re.error as exc:
      raise ValueError(f"非法正则表达式: {pattern_text!r}（{exc}）") from exc
    if pattern.search(response) is not None:
      return True, _PASS
    return False, f"输出未匹配正则: 模式={pattern_text!r}, 实际={response!r}"


class ContainsMatcher:
  """子串包含匹配器：输出中出现期望子串即通过（大小写敏感）。

  语义：纯子串查找，不解释任何元字符——``"a.c"`` 只匹配字面量 ``a.c``，要正则
  语义请用 ``regex``。大小写敏感是刻意口径：中文场景不受影响，而英文场景里
  「Hello」与「hello」是否等价应由用例明确（需要忽略大小写时写 ``regex`` 加
  ``(?i)``），不在评分器里替调用方猜。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """判定输出是否包含期望子串。

    Args:
      response: 被测应用返回的原始输出文本。
      spec: 期望规范，``spec.value`` 为期望出现的子串。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 失败原因)``。

    Raises:
      ValueError: 期望值为空字符串时抛出（空串恒被包含，判定无信息量）。
    """
    expected = _require_non_empty_text(spec.value, "contains")
    if expected in response:
      return True, _PASS
    return False, f"输出未包含期望子串: 期望={expected!r}, 实际={response!r}（大小写敏感）"


class ExcludesMatcher:
  """子串排除匹配器：输出中出现被排除子串即失败。

  语义：``contains`` 的反面，用于「不该说的话」这类断言——例如要求模型不给诊断
  结论、不出现某段内部代号、不复述敏感信息。命中即失败，且失败原因里点名命中了
  哪个子串，便于回溯是哪句输出越界。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """判定输出是否**不**包含被排除的子串。

    Args:
      response: 被测应用返回的原始输出文本。
      spec: 期望规范，``spec.value`` 为不应出现的子串。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 命中了排除子串的原因)``。

    Raises:
      ValueError: 被排除子串为空字符串时抛出（空串恒被命中，判定无信息量）。
    """
    forbidden = _require_non_empty_text(spec.value, "excludes")
    if forbidden in response:
      return False, f"输出命中排除子串: 禁用={forbidden!r}, 实际={response!r}"
    return True, _PASS


class KeywordsMatcher:
  """关键词匹配器：全部关键词都出现才通过（AND 语义）。

  语义：AND 而非 OR——用例列出的每一个词都是必要条件，任一缺失即失败，失败原因
  列出**缺失的具体词**。之所以不做「命中 N 个即通过」的宽松口径：那种口径下
  「N 取多少」会变成隐式阈值，判定结论就不再能由用例独立复算。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """判定输出是否命中全部关键词。

    Args:
      response: 被测应用返回的原始输出文本。
      spec: 期望规范，``spec.value`` 为关键词列表或逗号分隔字符串。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 缺失关键词清单)``。

    Raises:
      ValueError: 期望值无法归一为非空、无空白项的关键词列表时抛出。
    """
    keywords = _keyword_list(spec.value)
    missing = [keyword for keyword in keywords if keyword not in response]
    if missing:
      return False, (
        f"输出缺少关键词: {missing}（共 {len(keywords)} 个，AND 语义须全部命中；"
        f"实际={response!r}）"
      )
    return True, _PASS


class JsonMatcher:
  """JSON 断言匹配器：解析输出后按「路径 → 期望值」逐条断言相等。

  语义：输出必须是合法 JSON 文本（整体解析，不做「抓取第一个花括号」这类容错——
  容错会掩盖模型多说话的问题）；``spec.value`` 为映射，键是 ``a.b.c`` / ``items.0``
  形式的路径，值是期望值，全部相等才通过。列表整体也可直接比较（JSON 数组与
  Python list 值相等即视为一致）。
  失败原因：未成立的路径逐条列出（路径不存在 / 值不匹配），一次给出全貌而不是
  遇到第一条就返回，减少「改一条跑一次」的往返。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """判定输出的 JSON 是否满足全部路径断言。

    Args:
      response: 被测应用返回的原始输出文本（须为合法 JSON）。
      spec: 期望规范，``spec.value`` 为「路径 → 期望值」的非空映射。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 未成立的断言清单)``。

    Raises:
      ValueError: 输出不是合法 JSON，或 ``spec.value`` 不是非空映射时抛出。
    """
    try:
      document = json.loads(response)
    except json.JSONDecodeError as exc:
      raise ValueError(f"输出不是合法 JSON: {exc.msg}（位置 {exc.pos}）") from exc
    assertions = spec.value
    if not isinstance(assertions, Mapping) or not assertions:
      raise ValueError(f"json 的期望值必须是「路径 → 期望值」的非空映射: {assertions!r}")
    mismatches: list[str] = []
    for path, expected in assertions.items():
      found, actual = _resolve_path(document, path)
      if not found:
        mismatches.append(f"路径 {path!r} 不存在")
      elif actual != expected:
        mismatches.append(f"路径 {path!r} 不匹配: 期望={expected!r}, 实际={actual!r}")
    if mismatches:
      return False, "JSON 断言未全部成立: " + "; ".join(mismatches)
    return True, _PASS


# type → 匹配入口的唯一映射：新增类型只改这一处（与契约层 ExpectedType 同步）。
# 存绑定方法而非实例，是为了让调用方无需再实例化；judge 有意缺席（需真实模型调用）。
_MATCHERS: dict[ExpectedType, Callable[[str, ExpectedSpec], tuple[bool, str]]] = {
  "exact": ExactMatcher().match,
  "regex": RegexMatcher().match,
  "contains": ContainsMatcher().match,
  "excludes": ExcludesMatcher().match,
  "keywords": KeywordsMatcher().match,
  "json": JsonMatcher().match,
}


def match_expected(response: str, spec: ExpectedSpec) -> tuple[bool, str]:
  """按 ``spec.type`` 分发到对应匹配器，返回判定结论与说明。

  这是评分器对外的唯一入口：批量执行器只需拿到 ``(是否通过, 说明)``，不必关心
  具体是哪一类断言、更不必逐个 if/elif 分支（分支写漏的类型会静默永远不通过）。

  Args:
    response: 被测应用返回的原始输出文本。
    spec: 期望规范，``spec.type`` 决定使用哪个匹配器。

  Returns:
    tuple[bool, str]: ``(True, "通过")``，或 ``(False, 失败原因)``。

  Raises:
    ValueError: ``spec.type`` 未注册（``judge`` 尚未接线；或绕过契约校验构造出的
      未知类型——pydantic 在正常路径已拦截，此处为防御性兜底）。
  """
  matcher = _MATCHERS.get(spec.type)
  if matcher is None:
    # 不静默返回 False：未知类型既不是「被测应用不通过」，也不该被计入通过率
    raise ValueError(f"未注册的期望类型: {spec.type!r}（当前支持 {sorted(_MATCHERS)}）")
  return matcher(response, spec)

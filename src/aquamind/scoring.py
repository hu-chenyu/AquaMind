"""精确匹配评分器：把 ``ExpectedSpec`` 翻译成对模型输出的一次确定性判定。

本模块只做**确定性断言**——判定规则与期望值都随用例预注册在文件里，评分过程不含
任何模型调用，故同一输入必然得到同一结论（可复算、零 key）。判定结果统一为
``(是否通过, 说明)``：通过时说明固定为「通过」，失败时说明必须能回答「差在哪」。

与 ``models/testcase.py`` 的分工：契约层只用 ``Literal`` 声明 type 的合法取值集合，
本模块负责每种 type 的判定语义。两层都不能省——契约层管「用例文件能不能读进来」，
本模块管「读进来的期望怎么判」。

判定结果与异常是两件事，本模块严格区分四类出口：

1. **判定不通过** → 返回 ``(False, 原因)``，属于**被测应用的表现**。``(bool, str)``
   只表达「通过 / 不通过」，不承载第三种状态；
2. **用例写错** → 抛 ``ValueError``：空期望串、空正则、非法正则、期望值形态不对
   （如 json 断言集不是映射）、路径键不是字符串；
3. **不可判（UNJUDGEABLE）** → 同样抛 ``ValueError``：输出侧无法按确定性规则解析
   （非法 JSON、JSON 嵌套过深触发 ``RecursionError``）。它的成因在**输出**而非用例，
   但与「用例写错」共用同一出口，因为两者都**不能**被计成通过或不通过——压成
   ``False`` 会让通过率虚低，压成 ``True`` 会让它虚高。上层批量执行器应把本模块的
   ``ValueError`` 记为不可判（与 README 的 UNJUDGEABLE 口径一致），而不是计入失败样本；
4. **期望值类型不对** → 抛 ``TypeError``（如 keywords 传了数字），属调用方把结构拼错了，
   与「内容非法」分开才便于定位。

确定性匹配器共八类：六类基础匹配器（exact / regex / contains / excludes / keywords /
json），加上 D22 新增的 ``all``（结构化断言组合）与 ``agent``（Agent 工具调用断言）。
``judge`` 类型需真实模型调用（M2-D07），不属确定性评分器，故未注册；``match_expected``
遇到它会明确报「未注册」，而不是静默给出一个假分数。

``ExpectedSpec.tolerance``（数值容差）当前**不参与**任何判定：八类确定性匹配器全部
精确比较（``all`` / ``agent`` 同样不消费它），容差留给数值型断言使用（exact 数值比较、
json 数值路径比较）。接线点集中在 ``match_expected``——未来由它按 type 把 tolerance
传给数值型匹配器，而不是散落进各匹配器的私有逻辑。

``all`` 通过递归调用 ``match_expected`` 判定每个子断言，因此天然支持嵌套组合；子断言
抛出的 ``ValueError``（含非法 JSON 这类不可判）一律**继续向上传播**，不被压成「不通过」
——组合里只要有一条判不了，整个组合就判不了。``agent`` 今天只实现三层断言里的参数层
（``assert_args``），顺序层（``order``）与工具层（``tools``）顺延 D31。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from .models.testcase import ExpectedSpec, ExpectedType

# 通过时的固定说明：调用方无需区分「哪种匹配器通过」，需要细节时应看失败分支
_PASS = "通过"


def _require_text(value: object, type_name: str) -> str:
  """把期望值转成字符串；None 视为用例写错，直接拦下。

  ``str(None)`` 会得到字面量 ``"None"``：用例里写 ``value: null``（YAML 把空值
  解析成 None）时，断言会悄悄变成「输出里要有 None 这几个字母」，于是
  ``return None`` 这类输出被误判为通过。None 没有任何文本语义，必须在判定前拒绝，
  而不是经一次隐式 str 化变成看似合法的期望值。

  Args:
    value: 用例里给出的期望值。
    type_name: 期望类型名，仅用于错误消息里指出是哪一类断言写错了。

  Returns:
    str: 期望值转成的字符串（不做 strip，保留原文）。

  Raises:
    ValueError: 期望值为 None 时抛出。
  """
  if value is None:
    raise ValueError(f"{type_name} 的期望值不能为 None：str(None) 会变成字面量 'None'")
  return str(value)


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
    ValueError: 期望值为 None 或空字符串时抛出。
  """
  text = _require_text(value, type_name)
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
    TypeError: 期望值的**类型**既不是列表/元组也不是字符串（None 除外，见下）。
    ValueError: 期望值为 None，或归一后为空列表/含空白项（内容非法）。
  """
  if value is None:
    # None 走 ValueError 而非 TypeError：它不是「类型拼错」，而是用例里把值留空了，
    # 与空列表同属「内容没写」，统一归入「用例写错」这一出口
    raise ValueError("keywords 的期望值不能为 None：空值无法表达任何关键词断言")
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


def _path_segments(path: object) -> list[str]:
  """把 JSON 断言路径键拆成点号分隔的段；非字符串键直接判为用例写错。

  YAML 里写 ``value: {1: x}`` 会把键解析成 int，而 int 没有 ``split``——若放任
  到 ``_resolve_path`` 内部再调用，抛出的是裸 ``AttributeError``，穿透本模块
  「用例写错一律 ValueError」的契约，调用方无法按类型分流处理。

  Args:
    path: 用例给出的路径键（任意类型）。

  Returns:
    list[str]: 路径段列表。

  Raises:
    ValueError: 路径键不是字符串时抛出，消息里带实际类型名。
  """
  if isinstance(path, str):
    return path.split(".")
  raise ValueError(f"JSON 断言路径必须为字符串，实际类型: {type(path).__name__}")


def _resolve_path(document: Any, path: object) -> tuple[bool, Any]:
  """按点号路径在已解析的 JSON 文档中取值。

  路径语法刻意保持最小：``a.b.c`` 逐层进入对象，``items.0.name`` 中的纯数字段表示
  列表下标（只接受非负下标，负下标在 YAML/JSON 用例里易与「倒数第几个」混淆，
  不做隐式支持）。路径走不通（键不存在、下标越界、中途碰到标量）一律返回
  ``(False, None)``，由调用方汇总为「路径不存在」而不是抛异常。

  Args:
    document: ``json.loads`` 解析后的文档根节点。
    path: 点号分隔的路径（字符串，非字符串由 ``_path_segments`` 拦下）。

  Returns:
    tuple[bool, Any]: ``(True, 取到的值)`` 或 ``(False, None)``。

  Raises:
    ValueError: 路径键不是字符串时抛出。
  """
  current: Any = document
  for segment in _path_segments(path):
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

    Raises:
      ValueError: 期望值为 None 时抛出（``str(None)`` 会变成字面量 ``"None"``）。
    """
    expected = _require_text(spec.value, "exact")
    if response == expected:
      return True, _PASS
    return False, f"输出与期望不相等: 期望={expected!r}, 实际={response!r}"


# 灾难性回溯（ReDoS）预检用的三条归一化正则 + 一条匹配正则。它们自身只含字符类、
# 单层分组与单层量词（无嵌套量词），最坏复杂度是多项式级，不会出现「用 ReDoS 正则
# 检测 ReDoS」的自伤；输入也只是用例里的模式串（通常几十个字符）。
_NON_CAPTURING_PREFIX = re.compile(r"\(\?:")
_ESCAPE_SEQUENCE = re.compile(r"\\.")
_CHARACTER_CLASS = re.compile(r"\[[^\]]*\]")
# 「组内可产生多种等长拆分方式、组后又是无上界重复」的四类形态（详见
# _detect_catastrophic_backtracking 的 docstring）：无上界量词嵌套、嵌套组内
# 无上界量词、交替冗余（平面）、交替冗余（嵌套）。前瞻/后顾组
# （(?= (?<= (?<! (?!）不是消费性分组，不参与判定。
#
# 组体前两条分支只认 `*` 与 `+`，**不认单独的 `?`**：`?` 是 0 或 1 次的有界量词，
# 纯 `?` 形态（如 `(ab?c)+`、`((a?))+`）一次迭代只消耗定长字符，回溯逐次线性收缩；
# 把它算进危险量词会误伤这类安全模式（复核 A 实测）。但 `?` 与交替 `|` 组合时不是
# 这样：`(a|a?)+` 的两条分支能消耗相同字符、形成冗余路径，零宽保护剪不掉，仍是
# 2^n（复核 B 实测 `(a|a?)+b` 在 24 个 a 上耗时 20s），故由分支三/四用
# 「`|` + 任意量词」的**联合条件**单独覆盖——不能简单回退成 `[*+?]`。
_NESTED_QUANTIFIER = re.compile(
  r"\((?!\?[=!<])"
  # 分支一：组体内直接含无上界量词（(a+)+）
  # 分支二：组体内含带无上界量词的子组（((a+))+）
  # 分支三：组体内含交替 | 且含任意量词（(a|a?)+ / (a|b+)+）
  # 分支四：组体内含「带交替 | 且带任意量词」的子组（((a|a?))+）
  r"(?:"
  r"[^()]*[*+][^()]*"
  r"|\([^()]*[*+][^()]*\)"
  r"|[^()]*\|[^()]*[*+?][^()]*"
  r"|\([^()]*\|[^()]*[*+?][^()]*\)"
  r")"
  r"\)\s*(?:[*+]|\{\d+,\})"
)


def _detect_catastrophic_backtracking(pattern: str) -> str | None:
  """预检灾难性回溯（ReDoS）风险；命中时返回危险片段。

  判定规则是启发式的：**组内存在多种等长拆分方式、组后又是无上界重复**
  （``*`` / ``+`` / ``{m,}``）时，匹配失败的路径会把同一段输入按不同方式反复拆分，
  尝试次数随输入长度指数增长——经典 ``(a+)+b`` 在 24 个 ``a`` 上就要 1 秒以上。
  正则来自用例作者时属于自伤，但加载第三方数据集时可被用来拖死整批执行，故在编译
  前拦下。这里不做超时线程：判定必须可复现，超时会把「慢」变成与环境相关的结论。

  四条检测分支各自覆盖的形态：

  1. **无上界量词嵌套**：组体内含 ``*`` / ``+``（``(a+)+``、``(a*)*``、``(x|a+)+``）
     ——内层量词能按任意长度拆分输入，外层再重复一遍，组合数指数增长；
  2. **嵌套组内无上界量词**：组体内含一个带 ``*`` / ``+`` 的子组（``((a+))+``、
     ``((x|a+))+``）——同一机制隔一层括号，指数量词不直接出现在最外层组体内；
  3. **交替冗余（平面）**：组体内含 ``|`` 且含任意量词（``*`` / ``+`` / ``?``），
     如 ``(a|a?)+``、``(a|ab?)+``——两条分支能消耗相同字符，形成等长的冗余路径；
  4. **交替冗余（嵌套）**：组体内含一个「带 ``|`` 且带任意量词」的子组，如
     ``((a|a?))+``——机制同分支三，只是隔了一层括号（实测 ``((a|a?))+b`` 在 20 个
     ``a`` 上 0.73s，呈 2^n）。

  ``?`` 的口径要说准：**纯 ``?`` 形态**（组内无交替，如 ``(a?){2,}``、``(ab?c)+``、
  ``(\\d\\s?)+``、``((a?))+``）每次迭代只消耗 0 或 1 个字符、组整体长度有界，失败时
  逐次收缩即可，复杂度是线性的，Python 的 re 还有零宽重复保护——故不触发检测（把
  ``?`` 一律算作危险量词会误伤这些线性模式，复核 A 实测）。但 ``?`` 一旦与交替并存
  就落入分支三/四：冗余路径不是「长度拆分」而是「同长不同路」，零宽保护无从剪枝。
  分支四的条件是 ``|`` 与量词的**联合**，而非把分支二回退成 ``[*+?]``——后者会把
  ``((a?))+`` 这类纯 ``?`` 嵌套误拦。

  分支三/四刻意采用**保守策略**：只要组内同时出现 ``|`` 与任意量词就判定危险，
  不区分分支是否真的重叠。因此互斥但带量词的线性模式（如 ``(x|y?)+``）会被误拦；
  作者可改写为 ``(x|y)+`` 或字符类 ``[xy]+`` 规避（``(a?|b)+`` 这类量词在 ``|``
  之前的写法则不在预检覆盖内，属已知缺口而非误伤）。

  已知未覆盖（均为启发式边界，不阻塞使用）：
  - ``(a|ab)+`` 类**前缀重叠交替但组内无量词**形态：等长冗余确实存在（``a`` 与
    ``ab`` 前缀重叠），但不含任何 ``*`` / ``+`` / ``?``，不在本预检范围；
  - ``((a+)(b+))+`` 类**多子组各带无上界量词**形态：分支二/四只匹配单个子组；
  - 三层及以上括号嵌套、回溯引用（``\\1``）等更复杂结构；
  - 分支互斥的交替（``(foo|bar)+``）本身线性，属「正确放行」而非缺口。

  预检前先做三步归一化，避免误判无害写法：
  - ``(?:`` 视作 ``(``：非捕获组与捕获组的回溯特性相同；
  - 去掉转义序列：``\\(a+\\)+`` 匹配的是字面括号，不是分组；
  - 去掉字符类：``([*])+`` 里的 ``*`` 是字面量，不是量词。

  Args:
    pattern: 待编译的正则表达式原文。

  Returns:
    str | None: 命中的危险片段（错误消息据此指名）；无风险时为 None。
  """
  view = _NON_CAPTURING_PREFIX.sub("(", pattern)
  view = _ESCAPE_SEQUENCE.sub("", view)
  view = _CHARACTER_CLASS.sub("", view)
  found = _NESTED_QUANTIFIER.search(view)
  return found.group(0) if found is not None else None


class RegexMatcher:
  """正则匹配器：``re.search`` 在输出中命中即通过（不要求整串匹配）。

  语义：用 ``re.search`` 而非 ``re.fullmatch``——断言的是「输出里是否出现了符合
  模式的内容」，前后带别的文字不影响结论；需要整串匹配时把模式写成 ``^...$``。
  编译前有两道用例侧拦截：空模式恒命中（与 contains/excludes 的空串口径对齐）、
  嵌套量词形态有灾难性回溯风险。失败原因：未命中给出模式与实际输出；模式本身非法
  则抛 ``ValueError``（用例写错，不是被测应用不通过，必须与判定失败分开，否则
  通过率会被用例错误污染）。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """用期望正则搜索输出。

    Args:
      response: 被测应用返回的原始输出文本。
      spec: 期望规范，``spec.value`` 为正则表达式字符串。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 失败原因)``。

    Raises:
      ValueError: ``spec.value`` 为 None、为空串、存在灾难性回溯风险，或无法编译
        为正则表达式时抛出。
    """
    pattern_text = _require_text(spec.value, "regex")
    if not pattern_text:
      raise ValueError("正则表达式不能为空：空模式恒命中任一输出，判定无信息量")
    risky = _detect_catastrophic_backtracking(pattern_text)
    if risky is not None:
      raise ValueError(f"正则存在灾难性回溯风险，请简化: {risky!r}")
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
  解析侧有两类「不可判」：语法非法（带位置）与嵌套过深（``json.loads`` 递归溢出）。
  后者在 D20 已在加载器三路径上踩过——它同样是**输出不可判**，不能让裸
  ``RecursionError`` 穿透本模块的异常契约。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """判定输出的 JSON 是否满足全部路径断言。

    Args:
      response: 被测应用返回的原始输出文本（须为合法 JSON）。
      spec: 期望规范，``spec.value`` 为「路径 → 期望值」的非空映射。

    Returns:
      tuple[bool, str]: ``(True, "通过")``，或 ``(False, 未成立的断言清单)``。

    Raises:
      ValueError: 输出不是合法 JSON、嵌套过深无法解析，或 ``spec.value`` 不是非空
        映射、路径键不是字符串时抛出。
    """
    try:
      document = json.loads(response)
    except json.JSONDecodeError as exc:
      raise ValueError(f"输出不是合法 JSON: {exc.msg}（位置 {exc.pos}）") from exc
    except RecursionError as exc:
      # 不带上原始异常文本：各版本 json 的递归错误措辞不同（「while decoding a
      # JSON array」等），统一成稳定口径便于上层按「不可判」归类
      raise ValueError("JSON 嵌套过深，无法解析") from exc
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


# ---------------------------------------------------------------------------
# D22：结构化断言组合（all）与 Agent 工具调用断言（agent）
# ---------------------------------------------------------------------------


def _sub_specs(value: object) -> list[ExpectedSpec]:
  """把 ``all`` 的期望值归一为已校验的非空子断言列表。

  ``value`` 必须是列表（也接受元组，便于程序内构造用例），元素是 ``ExpectedSpec``
  形态的映射（含 type / value / tolerance）。元素走 ``model_validate`` 而非直接信任：
  坏子断言（未知 type、负容差、拼错字段、根本不是映射）会在进入判定前被 pydantic
  拦下。``ValidationError`` 本身是 ``ValueError`` 的子类，故对外口径与模块其余
  「用例写错」出口一致，不需要再包一层——包了反而丢掉 pydantic 给出的字段级定位。

  Args:
    value: ``all`` 断言的期望值。

  Returns:
    list[ExpectedSpec]: 已校验的子断言列表，顺序与用例一致。

  Raises:
    ValueError: ``value`` 不是列表/元组，或归一后为空列表时抛出（空组合恒通过，
      不含任何被测信号）。
    ValidationError: 某个子断言不符合 ``ExpectedSpec`` 契约时抛出（ValueError 子类）。
  """
  if isinstance(value, (list, tuple)):
    specs = [ExpectedSpec.model_validate(item) for item in value]
    if not specs:
      raise ValueError("all 的子断言列表不能为空：空组合恒通过，判定无信息量")
    return specs
  # 走到这里说明 value 的类型就不对：与 _path_segments 同写法（正向分支先返回，
  # 报错放在 isinstance 之外），既避开 TRY004 的「类型错应抛 TypeError」口径，
  # 又保持本模块「用例写错一律 ValueError」的单一出口
  raise ValueError(f"all 的期望值必须是子断言列表: {value!r}")


class AllMatcher:
  """结构化断言组合匹配器：全部子断言通过才通过（AND 语义，首个失败即停）。

  语义：``spec.value`` 是一条用例对同一个输出提出的多条断言（例如「包含某关键词」
  且「JSON 某路径为某值」）。所有子断言都通过才通过；遇到第一条不通过就立即返回，
  并在原因里带上「组合断言第 N 条失败」与子断言自己的原因——定位到**首个**失败，
  而不是把全部失败汇总后让人猜哪条是最先崩的（改一条跑一次的往返代价更高）。
  刻意不短路「通过」：通过与否是 AND 关系，没有可提前收敛的捷径。

  不可判必须传播：子断言若抛 ``ValueError``（非法 JSON、未注册类型、空期望等），
  本匹配器**不捕获**，整个组合随之判为不可判。把不可判压成「不通过」会让通过率
  虚低，还会掩盖「输出压根解析不了」这个更该优先暴露的问题。

  嵌套：子断言可以又是 ``all``，递归走 ``match_expected``，天然支持多层组合；
  层数不设上限，但受 Python 递归深度约束（用例里的组合层级通常是 2-3 层）。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """按顺序判定全部子断言，返回首个失败。

    Args:
      response: 被测应用返回的原始输出文本（所有子断言共用同一份输出）。
      spec: 期望规范，``spec.value`` 为子断言列表（每项是 ExpectedSpec 形态的映射）。

    Returns:
      tuple[bool, str]: 全部通过返回 ``(True, "通过")``；否则返回
        ``(False, "组合断言第 N 条失败: <子断言原因>")``，N 从 1 开始。

    Raises:
      ValueError: ``spec.value`` 不是非空列表时抛出；子断言抛出的 ``ValueError``
        （用例写错或不可判）原样向上传播，不压成不通过。
    """
    sub_specs = _sub_specs(spec.value)
    for index, sub_spec in enumerate(sub_specs, start=1):
      passed, reason = match_expected(response, sub_spec)
      if not passed:
        return False, f"组合断言第 {index} 条失败: {reason}"
    return True, _PASS


def _parse_agent_output(response: str) -> Mapping[str, Any]:
  """把 Agent 输出解析为 JSON 对象（须是能承载 ``tool_calls`` 的对象结构）。

  与 ``JsonMatcher`` 同口径：整体解析，不做「抓取第一个花括号」这类容错——容错会把
  「模型在 JSON 前后多说了话」掩盖成通过。三类情况都算**输出不可判**，统一抛
  ``ValueError`` 而不是返回不通过：语法非法（消息带位置）、嵌套过深（``json.loads``
  递归溢出，D20 在加载器上踩过、D21 已在 JsonMatcher 收口）、顶层不是对象（数组或
  标量里无处取 ``tool_calls``）。

  Args:
    response: 被测应用返回的原始输出文本。

  Returns:
    Mapping[str, Any]: 解析出的输出对象（``tool_calls`` 的提取由 ``_tool_calls`` 负责）。

  Raises:
    ValueError: 输出不是合法 JSON、嵌套过深无法解析，或顶层不是对象时抛出。
  """
  try:
    document = json.loads(response)
  except json.JSONDecodeError as exc:
    raise ValueError(f"输出不是合法 JSON: {exc.msg}（位置 {exc.pos}）") from exc
  except RecursionError as exc:
    # 不带原始异常文本：各版本 json 的递归错误措辞不同，统一成稳定口径便于上层归类
    raise ValueError("JSON 嵌套过深，无法解析") from exc
  if isinstance(document, Mapping):
    return document
  # 正向分支先返回、报错放在 isinstance 之外（同 _path_segments），避开 TRY004 口径
  raise ValueError(
    f"输出不是合法 JSON 对象（Agent 断言需要含 tool_calls 的对象）: {type(document).__name__}"
  )


def _parse_tool_arguments(arguments: object) -> Any:
  """把 ``function.arguments`` 二次解析为 JSON 值（OpenAI 兼容格式下是 JSON 字符串）。

  非字符串（例如已经是对象）统一 str 化后再解析：其文本形态是单引号 repr、必然解析
  失败，于是以「不可判」收口，而不是把未经协议约束的对象直接拿来比较——否则「参数
  只差一个字符串/数字类型」这类差异会被 Python 的隐式等价吞掉。

  Args:
    arguments: 工具调用记录里的 ``arguments`` 字段。

  Returns:
    Any: 解析后的参数对象（通常是 dict，也可能是 list 或标量）。

  Raises:
    ValueError: 无法解析为合法 JSON、或嵌套过深（``json.loads`` 递归溢出）时抛出。
  """
  try:
    return json.loads(str(arguments))
  except json.JSONDecodeError as exc:
    raise ValueError(f"工具调用的 arguments 不是合法 JSON: {exc.msg}（位置 {exc.pos}）") from exc
  except RecursionError:
    # 与 _parse_agent_output 同口径：arguments 是**第二次**解析，深嵌套同样会触发递归
    # 溢出（D21 在 JsonMatcher 收过这条路径，此处是第四条解析路径）。不带上原始异常
    # 文本：各版本 json 的递归错误措辞不同，统一成稳定口径便于上层按「不可判」归类。
    raise ValueError("工具调用的 arguments JSON 嵌套过深，无法解析")


def _tool_calls(document: Mapping[str, Any]) -> list[tuple[Any, Any]]:
  """从已解析的 Agent 输出中取出工具调用序列 ``[(工具名, 参数对象), ...]``。

  输出格式假设为 OpenAI 兼容形态：

  ``{"tool_calls": [{"function": {"name": "get_weather", "arguments": "{\\"city\\": \\"bj\\"}"}}]}``

  ``arguments`` 是 JSON 字符串，需二次解析后才是可比较的参数对象。缺少 ``tool_calls``
  键或该键不是列表时返回空列表：这是**被测应用没有调用工具**，属于它的表现，由上层
  参数层断言判为不通过；而条目形状不符（不是映射、缺 ``function`` 对象）无法按协议
  解释，属于输出不可判，抛 ``ValueError``。

  Args:
    document: 已解析的 Agent 输出对象。

  Returns:
    list[tuple[Any, Any]]: ``(工具名, 已解析参数)`` 列表，顺序即输出顺序。

  Raises:
    ValueError: 条目形状不符合 OpenAI 兼容格式，或 ``arguments`` 非法（不是合法 JSON /
      嵌套过深）时抛出。
  """
  raw_calls = document.get("tool_calls")
  if not isinstance(raw_calls, list):
    return []
  calls: list[tuple[Any, Any]] = []
  for index, call in enumerate(raw_calls):
    calls.append(_tool_call_entry(call, index))
  return calls


def _tool_call_entry(call: object, index: int) -> tuple[Any, Any]:
  """校验单条工具调用记录并归一为 ``(工具名, 已解析参数)``。

  Args:
    call: ``tool_calls`` 数组里的一项（按 OpenAI 兼容格式应为映射）。
    index: 该项在数组中的下标，仅用于错误消息里定位。

  Returns:
    tuple[Any, Any]: ``(function.name, 已二次解析的 arguments)``。

  Raises:
    ValueError: 该项不是映射、或缺少 ``function`` 对象，或 ``arguments`` 不是合法
      JSON 时抛出（均为输出不可判）。
  """
  function = call.get("function") if isinstance(call, Mapping) else None
  if isinstance(function, Mapping):
    return function.get("name"), _parse_tool_arguments(function.get("arguments"))
  raise ValueError(f"tool_calls 第 {index} 项缺少 function 对象: {call!r}")


def _agent_assert_args(value: object) -> list[tuple[str, Any]]:
  """把 ``agent`` 的期望值归一为参数层断言列表 ``[(工具名, 期望参数), ...]``。

  只解释 ``assert_args`` 一个键：``order``（顺序层）与 ``tools``（工具层）今天不实现
  （顺延 D31），出现即忽略、不报错——这样 D31 补上两层时，今天的用例文件不必改写。
  反过来，``assert_args`` 缺失或为空则按用例写错拦下：没有参数断言就没有判定内容，
  放行等于给出一条恒通过的空断言。

  Args:
    value: ``agent`` 断言的期望值（应为含 ``assert_args`` 的映射）。

  Returns:
    list[tuple[str, Any]]: ``(工具名, 期望参数)`` 列表，顺序与用例一致。

  Raises:
    ValueError: 期望值不是映射、缺 ``assert_args`` 键、该键不是非空列表，或条目缺
      ``tool`` / ``arguments``（含 tool 非空字符串）时抛出。
  """
  entries = value.get("assert_args") if isinstance(value, Mapping) else None
  if not isinstance(entries, (list, tuple)) or not entries:
    raise ValueError(f"agent 的期望值必须是含非空 assert_args 列表的映射: {value!r}")
  return [_agent_assert_entry(entry, index) for index, entry in enumerate(entries, start=1)]


def _agent_assert_entry(entry: object, index: int) -> tuple[str, Any]:
  """校验 ``assert_args`` 的一条断言并归一为 ``(工具名, 期望参数)``。

  Args:
    entry: ``assert_args`` 数组里的一项（应为含 tool / arguments 的映射）。
    index: 该项在数组中的下标（从 1 开始），仅用于错误消息里定位。

  Returns:
    tuple[str, Any]: ``(tool, 期望参数)``。

  Raises:
    ValueError: 该项不是映射、``tool`` 不是非空字符串、或缺 ``arguments`` 键时抛出。
  """
  if isinstance(entry, Mapping):
    tool = entry.get("tool")
    if not isinstance(tool, str) or not tool:
      raise ValueError(f"assert_args 第 {index} 条的 tool 必须是非空字符串: {entry!r}")
    if "arguments" not in entry:
      raise ValueError(f"assert_args 第 {index} 条缺少 arguments 键: {entry!r}")
    return tool, entry["arguments"]
  raise ValueError(f"assert_args 第 {index} 条必须是映射（含 tool 与 arguments）: {entry!r}")


class AgentMatcher:
  """Agent 工具调用断言匹配器：今天只实现参数层（``assert_args``）。

  三层断言架构（按信息量从高到低组织）：**参数 > 顺序 > 工具**——
  - 参数层：指定工具被调用，且其 ``arguments`` 与期望完全相等（最严格，能定位到
    具体传错哪个字段）；
  - 顺序层：工具调用的先后顺序符合期望（顺延 D31）；
  - 工具层：该调用的工具都调用过、不该调用的没调用（顺延 D31）。
  今天只实现参数层，``order`` / ``tools`` 两个键在 value 里预留位置、出现即忽略。

  输出格式假设为 OpenAI 兼容的 ``tool_calls``：

  ``{"tool_calls": [{"function": {"name": "...", "arguments": "{...JSON 字符串...}"}}]}``

  ``arguments`` 需二次解析（JSON 字符串 → 对象）后再逐字段比较，且比较的是**完全
  相等**，不做部分匹配：只断言「包含某个键」这类宽松口径会把「多传了参数」「少传了
  参数」一起放过，而那正是工具调用出错的主要形态。同一工具被调用多次时，只要其中
  任一次的参数与期望相等即视为命中（不校验调用序，属顺序层职责）。
  """

  def match(self, response: str, spec: ExpectedSpec) -> tuple[bool, str]:
    """按参数层断言逐条核对工具调用。

    Args:
      response: 被测应用返回的原始输出文本（须为含 ``tool_calls`` 的 JSON 对象）。
      spec: 期望规范，``spec.value`` 为含 ``assert_args`` 非空列表的映射。

    Returns:
      tuple[bool, str]: 全部断言成立返回 ``(True, "通过")``；否则返回 ``(False, 原因)``，
        原因区分「未调用工具」与「参数不匹配」（后者同时给出期望与实际参数）。

    Raises:
      ValueError: 输出不是合法 JSON 对象、``arguments`` 不是合法 JSON，或
        ``spec.value`` 不符合 ``assert_args`` 结构时抛出（用例写错或不可判）。
    """
    document = _parse_agent_output(response)
    expected_calls = _agent_assert_args(spec.value)
    actual_calls = _tool_calls(document)
    for tool, expected_arguments in expected_calls:
      matched = [arguments for name, arguments in actual_calls if name == tool]
      if not matched:
        called = [name for name, _ in actual_calls]
        return False, f"未调用工具 {tool!r}: 期望调用 {tool!r}，实际调用={called}"
      if expected_arguments not in matched:
        return False, (
          f"工具 {tool!r} 参数不匹配: 期望={expected_arguments!r}, 实际={matched!r}"
        )
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
  "all": AllMatcher().match,
  "agent": AgentMatcher().match,
}


def match_expected(response: str, spec: ExpectedSpec) -> tuple[bool, str]:
  """按 ``spec.type`` 分发到对应匹配器，返回判定结论与说明。

  这是评分器对外的唯一入口：批量执行器只需拿到 ``(是否通过, 说明)``，不必关心
  具体是哪一类断言、更不必逐个 if/elif 分支（分支写漏的类型会静默永远不通过）。

  ``spec.tolerance`` 目前不参与判定（八类匹配器全部精确比较）。将来数值容差断言的
  接线点就在此处：由本函数按 type 决定是否把 ``tolerance`` 传给数值型匹配器，
  各匹配器不自行读取该字段——同一字段在八处被解释成八种含义，比暂时没有含义更糟。

  Args:
    response: 被测应用返回的原始输出文本。
    spec: 期望规范，``spec.type`` 决定使用哪个匹配器。

  Returns:
    tuple[bool, str]: ``(True, "通过")``，或 ``(False, 失败原因)``。

  Raises:
    ValueError: ``spec.type`` 未注册（``judge`` 尚未接线；或绕过契约校验构造出的
      未知类型——pydantic 在正常路径已拦截，此处为防御性兜底），以及各匹配器抛出的
      用例侧/不可判错误（空期望、非法正则、非法或过深 JSON 等）。
  """
  matcher = _MATCHERS.get(spec.type)
  if matcher is None:
    # 不静默返回 False：未知类型既不是「被测应用不通过」，也不该被计入通过率
    raise ValueError(f"未注册的期望类型: {spec.type!r}（当前支持 {sorted(_MATCHERS)}）")
  return matcher(response, spec)

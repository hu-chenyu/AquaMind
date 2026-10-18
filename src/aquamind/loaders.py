"""用例加载器：YAML/JSON/JSONL 三格式解析 + TestCase 契约校验。

输入约定：YAML/JSON 文件顶层为数组，JSONL 文件每行一个 JSON 对象（空行跳过，
行号即物理行号），每个元素/行是一条用例字典（input/expected 必填，
context/score_tags/weights 可选）。解析错误以 LoaderError 抛出并携带文件与行号；
字段契约由 TestCase（pydantic）负责校验，本模块不吞契约层异常。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# PyYAML 未随包提供 py.typed 类型存根，故对其导入做定向忽略；如需完整类型检查可安装 types-PyYAML。
import yaml # type: ignore[import-untyped]

from .exceptions import LoaderError
from .models import TestCase

# 支持的扩展名集合（比较前统一转小写）
_YAML_SUFFIXES: set[str] = {".yaml", ".yml"}
_JSON_SUFFIXES: set[str] = {".json"}
# JSONL：每行一个 JSON 对象，空行跳过；报错行号沿用物理行号，便于直接对照编辑器
_JSONL_SUFFIXES: set[str] = {".jsonl"}


def load_cases(path: str | Path) -> list[TestCase]:
  """从 YAML/JSON/JSONL 文件加载用例列表并完成契约校验。

  Args:
    path: 用例文件路径，支持 .jsonl/.yaml/.yml/.json 扩展名。

  Returns:
    list[TestCase]: 通过 TestCase 契约校验的非空用例列表。

  Raises:
    LoaderError: 扩展名不支持、文件不存在、不可读、路径是目录、编码不是 UTF-8、
      解析失败、文件为空，或其他读取失败（如父路径不是目录、路径过长）时抛出。
    ValidationError: 用例字段不符合 TestCase 契约时由 pydantic 抛出，本函数不捕获。

  Notes:
    三格式共用同一套契约收口（``_build_cases``）与读取失败映射（``_read_text``），
    差异只在「文本 → 原始字典列表」的解析环节。
  """
  file_path = Path(path)
  file_str = str(file_path)
  suffix = file_path.suffix.lower()

  if (
    suffix not in _YAML_SUFFIXES
    and suffix not in _JSON_SUFFIXES
    and suffix not in _JSONL_SUFFIXES
  ):
    raise _report_error(
      file_str,
      None,
      f"不支持的文件格式: {suffix or '(无扩展名)'}，仅支持 .jsonl/.yaml/.yml/.json",
    )

  content = _read_text(file_path)

  if suffix in _YAML_SUFFIXES:
    raw_items = _parse_yaml(content, file_str)
  elif suffix in _JSON_SUFFIXES:
    raw_items = _parse_json(content, file_str)
  else:
    raw_items = _parse_jsonl(content, file_str)
  return _build_cases(raw_items, file_str)


def _read_text(file_path: Path) -> str:
  """以 UTF-8 读取用例文件，并把所有读取失败统一包装为 LoaderError。

  Args:
    file_path: 用例文件路径。

  Returns:
    str: 文件文本内容（若带 BOM 则已剥离）。

  Raises:
    LoaderError: 文件不存在、不可读、路径是目录、编码不是 UTF-8，或其他读取
      失败（如父路径不是目录、路径过长）时抛出，context 携带文件路径与行号
      （读取阶段无法定位行，line 为 None）。
  """
  file_str = str(file_path)
  try:
    # utf-8-sig 同时兼容「有 BOM」与「无 BOM」的纯 UTF-8 文件：YAML 侧 PyYAML
    # 本就容忍 BOM，而 json.loads 不容忍，统一编码可消除多种格式的行为差异
    # （BOM 本身是合法的 UTF-8 字符序列，剥离后不影响内容）。
    return file_path.read_text(encoding="utf-8-sig")
  except FileNotFoundError as e:
    raise _report_error(file_str, None, f"文件不存在: {file_str}") from e
  except PermissionError as e:
    raise _report_error(file_str, None, f"文件不可读: {file_str}") from e
  except IsADirectoryError as e:
    raise _report_error(file_str, None, f"路径是目录而非文件: {file_str}") from e
  except UnicodeDecodeError as e:
    # 编码不符（如中文 Windows 默认的 GBK/GB18030）抛 UnicodeDecodeError，
    # 它既非 OSError 也非解析错误，必须显式包装：否则调用方按契约只捕获
    # LoaderError 时会被击穿，只能拿到无法定位的编解码堆栈。
    encoding_error = _report_error(
      file_str,
      None,
      "文件不是合法的 UTF-8 文本（可能使用了 GBK 等其他编码），请以 UTF-8 格式重新保存",
    )
    # _report_error 统一填充 file/line/msg，编码信息在此补充
    encoding_error.context["encoding"] = "utf-8"
    raise encoding_error from e
  except OSError as e:
    # 其余 OSError 家族（父路径不是目录的 NotADirectoryError、路径过长、符号链接
    # 成环等；Windows 下多数会归一为 FileNotFoundError，类 Unix 下不会）不属于
    # 上面三类具体场景，但根因同为「这个文件读不到」。不兜住它们会让裸 OSError
    # 击穿「调用方只需 except LoaderError」的契约——replay._load_cassette 用的是
    # 同口径的宽捕获，两处保持一致
    raise _report_error(file_str, None, f"文件读取失败: {file_str}") from e


def _build_cases(raw_items: list[dict[Any, Any]], file: str) -> list[TestCase]:
  """把原始用例字典列表逐条构造为 TestCase（三格式共用的契约收口）。

  Args:
    raw_items: 已通过结构校验的用例字典列表；键可能是非字符串（YAML 允许）。
    file: 文件路径（用于错误上下文）。

  Returns:
    list[TestCase]: 与输入等长、顺序一致的契约模型列表。

  Raises:
    LoaderError: 某条用例含非字符串键、无法展开为关键字参数时抛出。
    ValidationError: 用例字段不符合 TestCase 契约时由 pydantic 抛出，本函数不捕获。
  """
  cases: list[TestCase] = []
  for index, item in enumerate(raw_items):
    # YAML 允许数字/布尔等非字符串键，而 TestCase(**item) 展开要求键全为 str，
    # 否则会先抛 TypeError 走不到 pydantic 契约校验（与 docstring 声明不符）。
    non_str_keys = [key for key in item if not isinstance(key, str)]
    if non_str_keys:
      raise _report_error(
        file,
        None,
        f"第 {index + 1} 条用例包含非字符串键 {non_str_keys}："
        "用例字段名必须是字符串",
      )
    cases.append(TestCase(**item))
  return cases


def _parse_yaml(content: str, file: str) -> list[dict[str, Any]]:
  """解析 YAML 文本为用例字典列表。

  Args:
    content: YAML 文本内容。
    file: 文件路径（用于错误上下文）。

  Returns:
    list[dict[str, Any]]: 非空的用例字典列表。

  Raises:
    LoaderError: 语法错误（携带 1 起始行号）、嵌套过深（无法定位行）、顶层非数组
      或文件为空时抛出。
  """
  try:
    data = yaml.safe_load(content)
  except yaml.YAMLError as e:
    # YAML 解析器的行号从 0 开始，转换为人类可读的 1 起始行号
    mark = getattr(e, "problem_mark", None)
    line = mark.line + 1 if mark is not None else None
    raise _report_error(file, line, f"YAML 解析失败: {e}") from e
  except RecursionError as e:
    # 深嵌套 YAML（流样式/块样式）会耗尽解释器递归栈，RecursionError 不是
    # YAMLError 的子类，不单独捕收会以裸异常击穿 LoaderError 契约（校验器
    # 承诺不抛异常也会被打破）。整体解析无法定位到行，line 记 None 而非误报。
    raise _report_error(
      file,
      None,
      "YAML 嵌套过深（超出解释器递归上限），请降低嵌套层级",
    ) from e
  return _ensure_case_list(data, file)


def _parse_json(content: str, file: str) -> list[dict[str, Any]]:
  """解析 JSON 文本为用例字典列表。

  Args:
    content: JSON 文本内容。
    file: 文件路径（用于错误上下文）。

  Returns:
    list[dict[str, Any]]: 非空的用例字典列表。

  Raises:
    LoaderError: 语法错误（携带行号）、顶层非数组或文件为空时抛出。
  """
  if not content.strip():
    raise _report_error(file, None, "用例文件为空")
  try:
    data = json.loads(content)
  except json.JSONDecodeError as e:
    # JSONDecodeError.lineno 已是 1 起始行号，直接透传
    raise _report_error(file, e.lineno, f"JSON 解析失败: {e}") from e
  except RecursionError as e:
    # 深嵌套 JSON 会耗尽解释器递归栈。RecursionError 不是 JSONDecodeError 的
    # 子类，不单独捕收就会以裸异常击穿「调用方只需 except LoaderError」的契约。
    # 整体解析无法定位到行，line 记 None 而非误报。
    raise _report_error(
      file,
      None,
      "JSON 嵌套过深（超出解释器递归上限），请降低嵌套层级",
    ) from e
  return _ensure_case_list(data, file)


def _parse_jsonl(content: str, file: str) -> list[dict[str, Any]]:
  """解析 JSONL 文本为用例字典列表（每行一个对象，空行跳过）。

  Args:
    content: JSONL 文本内容。

  Returns:
    list[dict[str, Any]]: 非空的用例字典列表，顺序与文件一致。

  Raises:
    LoaderError: 某行 JSON 语法错误或某行不是 JSON 对象（均携带物理行号）、
      文件没有任何非空行时抛出。
  """
  items: list[dict[str, Any]] = []
  for line_no, raw_line in _iter_jsonl_lines(content):
    items.append(_parse_jsonl_line(raw_line, line_no, file))
  if not items:
    raise _report_error(file, None, "用例文件为空")
  return items


def _iter_jsonl_lines(content: str) -> Iterator[tuple[int, str]]:
  """按物理行号遍历 JSONL 的非空行。

  Args:
    content: JSONL 文本内容。

  Yields:
    tuple[int, str]: (1 起始的物理行号, 去除首尾空白后的行文本)。

  Notes:
    只跳过空白行，行号仍取物理行号（不因跳过而重新编号），
    这样报错位置与编辑器里看到的行号一一对应。

    这里刻意不用 ``str.splitlines()``：它会按 U+2028/U+2029/U+0085 等
    「额外行边界」切分，而这些字符在 JSON 字符串内是合法内容，用 splitlines
    会把一行合法 JSON 拆成两段、报出并不存在的语法错误。``_read_text`` 以
    universal newline 模式读取，CRLF 与孤立 CR 已归一为 LF，故按 ``\\n`` 切分
    得到的就是物理行；行尾残留的 CR 再由 ``strip()`` 兜底。
  """
  for line_no, raw_line in enumerate(content.split("\n"), start=1):
    stripped = raw_line.strip()
    if stripped:
      yield line_no, stripped


def _parse_jsonl_line(raw_line: str, line_no: int, file: str) -> dict[str, Any]:
  """解析单行 JSONL 并确认其顶层是 JSON 对象。

  Args:
    raw_line: 单行文本（已去除首尾空白）。
    line_no: 该行的物理行号（1 起始）。
    file: 文件路径（用于错误上下文）。

  Returns:
    dict[str, Any]: 该行解析出的 JSON 对象。

  Raises:
    LoaderError: JSON 语法错误或嵌套过深（context 的 error_type 为 ``json_parse``）、
      顶层不是对象（error_type 为 ``format``）时抛出，context 携带物理行号。
  """
  try:
    item = json.loads(raw_line)
  except json.JSONDecodeError as e:
    # JSONDecodeError 的行号是「行内相对行号」（跨行字符串时不为 1），
    # 这里统一用文件物理行号，避免与编辑器行号错位。
    error = _report_error(file, line_no, f"第 {line_no} 行 JSON 解析失败: {e}")
    error.context["error_type"] = "json_parse"
    raise error from e
  except RecursionError as e:
    # 深嵌套单行 JSON 同样会抛 RecursionError；它不是 JSONDecodeError 子类，
    # 不单独捕收会击穿 LoaderError 契约（校验器也会被裸异常打断）。
    error = _report_error(
      file,
      line_no,
      f"第 {line_no} 行 JSON 嵌套过深（超出解释器递归上限），请降低嵌套层级",
    )
    error.context["error_type"] = "json_parse"
    raise error from e
  if not isinstance(item, dict):
    error = _report_error(
      file,
      line_no,
      f"第 {line_no} 行必须是 JSON 对象（mapping），实际类型: {type(item).__name__}",
    )
    error.context["error_type"] = "format"
    raise error
  return item


def _ensure_case_list(data: Any, file: str) -> list[dict[Any, Any]]:
  """校验解析结果必须是“非空的用例字典列表”（内部辅助）。

  Args:
    data: 解析得到的原始对象。
    file: 文件路径（用于错误上下文）。

  Returns:
    list[dict[Any, Any]]: 校验通过的用例字典列表。键标注为 ``Any`` 而非 ``str``：
      YAML 允许数字/布尔等非字符串键，须交由调用方在展开前显式校验。

  Raises:
    LoaderError: 结果为 None/空列表（均视为空文件）或存在非字典元素时抛出。
  """
  if data is None or data == []:
    raise _report_error(file, None, "用例文件为空")
  if not isinstance(data, list):
    raise _report_error(file, None, f"用例文件顶层必须是数组，实际类型: {type(data).__name__}")
  for index, item in enumerate(data):
    if not isinstance(item, dict):
      raise _report_error(
        file,
        None,
        f"第 {index + 1} 条用例必须是字典（mapping），实际类型: {type(item).__name__}",
      )
  return data


def _report_error(file: str, line: int | None, msg: str) -> LoaderError:
  """构造携带文件/行号/消息上下文的 LoaderError。

  Args:
    file: 出错文件路径。
    line: 出错行号（1 起始；无法定位时为 None）。
    msg: 错误描述。

  Returns:
    LoaderError: 已填充 context 的加载异常实例。
  """
  return LoaderError(
    message=msg,
    context={"file": file, "line": line, "msg": msg},
  )
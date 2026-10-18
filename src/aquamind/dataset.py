"""测试集格式定义与统一入口：jsonl / yaml / yml / json。

与 ``loaders.load_cases`` 的分工：
- ``load_dataset`` 是加载的唯一入口，按扩展名分发；JSONL 走逐行解析，
 YAML/JSON 复用 ``load_cases``。
- ``validate_dataset`` 是校验器：**不抛异常**，把坏集的全部可定位错误收集后
 统一返回，供 CI 门禁与后续 CLI 子命令一次性列出问题。

JSONL 的读取与逐行解析原语由 ``loaders`` 提供（同一份实现，避免两份解析逻辑
漂移），本模块只负责「分发」与「逐行收集」。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from .exceptions import LoaderError
from .loaders import (
  _build_cases,
  _iter_jsonl_lines,
  _parse_jsonl,
  _parse_jsonl_line,
  _read_text,
  _report_error,
  load_cases,
)
from .models import TestCase

# 校验错误的分类：json_parse（JSON 语法）、format（结构/读取）、contract（TestCase 契约）。
# 用 Literal 而非裸 str：分类会一路进入门禁报告，拼错即静默丢分类，无法按类型聚合。
ValidationErrorType = Literal["json_parse", "format", "contract"]


class DatasetFormat(str, Enum):
  """测试集文件格式。

  Attributes:
    JSONL: 每行一个 JSON 对象，空行跳过，行号为物理行号
    YAML: 顶层为数组（.yaml/.yml）
    JSON: 顶层为数组（.json）
  """

  JSONL = "jsonl"
  YAML = "yaml"
  JSON = "json"


# 扩展名 → 格式的唯一映射：新增格式只改这一处，load_dataset 与 validate_dataset 同步生效。
_FORMAT_BY_SUFFIX: dict[str, DatasetFormat] = {
  ".jsonl": DatasetFormat.JSONL,
  ".yaml": DatasetFormat.YAML,
  ".yml": DatasetFormat.YAML,
  ".json": DatasetFormat.JSON,
}


@dataclass(frozen=True)
class ValidationError:
  """单条校验错误：坏在哪里、坏成什么、为什么坏。

  Attributes:
    line: 出错的物理行号（1 起始）；无法定位到行时为 None
    error_type: 错误分类（json_parse/format/contract）
    message: 人类可读的错误描述，含字段路径或解析器原文

  Notes:
    与 ``pydantic.ValidationError`` 同名但不同物：本类是一条**结果记录**，
    不会作为异常抛出，因此不会与 pydantic 的异常语义混淆。
  """

  line: int | None
  error_type: ValidationErrorType
  message: str


@dataclass(frozen=True)
class ValidationResult:
  """整份测试集的校验结果。

  Attributes:
    ok: 是否全部通过（恒等于 ``errors`` 为空）
    errors: 收集到的全部错误，按文件出现顺序排列
  """

  ok: bool
  errors: list[ValidationError]


def detect_format(path: str | Path) -> DatasetFormat:
  """按扩展名推断测试集格式（大小写不敏感）。

  Args:
    path: 测试集文件路径。

  Returns:
    DatasetFormat: 对应的格式枚举。

  Raises:
    LoaderError: 扩展名不在支持列表内时抛出，context 携带文件路径，line 为 None。
  """
  file_path = Path(path)
  suffix = file_path.suffix.lower()
  dataset_format = _FORMAT_BY_SUFFIX.get(suffix)
  if dataset_format is None:
    raise _report_error(
      str(file_path),
      None,
      f"不支持的文件格式: {suffix or '(无扩展名)'}，仅支持 .jsonl/.yaml/.yml/.json",
    )
  return dataset_format


def load_dataset(path: str | Path) -> list[TestCase]:
  """加载测试集（四种扩展名统一入口）。

  Args:
    path: 测试集文件路径，支持 .jsonl/.yaml/.yml/.json。

  Returns:
    list[TestCase]: 通过契约校验的用例列表；JSONL 的条目顺序与文件一致。

  Raises:
    LoaderError: 扩展名不支持、文件不存在/不可读/非 UTF-8、解析失败、
      文件为空等读取与格式错误时抛出。
    ValidationError: 用例字段不符合 TestCase 契约时由 pydantic 抛出
      （与 ``load_cases`` 同口径：遇到第一个契约错误即终止）。

  Notes:
    需要一次看全所有错误时改用 ``validate_dataset``。
  """
  file_path = Path(path)
  if detect_format(file_path) is DatasetFormat.JSONL:
    return _load_jsonl(file_path)
  return load_cases(file_path)


def validate_dataset(path: str | Path) -> ValidationResult:
  """校验测试集文件，收集全部错误而不抛出。

  与 ``load_dataset`` 的区别：加载器遇到第一个错误即抛异常，校验器把 JSONL
  的每行错误、YAML/JSON 的解析或契约错误全部收集后统一返回，每条错误携带
  行号（无法定位时为 None）。

  Args:
    path: 测试集文件路径，支持 .jsonl/.yaml/.yml/.json。

  Returns:
    ValidationResult: ``ok`` 为 True 时 ``errors`` 为空；为 False 时至少一条，
      且不因首个错误提前终止（JSONL 逐行收集）。

  Notes:
    YAML/JSON 复用 ``load_cases`` 的整体解析，最多报告一条错误：解析器本身
    只给出首个语法错误，契约错误也无法在不逐条解析的前提下定位到行。
    JSONL 逐行独立解析，才能收集多行错误并给出精确物理行号。
  """
  file_path = Path(path)
  try:
    dataset_format = detect_format(file_path)
  except LoaderError as e:
    # 扩展名不支持属于「整份文件不可校验」，直接作为单条错误返回
    return _result([ValidationError(line=None, error_type="format", message=e.message)])

  if dataset_format is DatasetFormat.JSONL:
    return _result(_validate_jsonl(file_path))
  return _result(_validate_array_file(file_path))


def _load_jsonl(file_path: Path) -> list[TestCase]:
  """加载 JSONL 测试集（首个错误即抛，语义与 ``load_cases`` 一致）。

  Args:
    file_path: 已确认扩展名为 .jsonl 的文件路径。

  Returns:
    list[TestCase]: 通过契约校验的用例列表。

  Raises:
    LoaderError: 读取失败、某行 JSON 语法错误（携带物理行号）、某行不是
      JSON 对象或文件为空时抛出。
    ValidationError: 用例字段不符合 TestCase 契约时由 pydantic 抛出。
  """
  file_str = str(file_path)
  content = _read_text(file_path)
  return _build_cases(_parse_jsonl(content, file_str), file_str)


def _validate_jsonl(file_path: Path) -> list[ValidationError]:
  """逐行校验 JSONL 测试集，收集全部错误。

  Args:
    file_path: 已确认扩展名为 .jsonl 的文件路径。

  Returns:
    list[ValidationError]: 全部错误（空列表表示通过）。文件读不到或没有任何
      非空行时返回单条错误。
  """
  file_str = str(file_path)
  try:
    content = _read_text(file_path)
  except LoaderError as e:
    return [ValidationError(line=e.context.get("line"), error_type="format", message=e.message)]

  # 与 load_cases 同口径：没有任何非空行的 jsonl 视同空文件（0 条用例不是有效测试集）
  if not content.strip():
    return [ValidationError(line=None, error_type="format", message="用例文件为空")]

  errors: list[ValidationError] = []
  for line_no, raw_line in _iter_jsonl_lines(content):
    try:
      item = _parse_jsonl_line(raw_line, line_no, file_str)
    except LoaderError as e:
      # 单行解析失败不中断：后续行继续校验，坏集一次报全
      errors.append(
        ValidationError(
          line=e.context.get("line"),
          error_type=e.context.get("error_type", "format"),
          message=e.message,
        )
      )
      continue
    try:
      TestCase(**item)
    except PydanticValidationError as e:
      errors.append(
        ValidationError(
          line=line_no,
          error_type="contract",
          message=f"契约校验失败: {_describe_contract_error(e)}",
        )
      )
  return errors


def _validate_array_file(file_path: Path) -> list[ValidationError]:
  """校验 YAML/JSON 测试集（复用 load_cases 的整体解析，最多一条错误）。

  Args:
    file_path: 已确认扩展名为 .yaml/.yml/.json 的文件路径。

  Returns:
    list[ValidationError]: 全部错误（空列表表示通过）。
  """
  try:
    load_cases(file_path)
  except LoaderError as e:
    return [ValidationError(line=e.context.get("line"), error_type="format", message=e.message)]
  except PydanticValidationError as e:
    # 整体解析拿不到「第几条」对应的源文件行，line 记为 None 而非误报
    return [
      ValidationError(
        line=None,
        error_type="contract",
        message=f"契约校验失败: {_describe_contract_error(e)}",
      )
    ]
  return []


def _describe_contract_error(exc: PydanticValidationError) -> str:
  """把 pydantic 校验错误压成单行「字段路径: 原因」，便于在错误列表里逐条展示。

  Args:
    exc: pydantic 抛出的契约校验异常。

  Returns:
    str: 以「；」连接的各字段错误描述，如 ``expected.type: Input should be ...``。
  """
  return "；".join(
    f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
    for error in exc.errors()
  )


def _result(errors: list[ValidationError]) -> ValidationResult:
  """由错误列表构造校验结果（保证 ok 与 errors 不互相矛盾）。

  Args:
    errors: 收集到的全部错误。

  Returns:
    ValidationResult: ``ok`` 取 ``errors`` 为空的否定。
  """
  return ValidationResult(ok=not errors, errors=errors)

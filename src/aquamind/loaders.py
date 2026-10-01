"""用例加载器：YAML/JSON 双格式解析 + TestCase 契约校验。

输入约定：文件顶层为数组，每个元素是一条用例字典（input/expected 必填，
context/score_tags/weights 可选）。解析错误以 LoaderError 抛出并携带文件与行号；
字段契约由 TestCase（pydantic）负责校验，本模块不吞契约层异常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# PyYAML 未随包提供 py.typed 类型存根，故对其导入做定向忽略；如需完整类型检查可安装 types-PyYAML。
import yaml  # type: ignore[import-untyped]

from .exceptions import LoaderError
from .models import TestCase

# 支持的扩展名集合（比较前统一转小写）
_YAML_SUFFIXES: set[str] = {".yaml", ".yml"}
_JSON_SUFFIXES: set[str] = {".json"}


def load_cases(path: str | Path) -> list[TestCase]:
    """从 YAML/JSON 文件加载用例列表并完成契约校验。

    Args:
        path: 用例文件路径，支持 .yaml/.yml/.json 扩展名。

    Returns:
        list[TestCase]: 通过 TestCase 契约校验的非空用例列表。

    Raises:
        LoaderError: 扩展名不支持、文件不存在、不可读或路径是目录、解析失败、文件为空时抛出。
        ValidationError: 用例字段不符合 TestCase 契约时由 pydantic 抛出，本函数不捕获。
    """
    file_path = Path(path)
    file_str = str(file_path)
    suffix = file_path.suffix.lower()

    if suffix not in _YAML_SUFFIXES and suffix not in _JSON_SUFFIXES:
        raise _report_error(
            file_str,
            None,
            f"不支持的文件格式: {suffix or '(无扩展名)'}，仅支持 .yaml/.yml/.json",
        )

    try:
        content = file_path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise _report_error(file_str, None, f"文件不存在: {file_str}") from e
    except PermissionError as e:
        raise _report_error(file_str, None, f"文件不可读: {file_str}") from e
    except IsADirectoryError as e:
        raise _report_error(file_str, None, f"路径是目录而非文件: {file_str}") from e

    if suffix in _YAML_SUFFIXES:
        raw_items = _parse_yaml(content, file_str)
    else:
        raw_items = _parse_json(content, file_str)

    return [TestCase(**item) for item in raw_items]


def _parse_yaml(content: str, file: str) -> list[dict[str, Any]]:
    """解析 YAML 文本为用例字典列表。

    Args:
        content: YAML 文本内容。
        file: 文件路径（用于错误上下文）。

    Returns:
        list[dict[str, Any]]: 非空的用例字典列表。

    Raises:
        LoaderError: 语法错误（携带 1 起始行号）、顶层非数组或文件为空时抛出。
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        # YAML 解析器的行号从 0 开始，转换为人类可读的 1 起始行号
        mark = getattr(e, "problem_mark", None)
        line = mark.line + 1 if mark is not None else None
        raise _report_error(file, line, f"YAML 解析失败: {e}") from e
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
    return _ensure_case_list(data, file)


def _ensure_case_list(data: Any, file: str) -> list[dict[str, Any]]:
    """校验解析结果必须是“非空的用例字典列表”（内部辅助）。

    Args:
        data: 解析得到的原始对象。
        file: 文件路径（用于错误上下文）。

    Returns:
        list[dict[str, Any]]: 校验通过的用例字典列表。

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
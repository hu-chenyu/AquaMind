"""测试集加载与校验器单元测试（D20）。

覆盖范围：
- JSONL 逐行加载（空行跳过、物理行号）；
- 坏 JSON 行 / 非对象行 / 契约错误行在 ``load_cases`` 与 ``validate_dataset``
 两条路径下的拦截与行号定位；
- ``validate_dataset`` 的全错误收集（多行混合错误、YAML/JSON 结构错误、
 空文件、文件不存在、非 UTF-8）；
- ``load_dataset`` 四扩展名分发与不支持扩展名的拦截；
- ``load_cases`` 对既有 YAML/JSON 行为的向后兼容；
- ``ValidationResult`` / ``ValidationError`` 结构；
- ``ScoreTag.name`` 非空约束（D20 输入侧收严）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from aquamind.dataset import (
  DatasetFormat,
  ValidationError,
  ValidationResult,
  detect_format,
  load_dataset,
  validate_dataset,
)
from aquamind.exceptions import LoaderError
from aquamind.loaders import load_cases
from aquamind.models import ScoreTag, TestCase

# 一行一条合法用例：JSONL 的每个物理行都是一条独立 TestCase
_CASE_ONE = '{"input": "1+1=?", "expected": {"type": "exact", "value": "2"}}'
_CASE_TWO = '{"input": "天空是什么颜色？", "expected": {"type": "contains", "value": "蓝"}}'
# 少一个右花括号：json.loads 必抛 JSONDecodeError
_BAD_JSON_LINE = '{"input": "坏行", "expected": {"type": "exact", "value": "x"}'
# 合法 JSON 但缺必填字段 expected：契约层拦截
_BAD_CONTRACT_LINE = '{"input": "缺期望"}'
# 合法 JSON 但不是对象：结构层拦截
_NON_OBJECT_LINE = "[1, 2]"

_SINGLE_YAML = """\
- input: "1+1=?"
 expected:
  type: exact
  value: "2"
"""

_SINGLE_JSON = """\
[
 {"input": "1+1=?", "expected": {"type": "exact", "value": "2"}}
]
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
  """把文本写入 tmp_path 下的指定文件并返回路径。"""
  path = tmp_path / name
  path.write_text(content, encoding="utf-8")
  return path


def _write_lines(tmp_path: Path, name: str, lines: list[str]) -> Path:
  """把逐行内容拼成以换行结尾的文件（模拟编辑器保存的 jsonl）。"""
  return _write(tmp_path, name, "\n".join(lines) + "\n")


class TestJsonlLoading:
  """测试 JSONL 正常加载路径（load_dataset 与 load_cases 两条入口）。"""

  def test_load_dataset_returns_cases_in_file_order(self, tmp_path: Path) -> None:
    """多行合法用例应按文件顺序全部加载，字段取值正确。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE, _CASE_TWO])
    cases = load_dataset(path)
    assert len(cases) == 2
    assert isinstance(cases[0], TestCase)
    assert cases[0].input == "1+1=?"
    assert cases[1].input == "天空是什么颜色？"
    assert cases[1].expected.value == "蓝"

  def test_blank_lines_are_skipped(self, tmp_path: Path) -> None:
    """空行与纯空白行应被跳过，不产生空用例也不计入条数。"""
    # 物理行：1 有效 / 2 空 / 3 纯空白 / 4 有效
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE, "", "  ", _CASE_TWO])
    cases = load_dataset(path)
    assert [case.input for case in cases] == ["1+1=?", "天空是什么颜色？"]

  def test_load_cases_supports_jsonl(self, tmp_path: Path) -> None:
    """load_cases 增加 .jsonl 分支后应能直接加载（向后兼容的第三条格式）。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE])
    cases = load_cases(path)
    assert len(cases) == 1
    assert cases[0].expected.type == "exact"


class TestJsonlLoadErrors:
  """测试 JSONL 坏文件在 load_cases 路径下的“首个错误即抛”语义与行号定位。"""

  def test_bad_json_line_reports_physical_line(self, tmp_path: Path) -> None:
    """坏 JSON 行应抛 LoaderError，context 行号为物理行号（第 3 行）。"""
    # 物理行：1 有效 / 2 空 / 3 坏 JSON——跳过空行不能把行号重新编号
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE, "", _BAD_JSON_LINE])
    with pytest.raises(LoaderError) as exc_info:
      load_cases(path)
    assert exc_info.value.context["line"] == 3
    assert "第 3 行" in str(exc_info.value)

  def test_non_object_line_rejected(self, tmp_path: Path) -> None:
    """某行是数组而非 JSON 对象时应抛 LoaderError，并点名实际类型与行号。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE, _NON_OBJECT_LINE])
    with pytest.raises(LoaderError) as exc_info:
      load_cases(path)
    assert exc_info.value.context["line"] == 2
    assert "JSON 对象" in str(exc_info.value)
    assert "list" in str(exc_info.value)

  def test_empty_jsonl_rejected(self, tmp_path: Path) -> None:
    """完全没有非空行的 jsonl 视同空文件（0 条用例不是有效测试集）。"""
    for index, content in enumerate(("", "\n \n")):
      path = _write(tmp_path, f"empty{index}.jsonl", content)
      with pytest.raises(LoaderError) as exc_info:
        load_cases(path)
      assert "用例文件为空" in str(exc_info.value)
      # 空文件无法定位到行，line 应为 None 而非误报
      assert exc_info.value.context["line"] is None

  def test_contract_error_propagates(self, tmp_path: Path) -> None:
    """缺必填字段时契约异常应由 pydantic 抛出（加载器不吞、不包装）。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_BAD_CONTRACT_LINE])
    with pytest.raises(PydanticValidationError) as exc_info:
      load_cases(path)
    assert ("expected",) in {err["loc"] for err in exc_info.value.errors()}


class TestValidateJsonl:
  """测试校验器在 JSONL 上的全错误收集行为。"""

  def test_valid_file_passes(self, tmp_path: Path) -> None:
    """全部有效时应 ok=True 且 errors 为空。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE, _CASE_TWO])
    result = validate_dataset(path)
    assert result.ok is True
    assert result.errors == []

  def test_mixed_errors_collected_with_lines(self, tmp_path: Path) -> None:
    """2 行有效 + 2 行错误：应收集 2 条错误且行号、类型均正确。"""
    # 物理行：1 有效 / 2 坏 JSON / 3 有效 / 4 契约错误
    path = _write_lines(
      tmp_path, "cases.jsonl", [_CASE_ONE, _BAD_JSON_LINE, _CASE_TWO, _BAD_CONTRACT_LINE]
    )
    result = validate_dataset(path)
    assert result.ok is False
    # 校验器不提前终止：两条错误都要出现，顺序与文件一致
    assert [error.line for error in result.errors] == [2, 4]
    assert [error.error_type for error in result.errors] == ["json_parse", "contract"]
    assert "第 2 行" in result.errors[0].message
    # 契约错误消息须点名缺失字段，用户才能直接改文件
    assert "expected" in result.errors[1].message

  def test_non_object_line_collected_as_format(self, tmp_path: Path) -> None:
    """某行不是 JSON 对象时应归类为 format 并携带该行物理行号。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_CASE_ONE, _NON_OBJECT_LINE])
    result = validate_dataset(path)
    assert result.ok is False
    assert len(result.errors) == 1
    assert result.errors[0].line == 2
    assert result.errors[0].error_type == "format"

  def test_empty_file_collected(self, tmp_path: Path) -> None:
    """空 jsonl 应被收集为单条 format 错误而非“通过”。"""
    path = _write(tmp_path, "cases.jsonl", "")
    result = validate_dataset(path)
    assert result.ok is False
    assert result.errors[0].line is None
    assert "用例文件为空" in result.errors[0].message

  def test_missing_file_collected(self, tmp_path: Path) -> None:
    """文件不存在时校验器不抛异常，而是返回一条 format 错误。"""
    missing = tmp_path / "not_exist.jsonl"
    result = validate_dataset(missing)
    assert result.ok is False
    assert result.errors[0].error_type == "format"
    assert "文件不存在" in result.errors[0].message
    assert result.errors[0].line is None

  def test_non_utf8_file_collected(self, tmp_path: Path) -> None:
    """GBK 编码的 jsonl 应被收集为 format 错误，并提示改用 UTF-8。"""
    path = tmp_path / "cases.jsonl"
    path.write_bytes(_CASE_TWO.encode("gbk"))
    result = validate_dataset(path)
    assert result.ok is False
    assert "UTF-8" in result.errors[0].message


class TestValidateArrayFiles:
  """测试校验器在 YAML/JSON 整体解析路径上的错误收集。"""

  def test_valid_yaml_passes(self, tmp_path: Path) -> None:
    """合法 YAML 应通过校验（复用 load_cases 的整体解析）。"""
    result = validate_dataset(_write(tmp_path, "cases.yaml", _SINGLE_YAML))
    assert result.ok is True
    assert result.errors == []

  def test_yaml_top_level_mapping_collected(self, tmp_path: Path) -> None:
    """YAML 顶层是 mapping（漏写列表符号）应被收集，行号为 None。"""
    content = 'input: "1+1=?"\nexpected:\n type: exact\n value: "2"\n'
    result = validate_dataset(_write(tmp_path, "cases.yaml", content))
    assert result.ok is False
    assert result.errors[0].error_type == "format"
    assert result.errors[0].line is None
    assert "用例文件顶层必须是数组" in result.errors[0].message

  def test_empty_yaml_collected(self, tmp_path: Path) -> None:
    """空 YAML 文件应被收集为 format 错误而非静默通过。"""
    result = validate_dataset(_write(tmp_path, "cases.yml", ""))
    assert result.ok is False
    assert "用例文件为空" in result.errors[0].message

  def test_json_syntax_error_reports_parser_line(self, tmp_path: Path) -> None:
    """坏 JSON 的行号取解析器给出的行号（第 3 行），而非笼统的 None。"""
    content = (
      "[\n"
      ' {"input": "1+1=?", "expected": {"type": "exact", "value": "2"}},\n'
      ' {"input": "z" "expected": {}}\n'
      "]\n"
    )
    result = validate_dataset(_write(tmp_path, "cases.json", content))
    assert result.ok is False
    assert result.errors[0].error_type == "format"
    assert result.errors[0].line == 3

  def test_yaml_contract_error_collected(self, tmp_path: Path) -> None:
    """YAML 契约错误应归类为 contract；整体解析无法定位到行，line 为 None。"""
    result = validate_dataset(_write(tmp_path, "cases.yaml", '- input: "缺期望"\n'))
    assert result.ok is False
    assert result.errors[0].error_type == "contract"
    assert result.errors[0].line is None
    assert "expected" in result.errors[0].message


class TestLoadDatasetDispatch:
  """测试统一入口按扩展名分发，以及与 load_cases 一致的扩展名拦截。"""

  @pytest.mark.parametrize(
    ("name", "content", "expected_format"),
    [
      ("cases.jsonl", _CASE_ONE + "\n", DatasetFormat.JSONL),
      ("cases.yaml", _SINGLE_YAML, DatasetFormat.YAML),
      ("cases.yml", _SINGLE_YAML, DatasetFormat.YAML),
      ("cases.json", _SINGLE_JSON, DatasetFormat.JSON),
    ],
  )
  def test_four_extensions_dispatch(
    self, tmp_path: Path, name: str, content: str, expected_format: DatasetFormat
  ) -> None:
    """四种扩展名都应被识别并加载出 1 条用例，格式推断结果正确。"""
    path = _write(tmp_path, name, content)
    assert detect_format(path) is expected_format
    cases = load_dataset(path)
    assert len(cases) == 1
    assert cases[0].input == "1+1=?"

  def test_suffix_is_case_insensitive(self, tmp_path: Path) -> None:
    """扩展名大小写不敏感：.JSONL 与 .jsonl 等价。"""
    path = _write(tmp_path, "CASES.JSONL", _CASE_ONE + "\n")
    assert detect_format(path) is DatasetFormat.JSONL
    assert len(load_dataset(path)) == 1

  def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
    """不支持的扩展名应抛 LoaderError（与 load_cases 行为一致），消息含支持列表。"""
    path = _write(tmp_path, "cases.txt", "x\n")
    with pytest.raises(LoaderError) as exc_info:
      load_dataset(path)
    assert "不支持的文件格式" in str(exc_info.value)
    assert ".jsonl" in str(exc_info.value)
    assert exc_info.value.context["file"] == str(path)

  def test_unsupported_extension_collected_by_validator(self, tmp_path: Path) -> None:
    """校验器遇到不支持的扩展名时返回 format 错误而不是抛出。"""
    result = validate_dataset(_write(tmp_path, "cases.txt", "x\n"))
    assert result.ok is False
    assert result.errors[0].error_type == "format"
    assert "不支持的文件格式" in result.errors[0].message


class TestBackwardCompatibility:
  """测试 loaders.load_cases 对既有 YAML/JSON 行为不回归。"""

  def test_yaml_unchanged(self, tmp_path: Path) -> None:
    """YAML 正常加载路径保持原样。"""
    cases = load_cases(_write(tmp_path, "cases.yaml", _SINGLE_YAML))
    assert len(cases) == 1
    assert cases[0].expected.type == "exact"

  def test_json_unchanged(self, tmp_path: Path) -> None:
    """JSON 顶层数组路径保持原样（未被新的分发逻辑改写）。"""
    cases = load_cases(_write(tmp_path, "cases.json", _SINGLE_JSON))
    assert len(cases) == 1
    assert cases[0].expected.value == "2"


class TestResultStructures:
  """测试 ValidationResult / ValidationError 的结果结构。"""

  def test_error_fields_are_complete(self, tmp_path: Path) -> None:
    """每条错误须含 line/error_type/message 三个字段且类型符合声明。"""
    path = _write_lines(tmp_path, "cases.jsonl", [_BAD_JSON_LINE])
    result = validate_dataset(path)
    error = result.errors[0]
    assert isinstance(error, ValidationError)
    assert isinstance(error.line, int)
    assert error.error_type == "json_parse"
    assert isinstance(error.message, str) and error.message

  def test_result_ok_matches_errors(self, tmp_path: Path) -> None:
    """ok 与 errors 必须自洽：有错则 False，无错则 True。"""
    bad = validate_dataset(_write(tmp_path, "bad.jsonl", _BAD_JSON_LINE + "\n"))
    good = validate_dataset(_write(tmp_path, "good.jsonl", _CASE_ONE + "\n"))
    assert isinstance(bad, ValidationResult) and isinstance(good, ValidationResult)
    assert bad.ok is False and bad.errors
    assert good.ok is True and good.errors == []


class TestScoreTagNameTightening:
  """测试 ScoreTag.name 非空收严（与输出侧 Score.matched_tags 的 NonBlankStr 对齐）。"""

  def test_empty_name_rejected(self) -> None:
    """空字符串标签名应被契约拒绝，错误定位到 name 字段。"""
    with pytest.raises(PydanticValidationError) as exc_info:
      ScoreTag(name="")
    assert ("name",) in {err["loc"] for err in exc_info.value.errors()}

  def test_non_empty_name_accepted(self) -> None:
    """收严不应误伤正常标签名。"""
    assert ScoreTag(name="accuracy").name == "accuracy"

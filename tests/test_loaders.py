"""用例加载器单元测试。

覆盖：YAML/JSON 单条与多条用例正常加载、坏 YAML/JSON 报错含行号、
空文件拦截、文件不存在拦截、不支持扩展名拦截、契约校验错误向上传播。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from aquamind.exceptions import LoaderError
from aquamind.loaders import load_cases
from aquamind.models import TestCase

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

_MULTI_YAML = """\
- input: "1+1=?"
  expected:
    type: exact
    value: "2"
- input: "天空是什么颜色？"
  context:
    lang: zh
  expected:
    type: contains
    value: "蓝"
  weights:
    accuracy: 0.8
"""

_MULTI_JSON = """\
[
  {"input": "1+1=?", "expected": {"type": "exact", "value": "2"}},
  {
    "input": "天空是什么颜色？",
    "expected": {"type": "contains", "value": "蓝"},
    "score_tags": [{"name": "accuracy", "weight": 0.9}]
  }
]
"""


def _write_yaml(tmp_path: Path, content: str) -> Path:
    """写入 YAML 临时文件并返回路径。"""
    path = tmp_path / "cases.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def _write_json(tmp_path: Path, content: str) -> Path:
    """写入 JSON 临时文件并返回路径。"""
    path = tmp_path / "cases.json"
    path.write_text(content, encoding="utf-8")
    return path


class TestLoadValidCases:
    """测试 YAML/JSON 正常加载路径。"""

    def test_yaml_single_case(self, tmp_path: Path) -> None:
        """YAML 单条用例应返回长度 1 的 list[TestCase]，字段取值正确。"""
        cases = load_cases(_write_yaml(tmp_path, _SINGLE_YAML))
        assert len(cases) == 1
        assert isinstance(cases[0], TestCase)
        assert cases[0].input == "1+1=?"
        assert cases[0].expected.type == "exact"
        assert cases[0].expected.value == "2"

    def test_json_single_case(self, tmp_path: Path) -> None:
        """JSON 单条用例应正常加载（同时覆盖 str 路径入参）。"""
        cases = load_cases(str(_write_json(tmp_path, _SINGLE_JSON)))
        assert len(cases) == 1
        assert cases[0].input == "1+1=?"

    def test_yaml_multiple_cases(self, tmp_path: Path) -> None:
        """YAML 多条用例应全部加载并保持文件内顺序。"""
        cases = load_cases(_write_yaml(tmp_path, _MULTI_YAML))
        assert len(cases) == 2
        assert cases[0].input == "1+1=?"
        assert cases[1].context == {"lang": "zh"}
        assert cases[1].weights == {"accuracy": 0.8}

    def test_json_multiple_cases(self, tmp_path: Path) -> None:
        """JSON 多条用例应全部加载，嵌套 score_tags 由 pydantic 转为契约模型。"""
        cases = load_cases(_write_json(tmp_path, _MULTI_JSON))
        assert len(cases) == 2
        assert cases[1].input == "天空是什么颜色？"
        assert cases[1].score_tags[0].name == "accuracy"
        assert cases[1].score_tags[0].weight == 0.9


class TestLoadErrors:
    """测试各类坏文件的错误拦截与定位信息。"""

    def test_bad_yaml_reports_line_number(self, tmp_path: Path) -> None:
        """坏 YAML 应抛 LoaderError，context 携带 1 起始的出错行号。"""
        content = (
            '- input: "1+1=?"\n'
            "  expected:\n"
            "    type: exact\n"
            "    value: [1, 2\n"
        )
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_yaml(tmp_path, content))
        line = exc_info.value.context.get("line")
        # 行号应落在文件有效行范围内（不锁死具体行号，兼容解析器版本差异）
        assert isinstance(line, int)
        assert 1 <= line <= 5
        assert "YAML 解析失败" in str(exc_info.value)

    def test_bad_json_reports_line_number(self, tmp_path: Path) -> None:
        """坏 JSON 应抛 LoaderError，context 携带出错行号。"""
        content = (
            "[\n"
            '  {"input": "x", "expected": {"type": "exact", "value": "y"}},\n'
            '  {"input": "z" "expected": {}}\n'
            "]\n"
        )
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_json(tmp_path, content))
        # 第 3 行缺少逗号，JSONDecodeError.lineno 应为 3（1 起始）
        assert exc_info.value.context.get("line") == 3
        assert "JSON 解析失败" in str(exc_info.value)

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        """空文件（YAML/JSON）应抛 LoaderError，消息含“空”。"""
        for name in ("empty.yaml", "empty.json"):
            path = tmp_path / name
            path.write_text("", encoding="utf-8")
            with pytest.raises(LoaderError) as exc_info:
                load_cases(path)
            assert "用例文件为空" in str(exc_info.value)

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        """文件不存在时应抛 LoaderError，context 携带出错路径。"""
        missing = tmp_path / "not_exist.yaml"
        with pytest.raises(LoaderError) as exc_info:
            load_cases(missing)
        assert "文件不存在" in str(exc_info.value)
        assert exc_info.value.context["file"] == str(missing)

    def test_unsupported_extension_rejected(self, tmp_path: Path) -> None:
        """不支持的扩展名（.txt）应抛 LoaderError，消息含“不支持”。"""
        path = tmp_path / "cases.txt"
        path.write_text("- input: x\n", encoding="utf-8")
        with pytest.raises(LoaderError) as exc_info:
            load_cases(path)
        assert "不支持的文件格式" in str(exc_info.value)


class TestContractPropagation:
    """测试契约校验错误向上传播（加载器不吞 ValidationError）。"""

    def test_missing_required_field_propagates(self, tmp_path: Path) -> None:
        """用例缺必填字段 input 时，ValidationError 应由 pydantic 抛出而非被包装。"""
        content = '- expected:\n    type: exact\n    value: "2"\n'
        with pytest.raises(ValidationError) as exc_info:
            load_cases(_write_yaml(tmp_path, content))
        assert "input" in str(exc_info.value)
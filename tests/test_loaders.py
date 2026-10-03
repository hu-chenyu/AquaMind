"""用例加载器单元测试。

覆盖：YAML/JSON 单条与多条用例正常加载、坏 YAML/JSON 报错含行号、
空文件拦截、文件不存在拦截、不支持扩展名拦截、契约校验错误向上传播。
"""

from __future__ import annotations

import os
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

    def test_directory_path_rejected(self, tmp_path: Path) -> None:
        """目录入参应统一包装为 LoaderError（跨平台覆盖目录读取失败场景）。"""
        dir_path = tmp_path / "cases.yaml"
        dir_path.mkdir()
        with pytest.raises(LoaderError) as exc_info:
            load_cases(dir_path)
        # context 必须精确携带坏入参路径，便于定位
        assert exc_info.value.context["file"] == str(dir_path)
        message = str(exc_info.value)
        if os.name == "nt":
            # Windows：open 目录抛 PermissionError，走“文件不可读”分支
            assert "文件不可读" in message
        else:
            # POSIX：open 目录抛 IsADirectoryError，走“路径是目录而非文件”分支（P2-1 修复点）
            assert "路径是目录而非文件" in message


class TestContractPropagation:
    """测试契约校验错误向上传播（加载器不吞 ValidationError）。"""

    def test_missing_required_field_propagates(self, tmp_path: Path) -> None:
        """用例缺必填字段 input 时，ValidationError 应由 pydantic 抛出而非被包装。"""
        content = '- expected:\n    type: exact\n    value: "2"\n'
        with pytest.raises(ValidationError) as exc_info:
            load_cases(_write_yaml(tmp_path, content))
        assert "input" in str(exc_info.value)


class TestCaseListShapeValidation:
    """测试顶层结构与元素类型校验（YAML/JSON 两条解析路径共用同一套规则）。"""

    def test_yaml_top_level_mapping_rejected(self, tmp_path: Path) -> None:
        """YAML 顶层是 mapping（单条用例漏写列表符号）时应报“顶层必须是数组”。"""
        content = 'input: "1+1=?"\nexpected:\n  type: exact\n  value: "2"\n'
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_yaml(tmp_path, content))
        # 错误消息须点名实际类型，用户才能直接定位写法问题
        assert "用例文件顶层必须是数组" in str(exc_info.value)
        assert "dict" in str(exc_info.value)
        # 结构性问题无法定位到具体行，line 应为 None 而非误报
        assert exc_info.value.context["line"] is None

    def test_json_top_level_mapping_rejected(self, tmp_path: Path) -> None:
        """JSON 顶层是 object 时同样应被拦截（与 YAML 走同一校验）。"""
        content = '{"input": "1+1=?", "expected": {"type": "exact", "value": "2"}}'
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_json(tmp_path, content))
        assert "用例文件顶层必须是数组" in str(exc_info.value)
        assert "dict" in str(exc_info.value)

    def test_yaml_top_level_scalar_rejected(self, tmp_path: Path) -> None:
        """YAML 顶层是标量时应报出标量类型，而非笼统的类型错误。"""
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_yaml(tmp_path, "just a string\n"))
        assert "用例文件顶层必须是数组" in str(exc_info.value)
        assert "str" in str(exc_info.value)

    def test_yaml_first_element_not_mapping_rejected(self, tmp_path: Path) -> None:
        """列表首元素不是 mapping 时，应报出 1 起始的条目序号与实际类型。"""
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_yaml(tmp_path, "- 1\n- 2\n"))
        assert "第 1 条用例必须是字典" in str(exc_info.value)
        assert "int" in str(exc_info.value)

    def test_json_non_mapping_element_reports_index(self, tmp_path: Path) -> None:
        """元素序号应为 1 起始：第 2 个元素非法时报“第 2 条”，便于按序修正。"""
        content = (
            '[{"input": "1+1=?", "expected": {"type": "exact", "value": "2"}}, 42]'
        )
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_json(tmp_path, content))
        assert "第 2 条用例必须是字典" in str(exc_info.value)
        assert "int" in str(exc_info.value)

    def test_is_a_directory_error_mapped_to_loader_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """读取目录时抛出的 IsADirectoryError 应被映射为 LoaderError 并保留异常链。

        该 OS 错误在 Windows 上表现为 PermissionError（已由
        test_directory_path_rejected 覆盖），仅在 POSIX 上出现；此处定向构造
        IsADirectoryError，使“路径是目录而非文件”这条错误映射在两个平台上都被验证。
        """

        def _fake_read_text(self: Path, *args: object, **kwargs: object) -> str:
            """模拟 Path.read_text 在目录上抛出 IsADirectoryError。"""
            raise IsADirectoryError(f"模拟目录读取失败: {self}")

        monkeypatch.setattr(Path, "read_text", _fake_read_text)
        dir_path = tmp_path / "cases.yaml"
        dir_path.mkdir()
        with pytest.raises(LoaderError) as exc_info:
            load_cases(dir_path)
        assert "路径是目录而非文件" in str(exc_info.value)
        # context 须精确携带坏入参路径
        assert exc_info.value.context["file"] == str(dir_path)
        assert exc_info.value.context["line"] is None
        # 异常链保留原始 OS 错误，便于定位真实原因
        assert isinstance(exc_info.value.__cause__, IsADirectoryError)
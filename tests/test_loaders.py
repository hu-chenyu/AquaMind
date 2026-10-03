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

# 非 UTF-8 编码回归用例（P1-1）：以 GBK 写入后，read_text(encoding="utf-8") 必失败。
# 内容取中文用例，确保 GBK 与 UTF-8 字节序列必然不同。
_GBK_YAML = """\
- input: "天空是什么颜色？"
  expected:
    type: contains
    value: "蓝"
"""

_GBK_JSON = """\
[{"input": "天空是什么颜色？", "expected": {"type": "contains", "value": "蓝"}}]
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


class TestNonUtf8Encoding:
    """测试非 UTF-8 编码文件的拦截。

    回归背景（P1-1）：``read_text(encoding="utf-8")`` 在文件不是 UTF-8 时抛
    ``UnicodeDecodeError``，而该异常既非 OSError 也非解析错误，原实现未捕获，
    会绕过 ``LoaderError`` 契约。中文 Windows 默认编码为 GBK/GB18030，
    该输入在本项目场景中完全现实。
    """

    def test_gbk_yaml_rejected_with_friendly_message(self, tmp_path: Path) -> None:
        """GBK 编码的 YAML 用例文件应抛 LoaderError，并提示改用 UTF-8。

        修复前此处会抛出裸 ``UnicodeDecodeError``，``pytest.raises(LoaderError)``
        匹配不到而直接失败——即本测试在修复撤销时必然变红。
        """
        path = tmp_path / "cases.yaml"
        path.write_bytes(_GBK_YAML.encode("gbk"))
        with pytest.raises(LoaderError) as exc_info:
            load_cases(path)
        # 消息须直接告诉用户改用 UTF-8，而不是暴露编解码堆栈
        assert "UTF-8" in str(exc_info.value)
        assert "GBK" in str(exc_info.value)

    def test_gbk_json_rejected_with_friendly_message(self, tmp_path: Path) -> None:
        """GBK 编码的 JSON 用例文件同样应被包装为 LoaderError（读文件阶段先于解析失败）。"""
        path = tmp_path / "cases.json"
        path.write_bytes(_GBK_JSON.encode("gbk"))
        with pytest.raises(LoaderError) as exc_info:
            load_cases(path)
        assert "UTF-8" in str(exc_info.value)

    def test_non_utf8_error_carries_context_and_cause(self, tmp_path: Path) -> None:
        """编码错误须携带 encoding 上下文，并保留原始异常链供定位。"""
        path = tmp_path / "cases.yaml"
        path.write_bytes(_GBK_YAML.encode("gbk"))
        with pytest.raises(LoaderError) as exc_info:
            load_cases(path)
        # context 携带文件路径与期望编码，便于调用方给出可操作提示
        assert exc_info.value.context["file"] == str(path)
        assert exc_info.value.context["encoding"] == "utf-8"
        # 编码问题无法定位到具体行，line 应为 None 而非误报
        assert exc_info.value.context["line"] is None
        # 异常链保留原始解码异常
        assert isinstance(exc_info.value.__cause__, UnicodeDecodeError)

    def test_utf8_file_unaffected(self, tmp_path: Path) -> None:
        """合法 UTF-8 文件不应被新增的编码校验误伤。"""
        cases = load_cases(_write_yaml(tmp_path, _SINGLE_YAML))
        assert len(cases) == 1
        assert cases[0].input == "1+1=?"


class TestNonStringKeys:
    """测试 YAML 非字符串键的拦截（P2-1）。

    回归背景：``TestCase(**item)`` 展开要求键全为 str，YAML 允许数字/布尔键，
    原实现直接展开会先抛 ``TypeError``，与 docstring 声明的 ``ValidationError`` 不符。
    """

    def test_non_string_key_rejected_as_loader_error(self, tmp_path: Path) -> None:
        """整数键应抛 LoaderError，而非穿透契约的 TypeError。"""
        content = (
            '- input: "1+1=?"\n'
            "  expected:\n"
            "    type: exact\n"
            '    value: "2"\n'
            "  1: 坏键\n"
        )
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_yaml(tmp_path, content))
        message = str(exc_info.value)
        # 消息须指出是哪条用例的哪个键，便于按序修正
        assert "第 1 条用例" in message
        assert "非字符串键" in message
        assert "1" in message

    def test_non_string_key_reports_its_index(self, tmp_path: Path) -> None:
        """序号应为 1 起始：第 2 条用例含布尔键时须报“第 2 条”。"""
        content = (
            '- input: "x"\n'
            "  expected:\n"
            "    type: exact\n"
            '    value: "2"\n'
            "- input: y\n"
            "  expected:\n"
            "    type: exact\n"
            '    value: "2"\n'
            "  true: 坏键\n"
        )
        with pytest.raises(LoaderError) as exc_info:
            load_cases(_write_yaml(tmp_path, content))
        assert "第 2 条用例" in str(exc_info.value)

    def test_string_extra_key_still_extra_forbidden(self, tmp_path: Path) -> None:
        """字符串形式的未知键仍走 pydantic 的 extra_forbidden，不被新分支截胡。"""
        content = (
            '- input: "x"\n'
            "  expected:\n"
            "    type: exact\n"
            '    value: "2"\n'
            '  "1": 坏键\n'
        )
        with pytest.raises(ValidationError):
            load_cases(_write_yaml(tmp_path, content))


class TestUtf8BomCompatibility:
    """测试 UTF-8 BOM 兼容（P2-7）。

    回归背景：PyYAML 自带 BOM 处理而 ``json.loads`` 不容忍前导 ``\\ufeff``，
    同一份逻辑内容的文件仅因扩展名不同而一个能加载、一个不能。
    """

    def test_json_with_bom_loads(self, tmp_path: Path) -> None:
        """带 BOM 的 JSON 应与 YAML 一样正常加载（utf-8-sig 自动剥离 BOM）。"""
        path = tmp_path / "cases.json"
        path.write_bytes(b"\xef\xbb\xbf" + _SINGLE_JSON.encode("utf-8"))
        cases = load_cases(path)
        assert len(cases) == 1
        assert cases[0].input == "1+1=?"

    def test_yaml_with_bom_loads(self, tmp_path: Path) -> None:
        """带 BOM 的 YAML 应正常加载（回归守卫：该行为修复前后一致）。"""
        path = tmp_path / "cases.yaml"
        path.write_bytes(b"\xef\xbb\xbf" + _SINGLE_YAML.encode("utf-8"))
        cases = load_cases(path)
        assert len(cases) == 1
        assert cases[0].input == "1+1=?"

    def test_plain_utf8_without_bom_unaffected(self, tmp_path: Path) -> None:
        """无 BOM 的纯 UTF-8 文件行为不应被 utf-8-sig 改变。"""
        cases = load_cases(_write_json(tmp_path, _SINGLE_JSON))
        assert len(cases) == 1
        assert cases[0].expected.value == "2"

    def test_gbk_still_rejected_under_utf8_sig(self, tmp_path: Path) -> None:
        """P1-1 不回归：utf-8-sig 对 GBK 仍抛 UnicodeDecodeError 并被包装。"""
        path = tmp_path / "cases.yaml"
        path.write_bytes(_GBK_YAML.encode("gbk"))
        with pytest.raises(LoaderError) as exc_info:
            load_cases(path)
        assert "UTF-8" in str(exc_info.value)
        assert exc_info.value.context["encoding"] == "utf-8"
"""AquaMind 用例加载器最小示例（loader_demo）。

本示例演示三件事：

1. 用 YAML 与 JSON 两种格式定义用例，经 ``load_cases()`` 加载为 ``TestCase`` 对象；
2. 读取用例的契约字段（``input`` / ``expected`` / ``context`` / ``score_tags`` / ``weights``）；
3. 坏数据在**加载期**即被拦截：格式与语法错误由 ``LoaderError`` 报出（携带文件与行号），
  契约字段错误由 pydantic ``ValidationError`` 报出（定位到具体字段名）。

要点：加载器**不吞契约层异常**——``LoaderError`` 覆盖"文件层面"的问题，
``ValidationError`` 覆盖"用例层面"的问题，调用方需要分别捕获。

本示例不发起任何网络请求，不需要任何 API 密钥（CI 零 key）。
用例文件一律写入系统临时目录，退出时自动清理，不改动仓库任何文件。

运行方式（在**仓库根目录**执行）::

  .venv\\Scripts\\python.exe examples/loader_demo/loader_demo.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import ValidationError

from aquamind.exceptions import LoaderError
from aquamind.loaders import load_cases

# 正常用例：同时覆盖 YAML 与 JSON 两种扩展名的分发
_VALID_YAML = """\
- input: "2 + 3 等于几？"
 context:
  subject: math
 expected:
  type: contains
  value: "5"
 score_tags:
  - name: accuracy
   weight: 1.0
- input: "再见"
 expected:
  type: exact
  value: "再见"
 weights:
  dialogue: 0.5
"""

_VALID_JSON = """\
[
 {
  "input": "中国的首都是哪里？",
  "expected": {"type": "contains", "value": "北京"}
 }
]
"""

# 语法错误：value 里的方括号未闭合，YAML 解析器会在该行给出 mark
_BROKEN_YAML = """\
- input: "正常的一条"
 expected:
  type: exact
  value: [未闭合的方括号
"""

# 契约错误：缺少必填字段 expected
_MISSING_FIELD_YAML = """\
- input: "缺少 expected 字段的用例"
"""

# 契约错误：expected.type 不在 Literal 允许的四个取值内
_BAD_TYPE_YAML = """\
- input: "类型拼错的用例"
 expected:
  type: excat
  value: "5"
"""

# 契约错误：多写了一个不存在的字段（TestCase 开启 extra="forbid"）
_UNKNOWN_FIELD_YAML = """\
- input: "多写了未知字段的用例"
 expected:
  type: exact
  value: "ok"
 timeout: 30
"""


def _write(directory: Path, name: str, content: str) -> Path:
  """把用例文本写到临时目录下的指定文件。

  Args:
    directory: 临时目录。
    name: 文件名（含扩展名，扩展名决定加载器走 YAML 还是 JSON 分支）。
    content: 文件文本内容。

  Returns:
    Path: 写好的文件路径。
  """
  path = directory / name
  path.write_text(content, encoding="utf-8")
  return path


def _show_loader_error(path: Path) -> None:
  """演示文件级错误：LoaderError 的消息与 context 定位信息。

  Args:
    path: 触发错误的用例文件路径。
  """
  try:
    load_cases(path)
  except LoaderError as exc:
    print(f" LoaderError: {exc}")
    print(f"  context: {exc.context}")
  else:  # pragma: no cover - 示例脚本的可读性分支，正常不会走到
    print(" 预期抛出 LoaderError，但没有")


def _show_validation_error(path: Path) -> None:
  """演示契约级错误：pydantic ValidationError 定位到具体字段。

  Args:
    path: 触发契约错误的用例文件路径。
  """
  try:
    load_cases(path)
  except ValidationError as exc:
    first = exc.errors()[0]
    field = ".".join(str(part) for part in first["loc"])
    print(f" ValidationError: 首个错误定位到字段 {field}")
    print(f"  原因: {first['msg']}")
  else:  # pragma: no cover - 示例脚本的可读性分支，正常不会走到
    print(" 预期抛出 ValidationError，但没有")


def main() -> None:
  """按顺序跑完加载器的正常路径与四类坏数据路径。"""
  with tempfile.TemporaryDirectory(prefix="aquamind-loader-demo-") as tmp:
    directory = Path(tmp)

    print("=" * 68)
    print("1. 正常路径：用 YAML 加载两条用例")
    print("=" * 68)
    cases = load_cases(_write(directory, "cases.yaml", _VALID_YAML))
    print(f" 加载成功，共 {len(cases)} 条用例")
    for index, case in enumerate(cases, start=1):
      print(f" 第 {index} 条：")
      print(f"  input   = {case.input!r}")
      print(f"  expected  = type={case.expected.type!r} value={case.expected.value!r}")
      print(f"  context  = {case.context}")
      print(f"  score_tags = {[(tag.name, tag.weight) for tag in case.score_tags]}")
      print(f"  weights  = {case.weights}")

    print()
    print("=" * 68)
    print("2. 正常路径：同一份内容写成 .json，扩展名决定解析分支")
    print("=" * 68)
    json_cases = load_cases(_write(directory, "cases.json", _VALID_JSON))
    print(f" 加载成功，共 {len(json_cases)} 条用例")
    print(f" 第 1 条 expected.value = {json_cases[0].expected.value!r}")

    print()
    print("=" * 68)
    print("3. 坏数据：YAML 语法错误——LoaderError 带行号")
    print("=" * 68)
    _show_loader_error(_write(directory, "broken.yaml", _BROKEN_YAML))

    print()
    print("=" * 68)
    print("4. 坏数据：缺少必填字段 expected——ValidationError 定位到字段")
    print("=" * 68)
    _show_validation_error(_write(directory, "missing.yaml", _MISSING_FIELD_YAML))

    print()
    print("=" * 68)
    print("5. 坏数据：expected.type 取值非法——Literal 拒绝拼写错误")
    print("=" * 68)
    _show_validation_error(_write(directory, "badtype.yaml", _BAD_TYPE_YAML))

    print()
    print("=" * 68)
    print("6. 坏数据：多写未知字段——extra='forbid' 拦截拼错的键名")
    print("=" * 68)
    _show_validation_error(_write(directory, "unknown.yaml", _UNKNOWN_FIELD_YAML))

    print()
    print("小结：文件层面归 LoaderError（可定位行号），"
       "用例层面归 ValidationError（可定位字段）。")


if __name__ == "__main__":
  main()

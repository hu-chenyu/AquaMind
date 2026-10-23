"""D25 Judge 适配器量化验证：异族判定矩阵 + JSON 契约校验矩阵。

用途：
  把「强制异族」与「结构化 JSON 契约」这两条硬约束变成**可复跑的数字**，而不是只靠
  单测里的零散断言。脚本分两张矩阵：

  1. **异族判定矩阵**：给定模型对，走真实的 ``JudgeAdapter`` 构造期校验，核对
     「同族必拒 / 异族必过」是否与预期一一对应；
  2. **JSON 契约校验矩阵**：用 ``CallableAdapter`` 注入各类裁判输出，走真实的
     ``aevaluate``，核对「合法输出判为通过、违约输出判为不可判」是否与预期一致。

运行（在仓库根目录）::

  .venv\\Scripts\\python.exe scripts/judge_contract_check.py

退出码：0 = 两张矩阵全部与预期一致；1 = 存在偏差（逐行标出不符项）。

输出可复现：全部输入是脚本内的固定数据，不读时钟、不读随机数、不读环境变量，
因此同一条命令的输出逐字相同。全脚本**不发起任何网络请求**、不需要任何 API 密钥
（CI 零 key）：裁判响应由本地函数注入。

Windows 备注：本脚本输出 UTF-8 文本。若要重定向到文件，先执行
``$env:PYTHONIOENCODING="utf-8"``，否则默认代码页（cp936）会把中文写成另一种编码。
"""

from __future__ import annotations

import asyncio
import sys
import unicodedata

from aquamind.adapters import CallableAdapter
from aquamind.judge import JudgeAdapter, JudgeError

# 被测模型与裁判模型的组合：第三列为 True 表示「预期在构造期被拒（同族）」。
# 前 5 组同族（含完全相同字符串与「厂商/模型」分段形态），后 5 组异族。
_FAMILY_CASES: list[tuple[str, str, bool]] = [
  ("gpt-4o", "gpt-4o-mini", True),
  ("claude-3-opus", "claude-3-sonnet", True),
  ("deepseek-chat", "deepseek-reasoner", True),
  ("qwen-max", "qwen-plus", True),
  ("acme-internal-7b", "acme-internal-7b", True),
  ("gpt-4o", "claude-3-opus", False),
  ("qwen-max", "deepseek-chat", False),
  ("gpt-4o", "acme-unknown-7b", False),
  ("acme-alpha", "acme-beta", False),
  ("openai/gpt-4o", "anthropic/claude-3-opus", False),
]

# 裁判输出样例：第二列是预期出口（通过 / 不可判），第三列是裁判返回的原始文本。
_CONTRACT_CASES: list[tuple[str, str, str]] = [
  ("合法三字段", "通过", '{"passed": true, "score": 0.82, "reason": "结论正确"}'),
  (
    "带前后缀文本",
    "通过",
    '判定如下：{"passed": false, "score": 0.18, "reason": "缺少因果说明"} 以上。',
  ),
  ("缺 passed 字段", "不可判", '{"score": 0.9, "reason": "理由"}'),
  ("score 为数字型字符串", "不可判", '{"passed": true, "score": "0.9", "reason": "理由"}'),
  ("score 越界", "不可判", '{"passed": true, "score": 1.5, "reason": "理由"}'),
  ("完全坏 JSON", "不可判", "这个回答不错，给 0.9 分"),
  ("顶层是数组", "不可判", '[{"passed": true, "score": 1.0, "reason": "理由"}]'),
  (
    "多余字段",
    "不可判",
    '{"passed": true, "score": 0.9, "reason": "理由", "confidence": 0.7}',
  ),
]

# 契约矩阵里的固定模型对：异族，保证构造期不会被异族校验拦下
_SUT_MODEL = "gpt-4o"
_JUDGE_MODEL = "claude-3-opus"


def _display_width(text: str) -> int:
  """计算终端显示宽度（中日韩全角字符按 2 列计）。

  直接用 ``len()`` 补空格会让含中文的表格列宽错位——一个汉字占两列却只算一个字符，
  表格看起来就像没对齐。

  Args:
    text: 待测宽度的单元格文本。

  Returns:
    int: 显示列数。
  """
  return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
  """按显示宽度右补空格到指定列宽。

  Args:
    text: 单元格文本。
    width: 目标显示宽度。

  Returns:
    str: 补足空格后的文本（已超宽时原样返回）。
  """
  return text + " " * max(0, width - _display_width(text))


def _print_table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
  """打印四列对齐的表格（表头 + 分隔线 + 数据行）。

  Args:
    header: 表头单元格。
    rows: 数据行，每行长度与表头一致。
  """
  widths = [_display_width(cell) for cell in header]
  for row in rows:
    for index, cell in enumerate(row):
      widths[index] = max(widths[index], _display_width(cell))
  line = "  ".join(_pad(cell, widths[index]) for index, cell in enumerate(header))
  print(line)
  print("-" * _display_width(line))
  for row in rows:
    print("  ".join(_pad(cell, widths[index]) for index, cell in enumerate(row)))


def _try_construct(sut_model: str, judge_model: str) -> bool:
  """尝试构造 ``JudgeAdapter``，返回是否被异族校验拒绝。

  Args:
    sut_model: 被测模型名。
    judge_model: 裁判模型名。

  Returns:
    bool: 被 ``JudgeError`` 拒绝返回 True，构造成功返回 False。
  """
  try:
    JudgeAdapter(
      CallableAdapter(lambda messages: "{}"),
      sut_model=sut_model,
      judge_model=judge_model,
    )
  except JudgeError:
    return True
  return False


def _run_family_matrix() -> tuple[int, int]:
  """跑异族判定矩阵并打印表格。

  Returns:
    tuple[int, int]: (符合预期组数, 总组数)。
  """
  print("== 异族判定矩阵（构造期校验：同族拒 / 异族过）==")
  rows: list[tuple[str, ...]] = []
  matched = 0
  for sut_model, judge_model, expect_reject in _FAMILY_CASES:
    rejected = _try_construct(sut_model, judge_model)
    expected = "拒绝" if expect_reject else "放行"
    if rejected:
      actual = "拒绝（同族）" if expect_reject else "拒绝（误拒）"
    else:
      actual = "放行（异族）" if not expect_reject else "放行（漏拒）"
    ok = rejected == expect_reject
    matched += 1 if ok else 0
    rows.append((f"{sut_model} vs {judge_model}", expected, actual, "一致" if ok else "偏差"))
  _print_table(("用例", "预期", "实测", "结论"), rows)
  return matched, len(_FAMILY_CASES)


async def _run_contract_case(content: str) -> tuple[bool, str]:
  """跑一组契约校验：注入裁判输出，看出口是「通过」还是「不可判」。

  Args:
    content: 注入的裁判输出文本。

  Returns:
    tuple[bool, str]: (是否成功得到 JudgeOutput, 实测结果描述)。
  """
  adapter = JudgeAdapter(
    CallableAdapter(lambda messages: content),
    sut_model=_SUT_MODEL,
    judge_model=_JUDGE_MODEL,
  )
  try:
    output = await adapter.aevaluate("2+2=?", "4")
  except JudgeError as exc:
    return False, f"不可判（{exc.context.get('stage')}）"
  return True, f"通过（score={output.score}）"


def _run_contract_matrix() -> tuple[int, int]:
  """跑 JSON 契约校验矩阵并打印表格。

  Returns:
    tuple[int, int]: (符合预期组数, 总组数)。
  """
  print()
  print("== JSON 契约校验矩阵（aevaluate 出口：合法过 / 违约判不可判）==")
  rows: list[tuple[str, ...]] = []
  matched = 0
  for name, expected, content in _CONTRACT_CASES:
    passed, actual = asyncio.run(_run_contract_case(content))
    ok = passed == (expected == "通过")
    matched += 1 if ok else 0
    rows.append((name, expected, actual, "一致" if ok else "偏差"))
  _print_table(("用例", "预期", "实测", "结论"), rows)
  return matched, len(_CONTRACT_CASES)


def _rate(matched: int, total: int) -> str:
  """把「符合预期组数 / 总组数」格式化成百分比字符串。

  Args:
    matched: 符合预期组数。
    total: 总组数。

  Returns:
    str: 形如 ``8/8 = 100.0%`` 的字符串。
  """
  return f"{matched}/{total} = {matched / total * 100:.1f}%"


def main() -> int:
  """跑两张矩阵并打印汇总。

  Returns:
    int: 全部符合预期返回 0，存在偏差返回 1。
  """
  family_matched, family_total = _run_family_matrix()
  contract_matched, contract_total = _run_contract_matrix()
  print()
  print("== 汇总 ==")
  print(f"异族判定通过率     : {_rate(family_matched, family_total)}")
  print(f"JSON 契约校验通过率: {_rate(contract_matched, contract_total)}")
  all_matched = family_matched == family_total and contract_matched == contract_total
  print(f"总判定             : {'全部符合预期' if all_matched else '存在偏差'}")
  return 0 if all_matched else 1


if __name__ == "__main__":
  sys.exit(main())

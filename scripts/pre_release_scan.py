#!/usr/bin/env python3
"""发布前最小扫描（Day11 添加）。

检查两类问题：
1. tracked 内容里的禁止模式（对外材料不应出现的口径）
2. 未跟踪文件里的内部材料关键词（不应被 git add -A 带进仓库）

用法：
    python scripts/pre_release_scan.py
退出码：0 = 通过；1 = 命中禁止模式
"""

from __future__ import annotations

import subprocess
import sys

# tracked 内容禁止出现：与对外口径冲突的表述
FORBIDDEN_TRACKED = [
    # 注：PyPI 分发已恢复（2026-10-09 决策），故不再禁止 pypi.org / pip install aquamind
    "校准夹具",
    "v0.0.4",
    "2507.10541",
    "工程化协作",
]

# 未跟踪文件名禁止包含：内部评审材料关键词
FORBIDDEN_NAME_PATTERNS = ["终评", "评审", "加强点", "共识"]


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, encoding="utf-8"
    )


def main() -> int:
    errors: list[str] = []

    # 1) tracked 内容扫描
    # 注意：git grep 的 -e 必须用空格分隔（-e PATTERN）。写成 -e=PATTERN 会
    # 静默匹配不到任何东西，扫描会"假通过"。且 -e 全部必须位于 "--" 之前。
    grep = _git(
        "grep", "-n", "-I",
        *[arg for p in FORBIDDEN_TRACKED for arg in ("-e", p)],
        "--",
    )
    # git grep 无匹配时退出码为 1，属正常
    if grep.returncode not in (0, 1):
        errors.append(f"git grep 执行失败：{grep.stderr.strip()}")
    elif grep.returncode == 0 and grep.stdout.strip():
        errors.append("tracked 内容命中禁止模式：\n" + grep.stdout.strip())

    # 2) 未跟踪文件名扫描
    status = _git("status", "--porcelain")
    if status.returncode != 0:
        errors.append(f"git status 执行失败：{status.stderr.strip()}")
    else:
        for line in status.stdout.splitlines():
            if not line.startswith("??"):
                continue
            name = line[3:].strip().strip('"')
            if any(p in name for p in FORBIDDEN_NAME_PATTERNS):
                errors.append(f"未跟踪内部材料未被忽略：{name}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"\n扫描失败：{len(errors)} 项", file=sys.stderr)
        return 1

    print("扫描通过：未发现禁止模式或未忽略的内部材料。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""发布前最小扫描。

检查两类问题：
1. tracked 内容里的禁止模式（对外材料不应出现的口径）
2. 未跟踪文件里的材料关键词（不应被 git add -A 带进仓库）

词表不随仓库分发，按以下优先级解析：
    1. 命令行 --wordlist PATH
    2. 环境变量 AQUAMIND_SCAN_WORDLIST
    3. 仓库根 .wordlist.local（已 gitignore）
三者都缺失或内容为空时直接失败，不静默放行。

用法：
    python scripts/pre_release_scan.py [--wordlist PATH]
退出码：0 = 通过；1 = 命中禁止模式；2 = 词表缺失或不可读
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

WORDLIST_ENV = "AQUAMIND_SCAN_WORDLIST"
DEFAULT_WORDLIST = Path(__file__).resolve().parent.parent / ".wordlist.local"

# 未跟踪文件名禁止包含：材料文件名关键词
FORBIDDEN_NAME_PATTERNS = ["内部", "计划草稿", "draft", "wip"]


def load_wordlist(explicit: str | None) -> list[str]:
    """解析词表并返回非注释、非空白的模式列表。

    优先级：显式参数 > 环境变量 > 仓库根默认文件。
    文件缺失抛 FileNotFoundError；无有效条目抛 ValueError。
    """
    if explicit:
        path = Path(explicit)
    elif os.environ.get(WORDLIST_ENV):
        path = Path(os.environ[WORDLIST_ENV])
    else:
        path = DEFAULT_WORDLIST

    if not path.is_file():
        raise FileNotFoundError(
            f"未找到词表文件：{path}\n"
            "请创建该文件（每行一个禁止模式，# 开头为注释），\n"
            f"或用 --wordlist 指定路径，或设置 {WORDLIST_ENV} 环境变量。"
        )

    # utf-8-sig：容忍 Windows 编辑器写入的 BOM，否则首条模式会被 \ufeff 污染成永远匹配不到
    patterns = [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not patterns:
        raise ValueError(f"词表无有效条目（全为注释或空行）：{path}")
    return patterns


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布前最小扫描")
    parser.add_argument(
        "--wordlist",
        help=f"词表路径；缺省依次回退到 {WORDLIST_ENV} 与 .wordlist.local",
    )
    args = parser.parse_args(argv)

    try:
        patterns = load_wordlist(args.wordlist)
    except (OSError, ValueError) as exc:
        print(f"扫描失败：{exc}", file=sys.stderr)
        return 2

    errors: list[str] = []

    # 1) tracked 内容扫描（含本文件自身：词表已外置，自身不应命中任何模式）
    # 注意：git grep 的 -e 必须用空格分隔（-e PATTERN）。写成 -e=PATTERN 会
    # 静默匹配不到任何东西，扫描会"假通过"。且 -e 全部必须位于 "--" 之前。
    grep = _git(
        "grep", "-n", "-I",
        *[arg for p in patterns for arg in ("-e", p)],
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
                errors.append(f"未跟踪材料未被忽略：{name}")

    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"\n扫描失败：{len(errors)} 项", file=sys.stderr)
        return 1

    print(f"扫描通过：{len(patterns)} 条模式，未发现禁止模式或未忽略的材料。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

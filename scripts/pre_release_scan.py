#!/usr/bin/env python3
"""发布前最小扫描。

检查三类问题：
1. tracked 内容里的禁止模式（对外材料不应出现的口径）
2. 未跟踪文件里的材料关键词（不应被 git add -A 带进仓库）
3. tracked 内容里的疑似明文凭据（内置正则规则，不依赖可配置词表）

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
import re
import subprocess
import sys
from pathlib import Path

WORDLIST_ENV = "AQUAMIND_SCAN_WORDLIST"
DEFAULT_WORDLIST = Path(__file__).resolve().parent.parent / ".wordlist.local"

# 未跟踪文件名禁止包含：材料文件名关键词
FORBIDDEN_NAME_PATTERNS = ["内部", "计划草稿", "draft", "wip"]

# 内置的**密钥形态**检测规则（正则，非字面量）。
#
# 为什么单独内置、而不是放进可配置词表：
#   词表是本地文件、可被修改或清空；一旦密钥检测依赖它，删掉一行就能绕过整个
#   防线。凭据泄露的后果不受「词表是谁写的」影响，所以这条规则必须写死在脚本里。
#   同理，词表支持中文等任意文本，这里只处理有固定形态的凭据，故用正则而非字面量。
#
# 三条规则分别对应常见的：OpenAI/Anthropic 风格密钥、AWS Access Key ID、
# Authorization 头里的 Bearer 令牌。
#
# 正则必须兼容两套引擎：本文件用 `git grep -E` 做粗筛（POSIX ERE），
# 再用 Python `re` 做逐行精筛。因此**不能用非捕获组 (?:...)**——POSIX ERE
# 遇 `(?:...)` 会 fatal 报 "Invalid preceding regular expression"，让扫描
# 每次假红，甚至被当成字面量而完全失效。
#
# sk- 的字符类必须含 `-`：现行密钥形态普遍带分段前缀
# （OpenAI 的 sk-proj- / sk-svcacct- / sk-admin-，Anthropic 的
# sk-ant-api03- / sk-ant-oat01-）。早期写成 [A-Za-z0-9] 时这些形态全部漏报，
# 已实测确认漏 5 类。
#
# 仓库内的示例与脱敏用例会写入"长得像真凭据"的占位串，它们**会被规则命中**，
# 由下方 SECRET_VALUE_ALLOWLIST 精确放行；sk- 类占位则刻意短于 20 字符阈值，
# 天然不中，无需豁免。
SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_-]{20,}", "疑似 OpenAI/Anthropic 风格密钥（sk- 前缀）"),
    (r"AKIA[0-9A-Z]{16}", "疑似 AWS Access Key ID（AKIA 前缀）"),
    (r"Bearer [A-Za-z0-9._-]{20,}", "疑似 Authorization: Bearer 令牌"),
]

# 密钥规则的**占位值豁免**：只豁免下面这几个确切的假凭据字符串。
#
# 为什么豁免：这些文件必须写入"长得像真凭据"的假值，否则测不到脱敏逻辑。
# 为什么按值而非按文件豁免：按文件豁免会让该文件内的**任何**真实凭据一起漏检
# （已实测确认）。按值豁免则只有这三个确切的占位串不报警，同一文件里换任何
# 别的密钥仍会被抓到。
#
# 收紧替代方案（要求更长的随机串、含特定字符集）被否决：那会让规则随阈值
# 收紧而漏掉真实凭据——豁免是可见、可审计、可逐条撤销的，收紧不是。
SECRET_VALUE_ALLOWLIST = {
    "Bearer demo-token-not-a-real-credential",  # examples/replay_demo/demo_record_play.py
    "Bearer baseline-token-not-a-real-cred",  # examples/replay_demo/generate_baseline.py
    "Bearer super-secret-credential",  # tests/test_replay.py，脱敏用例的输入夹具
}


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

    # 1b) 内置密钥形态检测（不依赖可配置词表，无法被绕过）
    # 与词表扫描同理：git grep 只认第一个 pattern，其余会被当成 revision 解析
    # （报 "unable to resolve revision"），因此每条规则都要用 -e 单独传入。
    secret = _git(
        "grep", "-n", "-I", "-E",
        *[arg for p, _ in SECRET_PATTERNS for arg in ("-e", p)],
        "--",
    )
    if secret.returncode not in (0, 1):
        errors.append(f"密钥形态扫描执行失败：{secret.stderr.strip()}")
    elif secret.returncode == 0 and secret.stdout.strip():
        found = []
        for line in secret.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            for pat, desc in SECRET_PATTERNS:
                for m in re.finditer(pat, parts[2]):
                    if m.group(0) in SECRET_VALUE_ALLOWLIST:
                        continue
                    found.append(f"  {parts[0]}:{parts[1]}  {desc}")
                    break
                else:
                    continue
                break
        if found:
            errors.append(
                "tracked 内容疑似含明文凭据（内置规则，不可通过词表绕过）：\n" + "\n".join(found)
            )

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

    print(
        f"扫描通过：{len(patterns)} 条词表模式 + {len(SECRET_PATTERNS)} 条内置密钥规则，"
        "未发现禁止模式或未忽略的材料。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

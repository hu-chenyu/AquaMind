#!/usr/bin/env python3
"""cassette 专用密钥扫描（M1-D15：录制期脱敏之后的第二道防线）。

扫描指定目录下所有 .json cassette 文件，检测两类明文密钥泄漏：

1. **高熵密钥前缀形态**：sk- / pypi- / glpat- / ghp_ / xoxb- 等常见厂商前缀
   + 16 位以上字符。该规则扫全文（含 JSON 字符串内部嵌套的密钥文本）。
2. **未被脱敏的敏感字段**：字段名（大小写不敏感）包含 key / token / secret
   等模式，而值既不是 "REDACTED" 也不是 "sha256:" 短摘要，且长度大于 8。

设计口径（与 src/aquamind/replay.py 的录制期脱敏同口径，但刻意**不 import**
aquamind——本脚本是独立防线，应在未安装项目包的环境里也能直接运行；两处规则
若需变更必须同步修改，replay.py 是录制侧权威，本文件是扫描侧核查）：

- **已脱敏形态识别**：值等于 "REDACTED"（请求体/响应头/响应体）或形如
  "sha256:..."（请求头摘要，D6a 口径）都视为已脱敏，不报警；
- **失败即失败（fail-closed）**：文件不可读、JSON 解析失败、结构嵌套过深时
  按疑似泄漏处理（计入退出码 1）——扫描工具静默跳过文件等于给泄漏开后门；
- **不回显密钥**：报告只给文件、行号、字段路径，绝不打印匹配到的值本身，
  避免扫描输出成为二次泄漏源。

用法（在仓库根目录执行）：
    .venv\\Scripts\\python.exe scripts/scan_cassette_secrets.py --dir .aquamind_cache

退出码：0 = 无泄漏；1 = 发现疑似泄漏；2 = 参数或目录错误
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# 敏感字段模式：与 src/aquamind/replay.py 的 _SENSITIVE_KEY_PATTERNS 同口径
# （大小写不敏感的子串「包含」；"api_key"/"apikey" 已被 "key" 覆盖，显式列出
# 是为与设计清单逐字对应；D15 复核补强 P3-6 与录制侧同步追加
# jwt/passwd/pwd/bearer）
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "key",
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "cookie",
    "session",
    "private",
    "jwt",
    "passwd",
    "pwd",
    "bearer",
)
# 已脱敏形态：请求体/响应头/响应体为 REDACTED 占位，请求头为 sha256: 短摘要
REDACTED_PLACEHOLDER = "REDACTED"
MASK_PREFIX = "sha256:"
# 敏感字段值的报警长度阈值：长度 > 8 才报警。短值（空串、"bearer"、"null" 之类
# 占位符）不构成可用凭据，报出来只会稀释报告信噪比
MIN_PLAINTEXT_LENGTH = 8

# 高熵密钥前缀规则（正则，非字面量）。字符类必须含 `-` 与 `_`：现行密钥形态
# 普遍带分段前缀（OpenAI 的 sk-proj- / sk-svcacct-，Anthropic 的 sk-ant-api03-），
# 只写 [A-Za-z0-9] 会让这些形态全部漏报。
PREFIX_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "sk- 系列密钥形态（OpenAI/Anthropic）"),
    (re.compile(r"pypi-[A-Za-z0-9_-]{16,}"), "pypi- 系列密钥形态（PyPI）"),
    (re.compile(r"glpat-[A-Za-z0-9_-]{16,}"), "glpat- 系列密钥形态（GitLab）"),
    (re.compile(r"ghp_[A-Za-z0-9]{16,}"), "ghp_ 系列密钥形态（GitHub）"),
    (re.compile(r"gho_[A-Za-z0-9]{16,}"), "gho_ 系列密钥形态（GitHub OAuth）"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{16,}"), "github_pat_ 系列密钥形态"),
    (re.compile(r"xox[bpoas]-[A-Za-z0-9-]{16,}"), "xox 系列密钥形态（Slack）"),
    (re.compile(r"hf_[A-Za-z0-9]{16,}"), "hf_ 系列密钥形态（Hugging Face）"),
    # —— D15 复核补强（P2-1）：常见密钥前缀补齐 ——
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA 前缀（AWS Access Key ID）"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "AIza 前缀（Google API Key）"),
    (re.compile(r"sk_live_[A-Za-z0-9_-]{16,}"), "sk_live_ 前缀（Stripe live key）"),
    (re.compile(r"sk_test_[A-Za-z0-9_-]{16,}"), "sk_test_ 前缀（Stripe test key）"),
    (re.compile(r"ghu_[A-Za-z0-9]{16,}"), "ghu_ 前缀（GitHub user-to-server token）"),
    (re.compile(r"ghs_[A-Za-z0-9]{16,}"), "ghs_ 前缀（GitHub server-to-server token）"),
    (re.compile(r"ghr_[A-Za-z0-9]{16,}"), "ghr_ 前缀（GitHub refresh token）"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "PEM 私钥块头"),
)


def _is_sensitive_field(name: str) -> bool:
    """判断字段名是否命中敏感模式（大小写不敏感的子串匹配）。

    Args:
        name: JSON 键名（HTTP 头名同口径）。

    Returns:
        bool: 命中任一模式时为 True。
    """
    lowered = name.lower()
    return any(pattern in lowered for pattern in SENSITIVE_KEY_PATTERNS)


def _is_masked(value: str) -> bool:
    """判断字段值是否已是脱敏形态（REDACTED 占位或 sha256: 短摘要）。

    Args:
        value: 待判断的字符串值。

    Returns:
        bool: 已脱敏时为 True。
    """
    return value == REDACTED_PLACEHOLDER or value.startswith(MASK_PREFIX)


def _join_path(prefix: str, key: str) -> str:
    """拼接字段路径（顶层字段不带前导点）。

    Args:
        prefix: 上级路径，顶层为空串。
        key: 当前字段名。

    Returns:
        str: 拼接后的路径，如 ``request_body.api_key``。
    """
    return f"{prefix}.{key}" if prefix else key


def _walk(value: Any, path: str, findings: list[str]) -> None:
    """递归遍历已解析的 JSON 树，收集「未被脱敏的敏感字段」发现。

    Args:
        value: 当前节点（dict/list/str/数值/None）。
        path: 当前节点的字段路径（如 ``request_body.api_key``）。
        findings: 发现列表，就地追加描述字符串（只含路径，不含值）。

    Returns:
        None: 结果写入 ``findings``。

    Raises:
        RecursionError: 嵌套深度超过解释器上限时抛出，由调用方按疑似泄漏处理。
    """
    if isinstance(value, dict):
        for key, item in value.items():
            child = _join_path(path, str(key))
            if (
                isinstance(item, str)
                and _is_sensitive_field(str(key))
                and not _is_masked(item)
                and len(item) > MIN_PLAINTEXT_LENGTH
            ):
                findings.append(f"{child}  敏感字段值未被脱敏")
            _walk(item, child, findings)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk(item, f"{path}[{index}]", findings)
    elif isinstance(value, str):
        # 载荷字段（request_body / response_body）内部常嵌着一段 JSON 文本：
        # 结构上再走一遍，避免「字符串里的敏感字段」漏网
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                nested = json.loads(stripped)
            except json.JSONDecodeError:
                return
            _walk(nested, path, findings)


def _scan_file(path: Path) -> list[str]:
    """扫描单个 .json 文件，返回疑似泄漏描述列表（空列表 = 通过）。

    Args:
        path: 待扫描文件路径。

    Returns:
        list[str]: 疑似泄漏描述（含文件路径与行号或字段路径）；无发现时为空列表。
    """
    findings: list[str] = []
    try:
        # utf-8-sig：容忍 Windows 编辑器写入的 BOM（与仓库其他扫描口径一致）
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return [f"{path}  文件不可读（按疑似泄漏处理）"]

    # 规则①：全文扫描——能覆盖嵌在 JSON 字符串内部的密钥文本
    for pattern, description in PREFIX_RULES:
        for match in pattern.finditer(content):
            line_no = content.count("\n", 0, match.start()) + 1
            findings.append(f"{path}:{line_no}  命中{description}")

    # 规则②：结构化遍历，检查敏感字段值是否未被脱敏
    try:
        data: Any = json.loads(content)
    except json.JSONDecodeError:
        findings.append(f"{path}  JSON 解析失败（按疑似泄漏处理）")
        return findings
    walk_findings: list[str] = []
    try:
        _walk(data, "", walk_findings)
    except RecursionError:
        findings.append(f"{path}  结构嵌套过深，无法完整扫描（按疑似泄漏处理）")
    # 字段发现统一补文件路径前缀：多文件目录下必须能定位到是哪个文件
    findings.extend(f"{path}  字段 {item}" for item in walk_findings)
    return findings


def main(argv: list[str] | None = None) -> int:
    """命令行入口：扫描目录，打印结论并返回退出码。

    Args:
        argv: 命令行参数（None 时由 argparse 读 sys.argv）。

    Returns:
        int: 0 = 无泄漏；1 = 发现疑似泄漏；2 = 参数或目录错误。
    """
    parser = argparse.ArgumentParser(
        description="cassette 专用密钥扫描：检测 .json cassette 中的明文密钥",
    )
    parser.add_argument("--dir", required=True, help="待扫描目录（递归查找其中所有 .json 文件）")
    args = parser.parse_args(argv)

    target = Path(args.dir)
    if not target.is_dir():
        print(f"扫描失败：目录不存在或不是目录：{target}", file=sys.stderr)
        return 2

    files = sorted(target.rglob("*.json"))
    findings: list[str] = []
    for file in files:
        findings.extend(_scan_file(file))

    if findings:
        print(f"发现 {len(findings)} 处疑似明文密钥：")
        for finding in findings:
            print(f"  {finding}")
        print(f"扫描失败：检测到明文密钥泄漏（目录: {target}，共扫描 {len(files)} 个文件）")
        return 1

    print(f"扫描通过：未发现明文密钥（目录: {target}，共扫描 {len(files)} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
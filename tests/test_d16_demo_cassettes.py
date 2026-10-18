"""D16 工程演示采样 cassette 的验收测试。

覆盖范围（对应 D16 的五项验收要求）：
  1. ``examples/demo_cassettes/`` 恰好 10 个 cassette，文件名等于请求指纹，逐个通过
    ``Cassette`` 契约校验且格式为 v2；
  2. 场景覆盖度：流式/非流式、成功/429/500/超时、多轮对话、工具调用各至少 1 条，
    且全部标注为非观测（``is_observation is False``）；另含坏数据拦截（请求变更后
    不得静默命中已有 cassette）；
  3. 每个 cassette 可回放：``find_match`` + ``play()`` 不抛异常、状态码与响应体一致；
    时序回放逐片产出且片段文本与录制时逐字相同；
  4. 脱敏零泄漏：请求头为 sha256 摘要、敏感字段为 REDACTED、演示凭据明文零落盘，
    并用 ``scripts/scan_cassette_secrets.py`` 子进程复扫（退出码 0），另加**阳性对照**
    （注入明文密钥的副本必须被判红，退出码 1），避免「扫描通过」是空转结论；
  5. README 标注：含「演示采样」「非观测」「不进入任何统计结论」等关键词，且登记了
    全部 10 个文件名（目录、文档、场景登记表三者不漂移）。

所有用例只读仓库内已录制的文件，零网络、零真实密钥（CI 零 key）；场景登记表按文件路径
从 ``scripts/record_demo_cassettes.py`` 动态载入，避免「测试再抄一份请求定义」导致两份
事实源各自漂移。
"""

from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from aquamind.exceptions import ReplayError
from aquamind.replay import (
  CURRENT_FORMAT_VERSION,
  Cassette,
  find_match,
  play,
  play_timed,
  replay_request,
)

# 仓库根按测试文件位置反推，不依赖运行目录（pytest 可能从任意 CWD 启动）
_REPO_ROOT = Path(__file__).resolve().parents[1]
# 演示采样目录（D16 交付物）
_DEMO_DIR = _REPO_ROOT / "examples" / "demo_cassettes"
# 一键录制脚本：场景清单的唯一事实源
_RECORD_SCRIPT_PATH = _REPO_ROOT / "scripts" / "record_demo_cassettes.py"
# cassette 密钥扫描脚本（第二道防线）
_SCAN_SCRIPT_PATH = _REPO_ROOT / "scripts" / "scan_cassette_secrets.py"
# 演示采样条数：多一条少一条都说明注册表或目录被改过
_EXPECTED_COUNT = 10
# 时序回放用的高倍率：把分片间隔压到定时器粒度（约 1~15.6ms）以下，
# 于是回放不会真去 sleep，用例只验证「逐片产出顺序与文本」，不测调度精度
_FAST_SPEED = 1000.0
# 阳性对照用的假密钥：以「sk-」+ 长串**两段拼接**构造，源码里不出现
# 「sk- 紧跟 20 位以上随机串」的连续字面量，否则 tests/ 会被仓库自带的密钥扫描
# （pre_release_scan.py 的 sk-{20,} 规则）判为疑似明文凭据
_FAKE_LEAKED_KEY = "sk-" + "demo0leak1demo0leak2demo0leak3"

# README 必须出现的关键词：前三条是 D16 计划原文的标注要求，
# 后三条是「非观测」的具体含义（不进预注册冻结、不进功效计算、正式观测在 D47）
_README_KEYWORDS = (
  "演示采样",
  "非观测",
  "不进入任何统计结论",
  "不参与预注册冻结",
  "不计入统计功效计算",
  "D47",
)
# 16 位十六进制串（请求指纹形态），用于核对 README 与目录是否一一对应
_HASH_PATTERN = re.compile(r"\b[0-9a-f]{16}\b")


@functools.lru_cache(maxsize=1)
def _load_record_module() -> ModuleType:
  """按文件路径载入录制脚本模块，取回其中的场景登记表。

  用动态载入而非 import：``scripts/`` 不是包，也没有 ``__init__.py``；直接
  ``import`` 需要改 sys.path，而改 sys.path 会影响同一进程里的其他用例。

  Returns:
    ModuleType: 已执行的 ``scripts/record_demo_cassettes.py`` 模块对象；
      其 ``demo_scenarios()`` 即 10 个演示场景的登记表。
  """
  spec = importlib.util.spec_from_file_location("d16_record_demo_cassettes", _RECORD_SCRIPT_PATH)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  # 必须先登记到 sys.modules 再执行：脚本用了 `from __future__ import annotations`，
  # dataclass 处理字符串注解时要按 __module__ 回查模块命名空间，未登记的模块会让
  # 它拿到 None 并抛 AttributeError（发生在 exec_module 期间）
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


def _run_scan(target: Path) -> subprocess.CompletedProcess[str]:
  """以子进程运行 cassette 密钥扫描脚本（真实 CLI 口径，含退出码）。

  Args:
    target: 传给 ``--dir`` 的扫描目录。

  Returns:
    subprocess.CompletedProcess[str]: 含 returncode/stdout/stderr 的进程结果。
  """
  # PYTHONIOENCODING 固定子进程输出为 UTF-8：Windows 管道默认走本地代码页（cp936），
  # 与父进程按 UTF-8 解码的约定不一致会直接炸在解码步骤上
  env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
  return subprocess.run(
    [sys.executable, str(_SCAN_SCRIPT_PATH), "--dir", str(target)],
    capture_output=True,
    text=True,
    encoding="utf-8",
    env=env,
    check=False,
  )


def _cassette_path(scenario: Any) -> Path:
  """按场景的请求指纹算出其 cassette 文件路径。

  Args:
    scenario: 场景登记表中的一条（含 ``request``）。

  Returns:
    Path: ``examples/demo_cassettes/{request_hash}.json``。
  """
  return _DEMO_DIR / f"{Cassette.compute_request_hash(scenario.request)}.json"


@pytest.fixture(scope="module")
def scenarios() -> tuple[Any, ...]:
  """10 个演示场景（与录制脚本同源，非本文件另行手抄的一份）。

  Returns:
    tuple[Any, ...]: 场景对象元组，顺序与 README 清单一致。
  """
  return _load_record_module().demo_scenarios()


def test_demo_cassettes_are_exactly_ten_and_valid(scenarios: tuple[Any, ...]) -> None:
  """10 个 cassette 全部存在、JSON 合法、通过 Cassette 契约且格式为 v2。"""
  files = sorted(_DEMO_DIR.glob("*.json"))
  # 恰好 10 个：多于 10 说明有残留文件，少于 10 说明有场景没录上
  assert len(files) == _EXPECTED_COUNT
  assert len(scenarios) == _EXPECTED_COUNT

  for scenario in scenarios:
    request_hash = Cassette.compute_request_hash(scenario.request)
    path = _DEMO_DIR / f"{request_hash}.json"
    assert path.is_file(), f"{scenario.scenario_id} 的 cassette 缺失：{path.name}"

    payload = json.loads(path.read_text(encoding="utf-8"))
    cassette = Cassette.model_validate(payload)  # 契约校验：字段/类型/取值范围
    # 文件名即请求指纹，且与文件内记录的指纹一致（否则 find_match 永远匹配不上）
    assert cassette.request_hash == request_hash == path.stem
    # 与 M1-D15 收口后的格式完全一致：v2（流式带 timing，非流式为 null）
    assert cassette.format_version == CURRENT_FORMAT_VERSION


def test_demo_scenarios_cover_required_shapes(scenarios: tuple[Any, ...]) -> None:
  """场景覆盖度：流式/非流式、成功/429/500/超时、多轮、工具调用齐备且全部非观测。"""
  statuses = {scenario.response.status_code for scenario in scenarios}
  # 成功与三类失败（429 限流 / 500 服务端错误 / 超时）都要有
  assert {200, 429, 500, 504} <= statuses

  streaming = [scenario for scenario in scenarios if scenario.chunks]
  assert streaming, "至少要有 1 条流式场景"
  assert any(scenario.chunks is None for scenario in scenarios), "至少要有 1 条非流式场景"

  # 多轮对话：至少一条请求的 messages 达到 3 条以上（system + 两轮以上）
  message_counts = [
    len(json.loads(scenario.request.body or "{}").get("messages", []))
    for scenario in scenarios
  ]
  assert max(message_counts) >= 3

  # 工具调用形态：流式与非流式各至少 1 条（响应体里出现 tool_calls）
  def _has_tool_calls(scenario: Any) -> bool:
    """判断该场景的响应体是否为工具调用形态。"""
    return "tool_calls" in (scenario.response.body or "")

  assert any(_has_tool_calls(scenario) for scenario in streaming)
  assert any(_has_tool_calls(scenario) for scenario in scenarios if scenario.chunks is None)

  # 分类标签非空：README 的场景表按它逐条解释，空标签会让文档失去锚点
  assert all(scenario.category.strip() for scenario in scenarios)
  # 演示采样不是观测运行：逐条显式断言，避免日后有人把它当成观测数据引用
  assert all(scenario.is_observation is False for scenario in scenarios)

  # 流式自洽：分片增量拼接必须等于响应体里对应字段的值（内容流看 content，
  # 工具调用流看 tool_calls[0].function.arguments）
  content_stream = next(s for s in scenarios if s.scenario_id == "stream_basic")
  content_cassette = find_match(content_stream.request, _DEMO_DIR)
  assert content_cassette is not None and content_cassette.timing
  content = json.loads(content_cassette.response_body)["choices"][0]["message"]["content"]
  assert "".join(chunk.text for chunk in content_cassette.timing) == content

  tool_stream = next(s for s in scenarios if s.scenario_id == "stream_tool_call")
  tool_cassette = find_match(tool_stream.request, _DEMO_DIR)
  assert tool_cassette is not None and tool_cassette.timing
  arguments = json.loads(tool_cassette.response_body)["choices"][0]["message"]["tool_calls"][0][
    "function"
  ]["arguments"]
  assert "".join(chunk.text for chunk in tool_cassette.timing) == arguments
  assert json.loads(arguments)["city"] == "杭州"

  # 坏数据拦截：请求体变了 → 指纹变了 → 不得静默命中一条别的录制
  changed_request = content_stream.request.model_copy(
    update={"body": (content_stream.request.body or "") + " 追加一个字符"}
  )
  assert find_match(changed_request, _DEMO_DIR) is None
  with pytest.raises(ReplayError):
    replay_request(changed_request, _DEMO_DIR)


def test_every_demo_cassette_replays_offline(scenarios: tuple[Any, ...]) -> None:
  """每个 cassette 都能被同一请求命中并离线回放，状态码一致、时序回放逐片不串。"""
  for scenario in scenarios:
    cassette = find_match(scenario.request, _DEMO_DIR)
    assert cassette is not None, f"{scenario.scenario_id} 无匹配 cassette"

    replayed = play(cassette)
    assert replayed.status_code == scenario.response.status_code
    if scenario.response_redacted:
      # 落盘响应体已被录制期脱敏改写（敏感字段 → REDACTED），故不与原 mock 逐字比对
      assert "REDACTED" in replayed.text
    else:
      # 未被脱敏改写的场景：回放文本必须与录制时的响应体逐字一致
      assert replayed.text == scenario.response.body

    # 高倍率时序回放：间隔被压到定时器粒度以下（不真 sleep），验证逐片产出与文本顺序
    replayed_chunks = list(play_timed(cassette, speed=_FAST_SPEED))
    if cassette.timing:
      assert replayed_chunks == [chunk.text for chunk in cassette.timing]
    else:
      # 无时序可调度（非流式 / timing 为 None）→ 瞬时回放，一次性给全
      assert replayed_chunks == [replayed.text]


def test_demo_cassettes_are_redacted(scenarios: tuple[Any, ...], tmp_path: Path) -> None:
  """脱敏零泄漏：摘要与 REDACTED 形态齐备、演示凭据明文不落盘、扫描脚本有牙齿。"""
  module = _load_record_module()
  plaintext_credentials = {
    "authorization 头": module.DEMO_AUTH_TOKEN,
    "第二枚 authorization 头": module.DEMO_USER_TOKEN,
    "x-api-key 头": module.DEMO_X_API_KEY,
    "请求体 api_key": module.DEMO_API_KEY,
    "请求体 session_id": module.DEMO_SESSION_ID,
    "响应体 access_token": module.DEMO_ACCESS_TOKEN,
    "响应头 Set-Cookie": module.DEMO_COOKIE,
  }

  for path in sorted(_DEMO_DIR.glob("*.json")):
    content = path.read_text(encoding="utf-8")
    for label, value in plaintext_credentials.items():
      assert value not in content, f"{path.name} 落盘内容里出现明文「{label}」"
    # 请求头侧的敏感值只能以 sha256: 短摘要形态出现（参与指纹，保留区分度）
    for header, value in json.loads(content)["request_headers"].items():
      if header in {"authorization", "x-api-key"}:
        assert value.startswith("sha256:")

  # 脱敏探针：三通道（请求体 / 响应头 / 响应体）的结构化证据
  probe = next(s for s in scenarios if s.scenario_id == "redaction_probe")
  probe_payload = json.loads(_cassette_path(probe).read_text(encoding="utf-8"))
  stored_request_body = json.loads(probe_payload["request_body"])
  assert stored_request_body["api_key"] == "REDACTED"
  assert stored_request_body["session_id"] == "REDACTED"
  assert stored_request_body["model"] == "demo-model"  # 非敏感字段逐字保留
  assert probe_payload["response_headers"]["Set-Cookie"] == "REDACTED"
  assert json.loads(probe_payload["response_body"])["access_token"] == "REDACTED"

  # 保守脱敏的副作用（键名含 token 的计数也被替换）是刻意口径，落盘形态即证据
  basic = next(s for s in scenarios if s.scenario_id == "nonstream_basic")
  basic_payload = json.loads(_cassette_path(basic).read_text(encoding="utf-8"))
  basic_usage = json.loads(basic_payload["response_body"])["usage"]
  assert basic_usage["prompt_tokens"] == "REDACTED"
  assert basic_usage["total_tokens"] == "REDACTED"

  # 正例：目录复扫通过（退出码 0）
  assert _run_scan(_DEMO_DIR).returncode == 0

  # 阳性对照：把明文密钥写进一个副本，扫描必须判红——否则「扫描通过」可能只是空转
  leaky_payload = json.loads(_cassette_path(probe).read_text(encoding="utf-8"))
  leaky_payload["response_body"] = json.dumps({"note": _FAKE_LEAKED_KEY}, ensure_ascii=False)
  (tmp_path / "leaky.json").write_text(
    json.dumps(leaky_payload, ensure_ascii=False), encoding="utf-8"
  )
  leaky_result = _run_scan(tmp_path)
  assert leaky_result.returncode == 1
  # 扫描报告不回显密钥值本身（避免扫描输出变成二次泄漏源）
  assert _FAKE_LEAKED_KEY not in leaky_result.stdout


def test_readme_declares_demo_sampling_not_observation(scenarios: tuple[Any, ...]) -> None:
  """README 标注「演示采样 / 非观测 / 不进统计」，并登记全部 10 个文件名。"""
  readme_path = _DEMO_DIR / "README.md"
  assert readme_path.is_file()
  readme = readme_path.read_text(encoding="utf-8")

  for keyword in _README_KEYWORDS:
    assert keyword in readme, f"README 缺少标注关键词：{keyword}"

  expected_hashes = {Cassette.compute_request_hash(scenario.request) for scenario in scenarios}
  documented_hashes = set(_HASH_PATTERN.findall(readme))
  # 双向核对：10 个文件都在 README 里（正向），README 里也没有多余/残留的指纹（反向）
  assert expected_hashes <= documented_hashes
  assert documented_hashes == expected_hashes

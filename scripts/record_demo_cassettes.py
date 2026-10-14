#!/usr/bin/env python3
"""D16 工程演示采样：一键录制 10 个演示 cassette（幂等，已存在则跳过）。

用途：
  把 10 种典型请求形态（流式/非流式、成功/429/500/超时、多轮对话、工具调用、
  含敏感字段的脱敏探针）录成 cassette 存进 ``examples/demo_cassettes/``，供
  示例与文档直接打开对照。录制期脱敏（M1-D15）自动生效，录制完成后自动调用
  ``scripts/scan_cassette_secrets.py`` 复扫一遍（第二道防线）。

运行（在仓库根目录）：
  .venv\\Scripts\\python.exe scripts/record_demo_cassettes.py
  .venv\\Scripts\\python.exe scripts/record_demo_cassettes.py --force  # 覆盖重录

口径（对应 docs/private 计划文件 D16 行）：
  - 本目录内容是**工程演示采样**，不是**观测运行**：不进入预注册数据、不参与
   预注册冻结、不进统计功效计算、不进入任何统计结论；正式观测运行为 D47
   （3 档 ×1，同批、同工具、同口径）。
  - 全部请求由本地 mock 构造，**不发起任何真实网络调用**（CI 零 key）。URL 只
   作为请求指纹与场景标识的一部分，回放侧不会去连它。
  - cassette 格式与 M1-D15 收口后的形态完全一致：``format_version=2``，流式响应
   带 ``timing``，非流式为 ``timing: null``；文件名由请求指纹决定
   （``{request_hash}.json``，16 位十六进制）。

幂等：已存在的 cassette 默认跳过；``--force`` 时重新录制。重录会改写
``recorded_at``（随运行变化），因此会改动被 git 跟踪的文件内容——只想复现场景时
不加 ``--force`` 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aquamind.replay import Cassette, RequestInfo, ResponseInfo, record

# 演示 cassette 入库目录（仓库内，随 git 交付）
DEMO_CASSETTE_DIR = Path(__file__).resolve().parents[1] / "examples" / "demo_cassettes"
# 脱敏扫描脚本：录制完成后自动复扫，与 README 里的固定命令同一入口
SCAN_SCRIPT_PATH = Path(__file__).resolve().parent / "scan_cassette_secrets.py"
# 演示场景清单的固定条数：多一条少一条都说明注册表被改过
EXPECTED_SCENARIO_COUNT = 10

# 演示用凭据占位串：全部是明显的假值，不含真实密钥。
# DEMO_AUTH_TOKEN 落在 scripts/pre_release_scan.py 的 SECRET_VALUE_ALLOWLIST 里
# （「Bearer + 20 位以上」形态会被内置密钥规则命中，故按值精确豁免）；
# 其余几个刻意短于各扫描规则的阈值，天然不命中。
DEMO_AUTH_TOKEN = "Bearer demo-token-not-a-real-credential"
DEMO_USER_TOKEN = "Bearer demo-token-0002"
DEMO_X_API_KEY = "demo-x-api-key-0001"
DEMO_API_KEY = "sk-demo-0001"
DEMO_ACCESS_TOKEN = "eyJhbGciOiJIUzI1NiJ9.demo-access-token"
DEMO_SESSION_ID = "demo-session-0001"
DEMO_COOKIE = "session=demo-abc123; HttpOnly"

# 请求目标统一为本地演示端点：不解析、不连接，只参与指纹与场景说明
_BASE_URL = "http://127.0.0.1:8000/v1/chat/completions"
_CONTENT_TYPE_JSON = "application/json"


def _dumps(payload: dict[str, Any]) -> str:
  """把请求/响应载荷序列化为演示用 JSON 文本。

  ``sort_keys=True`` 使同一载荷无论字典书写顺序如何都得到同一请求指纹；
  ``ensure_ascii=False`` 让中文在 cassette 里直接可读（录制侧也只转义必要字符）。

  Args:
    payload: 待序列化的 JSON 载荷。

  Returns:
    str: 序列化后的 JSON 文本。
  """
  return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _completion_body(content: str, *, finish_reason: str = "stop") -> str:
  """构造一次 OpenAI 兼容的非流式补全响应体。

  Args:
    content: 助手回复正文。
    finish_reason: 结束原因，如 ``stop`` / ``tool_calls``。

  Returns:
    str: 响应体 JSON 文本。
  """
  return _dumps(
    {
      "id": "chatcmpl-demo-0001",
      "object": "chat.completion",
      "model": "demo-model",
      "choices": [
        {
          "index": 0,
          "finish_reason": finish_reason,
          "message": {"role": "assistant", "content": content},
        }
      ],
    }
  )


def _error_body(message: str, error_type: str, code: str) -> str:
  """构造 OpenAI 兼容风格的错误响应体。

  Args:
    message: 错误描述。
    error_type: 错误类型，如 ``rate_limit_error``。
    code: 错误码，如 ``rate_limit_exceeded``。

  Returns:
    str: 错误响应体 JSON 文本。
  """
  return _dumps({"error": {"code": code, "message": message, "type": error_type}})


def _tool_call_body(arguments: str, *, finish_reason: str = "tool_calls") -> str:
  """构造一次工具调用形态的补全响应体。

  Args:
    arguments: 工具入参 JSON 文本（流式场景下等于各分片拼接结果）。
    finish_reason: 结束原因，工具调用场景恒为 ``tool_calls``。

  Returns:
    str: 响应体 JSON 文本。
  """
  return _dumps(
    {
      "id": "chatcmpl-demo-tool-0001",
      "object": "chat.completion",
      "model": "demo-model",
      "choices": [
        {
          "index": 0,
          "finish_reason": finish_reason,
          "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
              {
                "index": 0,
                "id": "call_demo_0001",
                "type": "function",
                "function": {"name": "get_weather", "arguments": arguments},
              }
            ],
          },
        }
      ],
    }
  )


def _weather_tools() -> list[dict[str, Any]]:
  """构造一个演示用工具声明（供工具调用场景的请求体使用）。

  Returns:
    list[dict[str, Any]]: OpenAI 兼容的 tools 数组。
  """
  return [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "查询指定城市的天气（演示用，不发起真实调用）",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "城市名"},
            "unit": {"type": "string", "description": "温度单位"},
          },
          "required": ["city"],
        },
      },
    }
  ]


@dataclass(frozen=True)
class DemoScenario:
  """一个演示采样场景：请求 + 响应 + 流式分片 + 分类标签。

  Attributes:
    scenario_id: 场景标识（小写下划线），README 与测试据此引用同一场景。
    title: 一句话场景说明。
    category: 分类标签（如「非流式/成功」），供人工核对覆盖度。
    request: 请求信息，决定 cassette 文件名（请求指纹）。
    response: 响应信息（mock 构造，不来自真实端点）。
    chunks: 流式分片的 ``(到达毫秒, 增量文本)``；非流式场景为 None。
    is_observation: 是否为正式观测数据。演示采样恒为 False——D47 那批才是观测
      运行；该字段刻意显式存在，让「这不是观测数据」在代码里也是可断言的。
    response_redacted: 落盘响应体是否已被录制期脱敏改写（敏感字段 → REDACTED）。
      为 True 的场景，磁盘上的响应体与原 mock 响应体不再逐字相同。
  """

  scenario_id: str
  title: str
  category: str
  request: RequestInfo
  response: ResponseInfo
  chunks: tuple[tuple[float, str], ...] | None = None
  is_observation: bool = False
  response_redacted: bool = False


def _scenario_nonstream_basic() -> DemoScenario:
  """S1 非流式单轮成功：带鉴权头与噪声头，响应含 usage。

  usage 的键名（``prompt_tokens`` 等）含 ``token`` 子串，会被录制期脱敏按保守
  口径整值替换为 REDACTED——这是刻意保留的演示点（宁可多脱敏，不可漏脱敏）。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={
      "Content-Type": _CONTENT_TYPE_JSON,
      "Authorization": DEMO_AUTH_TOKEN,
      # 噪声头：不入指纹也不落盘，避免客户端版本变化让 cassette 莫名失效
      "User-Agent": "aquamind-demo/0.0.4",
    },
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "请用一句话介绍你自己"}],
        "temperature": 0.7,
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "id": "chatcmpl-demo-0001",
        "object": "chat.completion",
        "model": "demo-model",
        "choices": [
          {
            "index": 0,
            "finish_reason": "stop",
            "message": {
              "role": "assistant",
              "content": "我是 AquaMind 演示采样里的离线响应。",
            },
          }
        ],
        "usage": {"prompt_tokens": 18, "completion_tokens": 12, "total_tokens": 30},
      }
    ),
  )
  return DemoScenario(
    scenario_id="nonstream_basic",
    title="非流式单轮成功（鉴权头 + usage 保守脱敏）",
    category="非流式/成功/单轮",
    request=request,
    response=response,
    response_redacted=True,
  )


def _scenario_nonstream_multi_turn() -> DemoScenario:
  """S2 非流式多轮对话成功：请求体带 system + 两轮问答共 4 条 messages。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [
          {"role": "system", "content": "你是一个离线演示助手。"},
          {"role": "user", "content": "第一轮：介绍一下你自己"},
          {"role": "assistant", "content": "我是演示助手。"},
          {"role": "user", "content": "第二轮：用一句话总结上面的对话"},
        ],
        "temperature": 0.2,
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_completion_body("第二轮答复：以上是两轮问答的演示对话。"),
  )
  return DemoScenario(
    scenario_id="nonstream_multi_turn",
    title="非流式多轮对话成功（4 条 messages）",
    category="非流式/成功/多轮",
    request=request,
    response=response,
  )


# S3 流式场景的 chunk 到达时刻与增量文本：(到达毫秒, 增量文本)
_STREAM_CHUNKS: tuple[tuple[float, str], ...] = (
  (0.0, "分片"),
  (92.5, "演示"),
  (210.0, "响应"),
  (318.75, "结束。"),
)


def _scenario_stream_basic() -> DemoScenario:
  """S3 流式成功：4 个 chunk，增量文本拼接后等于响应体 content。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  content = "".join(text for _, text in _STREAM_CHUNKS)
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "请分片输出一句演示响应"}],
        "stream": True,
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={"Content-Type": "text/event-stream"},
    body=_completion_body(content),
  )
  return DemoScenario(
    scenario_id="stream_basic",
    title="流式成功（4 个 chunk，拼接等于响应体 content）",
    category="流式/成功",
    request=request,
    response=response,
    chunks=_STREAM_CHUNKS,
  )


# S4 工具调用的入参分片：流式场景下服务端逐片下发 arguments，
# 拼接结果等于响应体里 tool_calls[0].function.arguments
_TOOL_ARGUMENT_CHUNKS: tuple[tuple[float, str], ...] = (
  (0.0, '{"city":'),
  (74.0, ' "杭州",'),
  (168.25, ' "unit": "celsius"}'),
)


def _scenario_stream_tool_call() -> DemoScenario:
  """S4 流式工具调用：分片内容是工具入参 JSON 的增量碎片。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  arguments = "".join(text for _, text in _TOOL_ARGUMENT_CHUNKS)
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "杭州今天天气怎么样？"}],
        "tools": _weather_tools(),
        "stream": True,
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={"Content-Type": "text/event-stream"},
    body=_tool_call_body(arguments),
  )
  return DemoScenario(
    scenario_id="stream_tool_call",
    title="流式工具调用（入参 JSON 分片拼接）",
    category="流式/成功/工具调用",
    request=request,
    response=response,
    chunks=_TOOL_ARGUMENT_CHUNKS,
  )


def _scenario_nonstream_tool_call() -> DemoScenario:
  """S5 非流式工具调用：响应体带 finish_reason=tool_calls。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "上海今天天气怎么样？"}],
        "tools": _weather_tools(),
        "stream": False,
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_tool_call_body('{"city": "上海", "unit": "celsius"}'),
  )
  return DemoScenario(
    scenario_id="nonstream_tool_call",
    title="非流式工具调用成功（finish_reason=tool_calls）",
    category="非流式/成功/工具调用",
    request=request,
    response=response,
  )


def _scenario_rate_limited() -> DemoScenario:
  """S6 失败：429 限流，带 Retry-After 与剩余额度头。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "限流场景的演示请求"}],
      }
    ),
  )
  response = ResponseInfo(
    status_code=429,
    headers={
      "Content-Type": _CONTENT_TYPE_JSON,
      "Retry-After": "1",
      "X-RateLimit-Remaining-Requests": "0",
    },
    body=_error_body(
      "Rate limit reached for demo-model", "rate_limit_error", "rate_limit_exceeded"
    ),
  )
  return DemoScenario(
    scenario_id="rate_limited_429",
    title="失败：429 限流（含 Retry-After）",
    category="非流式/失败/429",
    request=request,
    response=response,
  )


def _scenario_server_error() -> DemoScenario:
  """S7 失败：500 服务端内部错误。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "服务端错误场景的演示请求"}],
      }
    ),
  )
  response = ResponseInfo(
    status_code=500,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_error_body("Internal server error (demo)", "server_error", "internal_error"),
  )
  return DemoScenario(
    scenario_id="server_error_500",
    title="失败：500 服务端错误",
    category="非流式/失败/500",
    request=request,
    response=response,
  )


def _scenario_timeout() -> DemoScenario:
  """S8 失败：超时，以 504 + 空响应体 + 形态标记头表示。

  口径说明：客户端侧真实超时（``httpx.TimeoutException``）根本没有 HTTP 响应，
  而 cassette 契约要求响应状态码，故取网关超时形态（504、空响应体、标记头）作为
  「可落盘的等价表示」。标记头只标识形态，不改回放语义。

  响应头**不设 Content-Type**（D16-fix 复核修正）：空响应体没有可声明的媒体类型，
  真实网关的 504 空体通常也不带 JSON 类型头——留一个 ``application/json`` 会让
  「有类型头 ⇒ 体里有 JSON」这条读者默认的对应关系在这个文件上落空。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "超时场景的演示请求"}],
      }
    ),
  )
  response = ResponseInfo(
    status_code=504,
    # 只有形态标记头：空体不声明 Content-Type（见 docstring 口径说明）
    headers={"X-AquaMind-Demo-Failure-Mode": "timeout"},
    body="",
  )
  return DemoScenario(
    scenario_id="timeout_504",
    title="失败：超时（504 + 空响应体 + 形态标记头）",
    category="非流式/失败/超时",
    request=request,
    response=response,
  )


def _scenario_multimodal_parts() -> DemoScenario:
  """S9 非流式多轮 + 多模态 content parts，请求头走 x-api-key。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={
      "Content-Type": _CONTENT_TYPE_JSON,
      # 敏感头：参与指纹，但落盘为 sha256: 短摘要
      "X-API-Key": DEMO_X_API_KEY,
    },
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [
          {
            "role": "user",
            "content": [
              {"type": "text", "text": "这张图里是什么？"},
              {
                "type": "image_url",
                "image_url": {"url": "http://127.0.0.1:8000/demo-assets/water.png"},
              },
            ],
          },
          {"role": "assistant", "content": "这是一张演示水样图片。"},
          {"role": "user", "content": "再用一句话概括。"},
        ],
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={"Content-Type": _CONTENT_TYPE_JSON},
    body=_completion_body("概括：一张演示水样图片。"),
  )
  return DemoScenario(
    scenario_id="multimodal_parts",
    title="非流式多轮 + 多模态 content parts（x-api-key 摘要）",
    category="非流式/成功/多轮/多模态",
    request=request,
    response=response,
  )


def _scenario_redaction_probe() -> DemoScenario:
  """S10 脱敏探针：三通道（请求体 / 响应头 / 响应体）敏感字段各命中一次。

  落盘的请求体与响应体因脱敏被改写，因此 ``response_redacted=True``；请求头
  另有一枚凭据走 sha256 摘要，用来对照「摘要 vs REDACTED」两种落盘形态。

  Returns:
    DemoScenario: 该场景的完整定义。
  """
  request = RequestInfo(
    method="POST",
    url=_BASE_URL,
    headers={
      "Content-Type": _CONTENT_TYPE_JSON,
      "Authorization": DEMO_USER_TOKEN,
    },
    body=_dumps(
      {
        "model": "demo-model",
        "messages": [{"role": "user", "content": "脱敏探针的演示请求"}],
        "api_key": DEMO_API_KEY,
        "session_id": DEMO_SESSION_ID,
        "temperature": 0.7,
      }
    ),
  )
  response = ResponseInfo(
    status_code=200,
    headers={
      "Content-Type": _CONTENT_TYPE_JSON,
      "Set-Cookie": DEMO_COOKIE,
    },
    body=_dumps(
      {
        "id": "chatcmpl-demo-redact",
        "object": "chat.completion",
        "model": "demo-model",
        "choices": [
          {
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "脱敏探针的离线响应。"},
          }
        ],
        "access_token": DEMO_ACCESS_TOKEN,
      }
    ),
  )
  return DemoScenario(
    scenario_id="redaction_probe",
    title="脱敏探针成功（请求体/响应头/响应体各命中敏感字段）",
    category="非流式/成功/脱敏探针",
    request=request,
    response=response,
    response_redacted=True,
  )


def demo_scenarios() -> tuple[DemoScenario, ...]:
  """返回 D16 的 10 个演示采样场景（脚本与测试共用的单一事实源）。

  Returns:
    tuple[DemoScenario, ...]: 10 个场景，顺序与 README 的清单一致；
      全部 ``is_observation=False``（演示采样，不是观测运行）。
  """
  return (
    _scenario_nonstream_basic(),
    _scenario_nonstream_multi_turn(),
    _scenario_stream_basic(),
    _scenario_stream_tool_call(),
    _scenario_nonstream_tool_call(),
    _scenario_rate_limited(),
    _scenario_server_error(),
    _scenario_timeout(),
    _scenario_multimodal_parts(),
    _scenario_redaction_probe(),
  )


def _record_one(scenario: DemoScenario, target: Path) -> Path:
  """录制单个场景并返回落盘路径（不判断是否已存在）。

  Args:
    scenario: 待录制的场景。
    target: cassette 输出目录。

  Returns:
    Path: 落盘文件路径（文件名即请求指纹）。
  """
  return Path(
    record(
      scenario.request,
      scenario.response,
      target,
      # record() 的 chunks 形参是 list；非流式场景必须传 None，
      # 传空列表会被解读为「流式但未采集到 chunk」
      chunks=None if scenario.chunks is None else list(scenario.chunks),
    )
  )


def main(argv: list[str] | None = None) -> int:
  """命令行入口：幂等录制 10 个演示 cassette，并复扫脱敏。

  Args:
    argv: 命令行参数（None 时由 argparse 读取 ``sys.argv``）。

  Returns:
    int: 0 = 场景齐备且脱敏扫描通过；1 = 数量不符或扫描发现疑似泄漏。
  """
  parser = argparse.ArgumentParser(
    description="D16 工程演示采样：一键录制演示 cassette（幂等，非观测数据）"
  )
  parser.add_argument(
    "--force",
    action="store_true",
    help="已存在的 cassette 也重新录制（默认跳过；重录会改写 recorded_at）",
  )
  parser.add_argument(
    "--dir",
    default=str(DEMO_CASSETTE_DIR),
    help="cassette 输出目录，默认 examples/demo_cassettes",
  )
  args = parser.parse_args(argv)

  scenarios = demo_scenarios()
  target = Path(args.dir)
  print(f"目标目录: {target}")
  print(f"场景清单: {len(scenarios)} 个（工程演示采样，非观测运行）")

  recorded = 0
  skipped = 0
  for scenario in scenarios:
    request_hash = Cassette.compute_request_hash(scenario.request)
    path = target / f"{request_hash}.json"
    if path.is_file() and not args.force:
      skipped += 1
      print(f" 跳过 {request_hash} {scenario.scenario_id}（已存在，--force 可重录）")
      continue
    _record_one(scenario, target)
    recorded += 1
    print(f" 录制 {request_hash} {scenario.scenario_id}")

  files = sorted(target.glob("*.json"))
  print(f"结果: 录制 {recorded} 个 / 跳过 {skipped} 个 / 目录内共 {len(files)} 个 cassette")
  if len(files) != EXPECTED_SCENARIO_COUNT:
    print(
      f"数量不符：目录内有 {len(files)} 个 cassette，"
      f"预期 {EXPECTED_SCENARIO_COUNT} 个（检查是否有残留或指纹碰撞）",
      file=sys.stderr,
    )
    return 1

  # 第二道防线：录制完成后立刻复扫一遍（与 README 的固定命令同一入口）
  env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
  scan = subprocess.run(
    [sys.executable, str(SCAN_SCRIPT_PATH), "--dir", str(target)],
    capture_output=True,
    text=True,
    encoding="utf-8",
    env=env,
    check=False,
  )
  print(f"脱敏复扫: {scan.stdout.strip()}（退出码 {scan.returncode}）")
  if scan.returncode != 0:
    print("脱敏扫描未通过，请检查录制期脱敏是否被绕过", file=sys.stderr)
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())

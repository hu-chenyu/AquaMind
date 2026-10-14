# AquaMind 工程演示采样 cassette（D16）

> **本目录为工程演示采样（D16），非观测运行数据：不进入任何统计结论、不参与预注册冻结、
> 不计入统计功效计算；正式观测数据见 D47。**

这 10 个 cassette 是用本地 mock 录下来的**典型请求形态样本**，用途只有两个：让人
直接打开文件看清 v2 格式长什么样，以及让示例/文档有一个稳定的对照物。它们**不是**
测量结果——不构成任何档位的质量判定依据，也不能被引用为「AquaMind 跑出来的数字」。
正式观测运行是 D47 的那一批（3 档 ×1，同批、同工具、同口径），与本目录无关。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `*.json`（10 个） | 演示采样 cassette，文件名即请求指纹（16 位十六进制），格式为 M1-D15 收口后的 **v2**（`format_version: 2`） |
| `README.md` | 本文件 |

## 10 个采样场景（文件名 → 场景）

| cassette 文件 | 场景 | 覆盖点 |
| --- | --- | --- |
| `a467e14649e0c0b5.json` | 非流式单轮成功：带 `Authorization` 头与 `User-Agent` 噪声头，响应含 `usage` | 非流式 / 成功；请求头脱敏（sha256 摘要）；`usage` 的保守脱敏（见下） |
| `1dc20b537098e0a7.json` | 非流式多轮对话成功：`system` + 两轮问答共 4 条 `messages` | 非流式 / 成功 / 多轮对话 |
| `11342c726590f01c.json` | 流式成功：4 个 chunk，增量拼接等于响应体 `content` | 流式 / 成功；`timing` 时序录制与首片归一化 |
| `1a3f260b4ddd01d7.json` | 流式工具调用：分片内容是工具入参 JSON 的增量碎片 | 流式 / 成功 / 工具调用；分片拼接等于 `tool_calls[0].function.arguments` |
| `8a85db81fd11963e.json` | 非流式工具调用成功：`finish_reason=tool_calls` | 非流式 / 成功 / 工具调用 |
| `5ea6ebd955b60b82.json` | 失败：429 限流，带 `Retry-After` 与剩余额度头 | 非流式 / 失败 / 429 |
| `0e7b51805fc33fa8.json` | 失败：500 服务端内部错误 | 非流式 / 失败 / 500 |
| `1e3bacfbe1e2f463.json` | 失败：超时（504 + 空响应体 + 形态标记头） | 非流式 / 失败 / 超时（口径见下） |
| `2864b699f341cdc2.json` | 非流式多轮 + 多模态 `content` parts，请求头走 `X-API-Key` | 非流式 / 成功 / 多轮 / 多模态；`x-api-key` 脱敏 |
| `22968b1f48502da7.json` | 脱敏探针成功：请求体 / 响应头 / 响应体各命中一次敏感字段 | 非流式 / 成功；三通道脱敏（REDACTED） |

场景清单的**单一事实源**是 `scripts/record_demo_cassettes.py` 的 `demo_scenarios()`
（请求、响应、分片、分类标签都在那里定义）；上表是它的人工可读版本，`tests/test_d16_demo_cassettes.py`
会校验「目录内文件 ←→ README 登记 ←→ 场景登记表」三者不漂移。

## 复现录制

在仓库根目录执行（幂等：已存在的 cassette 默认跳过）：

```powershell
.venv\Scripts\python.exe scripts/record_demo_cassettes.py
```

- 需要覆盖重录时加 `--force`（会改写 `recorded_at`，从而改动被 git 跟踪的文件内容；
 只想复现场景时不要加）。
- 录制完成后脚本会**自动复扫脱敏**（第二道防线）。手动复扫命令：

```powershell
.venv\Scripts\python.exe scripts/scan_cassette_secrets.py --dir examples/demo_cassettes
# 预期：扫描通过：未发现明文密钥（...）；退出码 0
```

- 直接查看某个场景的录制内容（离线，不发网络请求）：

```python
from pathlib import Path

from aquamind.replay import Cassette, play

cassette = Cassette.model_validate_json(
  Path("examples/demo_cassettes/11342c726590f01c.json").read_text(encoding="utf-8")
)
print(play(cassette).status_code, play(cassette).text)
print(cassette.timing) # 流式场景的 chunk 到达时序；非流式场景为 None
```

> 注意：这里用 `Cassette.model_validate_json` 直接读盘只为查看内容。要让回放**按请求
> 指纹校验**（推荐做法，避免拿到「别人的响应」），请改用 `find_match(request, dir)`
> 或 `replay_request(request, dir)`——它们会重算指纹并与文件内记录的值比对。

## 口径说明（读这几个文件前先看）

1. **零网络、零真实密钥**。全部请求与响应都是本地 mock 构造的，脚本不发起任何真实
  API 调用（CI 零 key 纪律）；文件里的凭据全是明显的占位串（`sk-demo-0001` 之类），
  且落盘前已被录制期脱敏替换。
2. **脱敏两种落盘形态并存**，与 M1-D15 的口径一致：
  - 请求头敏感值（`authorization` / `x-api-key`）→ `sha256:<前16位摘要>`：它参与请求
   指纹，必须保留「换密钥即换指纹」的区分度；
  - 请求体 / 响应头 / 响应体的敏感字段 → `REDACTED`：这三处不参与指纹，直接替换即可。
3. **保守脱敏的副作用是真实现象，不是 bug**。敏感字段按「键名包含 key/token/secret
  等模式」识别，因此 `a467e14649e0c0b5.json` 里 `usage` 的 `prompt_tokens` /
  `completion_tokens` / `total_tokens` 数值也被替换成了 `REDACTED`——这些计数不是凭据，
  但「宁可多脱敏，不可漏脱敏」是刻意选择的取向（取舍见 `docs/adr/0011-cassette-redaction.md`）。
4. **超时用 504 形态表示**。客户端侧真实超时（`httpx.TimeoutException`）根本没有 HTTP
  响应，而 cassette 契约要求响应状态码，因此 `1e3bacfbe1e2f463.json` 取网关超时形态：
  `504` + 空响应体 + `X-AquaMind-Demo-Failure-Mode: timeout` 标记头。标记头只标识形态，
  不改回放语义（回放仍原样返回 504 与空体）。
5. **流式分片是内容增量，不是 SSE 帧**。`timing` 里的 `text` 是该 chunk 的增量文本，
  各片拼接后等于响应体里对应字段的值（`stream_basic` 是 `content`，`stream_tool_call`
  是 `tool_calls[0].function.arguments`）。适配器层面的 SSE 帧切分（`data:` 行解析）
  属 `sse` 模块的口径，不在本目录的演示范围内。
6. **`recorded_at` 随录制时刻变化**。它不是校验点；除它之外，本目录内容在同一版本代码
  下重录应当逐字一致（`recorded_at` 之外无随机量）。

## 与其他示例目录的关系

- `examples/replay_demo/`：`replay` 模块的最小可运行演示（录制 → 查找 → 回放、时序录制、
 时序调度、版本闸口）。想看**怎么用**，看那里；想看**录出来长什么样**（10 种形态的
 现成文件），看本目录。
- `examples/redaction_demo/`：录制期脱敏的四通道演示（含明文自查与扫描脚本复跑）。

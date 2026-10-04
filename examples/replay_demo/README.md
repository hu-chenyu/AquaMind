# AquaMind VCR 录制回放演示（M1-D06a）

本目录是 AquaMind `replay` 模块的最小可运行示例，展示**文本层面**的 VCR
录制回放：把一次请求与其响应落成 cassette 文件，离线用同一请求找回并逐字还原响应。

对应任务 M1-D06a。chunk 到达时间录制（timing）与时序调度回放分别在
M1-D06b / M1-D06c 实现，届时本目录会补上带时序的 cassette 与倍率回放示例。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `demo_record_play.py` | 最简演示：构造模拟请求与响应 → `record()` → `find_match()` → `play()` |
| `generate_baseline.py` | 基线生成脚本：8 组请求全量录制回放，统计一致率并写出基线 JSON |
| `cassettes/` | 示例 cassette 文件，由 `demo_record_play.py` 生成 |
| `README.md` | 本文件 |

## 环境准备

在仓库根目录执行（依赖已装则可跳过）：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

示例不发起任何真实网络请求，响应体在本地构造，因此**不需要任何 API 密钥**
（CI 零 key）。

## 运行命令

两条命令都在**仓库根目录**执行：

```powershell
# 1. 最简录制回放演示（cassette 写入系统临时目录，不改动仓库任何文件）
.venv\Scripts\python.exe examples/replay_demo/demo_record_play.py

# 2. 生成基线数字（写入 scripts/replay_baseline.json）
.venv\Scripts\python.exe examples/replay_demo/generate_baseline.py
```

演示脚本默认把 cassette 写进**系统临时目录**（`%TEMP%\aquamind-demo-*`），跑完
`git status` 保持干净。需要更新仓库内的示例 cassette 时显式指定目录：

```powershell
# 注意：会改写被 git 跟踪的 examples/replay_demo/cassettes/4c289c1a45b66aba.json
.venv\Scripts\python.exe examples/replay_demo/demo_record_play.py `
    --cassette-dir examples/replay_demo/cassettes
```

> **交付纪律**：`generate_baseline.py` 写的是 `scripts/replay_baseline.json`，
> 那是已提交的三个一数字产物。**交付提交后请勿重跑它**，否则会就地覆盖产物、
> 让 HEAD 与实际验收数据漂移。确需重新生成时，请在独立分支或临时目录里做，
> 确认无误后再作为一次显式提交。

## 预期输出

`demo_record_play.py`（节选）：

```text
[0] 写入目录   : C:\Users\<user>\AppData\Local\Temp\aquamind-demo-xxxx（系统临时目录，退出后仍保留）
[1] 请求指纹   : 4c289c1a45b66aba
[2] 落盘文件   : 4c289c1a45b66aba.json（838 字节）
[3] 鉴权头落盘 : sha256:e9d13b66d7bb7e7e  ← 摘要而非明文
[4] 回放状态码 : 200
[5] 回放 text  : {"choices": [{"index": 0, "message": {"content": "我是一个演示用的模型...
[6] 回放 json  : 我是一个演示用的模型。
[7] 录制时刻   : 2026-10-04T04:59:45.177973+00:00（来源: cassette，随运行变化）
[8] 改 body 后查找: None（None = 无匹配）
[9] 无匹配报错 : 未找到匹配的 cassette（首次运行或请求已变更），请先执行录制 (context keys: [request_hash, method, url, cassette_dir])
[10] 指纹变更报错: cassette 请求指纹已变更，请重新录制 (context keys: [cassette, recorded_request_hash, current_request_hash])
```

其中 `[1]`~`[7]` 是正常链路，`[8]`~`[10]` 是两道必须显式失败的防线：
请求内容变了 → 指纹不同 → 无匹配报错；cassette 被手改 → 指纹版本不符 → 报错。
两者都不会静默返回一个「看起来正常」的响应。

`[0]` 的临时目录名、`[7]` 的录制时刻随每次运行变化，属于正常现象，不是校验点。

`generate_baseline.py`：

```text
基线已写入: scripts\replay_baseline.json
  录制 8 条 / 回放成功 8 条 / 无匹配 0 条
  status_code 一致率 100.00% / body 100.00% / text 100.00%
  平均指纹计算耗时 0.005 ms
```

耗时数字随机器波动属正常；一致率类字段是确定性结果，重跑必然相同。

## 数字产物

`scripts/replay_baseline.json` 由上面第 2 条命令生成，字段含义：

| 字段 | 含义 |
| --- | --- |
| `generated_at` | 生成时刻（UTC，ISO 8601） |
| `summary.recorded_requests` | 录制请求数 |
| `summary.replay_succeeded` | 回放成功数（`find_match` 命中数） |
| `summary.unmatched_requests` | 无匹配数，应为 0 |
| `summary.status_code_match_rate` | 状态码一致率 |
| `summary.body_match_rate` | 响应体一致率 |
| `summary.text_match_rate` | 响应体文本一致率 |
| `summary.request_hash_stability_rate` | 指纹稳定性（同一请求多次计算一致的比例） |
| `hash_cost.avg_compute_ms` | 平均单次指纹计算耗时（毫秒） |
| `cases[]` | 逐条明细：方法、URL、指纹、录制/回放状态码与三个一致标记 |

场景覆盖 8 组：GET/POST/DELETE、中文 body、英文 body、中文 + emoji body、
带鉴权头（authorization / x-api-key）、带非关键噪声头（User-Agent）、
bytes 响应体、无响应体（204）、非 2xx 响应（503）。

## cassette 文件格式

cassette 就是一个 UTF-8 编码的 JSON 文件，文件名为 `{request_hash}.json`，
其中 `request_hash` 是请求指纹（16 位十六进制）。示例：

```json
{
  "request_method": "POST",
  "request_url": "http://127.0.0.1:8000/v1/chat/completions",
  "request_headers": {
    "content-type": "application/json",
    "authorization": "sha256:e9d13b66d7bb7e7e"
  },
  "request_body": "{\"messages\": [{\"content\": \"请用一句话介绍你自己\"}], \"model\": \"demo-model\"}",
  "response_status_code": 200,
  "response_headers": {
    "Content-Type": "application/json"
  },
  "response_body": "{\"choices\": [{\"index\": 0, ...}]}",
  "request_hash": "4c289c1a45b66aba",
  "recorded_at": "2026-10-04T04:59:45.177973+00:00",
  "format_version": 1
}
```

几点值得注意的约定：

1. **`request_hash` 兼作请求版本字段**。它是「方法 + URL + 关键请求头 + 请求体」
   规范化后取 SHA-256 前 16 位。回放时重算并与文件里的值比对，不一致就说明
   请求已经变更（改了模型名、换了密钥……），必须重新录制，否则回放出来的
   是与当前请求无关的响应。
2. **请求头只留关键的**。`content-type` / `accept` 参与指纹；`User-Agent`
   之类的噪声头不参与，避免客户端升级导致 cassette 莫名失效。
3. **凭据不落明文**。`authorization` / `x-api-key` 参与指纹但只以
   `sha256:<前16位>` 的短摘要落盘——cassette 是会被提交进版本库的文件，
   换成密钥仍能被指纹察觉并触发重新录制。
4. **同一请求重复录制即覆盖**，文件名由指纹决定，不会堆积歧义副本。
5. **`format_version` 是文件格式版本**（与上面的请求版本是两件事）：缺该字段
   的旧 cassette 按 v1 解析；文件声明的版本高于当前代码支持值时，回放直接报
   「cassette 格式版本过新（vN），当前代码支持 v1，请升级 aquamind」，而不是
   笼统的「文件损坏」——后者会把「代码太旧」说成「文件坏了」，排障方向一开始就错。

## 扩展预留

`Cassette` 后续追加的字段一律带默认值，因此**不带新字段的旧 cassette 仍能正常
加载**（M1-D06c 的「旧格式按瞬时回放」即基于此）；同时 `format_version` 提供
前向闸口，让旧代码读到新 cassette 时报「版本过新」而不是「文件损坏」：

- M1-D06b：`timing`（每个 chunk 的到达时间），并把 `format_version` 升到 2
- M1-D06c：时序调度回放（加速/减速倍率）

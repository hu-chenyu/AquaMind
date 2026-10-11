# AquaMind VCR 录制回放演示（M1-D06a / M1-D06b / M1-D06c）

本目录是 AquaMind `replay` 模块的最小可运行示例，展示三层能力：

1. **文本层面**（M1-D06a）：把一次请求与其响应落成 cassette 文件，离线用同一
   请求找回并逐字还原响应；
2. **时序录制层面**（M1-D06b）：流式响应的**每个 chunk 及其到达时刻**一并录进
   cassette 的 `timing` 字段，落盘再读回逐字段一致；
3. **时序调度层面**（M1-D06c）：`play_timed()` 按 `timing` 里的时刻**逐片产出**，
   支持加速/减速倍率，并给出实测时序偏差；无时序可调度时退化为瞬时回放。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `demo_record_play.py` | 最简演示：构造模拟请求与响应 → `record()` → `find_match()` → `play()`；第二段演示 chunk 时序录制与版本闸口；第三段演示时序调度回放（倍率 / 偏差 / 瞬时回放 / 倍率守卫） |
| `generate_baseline.py` | 基线生成脚本：11 组请求（8 组非流式 + 3 组流式）全量录制回放，统计一致率与时序录制精度；再按原速 / 2x / 0.5x 三档真实调度回放，实测时序偏差并写出基线 JSON |
| `cassettes/4c289c1a45b66aba.json` | 示例 cassette，**v2 形态**（含 `format_version: 2` 与 `timing` 时序）。内容是手工整理的展示用例：3 个 chunk 的增量文本拼接后等于响应体里的 `content`。用 `--cassette-dir` 重跑 `demo_record_play.py` 会把它改写为同一请求的**非流式** v2 形态（`timing: null`）——两者都是合法 v2，只是演示重点不同 |
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

# 2. 生成基线数字（写入 scripts/replay_baseline.json，约需 10s）
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
[2] 落盘文件   : 4c289c1a45b66aba.json（881 字节）
[3] 鉴权头落盘 : sha256:e9d13b66d7bb7e7e  ← 摘要而非明文
[4] 回放状态码 : 200
[5] 回放 text  : {"choices": [{"index": 0, "message": {"content": "我是一个演示用的模型...
[6] 回放 json  : 我是一个演示用的模型。
[7] 录制时刻   : 2026-10-05T02:21:01.173303+00:00（来源: cassette，随运行变化）
[8] 改 body 后查找: None（None = 无匹配）
[9] 无匹配报错 : 未找到匹配的 cassette（首次运行或请求已变更），请先执行录制 (context keys: [request_hash, method, url, cassette_dir])
[10] 指纹变更报错: cassette 请求指纹已变更，请重新录制 (context keys: [cassette, recorded_request_hash, current_request_hash])

── 时序录制（M1-D06b）──────────────────────────────
[11] 时序落盘   : e3992dd5c37460ea.json（1095 字节，format_version=2）
[12] 时序明细   : 4 个 chunk，到达时刻(ms) 0.000 / 100.235 / 250.000 / 400.000
[13] 增量文本   : '你好' + '，我是' + '流式' + '模型。'
[14] 首片归一化 : 0.0（原始首包时刻不进时序）
[15] JSON 往返  : True（index/arrival_ms/text 逐字段一致）
[16] 回放全文   : 你好，我是流式模型。（114 字符，未按 chunk 切分）
[17] 拼接一致   : True（各 chunk 增量拼接 == 响应体 content）
[18] 读旧 v1    : format_version=1，timing=None（无 timing 字段 → None，按瞬时回放）
[19] 版本闸门   : cassette 格式版本过新（v3），当前代码支持 v2，请升级 aquamind 后重试 (context keys: [cassette, cassette_version, supported_version])
```

其中 `[1]`~`[10]` 是文本录制回放主链路与两道必须显式失败的防线：请求内容变了 →
指纹不同 → 无匹配报错；cassette 被手改 → 指纹版本不符 → 报错。两者都不会静默
返回一个「看起来正常」的响应。

`[11]`~`[19]` 是时序录制：`record(chunks=...)` 把每个 chunk 的相对到达时刻与文本
一起落盘（`[12]`~`[15]`），`[16]` 表明 `play()` 仍返回完整全文而不是按时刻切分——
**录制**与**调度**是两条口径。`[18]` 与 `[19]` 是 `format_version` 升到 2 后的双向
口径：新代码读旧 v1 cassette 正常加载（`timing` 为 `None`），旧代码读 v2 cassette
则报「版本过新」而不是「文件损坏」。

`[0]` 的临时目录名、`[7]` 的录制时刻随每次运行变化，属于正常现象，不是校验点。

`demo_record_play.py` 第三段（时序调度回放）：

```text
── 时序调度回放（M1-D06c）──────────────────────────────
[20] 原速回放 : speed=1.0，首片立即产出，其后按录下间隔 sleep
       计划    0.00ms / 实际    0.00ms / 偏差  +0.00ms  '你好'
       计划  100.23ms / 实际  100.41ms / 偏差  +0.17ms  '，我是'
       计划  250.00ms / 实际  250.38ms / 偏差  +0.38ms  '流式'
       计划  400.00ms / 实际  400.38ms / 偏差  +0.38ms  '模型。'
[21] 文本一致 : True（逐片拼接 == 录制时拼接：'你好，我是流式模型。'）
[22] 偏差统计 : 4 片 / 最大 0.381ms / 平均 0.232ms（真实测量，随机器波动）
[23] 加速 2x  : 总耗时   200.3ms（原速   400.6ms，约 1/2） / 产出 4 片
[24] 减速 0.5x: 总耗时   800.6ms（原速   400.6ms，约 2 倍） / 产出 4 片
[25] 倍率守卫 : speed=0 显式报错 → speed 必须为正数（0 速与非有限数都没有调度意义），实际: 0.0 (context keys: [speed, request_hash])
[26] 瞬时回放 : 旧格式 timing=None → instant=True / 产出 1 项 / 耗时 0.009ms（不 sleep）/ 文本同 play(): True
[27] 瞬时回放 : 空 timing=[] → instant=True / 产出 1 项 / 耗时 0.002ms（不 sleep）/ 文本同 play(): True
[28] 调度入口 : replay_request_timed 产出 4 片（查找 + 指纹校验 + 调度，与 replay_request 同一条链路）
```

这一段要读出四件事：

1. **节奏可复现**：`[20]` 每一行是「这一片实际什么时候到的」——首片立刻出现，
   其后各片落在录下的时刻附近，偏差不足 1ms。
2. **倍率只改到达时间**：`[23]`/`[24]` 换 speed 重跑同一份 cassette，产出仍是 4 片、
   内容逐字相同，只是总耗时从 400ms 变成 200ms / 800ms。
3. **无时序即瞬时**：`[26]`/`[27]` 两种「没有时序可调度」的形态都一次性给全、
   耗时在 0.01ms 量级（没有 sleep），文本与 `play()` 完全一致。
4. **错误照样显式抛出**：`[25]` 非法倍率报错，`[28]` 表明调度入口同样先做指纹校验。

`[22]`~`[24]` 与 `[26]`/`[27]` 的耗时是**真实测量**，随机器与运行波动属正常；
`[21]`、`[25]`、`[28]` 是确定性结果，重跑必然相同。

`generate_baseline.py`：

```text
基线已写入: scripts\replay_baseline.json
  录制 11 条 / 回放成功 11 条 / 无匹配 0 条
  status_code 一致率 100.00% / body 100.00% / text 100.00%
  平均指纹计算耗时 0.0076 ms
  时序场景 3 组 / 共录制 12 个 chunk / 平均每片 3.92 字
  首片归零 100.00% / 单调递增 100.00% / 3 位小数 100.00%（实测最大小数位 3）
  timing JSON 往返一致 100.00% / 增量拼接对齐响应体 100.00%
  调度回放 1.0x: 12 片 / 文本一致率 100.00% / 计划 1612.1ms → 实测 1613.3ms / 偏差 max 0.445ms / mean 0.246ms
  调度回放 2.0x: 12 片 / 文本一致率 100.00% / 计划 806.1ms → 实测 807.2ms / 偏差 max 0.782ms / mean 0.329ms
  调度回放 0.5x: 12 片 / 文本一致率 100.00% / 计划 3224.3ms → 实测 3226.2ms / 偏差 max 0.752ms / mean 0.351ms
  瞬时回放: timing=None 产出 1 项耗时 0.011ms / timing=[] 产出 1 项耗时 0.018ms（均不 sleep）
  守卫: 非法倍率 4 种全部拦截=True / 时序回放拦截篡改 hash=True
```

以上为某一次运行的输出示例，非入库基线；入库值见 `scripts/replay_baseline.json`。耗时与偏差数字随机器波动属正常，偏差下限取决于操作系统定时器粒度（环境口径见 ADR-0008 边界一）；一致率与守卫类字段是确定性结果，重跑必然相同。

## 时序录制怎么用

```python
from aquamind.replay import RequestInfo, ResponseInfo, record, find_match

# 录制方给出的是每个 chunk 的到达时刻（毫秒）与增量文本；
# 非流式响应不传 chunks，cassette 的 timing 为 None
path = record(request_info, response_info, cassette_dir,
              chunks=[(0.0, "你好"), (100.23456, "，我是"), (250.0, "流式模型。")])

cassette = find_match(request_info, cassette_dir)
for chunk in cassette.timing or []:
    print(chunk.index, chunk.arrival_ms, chunk.text)
# 0 0.0 你好
# 1 100.235 ，我是
# 2 250.0 流式模型。
```

三点约定：

1. **时刻是相对值**。录制方给的是绝对到达时刻，但绝对时刻取决于发起机器的时钟
   与运行时刻，既不可复现也对回放无意义。`record()` 以第一个 chunk 为原点平移，
   落盘后首片恒为 `0.0`——首包延迟属链路特性，不进时序。
2. **`arrival_ms` 保留 3 位小数**（微秒精度）。既足以表达 chunk 之间的到达间隔，
   又把浮点尾差（如 `0.1+0.2`）挡在落盘前，同一段流在不同机器上录出的数字一致。
3. **顺序按录制顺序保留**。`index` 即位置，不按 `arrival_ms` 重排：录制方给出的
   顺序才是服务端真实下发的顺序，贸然排序会掩盖真实的乱序到达。因此
   `arrival_ms` 允许为负，它表达的是「该 chunk 比第一个 chunk 还早到达」。

不传 `chunks`（非流式响应）时 `timing` 为 `None`；显式传空列表则表示「流式但
未采集到 chunk」，`timing` 为 `[]`。两者回放行为相同（都是瞬时给出完整响应体），
但只有 `None` 能断定「这是一次非流式响应」。

## 时序调度回放怎么用

```python
from aquamind.replay import find_match, play_timed, replay_request_timed

# 两种入口：手上有 cassette 就直接调度；要「查找 + 校验 + 调度」一条链路就用
# replay_request_timed（与 replay_request 同构，无匹配/指纹不符都显式报错）
cassette = find_match(request_info, cassette_dir)

stream = play_timed(cassette)                  # 原速：按录下时刻逐片产出
for chunk in stream:                            # 逐片消费，节奏与录制时一致
    print(chunk)

print(stream.deviation.max_deviation_ms)        # 调度偏差（毫秒）
print(stream.deviation.mean_deviation_ms)
print(stream.deviation.deviations_ms)           # 逐片带符号偏差

play_timed(cassette, speed=2.0)                # 加速 2x：间隔减半，内容不变
play_timed(cassette, speed=0.5)                # 减速 0.5x：间隔翻倍
list(replay_request_timed(request_info, cassette_dir, speed=4.0))
```

五点约定：

1. **`play_timed()` 与 `play()` 并存，不是替代**。`play()` 是「一次性拿到完整响应」
   的读取面（读取方式与 `httpx.Response` 对齐），`play_timed()` 是「复现流式到达
   节奏」的行为面。分两个入口而不是给 `play()` 挂开关，是为了让调用点就必须
   表态要不要时序——隐式开关会让同一次回放在不同调用点给出不同的 chunk 边界。
2. **只改到达时间，不改内容**。逐片文本按录制顺序原样产出，拼接结果与录制时一致。
3. **首片立即产出**，其后各片按 `(arrival_ms − 首片 arrival_ms) / speed` 等待。
   调度内部按**绝对时刻对齐**（每片只补「目标时刻 − 当前时刻」的差额），因此某片
   睡过头不会把误差累积到后面每一片。
4. **倍率必须为正有限数**。`speed <= 0`、NaN、inf 一律抛 `ReplayError`，不静默
   回退成原速——「回放得慢一点」和「回放的根本不是这段时序」在结果里长得一样。
5. **无时序即瞬时**：`timing` 为 `None` 或 `[]` 时一次性产出完整响应文本、不 sleep，
   文本口径与 `play()` 完全一致；偏差恒为 0.0（没有等待就没有等待偏差）。

设计取舍与平台边界（Windows 定时器粒度、极大倍率退化、浮点精度）见
[`docs/adr/0008-timing-replay.md`](../../docs/adr/0008-timing-replay.md)。

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
| `timing_recording.streaming_scenarios` | 流式场景数（其余为非流式场景） |
| `timing_recording.total_chunks` | 录制的 chunk 总数 |
| `timing_recording.avg_chunk_chars` | 平均每个 chunk 的文本长度（字） |
| `timing_recording.first_chunk_zero_rate` | 首片归一化为 0.0 的比例 |
| `timing_recording.arrival_monotonic_rate` | 到达时刻单调不减的比例 |
| `timing_recording.three_decimal_precision_rate` | 全部时刻保留 3 位小数的比例 |
| `timing_recording.max_decimals_observed` | 实测最大小数位数（应 ≤ 3） |
| `timing_recording.json_round_trip_rate` | 落盘再加载后 timing 逐字段一致的比例 |
| `timing_recording.chunks_concat_matches_content_rate` | chunk 增量拼接等于响应体 content 的比例 |
| `timing_replay.speeds_measured` | 实测过的倍率列表（原速 / 加速 / 减速） |
| `timing_replay.all_text_match` | 三档倍率下逐片拼接是否都与录制时一致（恒为 true） |
| `timing_replay.by_speed.<倍率>.text_match_rate` | 该倍率下的回放一致率（文本层面） |
| `timing_replay.by_speed.<倍率>.total_scheduled_span_ms` | 该倍率下各场景计划总跨度（毫秒） |
| `timing_replay.by_speed.<倍率>.total_elapsed_ms` | 该倍率下实测总耗时（毫秒） |
| `timing_replay.by_speed.<倍率>.elapsed_over_scheduled_ratio` | 实测/计划之比，接近 1.0 说明调度没有累积漂移 |
| `timing_replay.by_speed.<倍率>.max_deviation_ms` | **该倍率下的最大时序偏差（毫秒）** |
| `timing_replay.by_speed.<倍率>.mean_deviation_ms` | **该倍率下的平均时序偏差（毫秒）** |
| `timing_replay.by_speed.<倍率>.deviations_ms` | 逐片偏差明细（绝对值，毫秒） |
| `timing_replay.by_speed.<倍率>.cases[]` | 逐场景明细：指纹、片数、计划跨度、实测耗时、偏差 |
| `timing_replay.instant_replay.legacy_timing_none` | 旧格式（`timing=None`）瞬时回放量测：instant / 产出项数 / 耗时 / 是否远低于任何调度间隔 / 文本是否同 `play()` |
| `timing_replay.instant_replay.empty_timing` | 空 `timing=[]` 的同上量测 |
| `timing_replay.speed_guard.all_rejected` | 非法倍率（0 / 负 / NaN / inf）是否全部显式报错 |
| `timing_replay.hash_guard.tampered_hash_rejected` | 时序回放入口是否仍拦截被篡改的指纹 |
| `cases[]` | 逐条明细：方法、指纹、三个一致标记、指纹耗时；流式场景另带 `chunk_count`/`first_arrival_ms`/`last_arrival_ms`/`arrival_monotonic`/`max_decimals`/`timing_round_trip_ok` |

场景覆盖 11 组。非流式 8 组：GET/POST/DELETE、中文 body、英文 body、中文 + emoji
body、带鉴权头（authorization / x-api-key）、带非关键噪声头（User-Agent）、
bytes 响应体、无响应体（204）、非 2xx 响应（503）。流式 3 组：4 chunk 起点为 0、
3 chunk 起点非 0（验证首包延迟被归一化）、5 chunk 含亚毫秒时刻与中英混排
（验证 3 位小数的进位行为）。

> **确定性 vs 实测**：一致率类与守卫类字段（一律 100% / true）重跑必然相同；
> 时序偏差与耗时字段是**真实测量**，随机器与运行波动属正常——它们正是「调度准不准」
> 的答案，不应被平滑成固定值。Windows 传统定时器粒度约 15.6ms，Linux 通常 1ms 量级，
> 因此偏差数字在两个平台上的量级本就不同，比较时须先看 `environment` 里的平台。

## cassette 文件格式

cassette 就是一个 UTF-8 编码的 JSON 文件，文件名为 `{request_hash}.json`，
其中 `request_hash` 是请求指纹（16 位十六进制）。非流式示例：

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
  "recorded_at": "2026-10-05T02:21:01.173303+00:00",
  "format_version": 2,
  "timing": null
}
```

流式响应多一个 `timing` 数组（其余字段结构相同）：

```json
  "format_version": 2,
  "timing": [
    { "index": 0, "arrival_ms": 0.0, "text": "你好" },
    { "index": 1, "arrival_ms": 100.235, "text": "，我是" },
    { "index": 2, "arrival_ms": 250.0, "text": "流式" },
    { "index": 3, "arrival_ms": 400.0, "text": "模型。" }
  ]
```

仓库内的示例文件 `cassettes/4c289c1a45b66aba.json` 就是后一种形态（3 个 chunk，
`arrival_ms` 为 `0.0 / 85.5 / 240.0`），可直接打开对照上面的字段约定。

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
5. **`format_version` 是文件格式版本**（与上面的请求版本是两件事）：当前为
   **v2**（v2 相对 v1 新增 `timing`）。缺该字段的旧 cassette 按 v1 解析，
   `timing` 取 `None`；文件声明的版本高于当前代码支持值时，回放直接报
   「cassette 格式版本过新（vN），当前代码支持 v2，请升级 aquamind」，而不是
   笼统的「文件损坏」——后者会把「代码太旧」说成「文件坏了」，排障方向一开始就错。
   D6c 只新增了回放侧接口、没有新增字段，因此 `format_version` 保持 2。

## 扩展预留

`Cassette` 后续追加的字段一律带默认值，因此**不带新字段的旧 cassette 仍能正常
加载**（D6c 的「旧格式按瞬时回放」即基于此）；同时 `format_version` 提供
前向闸口，让旧代码读到新 cassette 时报「版本过新」而不是「文件损坏」：

- ~~M1-D06b：`timing`（每个 chunk 的到达时间），`format_version` 升到 2~~ ✅ 已完成
- ~~M1-D06c：时序调度回放（按录下时间调度 chunk 输出，加速/减速倍率，旧格式瞬时
  回放）~~ ✅ 已完成（不新增 cassette 字段，故 `format_version` 保持 2）

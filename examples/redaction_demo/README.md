# AquaMind cassette 录制期脱敏演示（M1-D15）

本目录演示 AquaMind 的**录制期脱敏**：调用 `record()` 落盘 cassette 时，请求体、
响应头、响应体里命中敏感模式的字段值自动替换为 `"REDACTED"`，请求头敏感值以
`sha256:` 短摘要落盘——调用方无需做任何事，也无法绕过（M1 阶段不提供"不脱敏"
开关，取舍见 [`docs/adr/0011-cassette-redaction.md`](../../docs/adr/0011-cassette-redaction.md)）。

## 演示内容

一次含敏感字段的模拟请求 / 响应，四个通道的落盘形态：

| 通道 | 录制前 | 落盘后 | 口径 |
| --- | --- | --- | --- |
| 请求头 `Authorization` | `Bearer demo-token-0001` | `sha256:<前16位摘要>` | 参与指纹，保留「换密钥即换指纹」的区分度 |
| 请求体 `api_key` | `sk-demo-0001`（假密钥） | `REDACTED` | 不参与指纹，直接替换 |
| 响应头 `Set-Cookie` | `session=abc123; HttpOnly` | `REDACTED` | 同上 |
| 响应体 `access_token` | JWT 形态假 token | `REDACTED` | 同上 |

非敏感字段（`model` / `messages` / `temperature` / `id` / `object`）原样保留；
未命中敏感字段的载荷逐字保留（不做无谓重序列化）。

## 环境准备

在仓库根目录执行（依赖已装则可跳过）：

```powershell
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## 运行命令

```powershell
.venv\Scripts\python.exe examples/redaction_demo/run_demo.py
```

演示脚本把 cassette 写进**系统临时目录**（`%TEMP%\aquamind-redaction-demo-*`），
跑完 `git status` 保持干净。脚本不发起任何真实网络请求，也不含真实密钥。

## 预期输出（节选）

```text
[0] 写入目录    : C:\Users\<user>\AppData\Local\Temp\aquamind-redaction-demo-xxxx（系统临时目录，退出后仍保留）
[1] 落盘文件    : <request_hash>.json（xxxx 字节）

── 脱敏前后对比 ──────────────────────────────────────
[2] 录制前请求体: {"api_key": "sk-demo-0001", "messages": [{"content": "你好，请用一句话介绍你自己", "role": "user"}], "model": "demo-model", "temperature": 0.7}
[3] 落盘请求体  : {"api_key": "REDACTED", "messages": [{"content": "你好，请用一句话介绍你自己", "role": "user"}], "model": "demo-model", "temperature": 0.7}

── 四个通道的落盘形态 ────────────────────────────────
[4] 请求头 Authorization: sha256:<16位摘要>
    （sha256: 短摘要——请求头参与指纹，必须保留「换密钥即换指纹」的区分度）
[5] 请求体 api_key      : REDACTED
[6] 响应头 Set-Cookie   : REDACTED
[7] 响应体 access_token : REDACTED
[8] 非敏感字段保留示例  : model='demo-model'，temperature=0.7（未被改动）
[9] 明文自查    : 4 项演示密钥均不在 cassette 文本中

── cassette 扫描脚本复扫 ─────────────────────────────
[10] 扫描输出   : 扫描通过：未发现明文密钥（目录: ...，共扫描 1 个文件）
[11] 扫描退出码 : 0

结论：录制期脱敏生效，四项密钥零落盘，扫描通过（退出码 0）。
```

`[0]` 的临时目录名与 `[4]` 的摘要值随每次运行变化，属正常现象，不是校验点；
`[3]`、`[5]`~`[9]`、`[11]` 是确定性结果，重跑必然相同。

## 扫描脚本复跑（固定命令）

```powershell
# <demo 目录> 取上面 [0] 行输出的实际路径（脚本退出后目录保留）
.venv\Scripts\python.exe scripts/scan_cassette_secrets.py --dir <demo 目录>
# 预期：扫描通过：未发现明文密钥（...）；退出码 0
```

## 边界（M1 已知限制）

- 只对 **JSON 对象/数组载荷**做结构化脱敏。非 JSON 文本（纯文本、XML）原样
  落盘，不会猜着替换——这类载荷需调用方自行确保不含密钥。
- 敏感字段按「键名包含 key/token/secret 等模式」识别（大小写不敏感）。代价是
  `max_tokens` 这类「词里恰好含 token」的非凭据字段也会被替换——保守取向：
  宁可多脱敏，不可漏脱敏。
- 密钥零泄漏由三道闸共同保证：录制期脱敏（本演示）+ cassette 扫描脚本
  （`scripts/scan_cassette_secrets.py`）+ `.gitignore` 录制目录兜底。
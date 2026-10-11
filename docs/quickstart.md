# 快速上手

> **本文的安装与校验章节已可用；评测流程章节仍为占位。**
> 当前版本 **0.0.4** 已提供 M1 底座：用例契约与 YAML/JSON 加载、适配器底座（本地函数 + OpenAI 兼容）、重试退避与错误分类、预算账本、SSE 帧解析、VCR 录制与时序回放，以及 `aquamind run` 命令行入口；**评测与负载能力随 M2-M4 交付**，因此"五分钟跑通第一个评测"部分暂时只给出待补充清单。
> 第 3 章的**配置系统说明已在 M1 兑现**（本版本实现即以此为准）；三个可运行示例见 [`examples/`](../examples/)。

## 1. 安装

AquaMind 要求 Python 3.11 及以上：

```bash
pip install aquamind
```

若希望锁定版本：

```bash
pip install aquamind==0.0.4
```

贡献者可从源码安装开发依赖：

```bash
git clone https://github.com/hu-chenyu/AquaMind.git && cd AquaMind
pip install -e ".[dev]"
```

## 2. 校验安装

安装完成后，可执行以下内容确认包已正确安装：

```python
import aquamind

print(aquamind.__version__) # 期望输出 0.0.4
```

若能够正常输出版本号，说明安装成功。

> **说明**：0.0.4 已提供 M1 底座模块（用例契约与 YAML/JSON 加载器、适配器底座、重试退避与错误分类、
> 预算账本、SSE 帧解析、VCR 录制与时序回放），并已注册 `aquamind` 命令行入口
> （`aquamind --help` / `version` / `run`，`run` 含 `--speed` 参数）；
> `report` 与门禁子命令随 **M4-D07** 提供。

## 3. 配置系统

AquaMind 的全局配置由 `aquamind.config.Settings` 管理，统一走 **`AQ_` 前缀的环境变量**。

### 3.1 配置来源与优先级

当前版本只支持两种来源，优先级从高到低为：

```text
AQ_ 前缀环境变量 > 当前工作目录下的 .env 文件 > 代码内默认值
```

> **没有** YAML / JSON 格式的全局配置文件，也没有命令行覆盖层。
> `aquamind run` 用的那份 JSON 是**单次运行的请求参数**（base_url / model / api_key / prompt /
> timeout），与本节的全局配置体系是两套互不相干的东西，不要混为一谈。字段口径见
> [`examples/cli_run_demo/README.md`](../examples/cli_run_demo/README.md)。

读取入口是 `load_config()`：

```python
from aquamind.config import load_config

config = load_config()
print(config.log_level, config.default_timeout, config.replay_mode)
```

配置校验失败时它抛 `ConfigError`（属 `AquaMindError` 族），错误消息只渲染 `context`
的键名，不渲染取值。

### 3.2 环境变量清单

| 环境变量 | 类型 | 默认值 | 合法范围 / 说明 |
| --- | --- | --- | --- |
| `AQ_LOG_LEVEL` | 字符串 | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL`；输入的小写自动转大写 |
| `AQ_DEFAULT_TIMEOUT` | 浮点 | `60.0` | 单次请求默认超时秒数，范围 `1.0`–`600.0` |
| `AQ_MAX_RETRIES` | 整数 | `3` | 请求最大重试次数，范围 `0`–`10` |
| `AQ_RETRY_BASE_DELAY` | 浮点 | `1.0` | 重试退避基础延迟秒数，范围 `0.1`–`60.0` |
| `AQ_MAX_TOKENS_PER_RUN` | 整数 | 无（不限制） | 单次运行最大 token 消耗，须 ≥ 1 |
| `AQ_MAX_COST_PER_RUN` | 浮点 | 无（不限制） | 单次运行最大费用（美元），须 ≥ 0.0 |
| `AQ_REPLAY_MODE` | 字符串 | `auto` | `auto` / `record` / `play` / `off`，语义见下表 |
| `AQ_REPLAY_DIR` | 路径 | `./.aquamind_cache` | cassette 存储目录；**不能为空串或纯空白** |
| `AQ_REPLAY_STRICT_HASH` | 布尔 | `true` | cassette 哈希不匹配时是否报错（`true` 报错 / `false` 警告后继续） |
| `AQ_DEFAULT_CONCURRENCY` | 整数 | `10` | 默认并发数，范围 `1`–`1000` |

`AQ_REPLAY_MODE` 四种取值的含义：

| 取值 | 行为 |
| --- | --- |
| `auto` | 有匹配的 cassette 就回放，没有则录制（默认） |
| `record` | 强制录制，覆盖已有 cassette |
| `play` | 强制回放；没有匹配时**直接报错**，不退回录制 |
| `off` | 关闭回放 |

### 3.3 用法示例

临时改一个值（PowerShell）：

```powershell
$env:AQ_LOG_LEVEL = "DEBUG"
$env:AQ_MAX_RETRIES = "5"
```

或在项目根目录放一个 `.env`：

```dotenv
AQ_LOG_LEVEL=DEBUG
AQ_DEFAULT_TIMEOUT=30
AQ_REPLAY_MODE=play
```

配置文件写法示例（`.env`，UTF-8 编码）：

```dotenv
# 运行与重试
AQ_LOG_LEVEL=INFO
AQ_DEFAULT_TIMEOUT=60
AQ_MAX_RETRIES=3
AQ_RETRY_BASE_DELAY=1.0

# 预算（留空或整行删除表示不限制）
# AQ_MAX_TOKENS_PER_RUN=50000
# AQ_MAX_COST_PER_RUN=1.5

# 回放
AQ_REPLAY_MODE=auto
AQ_REPLAY_DIR=./.aquamind_cache
AQ_REPLAY_STRICT_HASH=true

# 并发
AQ_DEFAULT_CONCURRENCY=10
```

### 3.4 两个容易踩的坑

**一、`.env` 是按"当前工作目录"解析的，不是按代码位置解析的。**
同一份代码在 A 目录下执行会读到 A 的 `.env`，在 B 目录下执行会退回默认值——
结果就是"本地好好的，CI 上配置全丢了"。`Settings` 每次实例化都会重新读取。
需要与目录无关的确定性配置时，请显式设置 `AQ_` 环境变量，而不是依赖 `.env`。

**二、`.env` 里不要把值留空。**
`AQ_REPLAY_DIR=` 这种"留空表示不设置"的惯用写法会被**直接拒绝**并报错——
因为空串经 pydantic 解析会变成 `Path(".")`，展开后就是当前工作目录，
cassette 会被静默写到调用目录且没有任何告警。要取消这一项，请**整行删掉**而不是把值清空。

### 3.5 尚未交付的配置项

以下能力随后续里程碑交付，**当前版本配置里没有对应项**，写进 `.env` 会被忽略：

| 能力 | 计划里程碑 |
| --- | --- |
| 评分器权重、Judge 模型与结构化输出参数 | M2 |
| 负载引擎参数（并发阶梯、到达率模式、AIMD 参数） | M3 |
| 门禁阈值、报告输出选项 | M4 |
| 运行结果落库与两次运行对比的路径配置 | M5 |

> 本节配置项清单以 `src/aquamind/config.py` 的 `Settings` 定义为准。清单未列出的 `AQ_`
> 变量不会被读取（未知键一律忽略），因此拼错的变量名不会报错，只会静默不生效——
> 这一点与用例契约层"未知字段直接报错"的策略相反，是有意为之：环境变量往往来自
> 部署脚本，不应因为多传了一个变量就让进程起不来。

## 4. 下一步（待补充）

以下章节将在 M2 交付时补充：

- **待补充**：准备被测对象——配置一个 OpenAI 兼容端点用于接收评测请求
- **待补充**：准备用例集——用 YAML 或 JSON 描述一组待检问题
- **待补充**：执行第一次评测——一条命令跑完并生成评测报告
- **待补充**：读懂报告——报告各区块的含义与指标之间的关系
- **待补充**：把评测接进流水线——作为常规校验环节的挂接方式

> 原先此处写着"配置系统、环境变量与配置文件的完整说明，也将随 M1 一并给出"——
> 该承诺已由上面的**第 3 章**兑现（当前版本实际支持的是 `AQ_` 环境变量与 `.env`，
> 没有 YAML/JSON 全局配置文件）。

---

相关文档：[文档首页](./index.md) ｜ [完整教程](./tutorial.md) ｜ [API 参考](./api-reference.md) ｜ [架构决策记录](./adr/)

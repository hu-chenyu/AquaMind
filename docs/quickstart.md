# 快速上手

> **本文为占位文档，内容尚未补充。**
> 当前版本 **0.0.4** 已提供 M1 底座：用例契约与 YAML/JSON 加载、适配器底座（本地函数 + OpenAI 兼容）、重试退避与错误分类、预算账本、SSE 帧解析、VCR 录制与时序回放，以及 `aquamind run` 命令行入口；**评测与负载能力随 M2-M4 交付**，因此本文暂时只给出安装、校验与模块清单。
> 完整的"五分钟跑通第一个评测"将在 **M2** 里程碑（首个端到端可用版本）交付时同步补全。

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

print(aquamind.__version__)  # 期望输出 0.0.4
```

若能够正常输出版本号，说明安装成功。

> **说明**：0.0.4 已提供 M1 底座模块（用例契约与 YAML/JSON 加载器、适配器底座、重试退避与错误分类、
> 预算账本、SSE 帧解析、VCR 录制与时序回放），并已注册 `aquamind` 命令行入口
> （`aquamind --help` / `version` / `run`，`run` 含 `--speed` 参数）；
> `report` 与门禁子命令随 **M4-D09** 提供。

## 3. 下一步（待补充）

以下章节将在 M2 交付时补充：

- **待补充**：准备被测对象 —— 配置一个 OpenAI 兼容端点用于接收评测请求
- **待补充**：准备用例集 —— 用 YAML 或 JSON 描述一组待检问题
- **待补充**：执行第一次评测 —— 一条命令跑完并生成评测报告
- **待补充**：读懂报告 —— 报告各区块的含义与指标之间的关系
- **待补充**：把评测接进流水线 —— 作为常规校验环节的挂接方式

配置系统、环境变量与配置文件的完整说明，也将随 M1 一并给出。

---

相关文档：[文档首页](./index.md) ｜ [完整教程](./tutorial.md) ｜ [API 参考](./api-reference.md) ｜ [架构决策记录](./adr/)

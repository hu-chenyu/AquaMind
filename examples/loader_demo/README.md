# AquaMind 用例加载器示例（M1-D02 / M1-D03）

本目录是 AquaMind `loaders` 模块的最小可运行示例，展示**用例从文件到契约对象**的完整过程：

1. **正常路径**：用 YAML 与 JSON 两种格式定义用例，经 `load_cases()` 加载为 `TestCase`；
2. **字段读取**：读取用例的五个契约字段（`input` / `expected` / `context` / `score_tags` / `weights`）；
3. **坏数据拦截**：文件层面错误归 `LoaderError`（带行号），用例层面错误归 pydantic `ValidationError`（定位到字段）。

## 目录内容

| 文件 | 说明 |
| --- | --- |
| `loader_demo.py` | 全部演示逻辑；用例文本内嵌在脚本里，运行时写入系统临时目录 |
| `README.md` | 本文件 |

## 运行命令

在**仓库根目录**执行：

```powershell
.venv\Scripts\python.exe examples/loader_demo/loader_demo.py
```

## 注意事项

- **本示例不触真实 API，不需要任何 API 密钥**（CI 零 key）。全部用例文件写在
  系统临时目录（`%TEMP%\aquamind-loader-demo-*`），退出时自动清理，跑完 `git status` 保持干净。
- 示例覆盖四类坏数据，各自对应一条不同的校验路径：

| 坏数据 | 抛出的异常 | 为什么不是另一类 |
| --- | --- | --- |
| YAML 方括号未闭合 | `LoaderError` | 语法层面就坏了，用例对象根本构造不出来 |
| 缺少必填字段 `expected` | `ValidationError` | 文件语法合法，但不满足 `TestCase` 契约 |
| `expected.type` 拼成 `excat` | `ValidationError` | `Literal` 枚举拦截拼写错误，防止它飘到 M2 打分器静默算错分 |
| 多写未知字段 `timeout` | `ValidationError` | `extra="forbid"`，拼错的键名不会被静默忽略 |

## 为什么两类异常要分开捕获

`load_cases()` 的 docstring 明确声明「本模块不吞契约层异常」。这不是实现疏忽，而是刻意的边界划分：

- **`LoaderError`**——文件层面：扩展名不支持、文件不存在/不可读、编码不是 UTF-8、
  YAML/JSON 语法错误、顶层不是非空数组。`context` 里带 `file` / `line` / `msg`，可直接定位到行；
- **`ValidationError`**——用例层面：字段缺失、类型不符、多余字段。pydantic 自带字段定位。

调用方只捕获 `LoaderError` 会漏掉契约错误；只捕获 `ValidationError` 则会把"文件根本读不了"
误报成"某条用例字段不对"。两类都要处理。

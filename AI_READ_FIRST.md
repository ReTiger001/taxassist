# AI 入口：先读这一页

> 给接手这个项目的人，或 AI。目标是**读完这一页就能动手**，不必先读遍全部代码。
> 详细的专题文档在最后一节。

---

## 这是什么

**税务政策知识库 + 翻译 + 检索**。抓取全国税务机关的政策原文，归档原始响应，
抽取结构化字段，翻译成英文，并提供检索接口（网站 / MCP / API / AI 助手）。

使用者的口径是「**我们只要正式政策的内容**」——所以非正式内容（解读、问答、
办事指南、科普图说）**标记而不丢弃**：`policy.is_official = 0` 进参考层，
可查但不混进政策层。

---

## 怎么跑

```bash
# 一切入口
.venv/Scripts/python.exe -m taxassist --help

# 查看进度（最常用）
.venv/Scripts/python.exe -m taxassist watch --once
# 或者双击 查看进度.bat

# 建库 / 初始化
.venv/Scripts/python.exe -m taxassist initdb

# 测试与格式（**改动后必跑，pre-commit 也会跑**）
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check src/ tests/ scripts/
```

---

## 架构地图

**读代码从这里入手，按需要再展开**：

| 想去哪 | 看这个 |
|---|---|
| 数据怎么存 | `src/taxassist/db.py` 的 `SCHEMA`（policy 53 列是主表） |
| 数据怎么写 | `src/taxassist/repo/`（**领域仓储层，写规则只此一处**） |
| 抓取流程 | `src/taxassist/pipeline.py` + `src/taxassist/collect/` |
| 入库规则 | `src/taxassist/store.py`（政策 upsert、原始响应归档、抓取日志） |
| 检索 | `src/taxassist/kb.py`（**按后端分叉**：SQLite 用 FTS5，PG 用 PGroonga） |
| 翻译 | `src/taxassist/translate_llm.py`（本地 ollama，数据不出本机） |
| 网站 | `src/taxassist/web/`（每个页面一个 `*_routes.py`，`register()` 注入依赖） |
| 计费 / 账号 | `src/taxassist/billing.py`、`auth.py` |

**连接层是理解全局的钥匙**：`src/taxassist/repo/conn.py` 是**全项目唯一知道
底层是 SQLite 还是 PostgreSQL 的地方**。设 `TAXASSIST_BACKEND=pg` 即切到 PG，
默认仍是 SQLite。

---

## 铁律（违反会被测试或生产打回）

1. **`.bat` 必须纯 ASCII**（`tests/test_scripts.py` 会扫）。
2. **`src/taxassist/` 与 `scripts/` 下每个 subprocess 调用必须带**
   `creationflags=procutil.hidden_flags()`（`tests/test_no_window_guard.py` 会扫），
   否则会弹出黑窗口。
3. **改 SQL 前先看 `src/taxassist/repo/conn.py` 的写法规矩**：占位符写 `?`、
   取列一律 `row["列名"]`（不用 `row[0]`）、只用 `COALESCE` / `ON CONFLICT` /
   `CASE WHEN` 这些两边通用的写法。
4. **新加列必须同时改两处**：`db.py` 的 `SCHEMA` 与 `migrate()`。
   只改一边的表现是「**启动即失败**」（视图/索引引用它），不是某个功能降级。
5. **pre-commit（ruff + 全量测试）不许用 `--no-verify` 绕**。
6. **中文不要经 shell 传给 Python 或 psql**：Windows 会按 CP936 编码，
   到了那边就是乱码。用 `PGCLIENTENCODING=GBK`、或把脚本/ SQL 写成 UTF-8
   文件再执行。

---

## 当前正在进行的事

**PostgreSQL 迁移 + 领域仓储层**。**全部细节、表清单、方言映射表、
已踩的坑，都在 `docs/PG迁移与仓储层设计.md`** —— 那是最新最全的一份。

进度：环境已就绪、仓储层已建、迁移脚本已演练（14 表逐行一致）、
兼容连接层已让 **317 处既有 SQL 一行未改就跑在 PG 上**、
网站三块已完成。剩余：补完少量方言（清单在该文档里）+ 一次性切换上线。

---

## 容易踩的坑（都是实测换来的）

- **SQLite 的 `pending` 是字符串 `'pending'`，不是 NULL** —— 想选待处理的附件
  要看 `parse_status` 的三个值，别写 `IS NULL`。
- **psycopg 用 `%s` 做占位符**，所以 SQL 里字面量的 `%` 必须转义成 `%%`。
  漏了它的报错极具误导性：不是「未知占位符」，而是对中文解码失败抛
  `UnicodeDecodeError`（`adapt_sql` 里已处理，但新写的 PG 专有 SQL 要注意）。
- **`PGroonga` 的相关性分方向**：`pgroonga_score` 越大越相关，而 FTS5 的
  `bm25` 越小越相关 —— 漏掉 `DESC` 不会报错，只会让排序整体颠倒。
- **`translation` 与两张审核表不在 `db.SCHEMA` 里**，靠各模块 `ensure_table`
  惰性建。这是现状，写测试时要照现状来。
- **改模板颜色用 `base.html` 的 CSS 变量**（`--ink` / `--card` / `--bad-bg` …），
  别写死颜色：站点有**两套主题**（浅色与终端风），写死会在其中一套下变成
  白块黑字，而测试断言的是文本、看不见颜色。

---

## 更细的专题文档

| 想了解 | 看 |
|---|---|
| **PG 迁移与仓储层（最新，先看这个）** | `docs/PG迁移与仓储层设计.md` |
| 服务说明与免责 | `docs/服务说明与免责.md` |

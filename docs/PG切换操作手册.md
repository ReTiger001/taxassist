# PostgreSQL 切换操作手册

> **这份手册是给"真正切换那一刻"用的。**
>
> 现在的状态：代码已就位，但**还没有切换过**。`TAXASSIST_BACKEND` 不设时
> 仍然走 SQLite，生产行为一字未变 —— 所以系统随时可以切，也随时可以等。

---

## 为什么值得切

现在 13 个写入方抢同一把文件锁（`data/write.lock`），谁拿到谁写、其余全干瞪眼。
实测 `attach` 在锁内下载政府网站附件、网络超时卡住，翻译**连续 5 轮一条都译不出来**。

历史上三次修法都是把等锁时间调长（120 秒 → 40 分钟 → 2400 秒）——只让排队者
更容易等到，持锁者该干多久还是多久。

PostgreSQL 的行级锁 + 多连接解决这件事：写不同表的 worker 互不阻塞，写同一张表
的不同行也不阻塞。

---

## 前置检查（不通过就别往下走）

```bash
# 1) 后端连接正常
TAXASSIST_BACKEND=pg .venv/Scripts/python.exe -c "from taxassist.repo import conn; print(conn.ping())"

# 2) 测试与格式全绿（当前基线：ruff + 全量测试）
.venv/Scripts/python.exe -m ruff check src/ scripts/ tests/
.venv/Scripts/python.exe -m pytest -q

# 3) 端到端冒烟全过（12 项，跑在 PG 上）
.venv/Scripts/python.exe scripts/smoke_pg.py
```

三项都应通过。第 3 项测的是「同一份代码在 PG 上真能干活」，不是单元测试那种
SQLite 侧的绿灯 —— 改造期间它抓到过一个单元测试永远看不见的 bug。

---

## 第 1 步：停机

**要停的东西**（都是独立窗口/进程，不是服务）：

| 停什么 | 怎么停 |
|---|---|
| 工作流（`启动工作流.bat` 开的窗口） | 关掉那个窗口 |
| 翻译（`启动翻译.bat`） | 关掉窗口 |
| 网页服务（`启动服务.bat`） | 关掉窗口 |
| 知识库接口 / 对外访问 | 关掉窗口 |
| 定时任务 | 随工作流一起停 |

**确认真的停了**（这步不能省 —— 有任务在写时迁移会把不一致的数据搬过去）：

```bash
.venv/Scripts/python.exe -c "from taxassist import writelock; h = writelock.holder(); print('holder:', h if h else '(空闲)')"
```

要看到 `(空闲)`。若显示 `PID:任务名`，等它结束或确认那个进程已经不在
（旧锁靠心跳租约自动过期，最多等 `LEASE_SEC` 秒）。

---

## 第 2 步：备份 SQLite（回退的唯一退路）

**WAL 模式的库不止一个文件**，三个都要拷 —— 只拷 `.db` 会丢掉最近的写入：

```bash
cd /d/EY-project/data
cp taxassist.db "taxassist.备份-$(date +%Y%m%d-%H%M).db"
cp taxassist.db-wal "taxassist.备份-$(date +%Y%m%d-%H%M).db-wal" 2>/dev/null || true
cp taxassist.db-shm "taxassist.备份-$(date +%Y%m%d-%H%M).db-shm" 2>/dev/null || true
ls -la taxassist.备份-*
```

⚠️ **这三个文件保留至少两周**。476MB 不占什么地方，但它是唯一能回到今天的东西。

---

## 第 3 步：迁移最新数据

```bash
.venv/Scripts/python.exe scripts/migrate_to_pg.py
```

它会：每张表先 `TRUNCATE` 再灌 → **逐表对账（全表逐列 md5，不是只看行数）** →
导入后 `setval` 重置序列。

**必须看到 14 张表全部「一致」且「0 个问题」**。任何一张不一致就停下来查 ——
脚本可以整只重跑，不会留下"灌了一半"的状态。

---

## 第 4 步：切后端

**两种方式，选一种：**

**方式 A：设环境变量**（**推荐**）
  在 `启动服务.bat` / `启动工作流.bat` / `启动翻译.bat` / `启动知识库接口.bat`
  等入口脚本里加：
  ```bat
  set TAXASSIST_BACKEND=pg
  ```

**方式 B：改 `conn.py` 的 `DEFAULT_BACKEND` —— 不要用这个**
  （把 `"sqlite"` 改成 `"pg"`。看起来更省事，实测会让全量测试立刻变红。）

**为什么推荐 A（实测结论，不是偏好）**：改了默认值之后，**全量测试会立刻
变红** —— 测试套件假设默认库是 SQLite，而这不是随便的假设：`tests/conftest.py`
开头写明了理由（默认库是**真实库**，任何忘了传临时路径的测试都会直接改真数据，
而"跑通了"反而更危险）。默认值一改，所有测试都去连生产库。

环境变量方案三个场景都对：**测试**不设它（继续 SQLite）、**生产**启动脚本设它
（走 PG）、**回退**只要去掉它。

方式 B 的唯一优点是"不会漏设"，而那个风险可以用第 6 步的验证兜住 —— 与其
为了防漏设而破坏整个测试套件，不如在切换后**逐个入口确认真走了 PG**。

---

## 第 5 步：删掉 writelock

**这一步必须与第 4 步同时做，不能提前也不能推后**：

- 留着它而切到 PG → 12 个写入方仍然互相排队，白等（改造的意义全没了）
- 删了它而仍跑 SQLite → **并发写冲突**，那是数据损坏级别的风险

**要删的位置**（12 个调用点）：

```
src/taxassist/cli.py:241          "provincial"
src/taxassist/cli.py:361          "enrich"
src/taxassist/cli.py:391          "attach"
src/taxassist/scheduler.py:111    "scheduler:daily"
src/taxassist/worker.py:361       "worker:..."
scripts/fill_docno_sortkey.py:20  "docno_sort_fill"
scripts/fix_terms.py:244          "fix_terms"
scripts/retranslate.py:162        "retranslate"
scripts/translate_all.py:126      "translate"
scripts/translate_batch.py:99     "translate"
tools/fix_truncated_titles.py:18  "fix_titles"
tools/retry_attachments.py:15     "retry_attachments"
```

**但不止这 12 处** —— 全项目有 **17 个文件** 引用 `writelock`（含 `import` 与
辅助函数）。用这个命令找全：

```bash
grep -rln "writelock" src/ scripts/ tools/ --include=*.py
```

删法：每个调用点把 `acquire` / `release` 与它的 `try/finally` 去掉
（或改成 `contextlib.nullcontext()` 占位，便于某天回退），最后删掉
`src/taxassist/writelock.py` 本身。

**`worker.py` 与 `scheduler.py` 的调用点要特别小心**：它们不只是拿锁，
还在拿不到锁时决定"跳过这一轮"。删锁后这段逻辑要改成"直接执行"。

做完后必须跑：

```bash
.venv/Scripts/python.exe -m ruff check src/ scripts/ tests/
.venv/Scripts/python.exe -m pytest -q
```

---

## 第 6 步：验证

```bash
# 端到端冒烟（12 项）—— 脚本在 scripts/ 下，是项目的一部分，不是临时文件
.venv/Scripts/python.exe scripts/smoke_pg.py

# 起网页看一眼检索与详情
.venv/Scripts/python.exe -m taxassist serve
```

**再手动确认三件事**：

1. 检索能用，且**二字词有结果**（如「税率」—— 在 SQLite 上这个是 0 条，
   在 PG 上是 2200+ 条；这是切成功最容易认的标志）
2. 随便打开一条政策的详情页，译文与附件都在
3. **起两个写入任务并发跑**（这是这次改造的目的）—— 例如同时跑
   `attach` 与 `translate_batch`，两者应当**都在推进**，而不是一个等到超时

---

## 回退（万一有问题）

```bash
# 1) 关掉所有任务
# 2) 把后端改回 sqlite（方式 B 就把 "pg" 改回 "sqlite"）
# 3) 把 writelock 的删除撤销：git revert 第 5 步的提交（或 git checkout 那些文件）
# 4) 恢复数据（如果 SQLite 被改坏了才需要）
cd /d/EY-project/data
cp "taxassist.备份-YYYYMMDD-HHMM.db" taxassist.db
cp "taxassist.备份-YYYYMMDD-HHMM.db-wal" taxassist.db-wal 2>/dev/null || true
```

**回退是可行的** —— 这也是为什么第 5 步建议用 `nullcontext()` 占位而不是
彻底删代码：回退时改一行就能恢复串行保护。

---

## 清理（切换后**两周**再做）

确认两周内一切正常之后：

```bash
cd /d/EY-project/data
rm -f taxassist.备份-*.db taxassist.备份-*.db-wal taxassist.备份-*.db-shm
```

**探针库 `probe`（含 14518 条副本）也可一并删**：

```bash
"/c/Program Files/PostgreSQL/17/bin/psql" -U postgres -h 127.0.0.1 -c "DROP DATABASE probe;"
```

**SQLite 原库（`taxassist.db`）本身先别删** —— 它是 `data/` 下的生产文件，
留着不碍事；真要删也要等确认 PG 侧完全正常、且清理过引用之后。

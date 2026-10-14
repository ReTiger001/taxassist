# PG 迁移与仓储层：设计与进度

> 2026-10-14 立。给接手的人（或 AI）。
> 这份文档同时是「AI 入口文档」—— 读懂它就不用读全部实现。

---

## 一、为什么做（问题与目标）

**问题**：13 个写入方抢同一个 `writelock`（文件锁），谁拿到谁写，其余全干瞪眼。
实测 `attach` 在**锁内**下载政府网站附件，网络超时卡住 5 轮，翻译一条都译不出来。

代码注释自己承认了死局，三次修法都是**把等锁时间调长**（120 秒 → 40 分钟 → 2400 秒）
——只让排队者更容易等到，持锁者该干多久还是多久。

**目标（用户拍板的 9 条）**：

| # | 事项 | 定论 |
|---|---|---|
| 1 | 迁移范围 | **只 taxassist**（476MB），KLab 98GB 不碰 |
| 2 | 目标库 | **PostgreSQL**（行级锁，写不同表互不阻塞） |
| 3 | 检索 | **PGroonga**（探针已验，见下） |
| 4 | 「接口」 | **领域仓储层**：不为并发（PG 已解决），为约束集中 + 迁移只改一层 + AI 好读 |
| 5 | 慢 IO | **两阶段**：抓取/解析出事务，短事务写库（迁移前置条件） |
| 6 | `writelock` | **整体删除**；任务领取改用 `FOR UPDATE SKIP LOCKED` |
| 7 | 省 token | **两侧都做**：代码/文档对 AI 友好 + 业务 AI 调用链 |
| 8 | 网站三块 | 检索+详情 / AI 助手 / 翻译质量可视化，**全部本次** |
| 9 | 切换 | **一次性切换**，充分测试后上线，旧文件安全清理（备份留两周） |

---

## 二、已完成（可直接复用，别重做）

### 探针：检索方案已定 = PGroonga

全库 14518 条真实政策、20 个真实政策术语、以 **LIKE 为真值裁判**：

| 查询词 | FTS5（现状） | PGroonga | LIKE 真值 |
|---|---|---|---|
| **税率** | **0** | **2222** | 2222 |
| **国家税务总局** | **14167** | **12279** | 12279 |
| 增值税 | 4514 | 4508 | 4508 |

- **PGroonga 18/20 与真值完全一致**，另 2 个各多 1 条（分词合理扩展，非错误）。
- **FTS5 两个真缺陷**：二字词（trigram 无法索引）**全部漏召回**；长词拆词**虚高**。
- 14518 条灌库 + 建索引**仅 7 秒**。

### 环境（Windows，全程免编译）

- **PG 17.11**：`winget install --id PostgreSQL.PostgreSQL.17 --silent`
- **PGroonga 4.1.0**：GitHub release `pgroonga-4.1.0-postgresql-17-x64.zip`（51MB）
  → 解压后把 `bin/lib/share/etc/include` 复制进 `C:\Program Files\PostgreSQL\17\`
  （**需提权**：`Start-Process -Verb RunAs`）
- **库**：`taxassist`（正式）、`taxassist_test`（测试）、`probe`（探针，launch 阶段清）
- **角色**：`taxassist`（专用，非 superuser）；postgres 密码已改 24 位随机
- **凭据**：`%APPDATA%\postgresql\pgpass.conf`（两套）
- **监听**：已从 `*` 收紧到 `127.0.0.1`（需重启服务生效）
- **自启**：服务 `postgresql-x64-17`，`StartType=Automatic`

### PG schema

`src/taxassist/pg/schema.sql` —— 由 **SQLite 真实 DDL** 转换而来（不是手写），
**已在 `taxassist_test` 验证全部建成**（14 表 / 16 索引 / 1 视图）。

转换规则：`INTEGER PRIMARY KEY AUTOINCREMENT` → `BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY`；
`INTEGER`→`BIGINT`；`REAL`→`DOUBLE PRECISION`；`IFNULL(`→`COALESCE(`；`datetime('now')`→`now()`。
**顺序必须是 表 → 索引 → 视图**（索引引用表，后置；按 type 字母序会失败）。

---

## 三、表清单（14 张真实表）

| 表 | 列数 | 用途 | 写入口 |
|---|---|---|---|
| `policy` | **53** | 政策主表（`o_`=官方字段，`p_`=本系统推断） | `store.upsert_policy`（唯一） |
| `fetch_log` | 15 | 抓取完整性日志 | `store.log_fetch_start/finish` |
| `attachment` | 10 | 附件与解析文本 | `store.upsert_attachment` + pipeline 两条 UPDATE |
| `raw_snapshot` | 9 | 抓取原文归档索引（响应体存文件） | `store.record_snapshot` |
| `policy_relation` | 9 | 政策间 cites/repeals/amends | `effect.py` |
| `translation` | 8 | 译文 | `translate_llm.save`（唯一） |
| `translation_ai_review` | 8 | AI 审核判定 | `scripts/ai_review.py` |
| `translation_review` | 8 | **人工**审校判定（可追溯） | `scripts/import_review.py` |
| `invite_code` | 8 | 邀请码 | `auth.py` |
| `customer` | 7 | 客户 | `billing.py` |
| `usage_log` | 7 | 用量 | `billing.py` |
| `api_key` | 6 | API 密钥 | `billing.py` |
| `app_user` | 6 | 账号 | `auth.py` |
| `meta` | 3 | 键值元信息 | `db.set_meta` |

另有视图 `policy_official`（= policy 且 `COALESCE(is_official,1)=1`；网站计数/列表都查它）。

**FTS5 专属、已跳过（PGroonga 取代）**：`policy_fts` 及 5 张影子表、3 个同步触发器
（`policy_ai/ad/au`）。

**注意**：只有前 6 张表在 `db.py` 的 SCHEMA 里；其余靠**各模块 `ensure_table()` 惰性建**
—— 代价是新库不跑那个模块就没表（`browse_routes.py` 为此写了降级）。仓储层要把它们统一起来。

---

## 四、仓储层设计

**不做写队列**（PG 已解决并发，中间层只增加延迟且让写变成异步、难调试）。

**做**：领域仓储类，**写规则只此一处**。

```
src/taxassist/repo/
  conn.py          # 连接管理：PG 连接池 + 只读连接
  policy.py        # PolicyRepo：upsert / 查 / 更新 p_* 推断字段
  translation.py   # TranslationRepo：save / 按 doc_uid 取
  attachment.py    # AttachmentRepo
  fetch.py         # FetchRepo：fetch_log + raw_snapshot
  relation.py      # RelationRepo
  search.py        # SearchRepo：PGroonga 检索（取代 kb 的 FTS5 部分）
  meta.py          # MetaRepo
  billing.py       # BillingRepo：customer/api_key/usage_log
  auth.py          # AuthRepo：app_user/invite_code
```

**为什么值得做**（三条，与并发无关）：
1. **迁移本身需要**：改方言时只改一层；不建就要改 13 处。
2. **约束集中**：现在"写入必须走哪条路"散落各处。
3. **AI 友好**：AI 读一层就知道全部写路径，不用读 13 个脚本。

**现状**：`store.py` 已是事实上的仓储雏形（`upsert_policy`/`record_snapshot`/
`upsert_attachment`/`log_fetch_*`），`translate_llm.save` 是 translation 唯一写入口。
**缺的是把散落在 backfill / effect / dedupe / billing / auth 里的直接 SQL 收进来。**

---

## 五、方言映射（SQLite → PG）

| SQLite | PG | 影响面 |
|---|---|---|
| `?` 占位符 | `%s` | **全库（工作量最大）** |
| `IFNULL(x,y)` | `COALESCE(x,y)` | 遍布 |
| `MAX(0, x+?)` | `GREATEST(0, x+?)` | `billing.py:234` |
| `changes()` | `RETURNING` / `rowcount` | `billing.py:236` |
| `date('now', ?)` / `datetime('now','-7 days')` | `now() - interval` | `browse_routes.py:350`、`scheduler.py:59` |
| `sqlite_version()` | `version()` | `db.py:446` |
| `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` | `store.py:127,395`、`billing.py:211` |
| `executescript` | 多条 `execute` | `db.py`、`auth.py`、`billing.py` |
| `sqlite3.Row`（`row["x"]` 与 `row[0]` 两种用法） | `dict_row` / `tuple_row` | `db.py:268`、`kb.py:67` |
| `sqlite3.IntegrityError` / `OperationalError` | `psycopg.errors.*` | `auth.py`、`billing.py`、`db.py` 等 |
| `PRAGMA table_info` | `information_schema.columns` | 10 个文件 |
| `sqlite_master` | `pg_catalog` | `kb.py:75,85` |
| `AUTOINCREMENT` | identity（schema 已转） | 已解决 |
| 动态拼列名（f-string） | 同（但需白名单校验） | `store.py:166,186` 等 |

**注意**：`ON CONFLICT` 语法 **PG 原生支持**，10 个文件里的这部分不用改。
`INSERT OR REPLACE`、`json_extract`、`GROUP_CONCAT` **全库零命中**，不用担心。

---

## 六、连接与并发

**现状**：`db.connect()` 每次新建连接（无池、无单例），WAL 靠 PRAGMA，跨进程串行靠文件锁。
web 每请求建+关。

**改后**：
- 连接池（`psycopg_pool`），web 侧复用；脚本侧各建各的。
- **`writelock` 整个删除**——PG 是多客户端服务，行级锁天然支持并发。
- 防重复工作（两个 worker 抓同一批）改用 **`SELECT ... FOR UPDATE SKIP LOCKED`**。
- 只读连接 → 只读账号（替代现在的 `mode=ro` + `query_only`）。

---

## 七、迁移步骤（剩余）

1. **[repo] ✅ 已完成** —— `src/taxassist/repo/` 9 个模块 + 16 个专项测试全绿，
   与现有 SQL 逐字节等价（count 16594 / 整行 53 列全等 / 分页一致）。
   `conn.py` 是唯一知道引擎的地方，`TAXASSIST_BACKEND=pg` 即切换，两后端均已实测连通。
   billing / auth 两个模块后置到 cutover（不在写锁路径上）。
2. **[migrate] ⬅ 当前** SQLite → PG 数据搬运 + **逐表对账**（行数 + 抽样校验和）。
   **时机说明**：生产仍在 SQLite 上跑翻译工作流、数据仍在变，所以本阶段交付的是
   **可重复运行的迁移脚本 + 演练对账**；真正执行在 `launch` 的停机窗口重跑一次。
3. **[slowio] ✅ 已完成** —— attachment 加 `claimed_at` 租约列，仓储层 `claim_batch`
   用 `FOR UPDATE SKIP LOCKED` + 租约；实测连领三批不重叠、过期可重领。
4. **[cutover] ✅ 已完成（两处与原定义有意不同，见下）** —— 让系统能跑在
   PostgreSQL 上：`db.connect()` 在 `TAXASSIST_BACKEND=pg` 时返回**兼容包装**，
   由 `adapt_sql` 翻译方言，**317 处既有 SQL 一行未改**即可运行；`kb.py` 加
   PGroonga 检索分支（含把「**不可省略**」的相关性方向：`pgroonga_score` 越大越
   相关，与 bm25 相反）。
   - **与原定义的第一处不同**：不是「13 个写入方改走仓储层」，而是用**兼容连接层**
     吸收差异。理由：317 处调用逐处改写既不可能也不必要，改一层的风险面小得多。
     仓储层照旧存在并继续收拢（那是目标架构），但迁移不再依赖它先完工。
   - **与原定义的第二处不同**：**`writelock` 没有删**，移交给 launch。理由：
     它是 SQLite 上唯一的并发保护，删了它而仍跑 SQLite 会直接并发写冲突。
     **删除必须与切换后端同时发生**（launch 的原子动作）。
   - 已实测：检索在 PG 上返回真实结果；`row[0]` / `row["列名"]` / `IFNULL` /
     `PRAGMA table_info`（形状与 SQLite 逐项一致）/ `sqlite_master` / `date('now')` /
     `MAX(0,)` 全部工作；全量测试与 ruff 全绿。
5. **[web]** 网站三块：检索+详情、AI 助手、翻译质量可视化
6. **[tokens]** 两侧省 token
7. **[launch]** 端到端冒烟 + 一次性切换 + 旧文件清理（备份留两周）

**launch 前必须补完的方言清单**（不影响「机制成立」，但影响具体功能在 PG 上的完整性）：
- `INSERT OR IGNORE` 8 处 → `ON CONFLICT DO NOTHING`
- `lastrowid` 5 处（PG 无此属性）→ 改用 `RETURNING`（`repo.fetch.log_start` 已是范例）
- `changes()` 2 处 → 用 `cursor.rowcount`
- `sqlite3.OperationalError` / `IntegrityError` 13 处 → psycopg 的异常类型
- `datetime('now','-N days')` / `strftime` 各 2 处 → PG 的 `now() - interval` / `to_char`
- `executescript` 已在兼容层实现（按分号拆分），无需再改

**另一个必须记住的坑**：psycopg 用 `%s` 做占位符，所以 SQL 里**字面量的 `%`
必须转义成 `%%`** —— `LIKE '%的通知'` 这类写法全库到处都是，而 sqlite3 用 `?`、
`%` 从来只是普通字符。漏掉这步的表现极具误导性：psycopg 报的不是「未知占位符」，
而是构造错误消息时对中文解码失败，抛 `UnicodeDecodeError`，让人往编码方向查半天。
`adapt_sql` 里**先转义 `%`、后换 `?`，顺序不能反**。

**迁移必踩的坑**：
- PG 的 identity 列默认拒绝显式插入 id，而迁移必须保留原 id（引用都按 id 走）
  —— 所以 schema 用 **`GENERATED BY DEFAULT AS IDENTITY`**，并**在导入后 `setval` 重置序列**，
  否则后续 INSERT 会撞主键。

**测试策略**：496 个测试现在跑在 SQLite 上，迁移后跑在 `taxassist_test` 库上
（每次重建 schema，用 `src/taxassist/pg/schema.sql`）。

---

## 八、已踩的坑（别重踩）

1. **中文参数经 bash 传给 psql 会走 CP936**，报「无效的 UTF8 编码字节序」。
   解法：`PGCLIENTENCODING=GBK`，或把 SQL 写到 UTF-8 文件用 `psql -f`。
2. **复制 PGroonga 进 `C:\Program Files` 需提权**（`Start-Process -Verb RunAs`）。
3. **`ALTER SYSTEM` 改 `listen_addresses` 后必须重启服务**（`pg_reload_conf()` 不够；
   `pg_ctl reload` 会因权限失败）。
4. **`pg_relation_size` 对 PGroonga 索引报 0 是正常的**（它用自己的存储，不占 PG 表空间）。
5. **生成 DDL 时顺序必须是 表 → 索引 → 视图**（按 type 字母序会让索引先于表创建）。
6. **`.pgpass` 不能在 shell 里用变量路径引用**（做证据时没有 receipt）。

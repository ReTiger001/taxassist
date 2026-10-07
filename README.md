# 税务智能知识助手（本地部署）

面向税务岗的本地工作助手：**自动采集政策法规、维护效力状态、支持汇算清缴等批量作业**。

---

## 一、三条不可违反的边界

1. **客户数据绝不出本机。** 客户资料只进本地代码与本地模型。
   检索关键词里永远不得出现客户名、税号、金额——`collect/http.py` 的
   `GuardedClient` 会逐字符检查并**直接拦截**命中的请求（禁词表见
   `config.FORBIDDEN_OUTBOUND_PATTERNS` 与 `data/outbound_denylist.txt`）。
2. **联网只用于抓公开政策。** 抓取的是政府公开发布的信息。
3. **每条结论可溯源。** 任何效力判断都必须附原文片段与 URL，不允许"无出处的结论"。

> 注意：本文所述"本地"指**代码与数据在本机执行**。在对话式 AI 里粘贴客户资料
> 不属于本地处理——那一步会把数据送出本机。

---

## 二、快速开始

```bash
# 首次：初始化数据库
.venv/Scripts/python.exe -m taxassist initdb

# 每日增量抓取（回溯 7 天重叠窗口，靠 doc_uid 去重防漏）
.venv/Scripts/python.exe -m taxassist collect --days 7

# 首次全量导入（按年切窗口，约 5000+ 条 / 政策法规栏目）
.venv/Scripts/python.exe -m taxassist collect --full --year-from 1984

# 查看抓取日志与库统计（含不完整告警）
.venv/Scripts/python.exe -m taxassist status

# 全文检索
.venv/Scripts/python.exe -m taxassist search 研发费用加计扣除

# 让 AI 直接查这个库（两条路，都只读、都在本机跑）
.venv/Scripts/python.exe -m taxassist mcp    # MCP stdio 服务，挂给 Claude Desktop / Cursor 等
.venv/Scripts/python.exe -m taxassist kb     # 本机 JSON 接口 http://127.0.0.1:8767/
.venv/Scripts/python.exe -m taxassist kb --selftest   # 自检：库能否读、检索能否命中、是否真的只读

# 日常运行：一条命令管住整套（无窗口，日志落 data/logs/）
.venv/Scripts/python.exe -m taxassist service start    # 起 web(8765) 与 worker；已在跑的不重复起
.venv/Scripts/python.exe -m taxassist service status   # 谁在跑、/health 通不通、各日志尾部
.venv/Scripts/python.exe -m taxassist service stop     # 停 —— 只动它自己起的进程，不碰别的

# ollama 不归它管：它的可执行文件路径因机器而异，只检测 11434 端口。
# 想让 service 代劳，先设环境变量 TAXASSIST_OLLAMA_EXE 指向 ollama.exe。
# 也可以照旧双击 启动Taxassist.bat（会开三个控制台窗口）。
```

AI 接入的完整说明（客户端配置、端点清单、数据边界）见
**[docs/AI接入.md](docs/AI接入.md)**。

调试单个源的结构（**源改版后第一步就跑它**）：

```bash
.venv/Scripts/python.exe scripts/probe_source.py <URL>
```

---

## 三、数据来源与抓取原理

主源：**国家税务总局政策法规库** `fgk.chinatax.gov.cn`

- 列表页（`listflfg.html` / `zcwj.html`）是 **JS 壳页面**，不含政策条目，**不可直接解析**。
- 真正的数据接口：

  ```
  GET https://www.chinatax.gov.cn/search5/search/s
      ?siteCode=bm29000002&indexCode=1
      &column=政策法规
      &startTime=2026-09-01 00:00:00&endTime=2026-09-07 23:59:59
      &pageNum=0&orderBy=5
  ```

  返回 JSON，条目在 `searchResultAll.searchTotal`（数组），真实条数在 `searchResultAll.total`。

### 实测确认的五个坑（改动前务必复核）

| 坑 | 事实 | 后果 |
|---|---|---|
| `pageSize` | 传 >10 无效，服务端固定 **10 条/页** | 误以为一次能拉 50 条，实际只抓 10 条 |
| `cwrqStart`/`cwrqEnd` | **完全无效**（只存在于前端 JS） | 三个不同窗口返回同一批数据；会造出"每天认真抓旧数据"的假系统 |
| 正确日期参数 | **`startTime` / `endTime`** | 已实测：9 月窗口 total=3，近 3 天 total=0 |
| `xxgk_effectLevel` | 实际是**文件类型**（税务规范性文件/财税文件），不是效力等级 | 用它判效力会全错 |
| `govDoc.docNo` | 只是**序号**（如 `"19"`），不是完整文号 | 直接当文号用，检索与引用全错 |

真正的官方时效标注是 `xxgk_aging`（如"尚未生效"），但**填充率低**（实测 3/10），
因此缺失时不能默认"有效"，必须进待人工确认队列。

### 为什么要"按页归档原始响应"

政策网站会改版、文件会被撤下。归档 gzip 原始响应（`data/raw/`）保留了
"当时官方返回的就是这些"的证据，配合 `raw_snapshot` 表的哈希，可事后自证。

---

## 四、完整性契约（本项目最重要的一条）

每次抓取结束后，**`fetched_count` 必须等于接口声称的 `reported_total`**。

不等则 `fetch_log.status = 'incomplete'` 并显式告警。

理由：政府网站的典型故障不是报错，而是**静默成功**——HTTP 200、JSON 正常、
内容为空或不全。没有这条校验，你会安静地以为"今天没有新政策"，
而真相是抓取坏了。对税务工作而言，**漏掉一份公告的代价远大于多跑一次抓取**。

`tests/test_pipeline.py` 专门锁死这条不变量。

---

## 五、目录结构

```
src/taxassist/
    config.py          配置：路径、抓取限速、出网禁词
    db.py              SQLite schema（policy / raw_snapshot / fetch_log / attachment / policy_relation）
    store.py           入库：upsert、按页归档、抓取日志
    pipeline.py        编排：抓取→清洗→入库→归档→日志
    cli.py             命令行入口
    kb.py              AI 检索内核：只读、结构化、可溯源（网页与 AI 共用）
    mcp_server.py      MCP stdio 服务：把库挂给 Claude Desktop / Cursor 等
    kb_api.py          本机 JSON 接口（FastAPI，仅回环地址，无认证）
    collect/
        http.py        带出网守卫与限速的 HTTP 客户端
        fgk.py         法规库采集器（含实测结论）
        normalize.py   字段清洗、文号重建
scripts/probe_source.py   源探测：结构、新鲜度、JS 渲染判定
docs/AI接入.md            AI 接入说明：客户端配置、数据边界、已知限制
tests/                    不变量测试
data/                     数据库与原文归档（不入版本库）
```

### 字段命名约定

- `o_*` = **官方原始字段**（原样留存，便于与官方核对）
- `p_*` = **本系统推断/判定字段**（必须带来源与置信度）

两者绝不混用：出错时才能判断是官方数据的问题，还是我们推断的问题。

---

## 六、当前进度与未完成部分

已完成：

- 环境与地基、SQLite schema、完整性校验
- **政策采集层**：法规库按日期窗口增量抓取、详情页抓取（正文 / 完整文号 / 官方时效 /
  施行日期 / 关联文件）、附件下载与解析（PDF / xlsx / xls / docx）、原始响应按页归档
- **效力判定与引用关系**：文号归一与重建、书名号文件名匹配、废止关系图、三阶段判定、
  待人工确认队列
- **本地 Web 应用**：总览 / 检索 / 政策详情 / 每日简报（仅监听 127.0.0.1，只读，无外部资源）
- **后台调度与告警**：启动即补抓落后日期、每日定时日更、抓取不完整在首页显式报警
- **相关性过滤**：内容类型分类（实质政策/解读/新闻科普）+ 17 个税种筛选
- **省级源**：29 个省级行政区已接入（31 个源）。这些站多在加速乐 WAF 后面
  （响应 412 + 一段挑战 JS），走真浏览器执行挑战拿 cookie 后取页面；详情页
  同样走浏览器，且按域名并发复用会话。每个源可单独配挑战等待时长与导航超时 ——
  有的站要 12 秒以上，默认的 6 秒会让人误判成"抓不到"（辽宁、新疆就是这么来的）
- **汇算清缴样板**：输入模板 → 9 条调整规则计算（限额/不得扣除/加计扣除）→
  四页 Excel 底稿（含逐行计算过程、法条依据、依据在本地库中的核查状态、未考虑事项）

命令行一览：

```
initdb        初始化数据库
collect       抓取政策（--days 增量 / --full 全量）
provincial    抓省级税务局政策（--source 指定源，默认全部）
enrich        抓详情页（正文/文号/时效/施行日/附件）
attach        下载并解析附件
judge         效力判定与引用关系
review        待人工确认队列
serve         启动网页界面（含后台定时）
service       一条命令管住整套：start / stop / status（无窗口，日志落 data/logs/）
reparse       从归档快照重解析详情页（不联网，改进解析器后跑它）
backfill      从已有正文补全施行日期与文号（不联网）
dedupe        清理跨源重复
cit-template  生成汇算清缴输入模板
cit           生成纳税调整底稿
status        抓取日志与库统计
search        命令行全文检索
```

## 开发约定：改解析器前先离线验证

**不要靠"抓一个省看一个省"来验证解析器改动。** 这次改正文容器选择器时用那个
方式耗了好几轮仍没找准，后来改成在归档快照上批量统计，一次性就找到了最优组合
（命中页面数从 39 涨到 444）。

`data/raw/` 里存着**每一个抓过的页面**（gzip 原始 HTML），所以解析器改动可以
完全离线迭代：

```bash
python -m taxassist reparse    # 用新解析器重跑全部快照，不联网
```

再看效果 —— `worker --status` 的覆盖率报告，或直接查库。`reparse` 不联网、
也不动抓取记录，应当成为**解析器改动的标准前置步骤**。

另外两条同类约定：

- **阈值要用数据定，不要拍。** `_BODY_MIN_HINT`（正文容器最小长度）当年是试出来的，
  改它之前先在快照上统计一遍长度分布。
- **改完跑黄金样本。** `tests/test_parser_golden.py` 钉住了每个已修缺陷
  （文号来源、文号年份、施行日期表述、标题日期前缀）。写样本时**必须带正文容器** ——
  没有容器时 `body` 为空，断言会"通过"却什么都没测到。

未完成：

- [x] 省级源扩展：29 个省级行政区已接入（见 `src/taxassist/province.py` 的 `ADAPTERS`）
- [ ] 河北 / 青海：证书域名不匹配已放行，页面仍只回 1.9KB / 148 字节，原因待查
- [ ] 天津其余栏目（政策解读 lmdm=030002、通知公告 lmdm=010003）尚未逐个接
- [ ] 重庆 / 天津的「政策法规库」是 JS 检索页（没有静态列表），未接
- [ ] 地区维度筛选（税种维度已完成）
- [ ] `.doc` / `.wps` / `.et` 老格式附件解析（需接 WPS COM 或 LibreOffice）
- [ ] 官方《失效废止文件目录》公告的专项抓取（效力判定的最权威来源）

## 七、实测数据与踩过的坑

**样本**：2026-08-30 ~ 2026-09-29 窗口，抓取 50 条（政策法规 3 / 政策解读 47），
50 条详情页全部抓取成功，识别附件 3 个。

**字段真实填充率**（50 条样本，用于校准预期）：

| 字段 | 填充率 | 结论 |
|---|---|---|
| `cwrq` / `pub_name` | 100% | 列表接口元数据可靠 |
| `content`（列表接口） | 8% | **正文必须靠详情页补** |
| `xxgk_aging`（官方时效） | **2%** | 官方时效标注形同虚设，不能依赖 |
| `p_doc_no_full` | 6% | 文号需从详情页补，详情页有完整文号 |

**踩过的坑（已修，均有回归测试）**：

1. **详情页正文提取把导航标签当正文**——"相关政策文件"（6 字）被写入全库 12 条记录。
   比抓不到更危险，因为它看起来像成功。修复：正文最低长度门槛 + 界面标签黑名单。
   测试：`test_navigation_label_is_not_treated_as_body`。
2. **误判详情页为 JS 加载**——因为某条公告的最大文本块只有 493 字符，
   其实那条公告本身就很短。教训：别用文本长度判断页面是否需要渲染。
3. **联合发文文号只抓到最后一个机关**——"甲 乙 丙 国家税务总局公告2021年第10号"
   曾解析成"国家税务总局2021年第10号"，丢了三个发文机关。
4. **研发费用加计扣除从未生效**——规则 code 与输入字段名不一致，引擎按 code 取值
   永远取到 0，会静默算出一份漏掉加计扣除的底稿。已加结构性测试：
   `test_every_rule_code_matches_an_input_field`。
5. **省级列表页标题被拼接**——广东的 `<a>` 里除标题 `<font>` 还嵌着"文件解读"图标
   `<em>`，`text_content()` 把两者都取到，得到"…公告关于《…公告》的解读"这种畸形标题。
   修复：优先取 `<a>` 的 `title` 属性。
6. **跨源重复**——同一份文件既在总局库、又被省级站转载，入库成两条。已加入库去重
   与 `taxassist dedupe` 清理命令。
7. **抓完未自动判定**——新入库条目的效力状态停在 `unknown`，界面显示英文，看起来像没做完。
   修复：抓取后自动跑判定 + 界面统一显示中文「未判定」。
8. **顶栏在窄窗口塌陷**——`.top nav` 未禁止换行，960px 宽时导航被挤成竖排。
   修复：`white-space:nowrap` + 900px 以下收起导航。

**教训汇总**：这个项目里最危险的从来不是"抓取失败报错"，而是**看起来成功的错误数据**——
正文是导航标签、底稿少了 500 万调减、同一份公告出现两次、失效文件标成有效。
因此每条修复都配了回归测试，且测试名直接写明当时踩的坑。

**未验证**：官方对抓取频率的限制、省级站点结构、`.doc` 老格式的覆盖比例。

## 八、附件格式覆盖

解析器**按文件头认格式，不只看扩展名** —— 这些站点的扩展名会说谎：
实测有标为 `.docx` 的文件，文件头是 `d0cf11e0`（OLE2 老式文档），
按扩展名交给 python-docx 必然报错。那不是"文件坏了"，是工具选错了。

| 格式 | 状态 | 说明 |
|---|---|---|
| `.pdf` | ✅ 可用 | pymupdf 抽文本层；**扫描件**返回 `no_text_layer` 而非报错 |
| `.xlsx` / `.docx` | ✅ 可用 | openpyxl / python-docx（ZIP 容器） |
| `.xls` | ✅ 可用 | xlrd（OLE2 容器，税务申报表大量使用） |
| `.doc` / `.wps` / `.et` | ❌ 不支持 | 老式 OLE2 文档，如实标 `unsupported`，不假装成功 |
| 扩展名与内容不符 | ⚙️ 自动纠正 | `.doc` 实为 ZIP → 按 docx 解析；`.docx` 实为 OLE2 → 标 unsupported |

三种状态必须分清：`no_text_layer`（扫描件，打开看一眼就行）、
`failed`（解析器异常，要改代码）、`unsupported`（**我们读不了，不是文件坏了**）。

## 九、部署与对外访问

**这个应用跑在你自己的机器上，不上云。** 不是懒得部署，而是三个核心都依赖本机：
SQLite 政策库（约 200MB，每日在本机更新）、抓取调度器（要访问政府网站）、
客户资料（绝不能出本机）。

### 不要部署到 Cloudflare Workers / Pages

实测过，会失败：

```
Executing user deploy command: npx wrangler deploy
✗ [ERROR] Could not detect a directory containing static files (e.g. html, css and js)
```

深层原因：Workers 是 JS/TS 边缘运行时 —— 跑不了 Python/FastAPI，没有文件系统，
放不下 SQLite 政策库。**要让它上云，就得先把数据库搬出去，
而那正是本项目第一条边界禁止的事。**

### 正确的对外方式：Cloudflare Tunnel

本机服务只监听 `127.0.0.1`，由 `cloudflared` 反向代理出一个 HTTPS 地址。
数据始终在你本机的进程里，Cloudflare 只转发加密流量。

**临时地址（零配置，每次重启会变）**

```
启动对外访问.bat
```

**固定地址（需要一个托管在 Cloudflare 的域名）**

```bash
cloudflared tunnel login                      # 浏览器里选你的域名
cloudflared tunnel create taxassist           # 记下输出的隧道 ID
cloudflared tunnel route dns taxassist 你的域名
# 再写 ~/.cloudflared/config.yml：
#   tunnel: <隧道ID>
#   credentials-file: <隧道ID>.json
#   ingress:
#     - hostname: 你的域名
#       service: http://127.0.0.1:8765
#     - service: http_status:404
cloudflared tunnel run taxassist
```

之后地址永久不变，服务仍只在本机。

**对外暴露前请确认三件事**：已启用 HTTPS、已限制来源、
已向公司 IT/风险部门确认对外暴露的合规性。

# 把政策库接给 AI（本机知识库接口）

本文说明如何让 AI（桌面客户端或你自己的程序）**检索这个政策库**。

一句话概括：在原有政策库之上加了一层**只读的 AI 调用接口**，
数据与检索全部在本机执行，AI 拿到的是带文号、带 URL、带效力依据的结构化结果。

---

## 一、它能做什么

四个能力，MCP 工具与 HTTP 端点一一对应：

| 能力 | MCP 工具 | HTTP 端点 | 说明 |
|---|---|---|---|
| 全文检索 | `search_policies` | `GET /api/search` | 关键词 + 税种/地区/年份/效力/栏目筛选，返回文号、日期、效力、URL、正文摘要 |
| 取全文档案 | `get_policy` | `GET /api/policy/{doc_uid}` | 正文（分段读）、效力依据与证据片段、引用/废止关系、附件摘要 |
| 按文号查 | `lookup_by_doc_no` | `GET /api/docno/{doc_no}` | 兼容 `〔〕`/`[]`/`（）` 与空格差异；被引用但未入库的文号也能查到出处 |
| 库概况 | `kb_overview` | `GET /api/overview` | 总数、效力分布、可筛税种/地区/年份、数据新鲜度、待人工确认数 |

三条设计上的硬约束（不是"暂时没做"，是刻意的）：

1. **只读。** 连接用 SQLite 的 `mode=ro` + `PRAGMA query_only`，在文件层拒绝写入。
   AI 一次对话会发起几十次调用，能写就有和采集/翻译长任务抢写锁的风险。
2. **与网页共用同一套检索。** AI 与网页对同一个问题必须给同样的结果 ——
   否则「网页搜得到、AI 说没有」会直接摧毁对系统的信任。检索逻辑在
   `filters.py` / `translate.py` / `kb.py` 里只有一份。
3. **可溯源。** 每条结果都带 `url`；效力结论带来源与证据片段。
   这是 README 第一条边界，不因为调用方是 AI 就放松。

---

## 二、接入方式 A：MCP（桌面 AI 客户端，推荐）

MCP（Model Context Protocol）是 Claude Desktop、Cursor、Cherry Studio 等
客户端直接挂载本地工具的标准方式。挂上之后，AI 会**自己决定**什么时候检索、
检索什么，你直接问它问题即可。

### 2.1 Claude Desktop

编辑 `%APPDATA%\Claude\claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "taxassist": {
      "command": "D:\\EY-project\\.venv\\Scripts\\python.exe",
      "args": ["-m", "taxassist", "mcp"]
    }
  }
}
```

### 2.2 Cursor

编辑 `%USERPROFILE%\.cursor\mcp.json`（全局）或项目下的 `.cursor\mcp.json`，
内容同上。

### 2.3 Cherry Studio 等其他客户端

在「MCP 服务器」设置里新增一个 **stdio** 类型的服务器：

- 命令：`D:\EY-project\.venv\Scripts\python.exe`
- 参数：`-m taxassist mcp`

> **不需要设置工作目录**。包是 editable 安装的，从任何目录启动都能找到库
> （已实测：在 `C:\Users\Administrator` 下运行自检正常）。

### 2.4 挂上之后怎么确认

问它一句「用 kb_overview 看看知识库里有什么」，或「查一下研发费用加计扣除的
现行有效政策，给我文号和链接」。若客户端有 MCP 日志面板，那里能看到每次调用。

---

## 三、接入方式 B：本机 HTTP 接口

给自建脚本、内部系统，以及不支持 MCP 的本地模型用。

```bash
python -m taxassist kb                      # 默认 http://127.0.0.1:8766/
python -m taxassist kb --port 9000          # 换端口
```

Windows 上也可以直接双击 `启动知识库接口.bat`。

```bash
# 检索
curl "http://127.0.0.1:8766/api/search?q=研发费用加计扣除&limit=5"

# 只看新疆的、现行有效的增值税政策
curl "http://127.0.0.1:8766/api/search?tax=增值税&region=新疆&effect=现行有效"

# 按文号查
curl "http://127.0.0.1:8766/api/docno/财税〔2014〕116号"

# 取全文（正文很长时用 offset 续读）
curl "http://127.0.0.1:8766/api/policy/<doc_uid>?content_offset=6000"

# 库概况
curl "http://127.0.0.1:8766/api/overview"
```

> **Windows 上用 curl 传中文会乱码**（实测：`q=研发费用加计扣除` 到服务端成了
> `�з����üӼƿ۳�`，检索 0 条）。原因是 curl 是原生程序，命令行参数按系统 ANSI
> 代码页（GBK）解释，与接口无关 —— 同一参数用下面的 Python 请求正常命中 125 条。
> 命令行里试中文，请用 Python 片段、浏览器地址栏，或先把中文写成 URL 编码。

Python 里就是普通 HTTP：

```python
import httpx

r = httpx.get("http://127.0.0.1:8766/api/search",
             params={"q": "小微企业 所得税", "limit": 5}, timeout=30)
for hit in r.json()["hits"]:
    print(hit["cwrq"], hit["doc_no"], hit["effect_status"], hit["title"])
    print("  ", hit["url"])
```

这些端点**没有认证、也不该有**：它只监听 `127.0.0.1`，能访问的只有本机进程。
绑到其他地址时会打印风险警告 —— 那种场景请改用 `python -m taxassist serve`
（带登录认证与 HTTPS 的那条路）。

---

## 四、数据边界（务必看这一段）

政策库里的每一条都是**政府公开发布的信息**，检索它们、把它们交给 AI 讨论，
不涉及客户数据。

真正要守的是另一条线：**别把客户资料原文贴进云端 AI 对话框**。
「帮我看看这份合同/这个税号适用哪条政策」——这句话本身就把客户信息送出了本机，
而且这一步和本知识库无关，接不接 MCP 都一样。项目 README 第一条边界说的就是它。

安全的用法是让 AI 先取政策、你在本地对照客户资料：

- ✅「查一下 2024 年以后关于研发费用加计扣除的现行有效文件，列文号和链接」
- ✅「小型微利企业的所得税优惠，现行有效的依据是哪几份」
- ❌「我们客户 A 公司（税号 xxx）这种情况能不能适用这条」

如果确实要用本地模型做全链路（客户资料 + 政策都不过网），
可以把本接口接到本机运行的模型上（如 Ollama / LM Studio 搭配的本地前端），
此时整条链路都在本机。

---

## 五、检索效果与已知限制

**这是关键词检索，不是语义检索。** 说清楚它的边界，比夸大它能干的事更有用：

- **同义改述会漏。** 搜「小微企业」未必命中只写「小型微利企业」的文件。
  补救办法是让 AI 换词多试几次（工具的返回里会提示），或先看 `kb_overview`
  的税种列表收窄范围。
- **文号查询走专门工具。** 文号里的括号写法很杂，`lookup_by_doc_no` 已做归一，
  别用全文检索去凑。
- **多词之间是 AND。** 词越多命中越窄，搜不到时先去掉一些词。
- **2 字词（契税、关税、个税）** 走 LIKE 回退（trigram 分词器对 <3 字符会静默
  返回 0 条），能查到但比 FTS 慢一些。
- **正文按段返回。** `get_policy` 默认给前 6000 字，`content_offset` 续读，
  上限 40000 字。附件正文只给前 1500 字摘要。
- **效力状态要区分来源。** `official`=官方标注、`inferred`=据废止公告推断、
  `default`=**推定有效**（仅因未发现废止，最弱）、`manual`=人工确认。
  库里还有一部分 `unknown` 与 `needs_review`，引用前必须核对证据片段。
  MCP 的 `instructions` 已把这条规则下发给 AI，客户端若不支持该字段，
  可以在对话里自行交代一句。

---

## 六、排错

先跑自检，它会把每一环单独说清楚：

```bash
python -m taxassist kb --selftest
```

输出示例（全部通过时退出码 0）：

```
[√] 库可读：共 13823 条政策
[√] 检索可用：「研发费用加计扣除」命中 125 条（fts 模式）
[√] 只读：写入已被 SQLite 拒绝
[√] MCP 工具：search_policies, get_policy, lookup_by_doc_no, kb_overview
```

| 现象 | 原因与处理 |
|---|---|
| 客户端里看不到工具 | 检查配置里的 python 路径是否为绝对路径；重启客户端；看客户端的 MCP 日志 |
| 工具调用返回「知识库不可用」 | 库里没有 `policy` 表，先跑 `python -m taxassist initdb` 并采集数据 |
| 检索总是 0 条 | 换更短的词；用 `kb_overview` 看实际可筛值；确认数据是否已抓到（看 `data_freshness`） |
| 端口被占用 | `python -m taxassist kb --port 9001` |
| 想确认「AI 到底查了什么」 | 在 `python -m taxassist kb` 的窗口里有请求日志（uvicorn 默认打印访问日志） |

---

## 七、实现位置（改代码前先读这几处的注释）

| 文件 | 职责 |
|---|---|
| `src/taxassist/kb.py` | 只读检索内核：检索/详情/文号查找/概况。**改检索语义只改这里**，网页与 AI 同时生效 |
| `src/taxassist/mcp_server.py` | MCP stdio 服务：协议、工具定义、`instructions` 措辞 |
| `src/taxassist/kb_api.py` | 本机 HTTP 接口（FastAPI） |
| `tests/test_kb.py` | 锁住四条不变量：只读、可溯源、不因输入崩溃、协议正确 |

两个容易踩的坑，都在注释里写明了，改动时别绕过去：

- **MCP 的 stdout 是协议通道**，任何一行 print 都会让客户端「挂上去没反应」；
  日志一律走 stderr。
- **Windows 控制台默认 GBK**，中文正文里一个编不出的字符就能让服务抛
  `UnicodeEncodeError` 死掉。MCP 层直接操作字节流，CLI 层走 `_setup_console()`。

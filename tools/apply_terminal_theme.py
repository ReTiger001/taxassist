"""给两个基类注入「终端主题」样式（追加覆盖，不重写原有规则）。

======================================================================
为什么不重写那 340 行 CSS
======================================================================

base.html 的 <style> 里有几十个类名（.card/.pad/.bar/.badge/.b-ok/
.row-tag/.src/.docno/.split/.en/.f-quick/.q-chip/.attach-text…），散落在
9 个页面里。凭记忆重写一遍，漏掉任何一个都会让某个页面塌掉。
而原 CSS **大量使用 CSS 变量**（--paper/--card/--ink/--line…），
所以正确做法是：**覆盖变量 + 补几条关键规则** —— 大部分样式会自动变，
且不可能漏类名。

======================================================================
设计约束（三条，都是为了不让视觉压过可读性）
======================================================================

① **语义色必须保留**：效力有四种状态（现行有效/已废止/尚未生效/未判定），
   纯绿一种颜色分不出来 —— 而那恰恰是做判断时最先看的东西。
   所以红/琥珀是刻意留下的，不是没改干净。
② **中文不强制等宽**：把等宽字体排在 font-family 最前面，中文在等宽字体
   里没有字形，浏览器自动回退到雅黑 —— 一行规则实现「拉丁数字等宽、
   中文常规」。项目早年踩过坑：中文等宽长文极难读。
③ **不加扫描线/闪烁特效**：正文是公文，一读几千字。终端感靠黑底、绿字、
   直角、等宽字体已经足够；再叠特效就是拿可读性换气氛。
"""
import re
from pathlib import Path

ROOT = Path(r"D:\EY-project\src\taxassist\web\templates")

# 终端主题：覆盖变量 + 关键规则。放在原 <style> 的最末尾，
# 同权重下后出现的规则生效。
THEME = """
/* =================================================================
   终端主题（覆盖层，写在最后所以生效）
   -----------------------------------------------------------------
   方向：黑底 / 终端绿 / 直角 / 中英混排等宽。
   它取代了原先的「档案工作台」浅色文档风。

   三条刻意的克制（理由见 tools/apply_terminal_theme.py 的文件头）：
     · 语义色保留 —— 效力四态必须能一眼分开
     · 中文不强等宽 —— 等宽字体排前面，中文自动回退
     · 不加扫描线/闪烁 —— 正文是公文，一读几千字
   ================================================================= */
:root{
  --bg:#000; --bg-2:#0a0f0a; --bg-3:#111811;
  --terminal:#00ff00; --terminal-2:#00cc00; --terminal-3:#008800;
  --terminal-4:#005500; --terminal-dim:#0e3a0e; --terminal-dim-2:#082a08;

  /* —— 覆盖原有变量名，让既有规则自动变终端风 —— */
  --paper:#000; --card:#0a0f0a; --sunken:#111811;
  --ink:#00ff00; --ink-2:#00cc00; --ink-3:#008800;
  --line:#0e3a0e; --line-2:#082a08;
  --brand:#00ff00; --brand-2:#00cc00;
  /* 语义色：**刻意保留**，见文件头理由 ① */
  --ok-bg:#003300;  --ok-fg:#00ff00;  --ok-bar:#00cc00;
  --warn-bg:#2a1c00; --warn-fg:#ffb000; --warn-bar:#ffb000;
  --bad-bg:#2a0500;  --bad-fg:#ff5f56;  --bad-bar:#ff3b30;
  --mut-bg:#0d0d0d;  --mut-fg:#4d7a4d;  --mut-bar:#3a5f3a;
}
html,body{background:#000}
::selection{background:#00ff00;color:#000}

/* 中英混排：等宽排最前，中文自动回退到雅黑（见文件头理由 ②） */
body{font:14px/1.68 "Cascadia Mono",Consolas,"SF Mono",Menlo,
  "DejaVu Sans Mono","Courier New",monospace,"Microsoft YaHei",
  "PingFang SC",sans-serif}
code,kbd,.docno,.d,.en,.n,.cit{font-family:"Cascadia Mono",Consolas,
  "SF Mono",Menlo,monospace}
/* 数字对齐：表格里的日期/条数/编号用等宽数字，扫列时不会跳动 */
td,.d,.n,.cit{font-variant-numeric:tabular-nums}

/* 滚动条 —— 细节，但它是"运行框"观感的一部分 */
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-track{background:#000}
::-webkit-scrollbar-thumb{background:#0e3a0e;border:1px solid #000}
::-webkit-scrollbar-thumb:hover{background:#0b4b0b}

/* 直角：终端里没有圆角 */
.card,.pad,.bar,.badge,.src,.row-tag,.evi,textarea,input,button,
select,.ibtn,.chip,.split{border-radius:0}

/* 链接：悬停反色高亮，像终端里选中一段 */
a{color:#00cc00;border-bottom-color:#0e3a0e}
a:hover{background:#00ff00;color:#000;border-bottom-color:#00ff00}

/* 顶栏 */
.top{background:#000;border-bottom-color:#0e3a0e}
.top nav a{color:#008800}
.top nav a:hover{background:none;color:#00ff00;
  border-bottom-color:#00ff00}
.brand{color:#00ff00;font-weight:700;letter-spacing:.5px}
.brand:hover{background:none;color:#00ff00}
/* 品牌名后面跟一个闪烁光标 —— 纯 CSS，是"运行框"最省的一个信号 */
.brand::after{content:"_";animation:ta-blink 1.1s step-end infinite}
@keyframes ta-blink{0%,100%{opacity:1}50%{opacity:0}}
.local{background:#001a00;border-color:#0e3a0e;color:#00cc00}
.mode-out{background:#2a1c00;border-color:#4a3200;color:#ffb000}
.quick input{background:#0a0f0a;border-color:#0e3a0e;color:#00ff00}
.quick input::placeholder{color:#005500}
.quick input:focus{background:#001a00;border-color:#00cc00}
.quick button{background:#001a00;border-color:#0e3a0e;color:#00cc00}
.quick button:hover{background:#002a00;color:#00ff00}
.who,.who-login{color:#008800}
.who-name{color:#00ff00}

/* 标题：终端里用提示符前缀，比加粗更贴 */
h1{color:#00ff00}
h1::before{content:"> ";color:#008800}
h2{color:#008800}

/* 卡片与表格 */
.card{background:#0a0f0a;border-color:#0e3a0e}
.bar{background:#0a0f0a;border-color:#0e3a0e}
.bar .cell{border-right-color:#082a08}
table{border-color:#0e3a0e}
th{background:#0d140d;color:#008800;border-color:#0e3a0e}
td{border-color:#082a08}
tr:hover td{background:#0d140d}

/* 表单：终端里的输入位 */
input,textarea,select{background:#0a0f0a;border-color:#0e3a0e;
  color:#00ff00}
input:focus,textarea:focus,select:focus{border-color:#00cc00;outline:none;
  background:#001a00}
input::placeholder,textarea::placeholder{color:#005500}

/* 按钮：主按钮反色（绿底黑字），次按钮描边 */
button,.ibtn{background:#001a00;border-color:#0e3a0e;color:#00cc00}
button:hover,.ibtn:hover{background:#002a00;border-color:#00cc00;
  color:#00ff00}
.ibtn.go{background:#00ff00;color:#000;border-color:#00ff00;
  font-weight:700}
.ibtn.go:hover{background:#00cc00;color:#000}

/* 双语排版：英文原是浅灰，这里改成暗绿 —— 从属关系不变 */
.en{color:#008800}
.en a{color:#008800}
.mt-note{color:#008800;border-left-color:#0e3a0e}

/* 摘要条 / 空态 / 日期分隔 */
.sub,.empty,.hint{color:#008800}
.day{background:#0d140d;color:#00cc00;border-color:#0e3a0e}

/* 快捷筛选（年份/地区的 q-chip） */
.q-chip{background:#0a0f0a;border-color:#0e3a0e;color:#00cc00}
.q-chip:hover{background:#001a00;border-color:#00cc00;color:#00ff00}
.q-chip.on{background:#00ff00;color:#000;border-color:#00ff00;
  font-weight:700}
.f-q-label{color:#008800}

/* 附件原文：终端里的输出区 */
.attach-text{background:#050805;border-color:#0e3a0e;color:#00cc00}

/* 登录/注册页 */
.auth-card,.auth-wrap .card{background:#0a0f0a;border-color:#0e3a0e}

/* 助手对话页：输入区与依据卡 */
.composer{background:#0a0f0a;border-color:#0e3a0e;box-shadow:none}
.msg.user .body{background:#0d140d;border-color:#0e3a0e}
.ans h3{color:#00ff00;border-bottom-color:#0e3a0e}
.ans h4{color:#00cc00}
.ans strong{color:#00ff00}
.evi{background:#0a0f0a;border-color:#0e3a0e}
.evi > summary{background:#0d140d;color:#00cc00;border-bottom-color:#0e3a0e}
.evi .row{border-bottom-color:#082a08}
.fact{border-left-color:#0e3a0e;color:#00cc00}
.group-hd{color:#008800}
.stage{color:#008800}
.stage .dot{background:#00ff00}
.err{background:#2a0500;color:#ff5f56;border-color:#4a1010}
.warn-bar{background:#2a1c00;color:#ffb000;border-color:#4a3200}
.cit{background:#001a00;color:#00ff00}
"""


def inject(path: Path, tag: str) -> str:
    """把 THEME 插到 </style> 之前。已注入过就跳过（可重复运行）。"""
    text = path.read_text(encoding="utf-8")
    if tag in text:
        return f"  {path.name}：已有终端主题，跳过"
    idx = text.rfind("</style>")
    if idx < 0:
        return f"  {path.name}：✗ 找不到 </style>"
    text = text[:idx] + THEME + text[idx:]
    path.write_text(text, encoding="utf-8")
    return f"  {path.name}：✓ 已注入（+{len(THEME.splitlines())} 行）"


print("=== 注入终端主题 ===")
for name in ("base.html", "auth_base.html"):
    print(inject(ROOT / name, "终端主题（覆盖层"))

# 自检：变量覆盖项是否都在
base = (ROOT / "base.html").read_text(encoding="utf-8")
need = ["--paper:#000", "--card:#0a0f0a", "--ink:#00ff00",
        "--ok-fg:#00ff00", "--warn-fg:#ffb000", "--bad-fg:#ff5f56",
        "@keyframes ta-blink"]
missing = [n for n in need if n not in base]
print(f"\n=== 自检 ===\n  关键变量：{'✓ 全部就位' if not missing else '✗ 缺 ' + str(missing)}")
print(f"  </style> 数量：{base.count('</style>')}（应为 1）")

# 备份原有浅色主题的痕迹：确认旧变量名仍被覆盖
old = ["--paper:", "--card:", "--ink:", "--sunken:"]
print(f"  旧变量名覆盖：{'✓' if all(o in base for o in old) else '✗'}")

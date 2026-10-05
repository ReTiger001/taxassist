"""全局配置：路径、抓取礼貌参数、运行开关。

所有可调项集中在这里，避免散落在各模块。
环境变量可覆盖，便于把数据放到别的盘（例如 D:/EY-project/data 体积变大后迁移）。
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------- 路径

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("TAXASSIST_DATA_DIR") or (PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"                 # 政策原文归档（HTML/PDF/JSON 快照）
LOG_DIR = DATA_DIR / "logs"
DB_PATH = Path(os.environ.get("TAXASSIST_DB") or (DATA_DIR / "taxassist.db"))

# 客户资料目录：约定放在项目外的独立位置，避免与代码/政策库混放
CLIENT_DIR = Path(os.environ.get("TAXASSIST_CLIENT_DIR") or (PROJECT_ROOT / "clients"))


def ensure_dirs() -> None:
    """建好所有需要的目录（幂等）。"""
    for p in (DATA_DIR, RAW_DIR, LOG_DIR):
        p.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 抓取

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 礼貌抓取：同一站点连续请求的最小间隔（秒）。
# 政府网站无公开 API 配额说明，宁可慢，不可被判定为恶意抓取。
REQUEST_INTERVAL_SEC = 1.5
REQUEST_TIMEOUT_SEC = 40
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 3.0
# 遇到挑战页（HTTP 412）时的退避基数（秒）：第 n 次重试等 n × 这个值。
# 为什么单独给它一个常量：412 的语义是"你请求太密了，稍后再来"，与
# 403/404（重试无意义）不同，值得用比普通失败**更长的**等待。
# 实测河北的 TRS 检索页：连续请求必 412，隔 3 秒仍有；而它一页能给近
# 1000 条，放弃重试等于静默漏掉整页，且状态还记 ok。
CHALLENGE_RETRY_SEC = 5.0

# 服务端固定每页 10 条（已实测：传 pageSize=50 仍只返回 10 条）
FGK_PAGE_SIZE = 10

# ---------------------------------------------------------------- 归档

# 归档原始响应，作为"抓取当时政策原文就是这样"的证据
ARCHIVE_RAW = True

# ---------------------------------------------------------------- 数据边界

# 硬性守卫：任何出网请求的 URL/参数中若命中这些词，直接拒绝发送。
# 目的：防止客户信息被误拼进检索关键词而离开本机。
#
# 选词原则（实测踩过）：**避免过短、过泛的词**。
# 初版把"税号"也列为禁词，结果拦截了公开文件《…药品税号修正清单.pdf》的下载 ——
# 守卫拦下正常抓取，比不拦更糟（会让人怀疑机制而不信任它）。
# 因此只保留**不会出现在政策文件名里**的完整标识。
FORBIDDEN_OUTBOUND_PATTERNS: tuple[str, ...] = (
    "纳税人识别号",
    "统一社会信用代码",
    "营业执照号",
    # 客户名称由本地 outbound_denylist.txt 追加（见下）
)

# 可追加自定义禁词的本地文件（每行一个，不含注释）
FORBIDDEN_EXTRA_FILE = DATA_DIR / "outbound_denylist.txt"


# ---------------------------------------------------------------- 地区

# 省级行政区名称。用于从站点名识别地区、以及界面上的地区筛选。
# 只列省/自治区/直辖市；计划单列市（大连/宁波/厦门/青岛/深圳）暂归入所属省。
CN_PROVINCES: tuple[str, ...] = (
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
)

NATIONWIDE = "全国"


def region_from_site_name(site_name: str | None) -> str:
    """从站点名识别地区。

    ``国家税务总局广东省税务局`` → ``广东``；总局政策法规库 → ``全国``。
    识别不出时返回"全国"而不是空值 —— 空值会让地区筛选漏掉这些记录，
    而它们绝大多数确实来自总局。
    """
    site = site_name or ""
    for province in CN_PROVINCES:
        if province in site:
            return province
    return NATIONWIDE

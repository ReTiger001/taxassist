"""政策源注册表。

======================================================================
先说清现状（2026-09-29 实测），别被"36 个省级源"这个数字误导
======================================================================

各省税务局**不共用同一套系统**，所以不存在"补齐域名就能批量抓取"这回事：

- **国家税务总局政策法规库**（fgk.chinatax.gov.cn）：JSON 接口，**已接入并验证**
- **广东**：静态列表页（`common_list.shtml` 模式），最新条目当天，**可解析**
- **上海**：JS 异步加载，仅有老式 WAS 搜索接口（`/was5/web/search`、`.pfv`），
  **需单独适配**
- 其余省级/计划单列市：**尚未探测**

因此把源分成三档，写清楚比假装全覆盖有用：

===========  ==========================================================
状态          含义
===========  ==========================================================
verified     已实测可抓，采集器配好，纳入日常调度
candidate    站点可达、结构已初步探明，但采集器待写
unverified   仅从官方页面友链抄录，尚未访问验证
===========  ==========================================================

**待适配不是失败，是待办清单。** 未验证的源不会被静默跳过 ——
`taxassist status` 与 Web 首页会显示当前实际在抓的源有哪些。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["verified", "candidate", "unverified"]

# 全国性核心源（已接入）
FGK_SEARCH_API = "https://www.chinatax.gov.cn/search5/search/s"
FGK_SITE = "https://fgk.chinatax.gov.cn"


@dataclass(frozen=True)
class ProvincialSource:
    region: str
    url: str
    status: Status
    notes: str = ""
    column_hint: str = ""


# 站点清单来源：国家税务总局政策法规库首页的省级税局友链（官方清单）。
# 注意：本次仅抄录到 22 个；计划单列市（大连/宁波/厦门/青岛/深圳）与其余省份
# 未在该页完整出现，需另行核对后补充 —— 宁缺勿编。
PROVINCIAL_SOURCES: tuple[ProvincialSource, ...] = (
    ProvincialSource("北京", "http://beijing.chinatax.gov.cn/", "unverified"),
    ProvincialSource("天津", "http://tianjin.chinatax.gov.cn/", "unverified"),
    ProvincialSource("河北", "http://hebei.chinatax.gov.cn/", "unverified"),
    ProvincialSource("山西", "http://shanxi.chinatax.gov.cn/", "unverified"),
    ProvincialSource("内蒙古", "http://neimenggu.chinatax.gov.cn/", "unverified"),
    ProvincialSource("辽宁", "http://liaoning.chinatax.gov.cn/", "unverified"),
    ProvincialSource("吉林", "http://jilin.chinatax.gov.cn/", "unverified"),
    ProvincialSource("黑龙江", "http://heilongjiang.chinatax.gov.cn/", "unverified"),
    ProvincialSource(
        "上海", "http://shanghai.chinatax.gov.cn/", "candidate",
        notes="首页 JS 异步加载，无静态条目；见 /was5/web/search 与 .pfv 接口，需单独适配",
    ),
    ProvincialSource("江苏", "http://jiangsu.chinatax.gov.cn/", "unverified"),
    ProvincialSource("浙江", "http://zhejiang.chinatax.gov.cn/", "unverified"),
    ProvincialSource("安徽", "http://anhui.chinatax.gov.cn/", "unverified"),
    ProvincialSource("福建", "http://fujian.chinatax.gov.cn/", "unverified"),
    ProvincialSource("江西", "http://jiangxi.chinatax.gov.cn/", "unverified"),
    ProvincialSource("山东", "http://shandong.chinatax.gov.cn/", "unverified"),
    ProvincialSource("河南", "https://henan.chinatax.gov.cn/", "unverified"),
    ProvincialSource("湖北", "http://hubei.chinatax.gov.cn/", "unverified"),
    ProvincialSource("湖南", "http://hunan.chinatax.gov.cn/", "unverified"),
    ProvincialSource(
        "广东", "http://guangdong.chinatax.gov.cn/", "verified",
        notes="已接入：政策文件栏目为静态列表页 /gdsw/zcwj/zcwj.shtml，"
              "采集器见 province.py（仅第一页，正文抓取待适配）",
        column_hint="/gdsw/zcwj/zcwj.shtml",
    ),
    ProvincialSource("广西", "https://guangxi.chinatax.gov.cn/", "unverified"),
    ProvincialSource("海南", "http://hainan.chinatax.gov.cn/", "unverified"),
    ProvincialSource("重庆", "http://chongqing.chinatax.gov.cn/", "unverified"),
)







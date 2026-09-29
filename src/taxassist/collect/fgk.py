"""国家税务总局政策法规库（fgk.chinatax.gov.cn）采集器。

======================================================================
实测结论（2026-09，改动前务必先重跑 scripts/probe_source.py 复核）
======================================================================

入口： GET https://www.chinatax.gov.cn/search5/search/s
       （fgk.chinatax.gov.cn 上的列表页是 JS 壳，**不可直接解析**：
        listflfg.html 与 zcwj.html 返回字节数几乎相同且不含任何条目）

必要参数：
    siteCode=bm29000002   indexCode=1
    column=政策法规|政策解读|政策指引
    startTime / endTime   → 唯一生效的日期过滤（服务端按成文日期过滤）
    pageNum=0..N          orderBy=5 为日期倒序（orderBy=3 为日期正序）

已验证的坑：
  1. ``pageSize`` 传大于 10 无效 —— 服务端固定 10 条/页。
  2. ``cwrqStart`` / ``cwrqEnd`` **无效**（只存在于前端 JS，服务端不认）。
     曾用三个不同窗口测试，返回的 total 与条目完全相同 —— 若误用它做增量，
     系统会每天认真抓同一批旧数据且看起来完全正常。
  3. ``xxgk_effectLevel`` 实际是"文件类型"（税务规范性文件/财税文件/其他文件），
     不是效力等级；真正的时效标注是 ``xxgk_aging``，但**填充率低**（实测 3/10），
     所以不能假设它总有值 —— 缺失时必须进待人工确认队列。
  4. ``govDoc.docNo`` 只是序号（如 "19"），不是完整文号，必须自行重建。
  5. 完整性可校验：返回的 ``searchResultAll.total`` 是窗口内真实条数，
     抓到条数与之不等即为抓取不完整，必须告警而非静默通过。
  6. 条目字段路径为 ``searchResultAll.searchTotal``（名字有误导性，它才是结果数组）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterator

from .. import db as dbmod
from ..config import FGK_PAGE_SIZE
from .http import GuardedClient

log = logging.getLogger(__name__)

FGK_SEARCH_API = "https://www.chinatax.gov.cn/search5/search/s"
FGK_SITE_CODE = "bm29000002"
FGK_INDEX_CODE = "1"
FGK_REFERER = "https://fgk.chinatax.gov.cn/"

# 栏目 → 源 id（与 sources.py 保持一致）
COLUMNS = {
    "政策法规": "fgk_zcfg",
    "政策解读": "fgk_zcjd",
    "政策指引": "fgk_zczy",
    "行政法规": "fgk_xzfg",
    "国务院文件": "fgk_gwywj",
    "财税文件": "fgk_cswj",
    "税务规范性文件": "fgk_swgfxwj",
    "其他文件": "fgk_qtwj",
}

RESULT_PATH = ("searchResultAll", "searchTotal")
TOTAL_PATH = ("searchResultAll", "total")


class FgkResponseError(RuntimeError):
    """接口返回结构异常（例如返回了空壳页或结构变更）。"""


@dataclass
class SearchPage:
    column: str
    page_num: int
    total: int                 # 服务端声称的窗口内真实条数
    items: list[dict]
    raw: dict


def _dig(d: dict, path: tuple[str, ...]):
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


class FgkClient:
    """法规库检索接口客户端。"""

    def __init__(self, client: GuardedClient | None = None) -> None:
        self._own_client = client is None
        self.client = client or GuardedClient()

    # ------------------------------------------------------------ 单页

    def search_page(self, column: str, start: str, end: str, page_num: int) -> SearchPage:
        """抓取一页。start/end 形如 ``2026-09-01 00:00:00``。"""
        params = {
            "siteCode": FGK_SITE_CODE,
            "indexCode": FGK_INDEX_CODE,
            "column": column,
            "searchWord": "",
            "type": "",
            "likeDoc": "0",
            "wordPlace": "0",
            "searchSiteName": "GSFFK",
            "orderBy": "5",              # 日期倒序
            "pageSize": str(FGK_PAGE_SIZE),
            "pageNum": str(page_num),
            "startTime": start,
            "endTime": end,
        }
        headers = {"Referer": FGK_REFERER, "X-Requested-With": "XMLHttpRequest"}
        data = self.client.get_json(FGK_SEARCH_API, params=params, headers=headers)

        items = _dig(data, RESULT_PATH)
        total = _dig(data, TOTAL_PATH)
        if items is None or total is None:
            raise FgkResponseError(
                "接口返回结构与预期不符（searchResultAll.searchTotal/total 缺失）。"
                "官方站点可能已改版——请重跑 scripts/probe_source.py 并更新本模块的实测结论。"
                f" 实际顶层键: {sorted(data.keys())[:12]}"
            )
        return SearchPage(column=column, page_num=page_num, total=int(total), items=list(items), raw=data)

    # ------------------------------------------------------------ 翻页

    def iter_window(
        self,
        column: str,
        start: str,
        end: str,
        *,
        max_pages: int | None = None,
        max_pages_hard_limit: int = 3000,
    ) -> Iterator[SearchPage]:
        """按页遍历一个日期窗口。

        停止条件：抓满 total 条，或达到 max_pages / 硬上限。
        返回的每一页都由调用方累计，以便核对 fetched == total。
        """
        page_num = 0
        seen = 0
        while True:
            page = self.search_page(column, start, end, page_num)
            if page.page_num == 0:
                log.info("[%s] 窗口 %s ~ %s 声称 %d 条", column, start[:10], end[:10], page.total)
            if not page.items:
                # 空页但 total 没抓满 → 异常，必须暴露
                if seen < page.total:
                    log.warning("[%s] 第 %d 页返回空，但仅累计到 %d/%d 条，提前终止",
                                column, page_num, seen, page.total)
                return
            yield page
            seen += len(page.items)
            page_num += 1
            if seen >= page.total:
                return
            if max_pages is not None and page_num >= max_pages:
                log.warning("[%s] 达到 max_pages=%d（已抓 %d/%d 条）", column, max_pages, seen, page.total)
                return
            if page_num >= max_pages_hard_limit:
                log.error("[%s] 达到硬上限 %d 页，强制停止", column, max_pages_hard_limit)
                return

    def close(self) -> None:
        if self._own_client:
            self.client.close()

    def __enter__(self) -> "FgkClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------- 窗口工具

def incremental_window(days: int = 7, today: date | None = None) -> tuple[str, str]:
    """增量窗口：默认回溯 7 天（重叠抓取，靠 doc_uid 去重防漏）。"""
    end = today or date.today()
    start = end - timedelta(days=days)
    return f"{start.isoformat()} 00:00:00", f"{end.isoformat()} 23:59:59"


def year_windows(year_from: int = 1984, year_to: int | None = None) -> list[tuple[str, str]]:
    """按年切窗口，用于首次全量导入（避免单窗口过大导致翻页过多）。"""
    end_year = year_to or date.today().year
    out = []
    for y in range(year_from, end_year + 1):
        out.append((f"{y}-01-01 00:00:00", f"{y}-12-31 23:59:59"))
    return out

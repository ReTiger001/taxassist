"""全国公共资源交易平台（ggzy.gov.cn）采集器 —— 覆盖全国省级政府采购/工程。

## 为什么重要

它是**全国各省公共资源交易中心的聚合入口**，一条 API 能覆盖 31 个省的政府采购
与工程建设公告。之前的项目只做了 CCGP（中央政府采购网），地方项目一直是缺口。

## 关键：正确的参数名（2026-10-23 从 BidRadar 项目学到的）

```
POST https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList
form:
  SOURCE_TYPE     = 1
  DEAL_CLASSIFY   = 01(工程建设) / 02(政府采购)
  DEAL_STAGE      = 0101 / 0201（对应上面的交易公告阶段）
  DEAL_PROVINCE   = 省份编码 + '0000'（如 110000=北京）
  PAGENUMBER      = 页码
Headers: Referer=https://www.ggzy.gov.cn/deal/dealList.html, X-Pass-Token=''
```

**我之前自己逆向时参数名全错**（用了 FINDTXT/PAGESIZE 之类），一直返回 0 条。
这个参数集来自开源项目 BidRadar（`collectors/national.py`），实测返回
`{"code":200,"data":{"records":[...],"total":1000,"pages":50}}`。

## 验证码

接口对高频请求会返回 `code: 829`（要求图形验证码）。**不绕过** —— 遇到就停，
等一会儿再试。BidRadar 也是这么处理的（`ACCESS_RESTRICTED`）。

## 抓取策略

接口**没有关键词搜索参数**（只能按地区+分类+分页翻），所以是
「按日期倒序翻前 N 页 → 本地过滤税务关键词」。每省每分类抓前几页就够覆盖
最近几天的公告。
"""
from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

BASE = "https://www.ggzy.gov.cn"
API = BASE + "/information/pubTradingInfo/getTradList"

#: 省级行政区划编码（前两位 + 0000）。
PROVINCES: dict[str, str] = {
    "110000": "北京", "120000": "天津", "130000": "河北", "140000": "山西",
    "150000": "内蒙古", "210000": "辽宁", "220000": "吉林", "230000": "黑龙江",
    "310000": "上海", "320000": "江苏", "330000": "浙江", "340000": "安徽",
    "350000": "福建", "360000": "江西", "370000": "山东", "410000": "河南",
    "420000": "湖北", "430000": "湖南", "440000": "广东", "450000": "广西",
    "460000": "海南", "500000": "重庆", "510000": "四川", "520000": "贵州",
    "530000": "云南", "540000": "西藏", "610000": "陕西", "620000": "甘肃",
    "630000": "青海", "640000": "宁夏", "650000": "新疆",
}

#: 分类 → (DEAL_CLASSIFY, DEAL_STAGE)
CLASSIFIES: dict[str, tuple[str, str]] = {
    "政府采购": ("02", "0201"),
    "工程建设": ("01", "0101"),
}

_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Referer": BASE + "/deal/dealList.html",
    "Origin": BASE,
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Pass-Token": "",
    "Accept": "application/json, text/plain, */*",
}

PHONE_RE = re.compile(r"\b(?:(?:0[1-9]\d{1,2}[-\s]?\d{7,8})|(?:1[3-9]\d{9}))\b")
PERSON_RE = re.compile(r"(?:联系人|项目联系人)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})")


def fetch_page(province_code: str, classify: str, page: int = 1,
               timeout: int = 25) -> dict:
    """抓一页。返回 ``{"code": n, "records": [...], "total": n, "pages": n}``。

    ``code == 829`` 表示要验证码 —— 调用方应停止并稍后重试（**不绕过**）。
    """
    c_classify, c_stage = CLASSIFIES[classify]
    form = {
        "SOURCE_TYPE": "1",
        "DEAL_CLASSIFY": c_classify,
        "DEAL_STAGE": c_stage,
        "DEAL_PROVINCE": province_code,
        "PAGENUMBER": str(page),
    }
    try:
        r = httpx.post(API, data=form, headers=_HDR, timeout=timeout)
        j = r.json()
        code = j.get("code")
        data = j.get("data") or {}
        return {
            "code": code,
            "records": data.get("records") or [],
            "total": int(data.get("total") or 0),
            "pages": int(data.get("pages") or 0),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("全国平台失败 %s/%s p%d：%s: %s",
                    province_code, classify, page, type(e).__name__, e)
        return {"code": -1, "records": [], "total": 0, "pages": 0}


def fetch_detail(rel_url: str, timeout: int = 25) -> dict:
    """抓详情页正文 + 联系方式。``rel_url`` 是 API 返回的相对路径。"""
    if not rel_url:
        return {}
    url = rel_url if rel_url.startswith("http") else BASE + rel_url
    try:
        r = httpx.get(url, headers={"User-Agent": _HDR["User-Agent"]},
                      timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return {}
        s = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S | re.I)
        s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
        s = re.sub(r"</(?:p|div|tr|li|h\d)>", "\n", s, flags=re.I)
        s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
        s = re.sub(r"<[^>]+>", "", s)
        s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
              .replace("&lt;", "<").replace("&gt;", ">"))
        s = re.sub(r"[ \t\u3000]+", " ", s)
        s = re.sub(r"\n\s*\n+", "\n", s).strip()
        person = PERSON_RE.search(s)
        phones = [p for p in PHONE_RE.findall(s) if not p.replace("-", "").startswith("400")]
        return {
            "source_html": s,
            "contact_person": person.group(1) if person else None,
            "contact_phone": phones[0] if phones else None,
            "source_url": url,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("详情失败 %s：%s: %s", url, type(e).__name__, e)
        return {}


def collect(conn, *, provinces: list[str] | None = None,
            classifies: list[str] | None = None,
            max_pages: int = 5,
            tax_keywords: list[str] | None = None,
            sleep_between: float = 0.35) -> dict:
    """采集主流程：逐省逐分类翻页 → 本地过滤税务 → 抓详情 → 入库。

    ``max_pages`` 每省每分类翻几页（默认 5 页 = 100 条，覆盖最近几天）。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    provs = provinces or list(PROVINCES)
    cats = classifies or list(CLASSIFIES)
    kws = tax_keywords or ["税务", "涉税", "纳税", "税收", "汇算清缴", "转让定价",
                           "土地增值税", "增值税", "企业所得税", "个人所得税"]
    stats = {"requests": 0, "records": 0, "matched": 0, "new": 0,
             "updated": 0, "details": 0, "restricted": 0}

    for prov in provs:
        for cat in cats:
            for page in range(1, max_pages + 1):
                data = fetch_page(prov, cat, page)
                stats["requests"] += 1
                if data["code"] == 829:
                    stats["restricted"] += 1
                    log.warning("全国平台要求验证码（%s/%s），本轮停止该分片",
                                prov, cat)
                    break
                recs = data["records"]
                if not recs:
                    break
                stats["records"] += len(recs)
                for it in recs:
                    title = str(it.get("title") or "")
                    if not title or not any(k in title for k in kws):
                        continue
                    stats["matched"] += 1
                    rid = str(it.get("id") or "")
                    if not rid:
                        continue
                    doc_uid = f"ggzy:{rid}"
                    row = {
                        "doc_uid": doc_uid,
                        "source": "ggzy",
                        "notice_type": "gkzb",
                        "bid_process": "招标",
                        "title": title,
                        "project_no": rid,
                        "agency": it.get("transactionSourcesPlatformText") or cat,
                        "region": it.get("provinceText") or PROVINCES.get(prov),
                        "publish_at": str(it.get("publishTime") or "")[:10],
                        "deadline": str(it.get("deadline") or "")[:10] or None,
                        "url": it.get("url"),
                        "source_url": (BASE + str(it.get("url"))) if it.get("url") else None,
                        "is_tax": 1,
                        "tax_hits": [k for k in kws if k in title][:3],
                    }
                    det = fetch_detail(str(it.get("url") or ""))
                    if det:
                        for k, v in det.items():
                            if v:
                                row[k] = v
                        stats["details"] += 1
                    self_exists = conn.execute(
                        "SELECT 1 FROM tender WHERE doc_uid = ?", (doc_uid,)).fetchone()
                    if not self_exists:
                        dup = tmod.find_duplicate(conn, title, row.get("purchaser"))
                        if dup:
                            row["doc_uid"] = dup
                    _, is_new = tmod.upsert(conn, row)
                    stats["new" if is_new else "updated"] += 1
                    time.sleep(0.3)
                conn.commit()
                time.sleep(sleep_between)
    log.info("全国平台完成：%d 次请求 / 扫描 %d 条 / 匹配 %d",
             stats["requests"], stats["records"], stats["matched"])
    return stats

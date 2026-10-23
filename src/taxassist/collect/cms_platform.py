"""通用 CMS 平台采集器 —— 一套代码覆盖多个用同款 CMS 的招标平台。

## 为什么这个模块价值最大

2026-10-23 实测发现：**湖北联投电子采购平台**和**中国邮政储蓄银行邮银易采平台**
用的是同一个 CMS（`/cms/api/dynamicData/queryContentPage` + `/cms/default/webfile/`
详情页）。这意味着「适配一次 = 覆盖一批平台」，不用为每个平台从头逆向。

已确认可用的平台配置见 ``PLATFORMS``。后续每发现一个同款 CMS 平台，加一行配置即可。

## 两条链路（都实测通过）

- **列表**：``POST {base}/cms/api/dynamicData/queryContentPage``
  body: ``{"pageSize":"10","dto":{"siteId":"725","categoryId":"221","city":"",...}}``
  返回字段很全：标题、详情路径、发布/报名起止、标书售卖时间、代理机构、地区。
- **详情**：``GET {base}/cms/default/webfile{url}``
  （``url`` 是列表返回的相对路径，如 ``/1ywgg1/20261001/1290...html``）

## 关键词搜索参数没生效

API 的 ``title`` 参数实测无效（传"税务"返回 0）。所以策略是**全量翻页 + 本地
关键词过滤** —— 每个栏目几百条，翻十几页就够，成本可接受。
"""
from __future__ import annotations

import logging
import re
import time

import httpx

log = logging.getLogger(__name__)

#: 已确认可用的平台（同款 CMS）。
#:
#: ``categories``：categoryId → 栏目名。只配**可投标**的栏目；
#: 中标/候选人/合同类不收（用户明确要「还能投的标」）。
PLATFORMS: dict[str, dict] = {
    "ltzxgl": {
        "name": "湖北联投电子采购平台",
        "base": "https://www.ltzxgl.com.cn",
        "site_id": "725",
        # 221=招标公告（256 条）；225=采购公告（14983 条，量大）；
        # 227=变更公告（1201 条，可能含新项目）。
        # 222/223/226 是中标/候选人/结果类，**不收**（用户只要还能投的）。
        "categories": {"221": "招标公告", "225": "采购公告", "227": "变更公告"},
    },
    "psbc": {
        "name": "中国邮政储蓄银行邮银易采平台",
        "base": "https://cg.psbc.com",
        "site_id": "725",
        # 223=公开招标（10022 条）；226=其他采购（2263 条）
        "categories": {"223": "招标公告", "226": "采购公告"},
    },
}

_HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Content-Type": "application/json; charset=utf-8",
}

#: 只收「还能投」的公告：标题出现这些词说明已定标/已结束。
_FINISHED_WORDS = ("中标候选人", "中标结果", "成交结果", "结果公示", "合同公告",
                   "废标", "流标", "终止公告")


def _strip(html: str) -> str:
    s = re.sub(r"<script.*?</script>", " ", html or "", flags=re.S | re.I)
    s = re.sub(r"<style.*?</style>", " ", s, flags=re.S | re.I)
    s = re.sub(r"</(?:p|div|tr|li|h\d)>", "\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</t[dh]>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    s = re.sub(r"[ \t\u3000]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


_PHONE_RE = re.compile(
    r"(?:(?:0\d{2,3}[-\s]?\d{7,8}(?:[-\s]?\d{1,5})?)|(?:1[3-9]\d{9})|(?:400[-\s]?\d{3}[-\s]?\d{4}))")
_PERSON_RE = re.compile(r"(?:联系人|项目联系人|采购联系人)\s*[:：]\s*([\u4e00-\u9fa5]{2,4})")

#: **平台客服电话**：这些号码出现在每一条公告的页脚里，不是项目联系人。
#: 2026-10-23 实测：湖北联投每条抓到的都是 4001276998（平台客服），必须排除。
_PLATFORM_SERVICE_PHONES = ("4001276998", "400-127-6998")


def _pick_contact(text: str) -> tuple[str | None, str | None]:
    """从正文里挑**项目**联系人/电话。

    优先级：
    1. 「联系人：张三」旁边的电话（真正的项目联系人）
    2. 任意非平台客服的号码
    客服号（400 开头且等于平台已知号码）一律排除。
    """
    persons = list(dict.fromkeys(_PERSON_RE.findall(text or "")))

    # 在「联系人」出现位置之后 120 字内找电话（公告里联系人+电话通常相邻）
    cand_phones: list[str] = []
    for m in _PERSON_RE.finditer(text or ""):
        seg = (text or "")[m.start():m.start() + 160]
        cand_phones += _PHONE_RE.findall(seg)
    if not cand_phones:
        cand_phones = _PHONE_RE.findall(text or "")

    def ok(p: str) -> bool:
        digits = re.sub(r"[^\d]", "", p)
        # 400 号段是平台客服，招标公告的项目联系人几乎不用 400 号
        return not digits.startswith("400")

    phones = [p for p in dict.fromkeys(cand_phones) if ok(p)]
    return (persons[0] if persons else None), (phones[0] if phones else None)


def fetch_list(platform_key: str, category_id: str, page: int = 1,
               page_size: int = 20, timeout: int = 25) -> dict:
    """调列表接口。返回 ``{"total": n, "rows": [...]}``。"""
    cfg = PLATFORMS[platform_key]
    body = {
        "pageSize": str(page_size),
        "pageNo": str(page),
        "dto": {
            "siteId": cfg["site_id"], "categoryId": category_id,
            "city": "", "county": "", "purchaseMode": "",
        },
    }
    try:
        r = httpx.post(f"{cfg['base']}/cms/api/dynamicData/queryContentPage",
                       headers=_HDR, json=body, timeout=timeout)
        j = r.json()
        res = j.get("res") or {}
        return {"total": int(res.get("total") or 0),
                "rows": res.get("rows") or []}
    except Exception as e:  # noqa: BLE001
        log.warning("列表失败 %s/%s p%d：%s: %s",
                    platform_key, category_id, page, type(e).__name__, e)
        return {"total": 0, "rows": []}


def fetch_detail(platform_key: str, rel_url: str, timeout: int = 25) -> dict:
    """抓详情页正文 + 联系方式。

    ``rel_url`` 是列表返回的相对路径。详情页的真实地址是
    ``{base}/cms/default/webfile{rel_url}`` —— 少这个前缀会 404。
    """
    cfg = PLATFORMS[platform_key]
    if not rel_url:
        return {}
    url = cfg["base"] + "/cms/default/webfile" + (
        rel_url if rel_url.startswith("/") else "/" + rel_url)
    try:
        r = httpx.get(url, headers={"User-Agent": _HDR["User-Agent"]},
                      timeout=timeout, follow_redirects=True)
        if r.status_code != 200:
            return {}
        text = _strip(r.text)
        person, phone = _pick_contact(text)
        return {
            "source_html": text,
            "contact_person": person,
            "contact_phone": phone,
            "detail_url": url,
        }
    except Exception as e:  # noqa: BLE001
        log.debug("详情失败 %s：%s: %s", url, type(e).__name__, e)
        return {}


def collect(conn, *, platforms: list[str] | None = None,
            tax_keywords: list[str] | None = None,
            max_pages: int = 40, sleep_between: float = 0.3) -> dict:
    """采集主流程：逐平台逐栏目翻页 → 本地过滤税务 → 抓详情 → 入库。

    ``tax_keywords`` 只用于**筛标题**，避免把几千条非税务标全抓进详情。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    keys = platforms or list(PLATFORMS)
    kws = tax_keywords or ["税务", "涉税", "纳税", "税收", "汇算清缴", "转让定价",
                           "土地增值税", "增值税", "企业所得税", "个人所得税"]
    stats = {"platforms": 0, "pages": 0, "listed": 0, "matched": 0,
             "new": 0, "updated": 0, "details": 0, "failed": 0}

    for pk in keys:
        cfg = PLATFORMS.get(pk)
        if not cfg:
            continue
        stats["platforms"] += 1
        for cat_id, cat_name in cfg["categories"].items():
            for page in range(1, max_pages + 1):
                data = fetch_list(pk, cat_id, page=page, page_size=20)
                rows = data["rows"]
                stats["pages"] += 1
                if not rows:
                    break
                stats["listed"] += len(rows)
                for it in rows:
                    title = str(it.get("title") or "")
                    if not title:
                        continue
                    # 只收"还能投"的公告
                    if any(w in title for w in _FINISHED_WORDS):
                        continue
                    if not any(k in title for k in kws):
                        continue
                    stats["matched"] += 1
                    rel = str(it.get("url") or "")
                    content_id = it.get("contentId") or rel.rsplit("/", 1)[-1].split(".")[0]
                    if not content_id:
                        continue
                    doc_uid = f"cms:{pk}:{content_id}"
                    row = {
                        "doc_uid": doc_uid,
                        "source": f"cms_{pk}",
                        "notice_type": "gkzb",
                        "bid_process": "招标",
                        "title": title,
                        "project_no": str(content_id),
                        "purchaser": it.get("agentCompanyName"),
                        "agency": cfg["name"],
                        "region": it.get("provinceName"),
                        "publish_at": str(it.get("publishDate") or "")[:10],
                        "deadline": str(it.get("quoteEndTime") or "")[:10],
                        "signup_time": str(it.get("quoteBeginTime") or "")[:10],
                        "tender_time": str(it.get("quoteEndTime") or "")[:10],
                        "is_tax": 1,
                        "tax_hits": [k for k in kws if k in title][:3],
                    }
                    # 抓详情（正文 + 联系方式）
                    det = fetch_detail(pk, rel)
                    if det:
                        row["source_html"] = det.get("source_html")
                        row["contact_person"] = det.get("contact_person")
                        row["contact_phone"] = det.get("contact_phone")
                        row["source_url"] = det.get("detail_url")
                        stats["details"] += 1
                    self_exists = conn.execute(
                        "SELECT 1 FROM tender WHERE doc_uid = ?", (doc_uid,)).fetchone()
                    if not self_exists:
                        dup = tmod.find_duplicate(conn, title, row.get("purchaser"))
                        if dup:
                            row["doc_uid"] = dup
                            # 审计 B4：重定向后 row["source"] 仍是"来方"，会覆盖原记录的
                            # 源归属（10 条 zlbx 因此被标成 cebpub，导致按源补详情漏抓）。
                            # 正确做法是**改回原记录的 source** —— 注意不能直接删掉：
                            # upsert 走 INSERT 时 source 为 NULL 会触发 NOT NULL 约束。
                            orig = conn.execute(
                                "SELECT source FROM tender WHERE doc_uid = ?",
                                (dup,)).fetchone()
                            if orig:
                                row["source"] = orig["source"]
                    _, is_new = tmod.upsert(conn, row)
                    stats["new" if is_new else "updated"] += 1
                    time.sleep(0.3)
                conn.commit()
                log.info("%s/%s 第 %d 页：%d 条，累计匹配 %d",
                         pk, cat_name, page, len(rows), stats["matched"])
                if len(rows) < 20:
                    break
                time.sleep(sleep_between)
    return stats

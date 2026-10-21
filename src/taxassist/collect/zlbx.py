"""知了标讯（zhiliaobiaoxun）API 采集器 —— 税务服务标讯的真实来源。

## 为什么换到这个源

CCGP（中国政府采购网）实测 400 条标题里 **0 条税务服务标** —— 它是政府采购网，
主体是设备/工程。税务服务采购的买方是企业（国企/上市公司），标发在企业招投标
平台。实测知了标讯用「税务服务/税务咨询/汇算清缴/涉税服务」搜到 **32764 条**，
返回的全是真标（雅砻江水电税务咨询、新疆农牧投涉税服务选聘、惠州天然气汇算
清缴鉴证…）。

## API

- 基础：``https://mcp-server.zhiliaobiaoxun.com/api_v2/{工具名}``
- 认证：``X-API-Key: zlbx_...``
- 注册：``POST https://ai.zhiliaobiaoxun.com/web-api/internal/auto-register``
  自动注册新设备送 **100 次免费调用**（绑定手机号再送 100）
- 本模块只调 ``search_bids``（列表级，字段够用）；``get_bid_detail`` 要额外消耗
  额度，正文按需再抓

## 额度意识

100 次免费额度不多。``search_bids`` 一次最多返 50 条，所以：
- 每天增量只抓最近 1-2 天 → 1-2 次调用/天，够用一两个月
- 不要盲目翻几十页（每次翻页都是一次调用）
"""
from __future__ import annotations

import hashlib
import logging
import os
import socket
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

import httpx

from .. import proc as procutil

log = logging.getLogger(__name__)

API_BASE = "https://mcp-server.zhiliaobiaoxun.com/api_v2"
REGISTER_URL = "https://ai.zhiliaobiaoxun.com/web-api/internal/auto-register"
KEY_FILE = Path("data/zlbx_key.txt")

#: 用来搜索"税务服务标"的关键词。分两组：主词定领域，交付形态词定"买服务"。
TAX_QUERY_KEYWORDS: list[str] = [
    "税务服务", "税务咨询", "涉税服务", "汇算清缴",
    "税务代理", "涉税鉴证", "税务筹划", "转让定价", "税务尽调", "税务顾问",
]


def load_key() -> str | None:
    """读取 API Key：环境变量优先，其次本地文件。"""
    k = (os.environ.get("ZLBX_API_KEY") or "").strip()
    if k:
        return k
    try:
        if KEY_FILE.exists():
            k = KEY_FILE.read_text(encoding="utf-8").strip()
            return k or None
    except Exception as e:  # noqa: BLE001
        log.warning("读取 zlbx key 失败：%s", e)
    return None


def save_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key.strip(), encoding="utf-8")
    log.info("已保存知了标讯 API Key 到 %s", KEY_FILE)


def _device_features() -> dict:
    """采集设备特征（自动注册用）。任何一项失败都回退空串，不阻断注册。"""
    mac_hash = ""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "$n=(Get-NetAdapter | Where-Object {$_.Status -eq 'Up' -and "
             "$_.HardwareInterface} | Sort-Object ifIndex | Select-Object -First 1);"
             " if($n){$n.MacAddress}"],
            capture_output=True, text=True, timeout=30,
            creationflags=procutil.hidden_flags()).stdout.strip()
        if out:
            mac_hash = hashlib.sha256(
                out.replace("-", "").replace(":", "").lower().encode()).hexdigest()
    except Exception as e:  # noqa: BLE001
        log.debug("mac 采集失败：%s", e)
    return {
        "hostname": socket.gethostname(),
        "platform": "win32" if os.name == "nt" else "linux",
        "arch": os.environ.get("PROCESSOR_ARCHITECTURE", ""),
        "username": os.environ.get("USERNAME") or os.environ.get("USER", ""),
        "home_path": os.environ.get("USERPROFILE") or os.environ.get("HOME", ""),
        "mac_hash": mac_hash,
    }


def auto_register() -> str | None:
    """自动注册设备账号，返回 api_key（新设备送 100 次免费调用）。"""
    try:
        r = httpx.post(REGISTER_URL, json={
            "device_features": _device_features(),
            "agent_kind": "claude-code", "agent_version": "1.0.0",
            "skill_version": "tender-search/1.0",
        }, timeout=30)
        j = r.json()
        key = j.get("api_key")
        if key:
            save_key(key)
            log.info("知了标讯注册成功，剩余免费调用 %s 次", j.get("remaining_calls"))
            return key
        log.warning("注册返回无 api_key：%s", j)
    except Exception as e:  # noqa: BLE001
        log.warning("知了标讯自动注册失败：%s: %s", type(e).__name__, e)
    return None


def ensure_key() -> str | None:
    """拿一个可用 key：已有则用，没有则注册。"""
    return load_key() or auto_register()


def search_bids(key: str, keywords: list[str], *, days: int = 7,
                page: int = 1, page_size: int = 50,
                bid_type: str = "招标") -> dict:
    """调 ``search_bids``。返回原始响应 dict（含 data.items / total）。"""
    begin = (date.today() - timedelta(days=days)).isoformat()
    payload = {
        "keywords": keywords,
        "match_modes": ["title", "sm"],
        "bid_type": bid_type,
        "begin_date": begin,
        "page": page,
        "page_size": min(max(page_size, 1), 50),
    }
    r = httpx.post(f"{API_BASE}/search_bids",
                   headers={"X-API-Key": key, "Content-Type": "application/json"},
                   json=payload, timeout=45)
    r.raise_for_status()
    return r.json()


def _rows_from_items(items: list[dict]) -> list[dict]:
    """把 API 条目映射成 tender 表的行。"""
    out: list[dict] = []
    for it in items:
        bid_id = it.get("bid_id") or it.get("id")
        if not bid_id:
            continue
        money_wan = it.get("money_wan")
        out.append({
            "doc_uid": f"zlbx:{bid_id}",
            "source": "zlbx",
            "notice_type": "gkzb" if (it.get("bid_type") == "招标") else "zbgg",
            "title": it.get("title") or "",
            "project_no": str(bid_id),
            "purchaser": it.get("caller_name"),
            "region": it.get("province"),
            "publish_at": it.get("pub_time"),
            "url": it.get("url"),
            "budget": (float(money_wan) * 10000) if money_wan else None,
            # 搜索词本身就是税务领域 → 一律标税务相关
            "is_tax": 1,
            "tax_hits": [k for k in ("税务", "涉税", "汇算清缴", "转让定价")
                         if k in (it.get("title") or "")] or ["税务服务"],
            "summary": "、".join(it.get("sm_names") or []),
        })
    return out


def collect(conn, *, days: int = 7, max_pages: int = 2,
            page_size: int = 50) -> dict:
    """采集主流程：搜索税务服务标 → 写入 tender 表。

    ``max_pages`` 每次翻页都消耗一次免费额度，默认只翻 2 页（100 条）。
    """
    from .. import tender as tmod

    tmod.ensure_table(conn)
    key = ensure_key()
    if not key:
        raise RuntimeError("没有可用的知了标讯 API Key（自动注册也失败了）")

    stats = {"pages": 0, "fetched": 0, "new": 0, "updated": 0,
             "total_matched": 0, "failed": 0}
    for page in range(1, max_pages + 1):
        try:
            resp = search_bids(key, TAX_QUERY_KEYWORDS, days=days, page=page,
                               page_size=page_size)
        except Exception as e:  # noqa: BLE001
            log.warning("知了标讯第 %d 页失败：%s: %s", page, type(e).__name__, e)
            stats["failed"] += 1
            break
        data = resp.get("data") or {}
        items = data.get("items") or []
        if page == 1:
            stats["total_matched"] = int(data.get("total") or 0)
        if not items:
            break
        stats["pages"] += 1
        for row in _rows_from_items(items):
            stats["fetched"] += 1
            _, is_new = tmod.upsert(conn, row)
            stats["new" if is_new else "updated"] += 1
        conn.commit()
        log.info("知了标讯第 %d 页：%d 条，累计新增 %d",
                 page, len(items), stats["new"])
        time.sleep(1.0)
    return stats

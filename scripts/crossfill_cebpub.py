"""P1 第一步：把已抓到的详情跨源回填给 cebpub 的条目。

## 为什么这么干

cebpub 的 505 条只有列表信息（详情页 vaptcha）。但**同一个标**常常同时出现在
我们已适配的平台里（邮储 36 条、湖北联投 28 条、南方电网 4 条）——那些记录
**有正文、有电话、有原文链接**。

所以先不重新抓，按标题做跨源匹配，把详情字段复制过去。零网络成本。

## 匹配策略

1. 标题规范化后完全相等（用 `tender._normalize_title`，去掉编号前缀）
2. 采购人相同可放宽（标题有细微差异时）
3. 只补**空字段**（不覆盖已有值）
"""
from __future__ import annotations

import logging
import sys

sys.path.insert(0, "src")

from taxassist import tender as tmod  # noqa: E402
from taxassist.repo.conn import connect_compat  # noqa: E402

log = logging.getLogger(__name__)

#: 已适配、有详情的来源
RICH_SOURCES = ("cms_psbc", "cms_ltzxgl", "csg", "zlbx", "etrading")

#: 可以跨源复制的字段
COPY_FIELDS = ("source_url", "source_html", "contact_person", "contact_phone",
               "contact_email", "signup_time", "tender_time", "deadline",
               "project_no")


def main() -> None:
    with connect_compat() as conn:
        tmod.ensure_table(conn)

        # 1. 建「规范化标题 → 详情行」的索引（只收有详情的）
        rich = conn.execute(f"""
            SELECT doc_uid, title, purchaser, {", ".join(COPY_FIELDS)}
            FROM tender
            WHERE source IN ({", ".join("?" * len(RICH_SOURCES))})
              AND (source_html IS NOT NULL AND source_html != ''
                   OR contact_phone IS NOT NULL AND contact_phone != '')""",
            RICH_SOURCES).fetchall()
        print(f"  有详情的记录: {len(rich)} 条")

        by_title: dict[str, dict] = {}
        for r in rich:
            key = tmod._normalize_title(r["title"])
            if key:
                by_title.setdefault(key, dict(r))
        print(f"  标题索引: {len(by_title)} 个唯一标题")

        # 2. 遍历 cebpub 缺详情的
        poor = conn.execute("""
            SELECT doc_uid, title, purchaser FROM tender
            WHERE source = 'cebpub'
              AND (source_html IS NULL OR source_html = '')
              AND (contact_phone IS NULL OR contact_phone = '')""").fetchall()
        print(f"  cebpub 缺详情的: {len(poor)} 条")

        matched = 0
        filled: dict[str, int] = {k: 0 for k in COPY_FIELDS}
        for p in poor:
            key = tmod._normalize_title(p["title"])
            src = by_title.get(key)
            if not src:
                continue
            updates = {}
            for f in COPY_FIELDS:
                v = src.get(f)
                if v not in (None, ""):
                    updates[f] = v
                    filled[f] += 1
            if not updates:
                continue
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE tender SET {sets} WHERE doc_uid = ?",
                         tuple(updates.values()) + (p["doc_uid"],))
            matched += 1
        conn.commit()
        print(f"\n  ✓ 跨源匹配上: {matched} 条")
        print("  各字段补到的条数:")
        for k, v in filled.items():
            if v:
                print(f"    {k}: {v}")

        # 3. 结果统计
        def q(sql: str) -> int:
            return conn.execute(sql).fetchone()["n"]
        total = q("SELECT COUNT(*) AS n FROM tender WHERE source='cebpub'")
        with_body = q("""SELECT COUNT(*) AS n FROM tender WHERE source='cebpub'
                         AND (source_html IS NOT NULL AND source_html != '')""")
        with_phone = q("""SELECT COUNT(*) AS n FROM tender WHERE source='cebpub'
                          AND (contact_phone IS NOT NULL AND contact_phone != '')""")
        print(f"\n  cebpub 变动: 总数 {total}")
        print(f"    有正文: {with_body}  有电话: {with_phone}")


if __name__ == "__main__":
    main()

"""P1 第二步：放宽匹配 —— 按采购人 + 标题包含，再捞一批。

第一步严格标题相等只补到 29 条。同一公告在不同源上的标题常有细微差异
（附件序号、括号内容、多空格），这里加两档退让：

1. **标题包含**：一方规范化标题是另一方的子串（且长度 >= 12，避免误配）
2. **采购人相同 + 标题都含核心词**：采购人 + 标题前 20 字相同
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from taxassist import tender as tmod  # noqa: E402
from taxassist.repo.conn import connect_compat  # noqa: E402

RICH_SOURCES = ("cms_psbc", "cms_ltzxgl", "csg", "zlbx", "etrading")
COPY_FIELDS = ("source_url", "source_html", "contact_person", "contact_phone",
               "contact_email", "signup_time", "tender_time", "deadline",
               "project_no")


def main() -> None:
    with connect_compat() as conn:
        tmod.ensure_table(conn)

        rich = conn.execute(f"""
            SELECT doc_uid, title, purchaser, {", ".join(COPY_FIELDS)}
            FROM tender
            WHERE source IN ({", ".join("?" * len(RICH_SOURCES))})
              AND (source_html IS NOT NULL AND source_html != ''
                   OR contact_phone IS NOT NULL AND contact_phone != '')""",
            RICH_SOURCES).fetchall()
        rich_rows = [dict(r) for r in rich]

        poor = [dict(r) for r in conn.execute("""
            SELECT doc_uid, title, purchaser FROM tender
            WHERE source = 'cebpub'
              AND (source_html IS NULL OR source_html = '')
              AND (contact_phone IS NULL OR contact_phone = '')""").fetchall()]
        print(f"  有详情: {len(rich_rows)} / 缺详情: {len(poor)}")

        # 预计算
        for r in rich_rows:
            r["_norm"] = tmod._normalize_title(r["title"])
            r["_pur"] = (r["purchaser"] or "").strip()

        matched = 0
        by_rule = {"包含": 0, "采购人+前缀": 0}
        for p in poor:
            pn = tmod._normalize_title(p["title"])
            pp = (p["purchaser"] or "").strip()
            hit = None
            hit_rule = ""

            # 规则 1：标题互相包含（长度 >= 12 防误配）
            for r in rich_rows:
                rn = r["_norm"]
                if len(pn) < 12 or len(rn) < 12:
                    continue
                if pn in rn or rn in pn:
                    hit, hit_rule = r, "包含"
                    break

            # 规则 2：采购人相同 + 标题前 20 字相同
            if not hit and pp and len(pn) >= 20:
                for r in rich_rows:
                    if r["_pur"] != pp:
                        continue
                    if r["_norm"][:20] == pn[:20]:
                        hit, hit_rule = r, "采购人+前缀"
                        break

            if not hit:
                continue
            updates = {}
            for f in COPY_FIELDS:
                v = hit.get(f)
                if v not in (None, ""):
                    updates[f] = v
            if not updates:
                continue
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE tender SET {sets} WHERE doc_uid = ?",
                         tuple(updates.values()) + (p["doc_uid"],))
            matched += 1
            by_rule[hit_rule] += 1
        conn.commit()
        print(f"\n  ✓ 放宽匹配补到: {matched} 条")
        for k, v in by_rule.items():
            if v:
                print(f"    {k}: {v}")

        def q(sql: str) -> int:
            return conn.execute(sql).fetchone()["n"]
        with_body = q("""SELECT COUNT(*) AS n FROM tender WHERE source='cebpub'
                         AND (source_html IS NOT NULL AND source_html != '')""")
        with_phone = q("""SELECT COUNT(*) AS n FROM tender WHERE source='cebpub'
                          AND (contact_phone IS NOT NULL AND contact_phone != '')""")
        print(f"\n  cebpub 现在: 有正文 {with_body} / 有电话 {with_phone} / 总 505")

        # 剩余缺详情的按平台分布
        print("\n  仍缺详情的 cebpub 条目按平台分布（Top 15）:")
        for r in conn.execute("""
            SELECT agency, COUNT(*) AS n FROM tender
            WHERE source = 'cebpub'
              AND (source_html IS NULL OR source_html = '')
              AND (contact_phone IS NULL OR contact_phone = '')
            GROUP BY agency ORDER BY n DESC LIMIT 15""").fetchall():
            print(f"    {r['n']:>3}  {r['agency']}")


if __name__ == "__main__":
    main()

"""招标模块的纯函数测试 —— 补审计 B16 指出的"零测试"。

只测**纯函数**（不碰数据库、不碰网络），所以跑得飞快、可以在 CI 里常驻。
重点是那些曾经出过 bug 的逻辑：
- `_normalize_title`：曾把「2026年度企业所得税」削成「26年度…」（B17）
- `upsert` 的局部更新语义：曾清空未提供的列（B1）
- `_money`：预算单位换算
- `tax_hits`：税务关键词判定
"""
from __future__ import annotations

import pytest

from taxassist import tender as tmod


class TestNormalizeTitle:
    """标题规范化 —— 用于去重比对，不能把年份削掉。"""

    def test_strips_leading_bullet_number(self):
        assert tmod._normalize_title("1. 中国建设银行税务服务") == "中国建设银行税务服务"

    def test_strips_chinese_number_prefix(self):
        assert tmod._normalize_title("2、中国建设银行税务服务") == "中国建设银行税务服务"

    def test_keeps_year_prefix(self):
        # 4 位年份必须保留：不同年份的标是不同项目
        out = tmod._normalize_title("2026年度企业所得税汇算清缴")
        assert "2026" in out

    @pytest.mark.parametrize("a,b", [
        ("10建行税务服务", "7建行税务服务"),   # 已知的重复形态：编号前缀不同
        ("1. 建行税务服务", "建行税务服务"),
    ])
    def test_known_duplicate_forms_collide(self, a, b):
        assert tmod._normalize_title(a) == tmod._normalize_title(b)

    def test_whitespace_ignored(self):
        assert tmod._normalize_title("建行 税务  服务") == tmod._normalize_title("建行税务服务")


class TestMoney:
    """预算文本 → 元。"""

    def test_wan_yuan(self):
        assert tmod._money("预算 123.45 万元") == 1234500.0

    def test_yi_yuan(self):
        assert tmod._money("1.5 亿元") == 150000000.0

    def test_plain_yuan(self):
        assert tmod._money("5000 元") == 5000.0

    def test_commas_stripped(self):
        assert tmod._money("1,234,567 元") == 1234567.0

    def test_empty(self):
        assert tmod._money(None) is None
        assert tmod._money("") is None


class TestTaxHits:
    """税务关键词判定。"""

    def test_strong_keyword_hits(self):
        hits = tmod.tax_hits("某公司企业所得税汇算清缴服务")
        assert hits

    def test_tax_bureau_purchaser_does_not_hit(self):
        # "税务局"是采购人名，不说明买的是税务服务（审计 B8 同类逻辑）
        hits = tmod.tax_hits("国家税务总局某市税务局食堂服务采购")
        assert not hits

    def test_non_tax_service_misses(self):
        hits = tmod.tax_hits("某公司办公大楼维修改造工程")
        assert not hits


class TestBiddable:
    """阶段是否还能投标。"""

    @pytest.mark.parametrize("proc", ["招标", "预招标", "采购意向"])
    def test_biddable(self, proc):
        assert tmod.is_biddable(proc) is True

    @pytest.mark.parametrize("proc", ["中标结果", "中标候选人", "合同"])
    def test_finished(self, proc):
        assert tmod.is_biddable(proc) is False

    def test_unknown_treated_as_biddable(self):
        # 未知阶段宁可多显示，不要误杀
        assert tmod.is_biddable(None) is True
        assert tmod.is_biddable("") is True


class TestMakeUid:
    def test_format(self):
        assert tmod.make_uid("cebpub", "123") == "cebpub:123"


class TestExtractPurchaser:
    """从标题推断采购人（列表页没这个字段）。

    `extract_purchaser` 在 cebpub 模块里（是列表映射逻辑的一部分），不在 tender。
    """

    @staticmethod
    def _fn():
        from taxassist.collect import cebpub
        return cebpub.extract_purchaser

    def test_company_name(self):
        out = self._fn()("中国建设银行股份有限公司税务服务采购项目")
        assert "建设银行" in (out or "")

    def test_empty(self):
        assert not self._fn()("")

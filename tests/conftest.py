"""测试夹具。

==============================================================================
为什么要有这个文件
==============================================================================

本项目的 ``config.DB_PATH`` 默认指向 ``data/taxassist.db`` —— 那是**真实库**
（5400+ 条政策、2800+ 个附件、以及客户相关的运行痕迹）。任何测试只要忘了传
临时路径，就会直接在真库上跑；而"跑通了"反而更危险：它可能已经悄悄改了真实
数据，并且看起来一切正常。

==============================================================================
为什么不做"全局强制重定向"
==============================================================================

最直接的做法是在 conftest 里把 ``TAXASSIST_DATA_DIR`` 环境变量指向临时目录，
让所有测试都碰不到真库。但 ``tests/test_cli_smoke.py`` 的存在意义恰恰相反 ——
它要**跑真实的 CLI 命令**（``taxassist status``）来验证入口路径没有 NameError。
把库换成空的，那条冒烟测试就失去了意义（而且会失败）。

所以这里的取舍是：
- 提供 ``temp_db`` fixture，**新测试一律用它**（并示范了怎么用）；
- 需要跑真库的测试（目前只有 CLI 冒烟）显式说明理由，不强制。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# 单元测试不该依赖外部程序：解析老式文档时会尝试启动 WPS（要几秒、可能弹窗），
# 这里统一禁用。要专门测转换本身时，在用例里 delenv 即可。
os.environ.setdefault("TAXASSIST_NO_WPS", "1")


@pytest.fixture
def temp_db():
    """一个建好表结构、与真实库完全无关的临时数据库连接。

    用法::

        def test_xxx(temp_db):
            temp_db.execute("INSERT INTO policy (...) VALUES (...)")
    """
    from taxassist import db as dbmod

    with tempfile.TemporaryDirectory(prefix="taxassist-test-") as d:
        conn = dbmod.connect(str(Path(d) / "test.db"))
        dbmod.init_db(conn)
        try:
            yield conn
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def _isolate_writelock(tmp_path, monkeypatch):
    """把写锁文件隔离到临时目录。

    结论与上面那段"不做全局强制重定向"相反，理由也相反：锁文件**没有**任何
    "必须跑真实路径"的测试需求（不像 CLI 冒烟需要真库），而它的默认位置
    ``data/write.lock`` 属于运行现场。

    不隔离的后果是实测到的：``run_daily`` 现在会先抢锁，若真实环境里 worker
    正在写库，``acquire`` 返回 False、日更直接跳过 —— 于是
    ``test_run_daily_marks_incomplete_when_collect_partial`` 会平白失败，
    而失败原因与被测逻辑毫无关系。
    """
    from taxassist import writelock

    monkeypatch.setattr(writelock, "LOCK_FILE", tmp_path / "write.lock")


@pytest.fixture
def real_db_guard():
    """断言当前进程指向的不是项目里的真实库。

    给"万一被误用"留一道防线：需要真库的测试显式声明它**已知晓**这一点。
    """
    from taxassist import config

    project_data = (Path(__file__).resolve().parent.parent / "data").resolve()
    db_path = Path(config.DB_PATH).resolve()
    is_real = project_data in db_path.parents
    return is_real

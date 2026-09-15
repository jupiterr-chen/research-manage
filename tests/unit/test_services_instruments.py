"""app/services/instruments.py 单测(R-SVC-01 / AM-17)。"""

from __future__ import annotations

import pytest

from app.services import instruments


def make(conn, **kw):
    base = dict(market="hk", code="1810.HK", name="小米集团", actor="web")
    base.update(kw)
    return instruments.create(conn, **base)


class TestCreate:
    def test_ok_and_normalized(self, conn):
        d = make(conn, code=" 1810.hk ")
        assert d["code"] == "1810.HK"
        assert d["enabled"] is True

    def test_duplicate(self, conn):
        make(conn)
        with pytest.raises(instruments.ConflictError, match="已存在"):
            make(conn)

    def test_bad_market(self, conn):
        with pytest.raises(instruments.ValidationError):
            make(conn, market="jp", code="6501.T")

    def test_bad_code(self, conn):
        with pytest.raises(instruments.ValidationError, match="市场"):
            make(conn, market="hk", code="abc.HK")  # 字母代码,hk 只接受数字

    def test_audit_written(self, conn):
        make(conn)
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["actor"] == "web" and row["action"] == "create" and row["entity"] == "instrument"


class TestUpdate:
    def test_rename_and_disable(self, conn):
        d = make(conn)
        d2 = instruments.update(conn, d["id"], name="小米", enabled=False, actor="api")
        assert d2["name"] == "小米" and d2["enabled"] is False

    def test_not_found(self, conn):
        with pytest.raises(instruments.NotFound):
            instruments.update(conn, 999, name="x", actor="web")


class TestDelete:
    def test_ok(self, conn):
        d = make(conn)
        instruments.delete(conn, d["id"], actor="web")
        assert instruments.find_by_code(conn, "1810.HK") is None
        assert conn.execute("SELECT COUNT(*) AS n FROM audit_log WHERE action='delete'").fetchone()["n"] == 1

    def test_conflict_with_schedule(self, conn):
        from app.services import schedules

        d = make(conn)
        prof = conn.execute("SELECT id FROM profile LIMIT 1").fetchone()["id"]
        schedules.create(conn, instrument_id=d["id"], profile_id=prof, kind="daily_trading", actor="web")
        with pytest.raises(instruments.ConflictError, match="关联调度"):
            instruments.delete(conn, d["id"], actor="web")

    def test_not_found(self, conn):
        with pytest.raises(instruments.NotFound):
            instruments.delete(conn, 999, actor="web")


class TestQuery:
    def test_find_and_list(self, conn):
        make(conn)
        make(conn, market="us", code="NVDA", name="NVIDIA")
        make(conn, market="cn", code="600519.SS", name="贵州茅台")
        assert instruments.find_by_code(conn, "NVDA")["market"] == "us"
        assert instruments.find_by_code(conn, "1810.HK", market="hk")["name"] == "小米集团"
        assert instruments.find_by_code(conn, "NOPE") is None
        assert len(instruments.list_instruments(conn)) == 3
        instruments.update(conn, 1, enabled=False, actor="web")
        assert len(instruments.list_instruments(conn, enabled_only=True)) == 2

    def test_get(self, conn):
        d = make(conn)
        assert instruments.get(conn, d["id"])["code"] == "1810.HK"
        with pytest.raises(instruments.NotFound):
            instruments.get(conn, 42)

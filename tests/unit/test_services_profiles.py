"""app/services/profiles.py 单测(R-SVC-02 / R-SCH-06)。"""

from __future__ import annotations

import pytest

from app.services import profiles


class TestCreate:
    def test_ok_canonical_csv(self, conn):
        d = profiles.create(conn, name="自定义", analysts_csv=" news , market ", actor="web")
        assert d["analysts_csv"] == "market,news"
        assert d["analysts"] == ("market", "news")

    def test_duplicate_name(self, conn):
        profiles.create(conn, name="A", analysts_csv="market", actor="web")
        with pytest.raises(profiles.ConflictError, match="已存在"):
            profiles.create(conn, name="A", analysts_csv="news", actor="web")

    @pytest.mark.parametrize("csv", ["", "market,bogus"])
    def test_bad_analysts(self, conn, csv):
        with pytest.raises((profiles.ValidationError, ValueError)):
            profiles.create(conn, name="B", analysts_csv=csv, actor="web")

    def test_empty_name(self, conn):
        with pytest.raises(profiles.ValidationError):
            profiles.create(conn, name="  ", analysts_csv="market", actor="web")

    def test_default_unique(self, conn):
        profiles.create(conn, name="D1", analysts_csv="market", is_default=True, actor="web")
        profiles.create(conn, name="D2", analysts_csv="news", is_default=True, actor="web")
        defaults = conn.execute("SELECT name FROM profile WHERE is_default=1").fetchall()
        assert [r["name"] for r in defaults] == ["D2"]


class TestUpdate:
    def test_fields(self, conn):
        d = profiles.create(conn, name="U", analysts_csv="market", actor="web")
        d2 = profiles.update(
            conn, d["id"], name="U2", analysts_csv="news,social", is_default=True, actor="api"
        )
        assert d2["name"] == "U2" and d2["analysts_csv"] == "social,news" and d2["is_default"]

    def test_name_conflict(self, conn):
        profiles.create(conn, name="X", analysts_csv="market", actor="web")
        d = profiles.create(conn, name="Y", analysts_csv="news", actor="web")
        with pytest.raises(profiles.ConflictError):
            profiles.update(conn, d["id"], name="X", actor="web")

    def test_not_found(self, conn):
        with pytest.raises(profiles.NotFound):
            profiles.update(conn, 9, name="z", actor="web")

    def test_bad_analysts(self, conn):
        d = profiles.create(conn, name="Z", analysts_csv="market", actor="web")
        with pytest.raises((profiles.ValidationError, ValueError)):
            profiles.update(conn, d["id"], analysts_csv="bogus", actor="web")


class TestDelete:
    def test_ok(self, conn):
        d = profiles.create(conn, name="DEL", analysts_csv="market", actor="web")
        profiles.delete(conn, d["id"], actor="web")
        with pytest.raises(profiles.NotFound):
            profiles.get(conn, d["id"])

    def test_conflict_schedule(self, conn):
        from app.services import instruments, schedules

        inst = instruments.create(conn, market="us", code="NVDA", actor="web")
        d = profiles.create(conn, name="S-USED", analysts_csv="market", actor="web")
        schedules.create(
            conn, instrument_id=inst["id"], profile_id=d["id"], kind="daily_trading", actor="web"
        )
        with pytest.raises(profiles.ConflictError, match="调度引用"):
            profiles.delete(conn, d["id"], actor="web")

    def test_not_found(self, conn):
        with pytest.raises(profiles.NotFound):
            profiles.delete(conn, 9, actor="web")


class TestList:
    def test_seed_default_first(self, conn):
        items = profiles.list_profiles(conn)
        assert items[0]["name"] == "日频三件套" and items[0]["is_default"]
        assert profiles.default_profile(conn)["name"] == "日频三件套"

    def test_get_not_found(self, conn):
        with pytest.raises(profiles.NotFound):
            profiles.get(conn, 123)

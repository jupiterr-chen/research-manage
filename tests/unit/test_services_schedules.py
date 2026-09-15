"""app/services/schedules.py 单测(R-SVC-03)。"""

from __future__ import annotations

import pytest

from app.services import instruments, profiles, schedules


@pytest.fixture
def inst(conn):
    return instruments.create(conn, market="hk", code="1810.HK", name="小米集团", actor="web")


@pytest.fixture
def prof(conn):
    # 种子档案 1 = 日频三件套
    return profiles.get(conn, 1)


class TestValidation:
    def test_bad_kind(self, conn, inst, prof):
        with pytest.raises(schedules.ValidationError):
            schedules.create(
                conn, instrument_id=inst["id"], profile_id=prof["id"], kind="hourly", actor="web"
            )

    @pytest.mark.parametrize("at", ["9:30", "25:00", "08:60", "abc", ""])
    def test_bad_at_time(self, conn, inst, prof, at):
        with pytest.raises(schedules.ValidationError, match="HH:MM"):
            schedules.create(
                conn,
                instrument_id=inst["id"],
                profile_id=prof["id"],
                kind="daily_trading",
                at_time=at,
                actor="web",
            )

    def test_weekly_requires_weekday(self, conn, inst, prof):
        with pytest.raises(schedules.ValidationError, match="weekday"):
            schedules.create(
                conn, instrument_id=inst["id"], profile_id=prof["id"], kind="weekly", actor="web"
            )

    def test_weekly_bad_weekday(self, conn, inst, prof):
        with pytest.raises(schedules.ValidationError):
            schedules.create(
                conn, instrument_id=inst["id"], profile_id=prof["id"], kind="weekly", weekday=8, actor="web"
            )

    def test_daily_rejects_weekday(self, conn, inst, prof):
        with pytest.raises(schedules.ValidationError, match="不应指定"):
            schedules.create(
                conn,
                instrument_id=inst["id"],
                profile_id=prof["id"],
                kind="daily_trading",
                weekday=1,
                actor="web",
            )

    def test_missing_refs(self, conn, inst, prof):
        with pytest.raises(schedules.NotFound):
            schedules.create(conn, instrument_id=99, profile_id=1, kind="daily_trading", actor="web")
        with pytest.raises(schedules.NotFound, match="档案"):
            schedules.create(conn, instrument_id=inst["id"], profile_id=99, kind="daily_trading", actor="web")

    def test_get_existing(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        assert schedules.get(conn, d["id"])["kind"] == "daily_trading"


class TestCrud:
    def test_create_daily(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        assert d["kind"] == "daily_trading" and d["at_time"] == "08:30" and d["enabled"]

    def test_create_weekly(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="weekly", weekday=1, actor="web"
        )
        assert d["weekday"] == 1

    def test_update_switch_to_daily_clears_weekday(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="weekly", weekday=1, actor="web"
        )
        d2 = schedules.update(conn, d["id"], kind="daily_trading", actor="web")
        assert d2["weekday"] is None and d2["kind"] == "daily_trading"

    def test_update_to_weekly_without_any_weekday(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        with pytest.raises(schedules.ValidationError, match="weekday"):
            schedules.update(conn, d["id"], kind="weekly", actor="web")

    def test_update_weekly_weekday_directly(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="weekly", weekday=1, actor="web"
        )
        d2 = schedules.update(conn, d["id"], weekday=2, actor="web")
        assert d2["weekday"] == 2 and d2["kind"] == "weekly"

    def test_update_profile_and_enabled(self, conn, inst, prof):
        other = profiles.create(conn, name="另一个", analysts_csv="news", actor="web")
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        d2 = schedules.update(conn, d["id"], profile_id=other["id"], enabled=False, actor="web")
        assert d2["profile_id"] == other["id"] and d2["enabled"] is False
        with pytest.raises(schedules.NotFound, match="档案"):
            schedules.update(conn, d["id"], profile_id=99, actor="web")

    def test_update_weekly_without_weekday_keeps(self, conn, inst, prof):
        d = schedules.create(
            conn,
            instrument_id=inst["id"],
            profile_id=prof["id"],
            kind="weekly",
            weekday=3,
            at_time="09:00",
            actor="web",
        )
        d2 = schedules.update(conn, d["id"], at_time="10:00", actor="web")
        assert d2["weekday"] == 3 and d2["at_time"] == "10:00"

    def test_toggle(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        assert schedules.toggle(conn, d["id"], actor="web")["enabled"] is False
        assert schedules.toggle(conn, d["id"], actor="web")["enabled"] is True

    def test_delete(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        schedules.delete(conn, d["id"], actor="web")
        with pytest.raises(schedules.NotFound):
            schedules.get(conn, d["id"])

    def test_not_found(self, conn):
        with pytest.raises(schedules.NotFound):
            schedules.get(conn, 7)
        with pytest.raises(schedules.NotFound):
            schedules.update(conn, 7, at_time="09:00", actor="web")
        with pytest.raises(schedules.NotFound):
            schedules.delete(conn, 7, actor="web")
        with pytest.raises(schedules.NotFound):
            schedules.toggle(conn, 7, actor="web")

    def test_list_filter(self, conn, inst, prof):
        other = instruments.create(conn, market="us", code="NVDA", actor="web")
        schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        schedules.create(
            conn, instrument_id=other["id"], profile_id=prof["id"], kind="weekly", weekday=5, actor="web"
        )
        assert len(schedules.list_schedules(conn)) == 2
        assert len(schedules.list_schedules(conn, instrument_id=inst["id"])) == 1


class TestDescribe:
    def test_daily(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        text = schedules.describe(conn, d)
        assert text == "小米集团:每交易日 08:30 · 技术+舆情+新闻"

    def test_weekly_full(self, conn, inst):
        full = profiles.get(conn, 2)  # 全量
        d = schedules.create(
            conn,
            instrument_id=inst["id"],
            profile_id=full["id"],
            kind="weekly",
            weekday=1,
            at_time="09:00",
            actor="web",
        )
        assert schedules.describe(conn, d) == "小米集团:每周一 09:00 · 技术+舆情+新闻+基本面"

    def test_disabled_suffix(self, conn, inst, prof):
        d = schedules.create(
            conn,
            instrument_id=inst["id"],
            profile_id=prof["id"],
            kind="daily_trading",
            enabled=False,
            actor="web",
        )
        assert schedules.describe(conn, d).endswith("(已停用)")

    def test_fallback_without_name(self, conn, prof):
        inst2 = instruments.create(conn, market="us", code="NVDA", name="", actor="web")
        d = schedules.create(
            conn, instrument_id=inst2["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        assert schedules.describe(conn, d).startswith("NVDA:")

    def test_fallback_corrupt_profile_csv(self, conn, inst, prof):
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        conn.execute("UPDATE profile SET analysts_csv='bogus' WHERE id=?", (prof["id"],))
        assert schedules.describe(conn, d).endswith("· bogus")

    def test_fallback_missing_refs(self, conn, inst, prof):
        """标的/档案被外力删除时 describe 仍可读(兜底文案)。"""
        d = schedules.create(
            conn, instrument_id=inst["id"], profile_id=prof["id"], kind="daily_trading", actor="web"
        )
        conn.execute("DELETE FROM schedule WHERE id=?", (d["id"],))
        conn.execute("DELETE FROM instrument WHERE id=?", (inst["id"],))
        conn.execute("DELETE FROM profile WHERE id=?", (prof["id"],))
        text = schedules.describe(conn, d)
        assert text.startswith("标的#") and "技术" in text

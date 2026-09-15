"""app/audit.py 单测(R-FND-06 / AM-07)。"""

from __future__ import annotations

import json

import pytest

from app import audit


class TestScrub:
    def test_sensitive_keys(self):
        obj = {
            "api_key": "sk-1234567890abcdef",
            "token": "secret-token",
            "PASSWORD": "p",
            "my_secret": "s",
            "Authorization": "Bearer xyz",
            "ok_field": "plain",
        }
        out = audit.scrub(obj)
        assert out["api_key"] == "***"
        assert out["token"] == "***"
        assert out["PASSWORD"] == "***"
        assert out["my_secret"] == "***"
        assert out["Authorization"] == "***"
        assert out["ok_field"] == "plain"

    def test_sk_pattern_in_string(self):
        assert audit.scrub("leaked sk-abcdefgh12345678 here") == "leaked sk-*** here"
        assert audit.scrub("no leak here") == "no leak here"

    def test_nested(self):
        obj = {"a": [{"token": "t", "note": "sk-abcdefgh12345678"}, 42], "b": None}
        out = audit.scrub(obj)
        assert out["a"][0]["token"] == "***"
        assert out["a"][0]["note"] == "sk-***"
        assert out["a"][1] == 42
        assert out["b"] is None

    def test_does_not_mutate_input(self):
        obj = {"token": "t"}
        audit.scrub(obj)
        assert obj["token"] == "t"


class TestAudit:
    def test_writes_row_with_scrubbed_detail(self, conn):
        with conn:  # autocommit 事务
            audit.audit(
                conn,
                "web",
                "create",
                "instrument",
                "1",
                {"code": "NVDA", "token": "leak", "note": "sk-abcdefgh12345678"},
            )
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["actor"] == "web"
        assert row["action"] == "create"
        detail = json.loads(row["detail_json"])
        assert detail["code"] == "NVDA"
        assert detail["token"] == "***"
        assert detail["note"] == "sk-***"

    def test_none_detail(self, conn):
        with conn:
            audit.audit(conn, "api", "delete", "schedule", 5, None)
        row = conn.execute("SELECT * FROM audit_log").fetchone()
        assert row["detail_json"] is None
        assert row["entity_id"] == "5"

    def test_bad_actor(self, conn):
        with pytest.raises(ValueError):
            audit.audit(conn, "hacker", "x", "y", None, None)

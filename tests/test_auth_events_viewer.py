"""로그인 이력 전용 API 검증. 임시 SQLite만 사용하며 MySQL은 차단한다."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import auth_audit
import auth_events_viewer
import server


class AuthEventsViewerTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "auth.sqlite3"
        self.enterContext(patch.dict(server.app.config, {"AUTH_AUDIT_PATH": str(self.path)}))
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real MySQL forbidden")))
        self.mysql = self.enterContext(patch.object(server, "get_connection"))
        self.password = secrets.token_urlsafe(32)
        self.enterContext(patch.object(server, "ADMIN_PASSWORD", self.password))
        self.client = server.app.test_client()
        with patch.object(auth_audit, "record_event"):
            self.client.post("/login", data={"username": "admin_user", "password": self.password})

    def event(self, index):
        timestamp = datetime(2026, 10, 30, 6, 12, 22, 188465, tzinfo=timezone.utc) + timedelta(seconds=index)
        return {"event_time": timestamp.isoformat(timespec="microseconds"),
                "portal_user": "cs_user" if index % 2 == 0 else "unknown_user",
                "event_type": ("LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT")[index % 3],
                "client_ip": "172.18.0.1", "client_ip_source": "remote_addr",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0.0.0 Safari/537.36",
                "session_id": "0123456789abcdef0123456789abcdef" if index % 3 != 1 else None,
                "forwarded_for_untrusted": ["100.64.10.20"], "forwarded_for_present": True,
                "proxy_headers_trusted": False}

    def seed(self, count):
        auth_audit._append(self.path, self.event(0))
        if count > 1:
            fields = tuple(self.event(0))
            rows = []
            for index in range(1, count):
                event = self.event(index)
                event["forwarded_for_untrusted"] = json.dumps(event["forwarded_for_untrusted"])
                rows.append(tuple(event[key] for key in fields))
            with closing(sqlite3.connect(self.path)) as connection:
                with connection:
                    connection.executemany("INSERT INTO auth_events (" + ",".join(fields) + ") VALUES ("
                                           + ",".join("?" for _ in fields) + ")", rows)

    def get_page(self, **filters):
        response = self.client.get("/api/viewer/auth-events", query_string=filters)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.mysql.assert_not_called()
        return response.get_json()

    def test_admin_session_succeeds_without_api_key(self):
        self.seed(3)
        self.assertEqual(self.get_page()["total"], 3)

    def test_anonymous_general_and_forged_admin_sessions_are_denied(self):
        client = server.app.test_client()
        self.assertEqual(client.get("/api/viewer/auth-events").status_code, 401)
        for user in server.ALLOWED_PORTAL_USERS:
            with self.subTest(user=user):
                with client.session_transaction() as session:
                    session["portal_user"] = user
                self.assertEqual(client.get("/api/viewer/auth-events", headers={"X-API-Key": "any-key"}).status_code, 403)
        self.assertFalse(self.path.exists())
        self.mysql.assert_not_called()

    def test_event_types_kst_and_session_prefix_without_secret_fields(self):
        self.seed(3)
        data = self.get_page()
        self.assertEqual([row["event_type"] for row in data["events"]], ["LOGOUT", "LOGIN_FAILED", "LOGIN_SUCCESS"])
        success = data["events"][-1]
        self.assertEqual(success["event_time"], "2026-10-30T06:12:22.188465+00:00")
        self.assertEqual(success["event_time_kst"], "2026-10-30T15:12:22+09:00")
        self.assertEqual(success["session_id_prefix"], "0123456789ab")
        self.assertIsNone(data["events"][1]["session_id_prefix"])
        self.assertEqual(success["client_ip"], "172.18.0.1")
        self.assertEqual(success["forwarded_for_untrusted"], ["100.64.10.20"])
        self.assertFalse(success["proxy_headers_trusted"])
        self.assertNotIn("0123456789abcdef0123456789abcdef", json.dumps(data))
        for row in data["events"]:
            for key in ("session_id", "password", "api_key", "session_cookie", "auth_token", "viewer_admin_auth"):
                self.assertNotIn(key, row)

    def test_user_and_event_filters_include_failed_unregistered_names(self):
        self.seed(6)
        self.assertEqual(self.get_page(portal_user="unknown_user")["total"], 3)
        self.assertEqual(self.get_page(event_type="LOGIN_FAILED")["total"], 2)
        combined = self.get_page(portal_user="unknown_user", event_type="LOGIN_FAILED")
        self.assertEqual(combined["total"], 1)
        self.assertEqual(combined["events"][0]["portal_user"], "unknown_user")
        self.assertEqual(self.get_page(portal_user="' OR 1=1 --")["total"], 0)

    def test_pagination_limits_rows_and_clamps_last_page(self):
        self.seed(127)
        pages = [self.get_page(page=page) for page in (1, 2, 999)]
        self.assertEqual([page["count"] for page in pages], [50, 50, 27])
        self.assertEqual([page["page"] for page in pages], [1, 2, 3])
        self.assertTrue(all(page["total"] == 127 and page["page_size"] == 50 and page["total_pages"] == 3 for page in pages))
        self.assertEqual([row["id"] for page in pages for row in page["events"]], list(range(127, 0, -1)))
        self.assertFalse(pages[0]["has_previous"])
        self.assertFalse(pages[-1]["has_next"])

    def test_invalid_filters_are_rejected_before_reading(self):
        with patch.object(auth_events_viewer, "load_page") as read:
            for filters in ({"page": "0"}, {"page": "-1"}, {"page": "abc"}, {"page": "1000001"},
                            {"event_type": "DB_SELECT"}, {"portal_user": "x" * 129}):
                with self.subTest(filters=filters):
                    self.assertEqual(self.client.get("/api/viewer/auth-events", query_string=filters).status_code, 400)
            read.assert_not_called()

    def test_reader_is_read_only_and_preserves_source_file(self):
        self.seed(3)
        before = hashlib.sha256(self.path.read_bytes()).digest()
        modified = self.path.stat().st_mtime_ns
        statements, opens = [], []
        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            opens.append((args, kwargs))
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch.object(auth_events_viewer.sqlite3, "connect", side_effect=connect):
            self.get_page()
        self.assertEqual(len(opens), 1)
        self.assertTrue(opens[0][0][0].endswith("?mode=ro"))
        self.assertTrue(opens[0][1]["uri"])
        self.assertIn("PRAGMA query_only=ON", statements)
        self.assertTrue(any("LIMIT 50 OFFSET 0" in sql for sql in statements))
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER")) for sql in statements))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).digest(), before)
        self.assertEqual(self.path.stat().st_mtime_ns, modified)

    def assert_db_viewer_still_works(self):
        response = self.client.get("/viewer")
        self.assertEqual(response.status_code, 200)
        self.assertIn('id="filterForm"', response.get_data(as_text=True))
        payload = {"logs": [], "count": 0, "total": 0, "page": 1, "page_size": 50,
                   "total_pages": 1, "has_previous": False, "has_next": False,
                   "summary": {"total": 0, "db_select": 0, "excel_download": 0, "portal_users": 0}}
        with patch.object(server.audit_viewer, "load_page", return_value=payload):
            response = self.client.get("/api/viewer/db-logs")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), payload)

    def test_missing_sqlite_does_not_create_file_or_break_db_viewer(self):
        response = self.client.get("/api/viewer/auth-events")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.path.exists())
        self.assert_db_viewer_still_works()

    def test_corrupt_sqlite_returns_safe_error_without_breaking_db_viewer(self):
        self.path.write_bytes(b"fixture corrupt store")
        response = self.client.get("/api/viewer/auth-events")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(str(self.path), response.get_data(as_text=True))
        self.assertNotIn("fixture corrupt store", response.get_data(as_text=True))
        self.assert_db_viewer_still_works()

    def test_locked_sqlite_returns_safe_error(self):
        self.seed(3)
        with closing(sqlite3.connect(self.path)) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            self.assertEqual(self.client.get("/api/viewer/auth-events").status_code, 503)
            blocker.rollback()
        self.assertEqual(self.get_page()["total"], 3)

    def test_expensive_read_can_be_interrupted_without_affecting_db_viewer(self):
        self.seed(2000)
        with patch.object(auth_events_viewer, "READ_BUDGET_SECONDS", -1):
            self.assertEqual(self.client.get("/api/viewer/auth-events?event_type=LOGIN_FAILED").status_code, 503)
        self.assert_db_viewer_still_works()

    def test_viewer_defaults_to_db_tab_and_does_not_eagerly_read_auth_store(self):
        with patch.object(auth_events_viewer, "load_page", side_effect=AssertionError("eager read")) as read:
            html = self.client.get("/viewer").get_data(as_text=True)
            read.assert_not_called()
        self.assertIn('id="dbAuditTab" aria-controls="dbAuditPanel" aria-selected="true"', html)
        self.assertIn('id="authAuditPanel" role="tabpanel" aria-labelledby="authAuditTab" hidden', html)
        for label in ("DB 접근 이력", "로그인 이력", "직접 연결 IP", "전달 IP(미신뢰)", "session_id(앞 12자리)"):
            self.assertIn(label, html)
        with self.client.get("/static/auth-events.js") as response:
            script = response.get_data(as_text=True)
        self.assertIn("textContent", script)
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("X-API-Key", script)


if __name__ == "__main__":
    unittest.main()

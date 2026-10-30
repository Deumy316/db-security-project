"""인증 관찰·독립 SQLite 저장·장애 격리 검증. 실제 MySQL 연결은 차단한다."""

from contextlib import closing
import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import auth_audit
import server


class AuthenticationAuditTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "audit.sqlite3"
        self.enterContext(patch.dict(server.app.config, {"AUTH_AUDIT_PATH": str(self.path)}))
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real MySQL forbidden")))
        self.db_connect = self.enterContext(patch.object(server, "get_user_connection"))
        self.warning = self.enterContext(patch.object(server.app.logger, "warning"))
        self.password = secrets.token_urlsafe(32)
        self.enterContext(patch.dict(server.DEPT_ACCOUNTS["cs_user"], password=self.password))
        self.enterContext(patch.object(server, "ADMIN_PASSWORD", self.password))
        self.client = server.app.test_client()

    def login(self, username="cs_user", password=None, client=None, **kwargs):
        return (client or self.client).post("/login", data={
            "username": username, "password": self.password if password is None else password,
        }, **kwargs)

    def rows(self):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM auth_events ORDER BY id")]

    def test_login_success_records_only_allowed_metadata(self):
        api_key = secrets.token_urlsafe(32)
        auth_token = secrets.token_urlsafe(32)
        self.client.set_cookie("other_token", auth_token)
        response = self.login(headers={"User-Agent": "Audit test browser", "X-API-Key": api_key,
                                      "Authorization": f"Bearer {auth_token}"},
                              environ_overrides={"REMOTE_ADDR": "100.64.10.20"})
        self.assertEqual((response.status_code, response.location), (302, "/home"))
        row, = self.rows()
        self.assertEqual(row["event_type"], "LOGIN_SUCCESS")
        self.assertEqual(row["portal_user"], "cs_user")
        self.assertEqual(row["client_ip"], "100.64.10.20")
        self.assertEqual(row["client_ip_source"], "remote_addr")
        self.assertEqual(row["user_agent"], "Audit test browser")
        self.assertRegex(row["session_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(row["event_time"].endswith("+00:00"))
        serialized = json.dumps(row)
        for secret in (self.password, api_key, auth_token, self.client.get_cookie("session").value,
                       self.client.get_cookie(auth_audit.COOKIE_NAME).value):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("password", row)
        with self.client.session_transaction() as session:
            self.assertEqual(set(session), {"portal_user", "_permanent"})

    def test_admin_success_does_not_record_admin_proof_or_connect_to_mysql(self):
        response = self.login("admin_user")
        self.assertEqual((response.status_code, response.location), (302, "/viewer"))
        self.db_connect.assert_not_called()
        row, = self.rows()
        self.assertEqual(row["portal_user"], "admin_user")
        self.assertEqual(row["event_type"], "LOGIN_SUCCESS")
        with self.client.session_transaction() as session:
            self.assertNotIn(session["viewer_admin_auth"], json.dumps(row))

    def test_failed_login_records_attempted_user_without_password(self):
        failed_password = secrets.token_urlsafe(32)
        response = self.login(password=failed_password)
        self.assertEqual(response.status_code, 200)
        self.assertIn("아이디 또는 비밀번호", response.get_data(as_text=True))
        row, = self.rows()
        self.assertEqual((row["portal_user"], row["event_type"]), ("cs_user", "LOGIN_FAILED"))
        self.assertIsNone(row["session_id"])
        self.assertNotIn(failed_password, json.dumps(row))
        self.assertIsNone(self.client.get_cookie(auth_audit.COOKIE_NAME))
        with self.client.session_transaction() as session:
            self.assertNotIn("portal_user", session)

    def test_db_login_failure_keeps_existing_error_result(self):
        self.db_connect.side_effect = server.mysql.connector.Error("private database details")
        response = self.login()
        self.assertEqual(response.status_code, 200)
        self.assertIn("DB 계정 연결에 실패", response.get_data(as_text=True))
        row, = self.rows()
        self.assertEqual(row["event_type"], "LOGIN_FAILED")
        self.assertNotIn("private database details", json.dumps(row))

    def test_logout_records_original_user_and_same_random_id_then_clears_cookies(self):
        self.login()
        first, = self.rows()
        self.client.get("/db")
        response = self.client.get("/logout")
        self.assertEqual((response.status_code, response.location), (302, "/login"))
        success, logout = self.rows()
        self.assertEqual(logout["event_type"], "LOGOUT")
        self.assertEqual(logout["portal_user"], "cs_user")
        self.assertEqual(logout["session_id"], first["session_id"])
        self.assertIsNone(self.client.get_cookie("session"))
        self.assertIsNone(self.client.get_cookie(auth_audit.COOKIE_NAME))
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {})
        self.client.get("/logout")
        self.assertEqual(len(self.rows()), 2)

    def test_failed_attempt_does_not_change_existing_login_or_audit_id(self):
        self.login()
        cookie = self.client.get_cookie(auth_audit.COOKIE_NAME).value
        response = self.login("unknown", "invalid")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get_cookie(auth_audit.COOKIE_NAME).value, cookie)
        with self.client.session_transaction() as session:
            self.assertEqual(session["portal_user"], "cs_user")
        self.assertEqual(self.rows()[-1]["portal_user"], "unknown")
        self.assertIsNone(self.rows()[-1]["session_id"])

    def test_storage_errors_do_not_change_success_failure_or_logout(self):
        with patch.object(auth_audit, "_append", side_effect=OSError("sensitive error details")):
            self.assertEqual(self.login().location, "/home")
            with self.client.session_transaction() as session:
                self.assertEqual(session["portal_user"], "cs_user")
            self.assertEqual(self.login(password="invalid").status_code, 200)
            self.assertEqual(self.client.get("/logout").location, "/login")
            with self.client.session_transaction() as session:
                self.assertEqual(dict(session), {})
        self.assertEqual(self.warning.call_count, 3)
        self.assertNotIn("sensitive error details", str(self.warning.call_args_list))
        self.assertEqual(self.rows(), [])

    def test_unexpected_recorder_and_warning_errors_still_do_not_affect_authentication(self):
        self.warning.side_effect = RuntimeError("logging failure")
        with patch.object(auth_audit, "record_event", side_effect=RuntimeError("recorder failure")):
            self.assertEqual(self.login().location, "/home")
            self.assertEqual(self.login(password="invalid").status_code, 200)
            self.assertEqual(self.client.get("/logout").location, "/login")
        with self.client.session_transaction() as session:
            self.assertEqual(dict(session), {})

    def test_event_collection_failure_is_fail_open(self):
        with patch.object(auth_audit, "_event", side_effect=RuntimeError("collection failure")):
            self.assertEqual(self.login().location, "/home")
            self.assertEqual(self.client.get("/logout").location, "/login")
        self.assertEqual(self.rows(), [])

    def test_proxy_headers_remain_untrusted_and_cannot_override_peer_ip(self):
        self.login(headers={"X-Forwarded-For": "198.51.100.9, 100.64.1.2, invalid-secret",
                            "X-Real-IP": "203.0.113.99"},
                   environ_overrides={"REMOTE_ADDR": "172.18.0.1"})
        row, = self.rows()
        self.assertEqual(row["client_ip"], "172.18.0.1")
        self.assertEqual(json.loads(row["forwarded_for_untrusted"]), ["198.51.100.9", "100.64.1.2"])
        self.assertEqual(row["forwarded_for_present"], 1)
        self.assertEqual(row["proxy_headers_trusted"], 0)
        self.assertNotIn("invalid-secret", json.dumps(row))
        self.assertNotIn("203.0.113.99", json.dumps(row))
        self.login(headers={"X-Forwarded-For": "fe80::1%hidden-zone-token"},
                   environ_overrides={"REMOTE_ADDR": "2001:db8::2"})
        ipv6 = self.rows()[-1]
        self.assertEqual(ipv6["client_ip"], "2001:db8::2")
        self.assertEqual(json.loads(ipv6["forwarded_for_untrusted"]), ["fe80::1"])
        self.assertNotIn("hidden-zone-token", json.dumps(ipv6))

    def test_missing_peer_ip_does_not_fall_back_to_forwarded_header(self):
        self.login(headers={"X-Forwarded-For": "198.51.100.9"}, environ_overrides={"REMOTE_ADDR": ""})
        row, = self.rows()
        self.assertIsNone(row["client_ip"])
        self.assertEqual(row["proxy_headers_trusted"], 0)

    def test_identifier_rotates_on_login_and_committed_rows_survive_reopening_store(self):
        self.login()
        first, = self.rows()
        self.login()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[1]["session_id"], first["session_id"])
        with closing(sqlite3.connect(self.path)) as reopened:
            self.assertEqual(reopened.execute("SELECT COUNT(*) FROM auth_events").fetchone()[0], 2)

    def test_locked_store_does_not_block_login_result(self):
        self.login()
        with closing(sqlite3.connect(self.path)) as blocker:
            blocker.execute("BEGIN EXCLUSIVE")
            response = self.login()
            self.assertEqual((response.status_code, response.location), (302, "/home"))
            blocker.rollback()
        self.assertEqual(len(self.rows()), 1)
        self.warning.assert_called_once()

    def test_corrupt_store_does_not_change_login_result(self):
        self.path.write_bytes(b"not a sqlite database")
        self.assertEqual(self.login().location, "/home")
        self.warning.assert_called_once()

    def test_invalid_audit_cookie_does_not_authorize_user_or_prevent_logout(self):
        self.client.set_cookie(auth_audit.COOKIE_NAME, "forged-identifier")
        self.assertEqual(self.client.get("/viewer").location, "/login")
        self.assertEqual(self.client.get("/api/viewer/db-logs").status_code, 401)
        self.login()
        self.client.set_cookie(auth_audit.COOKIE_NAME, "forged-identifier")
        self.assertEqual(self.client.get("/logout").location, "/login")
        row = self.rows()[-1]
        self.assertEqual((row["event_type"], row["portal_user"]), ("LOGOUT", "cs_user"))
        self.assertIsNone(row["session_id"])
        self.assertNotIn("forged-identifier", json.dumps(row))

    def test_other_requests_and_legacy_apis_do_not_use_auth_store(self):
        record = {"portal_user": "cs_user", "db_user": "sales_user", "user_name": "sales_user",
                  "event_time": "2026-10-30T12:00:00", "action": "DB_SELECT",
                  "target_table": "customer_vault", "query_string": "SELECT 1", "rows_affected": 1}
        with patch.object(server, "API_KEY", "fixture-key"), \
                patch.object(server, "load_logs", return_value=[record]), \
                patch.object(server, "load_logs_since", return_value=[record]), \
                patch.object(auth_audit, "_append", side_effect=AssertionError("must not write")) as append:
            self.assertEqual(self.client.get("/login").status_code, 200)
            for path in ("/api/db-logs", "/api/db-logs/latest", "/api/db-logs/since?after=2026-10-01"):
                self.assertEqual(self.client.get(path).status_code, 401)
                response = self.client.get(path, headers={"X-API-Key": "fixture-key"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["logs"], [record])
            append.assert_not_called()

    def test_metadata_size_is_bounded_and_registration_does_not_duplicate_events(self):
        auth_audit.register(server.app)
        self.login(username="x" * 512, headers={"User-Agent": "y" * 2000,
                   "X-Forwarded-For": ",".join(["198.51.100.1"] * 20)})
        row, = self.rows()
        self.assertEqual(len(row["portal_user"]), 128)
        self.assertEqual(len(row["user_agent"]), 512)
        self.assertEqual(len(json.loads(row["forwarded_for_untrusted"])), 8)


if __name__ == "__main__":
    unittest.main()

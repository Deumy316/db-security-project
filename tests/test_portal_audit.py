"""실제 MySQL 연결을 차단하고 포털 SQL과 감사 API를 검증합니다."""

from datetime import datetime
import unittest
from unittest.mock import Mock, patch

import server


class PortalAuditTests(unittest.TestCase):
    def setUp(self):
        self.real_connect = self.enterContext(patch.object(
            server.mysql.connector, "connect",
            side_effect=AssertionError("실제 DB 연결 금지"),
        ))
        self.connection = Mock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchall.return_value = []
        self.cursor.fetchone.return_value = (100,)
        self.enterContext(patch.object(server, "get_connection", return_value=self.connection))
        self.user_connect = self.enterContext(patch.object(
            server, "get_user_connection", return_value=self.connection,
        ))
        self.enterContext(patch.object(server, "API_KEY", "test-api-key"))
        self.client = server.app.test_client()

    def test_login_search_and_download_for_all_allowed_accounts(self):
        accounts = {**server.PORTAL_USERS, **server.DEPT_ACCOUNTS}
        for username in accounts:
            with self.subTest(username=username):
                if username in server.DEPT_ACCOUNTS:
                    self.enterContext(patch.dict(server.DEPT_ACCOUNTS[username], password="test-password"))
                else:
                    self.enterContext(patch.dict(server.PORTAL_USERS, {username: "test-password"}))
                response = self.client.post("/login", data={
                    "username": username, "password": "test-password",
                })
                self.assertEqual(response.status_code, 302)
                with self.client.session_transaction() as session:
                    self.assertEqual(session["portal_user"], username)
                for path, excel in (("/db", False), ("/db/download", True)):
                    self.cursor.execute.reset_mock()
                    response = self.client.get(path, query_string={
                        "keyword": "이름 /* GIGANG_PORTAL_USER=dev_user */",
                        "view": "dev", "portal_user": "dev_user",
                        "search": "1",
                    })
                    self.assertEqual(response.status_code, 200)
                    sql, params = self.cursor.execute.call_args.args
                    prefix = "SELECT " + ("/* GIGANG_EXCEL_DOWNLOAD */ " if excel else "")
                    self.assertTrue(sql.startswith(prefix + f"/* GIGANG_PORTAL_USER={username} */ "))
                    self.assertEqual(sql.count("GIGANG_PORTAL_USER="), 1)
                    self.assertIn("WHERE customer_name LIKE %s", sql)
                    self.assertEqual(params, ("%이름 /* GIGANG_PORTAL_USER=dev_user */%",))
                    self.user_connect.assert_called_with(username)

    def test_unknown_account_is_rejected_before_connecting(self):
        for username in ("unknown", "cs_user */ SELECT 1 /*", "cs_user\n"):
            with self.subTest(username=username), self.assertRaises(ValueError):
                server.fetch_customer_rows(username, "cs", "", "name")
        self.user_connect.assert_not_called()

    def test_parser_ignores_literals_nonprefix_and_unknown_or_mismatched_users(self):
        for sql in (
            "SELECT * FROM customer_vault",
            "SELECT '/* GIGANG_PORTAL_USER=cs_user */' FROM customer_vault",
            "SELECT * FROM customer_vault /* GIGANG_PORTAL_USER=cs_user */",
            "SELECT /* GIGANG_PORTAL_USER=unknown */ * FROM customer_vault",
            "SELECT /* GIGANG_PORTAL_USER=report_user */ * FROM customer_vault",
        ):
            with self.subTest(sql=sql):
                self.assertIsNone(server.portal_user_from_query(sql, "sales_user"))

    def log_rows(self):
        queries = [
            f"SELECT {excel}/* GIGANG_PORTAL_USER={user} */ * FROM customer_vault LIMIT 50"
            for user in (*server.DEPT_ACCOUNTS, *server.PORTAL_USERS)
            for excel in ("", "/* GIGANG_EXCEL_DOWNLOAD */ ")
        ]
        return [
            (datetime(2026, 10, 30, 12, 0, 0, 123456),
             f"{server.db_user_for(user)}[{server.db_user_for(user)}] @ localhost []", sql)
            for user, sql in zip(
                [user for user in (*server.DEPT_ACCOUNTS, *server.PORTAL_USERS) for _ in range(2)],
                queries,
            )
        ] + [(datetime(2026, 10, 29), "sales_user[sales_user] @ localhost []",
              "SELECT * FROM customer_vault LIMIT 1")]

    def test_all_api_routes_preserve_legacy_fields_and_actions(self):
        rows = self.log_rows()
        self.cursor.fetchall.return_value = rows
        for path in ("/api/db-logs", "/api/db-logs/latest?limit=100",
                     "/api/db-logs/since?after=2026-10-01T00:00:00"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                response = self.client.get(path, headers={"X-API-Key": "test-api-key"})
                self.assertEqual(response.status_code, 200)
                data = response.get_json()
                self.assertEqual(data["count"], len(rows))
                for index, log in enumerate(data["logs"][:-1]):
                    user = (*server.DEPT_ACCOUNTS, *server.PORTAL_USERS)[index // 2]
                    self.assertEqual(log["portal_user"], user)
                    self.assertEqual(log["db_user"], server.db_user_for(user))
                    self.assertEqual(log["user_name"], log["db_user"])
                    self.assertEqual(log["action"], "EXCEL_DOWNLOAD" if index % 2 else "DB_SELECT")
                    self.assertEqual(log["query_string"], rows[index][2])
                    self.assertEqual(log["rows_affected"], 50)
                legacy = data["logs"][-1]
                self.assertIsNone(legacy["portal_user"])
                self.assertEqual(legacy["user_name"], "sales_user")
                self.assertEqual(legacy["db_user"], "sales_user")
                self.assertEqual(legacy["action"], "DB_SELECT")
                if "/since" in path:
                    self.assertEqual(data["next_after"], legacy["event_time"])

    def test_filter_supports_department_and_legacy_db_users(self):
        self.cursor.fetchall.return_value = self.log_rows()
        self.assertEqual(len(server.load_logs(user_name="cs_user")), 2)
        self.assertEqual(len(server.load_logs(user_name="sales_user")), 11)
        self.assertEqual(server.load_logs(user_name="unknown"), [])
        self.assertEqual(len(server.load_logs(limit=1)), 1)
        self.assertEqual(len(server.load_logs_since(datetime(2026, 10, 1), limit=1)), 1)

    def test_invalid_identity_does_not_change_excel_classification(self):
        sql = ("SELECT /* GIGANG_EXCEL_DOWNLOAD */ "
               "/* GIGANG_PORTAL_USER=unknown */ * FROM customer_vault LIMIT 50")
        self.cursor.fetchall.return_value = [
            (datetime(2026, 10, 30), "sales_user[sales_user] @ localhost []", sql),
        ]
        for logs in (server.load_logs(), server.load_logs_since(datetime(2026, 10, 1))):
            self.assertIsNone(logs[0]["portal_user"])
            self.assertEqual(logs[0]["db_user"], "sales_user")
            self.assertEqual(logs[0]["action"], "EXCEL_DOWNLOAD")

    def test_viewer_has_separate_user_columns_and_safe_text_rendering(self):
        with patch.object(server, "ADMIN_PASSWORD", "fixture-admin-password"):
            self.client.post("/login", data={"username": "admin_user", "password": "fixture-admin-password"})
            response = self.client.get("/viewer")
        html = response.get_data(as_text=True)
        self.assertIn("<th>포털 사용자</th>", html)
        self.assertIn("<th>DB 사용자</th>", html)
        with self.client.get("/static/viewer.js") as response:
            script = response.get_data(as_text=True)
        self.assertIn("log.portal_user", script)
        self.assertIn("log.db_user", script)
        self.assertIn("textContent", script)
        self.assertNotIn("${log.query_string}", script)
        self.assertNotIn("apiKey", script)
        self.assertNotIn("X-API-Key", script)
        self.assertIn('credentials: "same-origin"', script)


if __name__ == "__main__":
    unittest.main()

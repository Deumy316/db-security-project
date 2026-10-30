"""관리자 세션과 기존 API Key 경계 검증. 실제 DB 연결은 차단한다."""

import secrets
from datetime import datetime
import unittest
from unittest.mock import patch

import server


class ViewerAdminTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real DB forbidden")))
        self.password = secrets.token_urlsafe(32)
        self.enterContext(patch.object(server, "ADMIN_PASSWORD", self.password))
        self.enterContext(patch.object(server, "API_KEY", "fixture-api-key"))
        self.open_user_connection = server.get_user_connection
        self.user_connection = self.enterContext(patch.object(server, "get_user_connection"))
        self.connect = self.enterContext(patch.object(server, "get_connection"))
        self.page = self.enterContext(patch.object(server.audit_viewer, "load_page", return_value={
            "logs": [], "count": 0, "total": 0, "page": 1, "page_size": 50,
            "total_pages": 1, "has_previous": False, "has_next": False,
            "summary": {"total": 0, "db_select": 0, "excel_download": 0, "portal_users": 0},
        }))
        self.client = server.app.test_client()

    def login_admin(self):
        return self.client.post("/login", data={"username": "admin_user", "password": self.password})

    def viewer_api(self, client=None, **kwargs):
        return (client or self.client).get("/api/viewer/db-logs", **kwargs)

    def test_admin_login_redirects_to_viewer_without_db_connection(self):
        response = self.login_admin()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "/viewer")
        self.assertNotIn(self.password, response.get_data(as_text=True))
        with self.client.session_transaction() as session:
            self.assertEqual(session["portal_user"], "admin_user")
            self.assertTrue(session.permanent)
            self.assertNotIn(self.password, str(dict(session)))
        self.user_connection.assert_not_called()
        self.connect.assert_not_called()
        self.assertIn("admin_user", server.ALLOWED_PORTAL_USERS)
        self.assertEqual(server.db_user_for("admin_user"), "sales_user")

    def test_wrong_missing_or_disabled_admin_password_never_authenticates(self):
        for configured, submitted in ((self.password, "wrong"), (self.password, ""), (None, ""), ("", "")):
            with self.subTest(configured=bool(configured), submitted=bool(submitted)):
                client = server.app.test_client()
                with patch.object(server, "ADMIN_PASSWORD", configured):
                    response = client.post("/login", data={"username": "admin_user", "password": submitted})
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn(self.password, response.get_data(as_text=True))
                    with client.session_transaction() as session:
                        self.assertNotIn("viewer_admin_auth", session)
                    self.assertEqual(client.get("/viewer").location, "/login")
        self.user_connection.assert_not_called()
        self.connect.assert_not_called()

    def test_general_accounts_login_to_home_and_check_db_connection(self):
        for username in server.ALLOWED_PORTAL_USERS - {"admin_user"}:
            with self.subTest(username=username):
                client = server.app.test_client()
                if username in server.DEPT_ACCOUNTS:
                    context = patch.dict(server.DEPT_ACCOUNTS[username], password="fixture-portal-password")
                else:
                    context = patch.dict(server.PORTAL_USERS, {username: "fixture-portal-password"})
                with context:
                    response = client.post("/login", data={"username": username, "password": "fixture-portal-password"})
                self.assertEqual((response.status_code, response.location), (302, "/home"))
                self.user_connection.assert_called_with(username)
                self.assertEqual(client.get("/viewer").status_code, 403)

    def test_anonymous_viewer_redirect_and_api_unauthorized_even_with_key(self):
        response = self.client.get("/viewer")
        self.assertEqual((response.status_code, response.location), (302, "/login"))
        self.assertEqual(self.viewer_api().status_code, 401)
        self.assertEqual(self.client.get("/api/viewer/db-logs", headers={"X-API-Key": "fixture-api-key"}).status_code, 401)
        self.connect.assert_not_called()
        self.page.assert_not_called()

    def test_all_general_users_forbidden_on_viewer_and_api_even_with_key(self):
        for username in server.ALLOWED_PORTAL_USERS - {"admin_user"}:
            with self.subTest(username=username):
                with self.client.session_transaction() as session:
                    session["portal_user"] = username
                self.assertEqual(self.client.get("/viewer").status_code, 403)
                self.assertEqual(self.viewer_api().status_code, 403)
                self.assertEqual(self.client.get("/api/viewer/db-logs", headers={"X-API-Key": "fixture-api-key"}).status_code, 403)
        self.connect.assert_not_called()
        self.page.assert_not_called()

    def test_admin_viewer_shows_identity_and_existing_logout(self):
        self.login_admin()
        response = self.client.get("/viewer")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("admin_user · 감사 관리자", html)
        self.assertIn('href="/logout"', html)
        self.assertNotIn(self.password, html)
        self.assertNotIn('id="apiKey"', html)
        self.assertNotIn("API Key", html)
        self.connect.assert_not_called()

    def test_navigation_is_localized_and_audit_menu_is_admin_only(self):
        login_html = self.client.get("/login").get_data(as_text=True)
        self.assertNotIn('aria-label="서비스 메뉴"', login_html)
        for path in ("/home", "/db", "/permissions", "/viewer"):
            self.assertNotIn(f'href="{path}"', login_html)
        self.assertIn('aria-label="GIGANG 홈"', login_html)
        self.assertNotIn('href="/viewer"', login_html)
        for username in server.ALLOWED_PORTAL_USERS - {"admin_user"}:
            with self.subTest(username=username):
                with self.client.session_transaction() as session:
                    session.clear()
                    session["portal_user"] = username
                response = self.client.get("/db")
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(">고객정보 조회</a>", html)
                self.assertNotIn('href="/viewer"', html)
                self.assertNotIn("Audit Viewer", html)
                self.assertEqual(self.client.get("/viewer").status_code, 403)
        self.login_admin()
        for path in ("/db", "/viewer"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                html = response.get_data(as_text=True)
                self.assertIn(">고객정보 조회</a>", html)
                self.assertIn(">감사 로그</a>", html)
                self.assertIn('href="/viewer"', html)
                self.assertNotIn("Audit Viewer", html)
                self.assertNotIn(">DB Portal</a>", html)
                if path == "/viewer":
                    self.assertIn("<title>GIGANG · 감사 로그 대시보드</title>", html)

    def test_admin_api_succeeds_with_session_only_regardless_of_api_key(self):
        self.login_admin()
        self.assertEqual(self.client.get("/api/viewer/db-logs").status_code, 200)
        self.assertEqual(self.client.get("/api/viewer/db-logs", headers={"X-API-Key": "wrong"}).status_code, 200)
        with patch.object(server, "API_KEY", None):
            self.assertEqual(self.viewer_api().status_code, 200)
        response = self.viewer_api(query_string={"page": "2", "legacy": "exclude"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), self.page.return_value)
        self.assertEqual(self.page.call_args.args[1]["page"], 2)
        self.assertEqual(self.page.call_args.args[1]["legacy"], "exclude")

    def test_admin_can_query_all_columns_and_download_with_audit_identity(self):
        self.login_admin()
        cursor = self.user_connection.return_value.cursor.return_value
        cursor.fetchall.return_value = []
        response = self.client.get("/db")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        for label in ("주민등록번호", "신용카드번호", "잔액", 'href="/viewer"', 'href="/db"'):
            self.assertIn(label, html)
        cursor.execute.assert_not_called()
        queries = []
        for path, excel in (("/db?search=1&keyword=1&search_type=id", False),
                            ("/db/download?keyword=1&search_type=id", True)):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            sql, params = cursor.execute.call_args.args
            prefix = "SELECT " + ("/* GIGANG_EXCEL_DOWNLOAD */ " if excel else "")
            self.assertTrue(sql.startswith(prefix + "/* GIGANG_PORTAL_USER=admin_user */ "))
            self.assertIn("rrn", sql)
            self.assertIn("credit_card_num", sql)
            self.assertIn("balance", sql)
            self.assertIn("WHERE user_id = %s", sql)
            self.assertEqual(params, (1,))
            self.user_connection.assert_called_with("admin_user")
            if excel:
                self.assertIn("gigang_customer_data.xlsx", response.headers["Content-Disposition"])
                self.assertTrue(response.data.startswith(b"PK"))
            queries.append(sql)
        audit_cursor = self.connect.return_value.cursor.return_value
        audit_cursor.fetchall.return_value = [
            (datetime(2026, 10, 30), "sales_user[sales_user] @ localhost []", sql)
            for sql in queries
        ]
        audit_cursor.fetchone.return_value = (100,)
        for logs in (server.load_logs(), server.load_logs_since(datetime(2026, 10, 1))):
            self.assertEqual([log["action"] for log in logs], ["DB_SELECT", "EXCEL_DOWNLOAD"])
            for log in logs:
                self.assertEqual(log["portal_user"], "admin_user")
                self.assertEqual(log["db_user"], "sales_user")
                self.assertEqual(log["user_name"], "sales_user")

    def test_forged_admin_session_cannot_query_or_download(self):
        with self.client.session_transaction() as session:
            session["portal_user"] = "admin_user"
        for path in ("/db", "/db?search=1", "/db/download"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.user_connection.assert_not_called()

    def test_admin_uses_existing_sales_db_connection(self):
        with patch.dict(server.PORTAL_USERS, {"sales_user": "fixture-db-password"}), \
                patch.object(server.mysql.connector, "connect") as connect:
            connection = self.open_user_connection("admin_user")
            self.assertIs(connection, connect.return_value)
            connect.assert_called_once()
            self.assertEqual(connect.call_args.kwargs["user"], "sales_user")
            self.assertEqual(connect.call_args.kwargs["database"], "gigang_db")
            self.assertEqual(connect.call_args.kwargs["password"], "fixture-db-password")

    def test_home_routes_admin_and_general_users_to_correct_page(self):
        self.assertEqual(self.client.get("/").location, "/login")
        self.login_admin()
        self.assertEqual(self.client.get("/").location, "/viewer")
        with self.client.session_transaction() as session:
            session.clear()
            session["portal_user"] = "cs_user"
        self.assertEqual(self.client.get("/").location, "/home")

    def test_forged_identity_or_role_values_do_not_grant_admin(self):
        for proof in (None, True, {}, "forged", "a" * 32 + ":" + "0" * 64):
            with self.subTest(proof_type=type(proof).__name__):
                with self.client.session_transaction() as session:
                    session.clear()
                    session["portal_user"] = "admin_user"
                    session["is_admin"] = True
                    session["role"] = "admin"
                    session["viewer_admin_auth"] = proof
                self.assertEqual(self.client.get("/viewer").status_code, 403)
                self.assertEqual(self.viewer_api().status_code, 403)
        self.connect.assert_not_called()

    def test_request_fields_cannot_grant_admin_without_authentication(self):
        response = self.client.post("/login", data={"username": "cs_user", "password": "wrong",
            "portal_user": "admin_user", "is_admin": "true", "role": "admin"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.viewer_api(query_string={"portal_user": "admin_user", "is_admin": "true"}).status_code, 401)
        self.assertEqual(self.client.get("/viewer?portal_user=admin_user").location, "/login")

    def test_logout_and_password_rotation_invalidate_admin_session(self):
        self.login_admin()
        with patch.object(server, "ADMIN_PASSWORD", secrets.token_urlsafe(32)):
            self.assertEqual(self.client.get("/viewer").status_code, 403)
            self.assertEqual(self.viewer_api().status_code, 403)
        response = self.client.get("/logout")
        self.assertEqual(response.location, "/login")
        self.assertEqual(self.client.get("/viewer").location, "/login")
        self.assertEqual(self.viewer_api().status_code, 401)

    def test_switching_from_admin_to_general_login_clears_admin_proof(self):
        self.login_admin()
        with patch.dict(server.PORTAL_USERS, {"sales_user": "fixture-portal-password"}):
            self.client.post("/login", data={"username": "sales_user", "password": "fixture-portal-password"})
        with self.client.session_transaction() as session:
            self.assertNotIn("viewer_admin_auth", session)
        self.assertEqual(self.viewer_api().status_code, 403)

    def test_legacy_apis_remain_key_only_with_unchanged_response(self):
        record = {"event_time": "2026-10-30T12:00:00.000000", "user_name": "sales_user",
            "portal_user": "cs_user", "db_user": "sales_user", "src_ip": "127.0.0.1",
            "action": "DB_SELECT", "target_table": "customer_vault",
            "query_string": "SELECT /* GIGANG_PORTAL_USER=cs_user */ * FROM customer_vault LIMIT 1",
            "rows_affected": 1}
        with patch.object(server, "load_logs", return_value=[record]), \
                patch.object(server, "load_logs_since", return_value=[record]):
            for role in (None, "cs_user", "admin_user"):
                client = server.app.test_client()
                if role == "admin_user":
                    client.post("/login", data={"username": role, "password": self.password})
                elif role:
                    with client.session_transaction() as session:
                        session["portal_user"] = role
                for path in ("/api/db-logs", "/api/db-logs/latest", "/api/db-logs/since?after=2026-10-01T00:00:00"):
                    with self.subTest(role=role, path=path):
                        self.assertEqual(client.get(path).status_code, 401)
                        response = client.get(path, headers={"X-API-Key": "fixture-api-key"})
                        self.assertEqual(response.status_code, 200)
                        expected = {"count": 1, "logs": [record]}
                        if "/since" in path:
                            expected["next_after"] = record["event_time"]
                        self.assertEqual(response.get_json(), expected)


if __name__ == "__main__":
    unittest.main()

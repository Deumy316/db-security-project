"""업무 포털의 표시·권한 경계 검증. 실제 DB 연결은 차단한다."""

import unittest
from unittest.mock import patch

import server
from test_viewer_dashboard import MemoryConnection


class PortalHomeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real DB forbidden")))
        self.connect = self.enterContext(patch.object(server, "get_connection"))
        self.load_activity = server.audit_viewer.load_page
        self.activity = self.enterContext(patch.object(server.audit_viewer, "load_page", return_value={
            "logs": [], "summary": {"total": 0, "db_select": 0, "excel_download": 0},
        }))
        self.enterContext(patch.object(server, "ADMIN_PASSWORD", "fixture-admin-password"))
        self.client = server.app.test_client()

    def set_user(self, username):
        if username == "admin_user":
            self.client.post("/login", data={"username": username, "password": "fixture-admin-password"})
        else:
            with self.client.session_transaction() as session:
                session.clear()
                session["portal_user"] = username

    def test_new_pages_require_login(self):
        for path in ("/home", "/permissions"):
            response = self.client.get(path)
            self.assertEqual((response.status_code, response.location), (302, "/login"))
        self.connect.assert_not_called()

    def test_notice_details_require_login(self):
        for slug in ("data-access-policy", "download-security", "access-scope"):
            response = self.client.get(f"/notices/{slug}")
            self.assertEqual((response.status_code, response.location), (302, "/login"))
        self.connect.assert_not_called()

    def test_notice_details_show_content_metadata_and_home_link_without_db(self):
        expected = {
            "data-access-policy": "업무 목적에 맞는 정보 조회",
            "download-security": "다운로드 파일 취급",
            "access-scope": "접근 범위 확인",
        }
        for username in server.ALLOWED_PORTAL_USERS:
            self.set_user(username)
            for slug, title in expected.items():
                with self.subTest(username=username, slug=slug):
                    response = self.client.get(f"/notices/{slug}")
                    self.assertEqual(response.status_code, 200)
                    html = response.get_data(as_text=True)
                    self.assertIn(f'<h1 id="notice-heading">{title}</h1>', html)
                    self.assertIn('작성일 <time datetime="2026-10-30">2026-10-30</time>', html)
                    self.assertIn("작성 부서 정보보안팀", html)
                    self.assertIn('href="/home"', html)
                    self.assertIn("← 홈으로", html)
                    body = html.split('<div class="notice-body">')[1].split('</div>')[0]
                    self.assertEqual(body.count('<p>'), 3)
        self.connect.assert_not_called()
        self.activity.assert_not_called()

    def test_unknown_notice_returns_404_without_db(self):
        self.set_user("cs_user")
        self.assertEqual(self.client.get("/notices/unknown-notice").status_code, 404)
        self.connect.assert_not_called()

    def test_home_notice_links_open_matching_details(self):
        self.set_user("cs_user")
        html = self.client.get("/home").get_data(as_text=True)
        section = html.split('aria-labelledby="notice-title"')[1].split('</section>')[0]
        self.assertEqual(section.count('<a '), 3)
        for slug in ("data-access-policy", "download-security", "access-scope"):
            self.assertIn(f'href="/notices/{slug}"', section)
            self.assertEqual(self.client.get(f"/notices/{slug}").status_code, 200)

    def test_anonymous_login_header_shows_brand_without_work_menus(self):
        for response in (self.client.get("/login"), self.client.post("/login", data={
            "username": "unknown", "password": "invalid",
        })):
            self.assertEqual(response.status_code, 200)
            header = response.get_data(as_text=True).split('<header class="app-header">')[1].split('</header>')[0]
            self.assertIn('aria-label="GIGANG 홈"', header)
            self.assertNotIn('<nav', header)
            self.assertNotIn('header-meta', header)
            for label in ("고객정보 조회", "내 권한", "감사 로그", "로그아웃"):
                self.assertNotIn(label, header)
        self.connect.assert_not_called()

    def test_home_sections_follow_priority_and_shortcuts_use_existing_routes(self):
        self.set_user("cs_user")
        html = self.client.get("/home").get_data(as_text=True)
        positions = [html.index(f'aria-labelledby="{name}-title"')
                     for name in ("activity", "notice", "shortcuts", "system", "security")]
        self.assertEqual(positions, sorted(positions))
        shortcuts = html.split('aria-labelledby="shortcuts-title"')[1].split('</section>')[0]
        self.assertEqual(shortcuts.count('<a '), 2)
        self.assertIn('href="/db"', shortcuts)
        self.assertIn('href="/permissions"', shortcuts)
        self.assertIn("고객지원 · 6개 정보 항목", shortcuts)
        for notice in ("업무 목적에 맞는 정보 조회", "다운로드 파일 취급", "접근 범위 확인"):
            self.assertIn(notice, html)
        for state in ("포털 서비스", "로그인 세션", "감사 로그 조회"):
            self.assertIn(f"<dt>{state}</dt>", html)

    def test_departments_and_permission_fields_follow_existing_scope(self):
        expected = {
            "cs_user": ("고객지원", 6, False, False),
            "hr_user": ("인사", 8, True, False),
            "fin_user": ("재무·정산", 7, False, True),
            "dev_user": ("개발/운영점검", 11, True, True),
            "admin_user": ("시스템 관리자", 11, True, True),
            "sales_user": ("공통 업무", 11, True, True),
            "report_user": ("공통 업무", 11, True, True),
        }
        for username, (department, count, rrn, card) in expected.items():
            with self.subTest(username=username):
                self.set_user(username)
                home = self.client.get("/home")
                self.assertEqual(home.status_code, 200)
                html = home.get_data(as_text=True)
                self.assertIn(f"{username}님, 환영합니다.", html)
                self.assertIn(department, html)
                self.assertIn(f"{count}개 정보 항목", html)
                permissions = self.client.get("/permissions").get_data(as_text=True)
                self.assertEqual("<li>주민등록번호</li>" in permissions, rrn)
                self.assertEqual("<li>신용카드번호</li>" in permissions, card)
                self.assertEqual(permissions.count('<li>'), count)
                for label in (">홈</a>", ">고객정보 조회</a>", ">내 권한</a>", ">로그아웃</a>"):
                    self.assertIn(label, html)
                self.assertEqual('href="/viewer"' in html, username == "admin_user")

    def test_recent_activity_is_filtered_to_current_identified_portal_account(self):
        self.set_user("cs_user")
        self.client.get("/home?portal_user=hr_user&db_user=report_user")
        connection, filters = self.activity.call_args.args
        self.assertIs(connection, self.connect.return_value)
        self.assertEqual(filters["portal_user"], "cs_user")
        self.assertEqual(filters["legacy"], "exclude")
        self.assertEqual(filters["page"], 1)
        self.connect.return_value.close.assert_called_once()

    def test_recent_activity_shows_five_records_and_no_sql(self):
        self.set_user("cs_user")
        self.activity.return_value = {
            "summary": {"total": 6, "db_select": 3, "excel_download": 3},
            "logs": [{"event_time": f"2026-10-30T12:00:0{index}",
                      "action": "DB_SELECT" if index % 2 else "EXCEL_DOWNLOAD",
                      "target_table": "customer_vault", "query_string": "SQL_SHOULD_NOT_BE_RENDERED"}
                     for index in range(6)],
        }
        html = self.client.get("/home").get_data(as_text=True)
        self.assertEqual(html.count("<time>"), 5)
        self.assertIn("2026-10-30 12:00:04", html)
        self.assertNotIn("2026-10-30 12:00:05", html)
        self.assertNotIn("SQL_SHOULD_NOT_BE_RENDERED", html)
        self.assertIn("일반 조회 3 · 엑셀 다운로드 3", html)
        self.assertIn("<strong>6건</strong>", html)

    def test_home_query_excludes_other_users_and_unidentified_records(self):
        from datetime import datetime
        connection = MemoryConnection()
        self.addCleanup(connection.db.close)
        for identity, hour in (("cs_user", 14), ("hr_user", 13), (None, 12), ("cs_user", 11)):
            marker = f"/* GIGANG_PORTAL_USER={identity} */ " if identity else ""
            connection.add_log(f"SELECT {marker}* FROM customer_vault LIMIT 50",
                               time=datetime(2026, 10, 30, hour))
        self.set_user("cs_user")
        with patch.object(server, "get_connection", return_value=connection), \
                patch.object(server.audit_viewer, "load_page", self.load_activity):
            response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("<strong>2건</strong>", html)
        self.assertIn("2026-10-30 14:00:00", html)
        self.assertIn("2026-10-30 11:00:00", html)
        self.assertNotIn("2026-10-30 13:00:00", html)
        self.assertNotIn("2026-10-30 12:00:00", html)
        self.assertTrue(connection.closed)

    def test_empty_activity_and_status_use_real_query_result(self):
        self.set_user("cs_user")
        html = self.client.get("/home").get_data(as_text=True)
        self.assertIn("아직 표시할 조회 활동이 없습니다.", html)
        self.assertIn("확인 가능", html)
        self.assertIn("외부 SOC의 상태를 의미하지 않습니다", html)

    def test_audit_connection_failure_keeps_home_available(self):
        self.set_user("cs_user")
        self.connect.side_effect = server.mysql.connector.Error("secret connection details")
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("최근 활동을 불러오지 못했습니다", html)
        self.assertIn("확인 불가", html)
        self.assertIn('href="/db"', html)
        self.assertNotIn("secret connection details", html)

    def test_audit_query_failure_closes_connection(self):
        self.set_user("cs_user")
        self.activity.side_effect = server.mysql.connector.Error("secret query details")
        response = self.client.get("/home")
        self.assertEqual(response.status_code, 200)
        self.connect.return_value.close.assert_called_once()
        self.assertNotIn("secret query details", response.get_data(as_text=True))

    def test_permissions_page_needs_no_db_connection(self):
        self.set_user("hr_user")
        self.assertEqual(self.client.get("/permissions").status_code, 200)
        self.connect.assert_not_called()
        self.activity.assert_not_called()

    def test_invalid_or_forged_admin_sessions_are_rejected(self):
        for username in ("unknown", "admin_user", "<script>alert(1)</script>"):
            self.set_user("cs_user")
            with self.client.session_transaction() as session:
                session["portal_user"] = username
            for path in ("/home", "/permissions", "/notices/data-access-policy"):
                self.assertEqual(self.client.get(path).status_code, 403)
        self.connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()

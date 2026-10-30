"""실제 DB 없이 SQL의 필터/집계/페이지 동작을 메모리 SQLite로 검증한다.

MySQL 전용 함수와 캐스트만 번역한다. MySQL 실행 계획/문법 검증을 대체하지 않는다.
"""

from datetime import datetime, timedelta
import re
import sqlite3
import unittest
from unittest.mock import Mock, patch

import audit_viewer
import server


class MemoryCursor:
    def __init__(self, db, dictionary=False):
        self.cursor = db.cursor()
        self.dictionary = dictionary

    def execute(self, sql, params=()):
        sql = sql.replace("CONVERT(argument USING utf8mb4)", "CAST(argument AS TEXT)")
        sql = sql.replace(" AS BINARY)", " AS BLOB)").replace("%s", "?")
        params = [value.isoformat(sep=" ") if isinstance(value, datetime) else value for value in params]
        self.cursor.execute(sql, params)

    def convert(self, row):
        if row is None or not self.dictionary:
            return row
        value = dict(zip((column[0] for column in self.cursor.description), row))
        if "event_time" in value:
            value["event_time"] = datetime.fromisoformat(value["event_time"])
        return value

    def fetchone(self):
        return self.convert(self.cursor.fetchone())

    def fetchall(self):
        return [self.convert(row) for row in self.cursor.fetchall()]

    def close(self):
        self.cursor.close()


class MemoryConnection:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.closed = False
        self.db.executescript("""
            ATTACH DATABASE ':memory:' AS mysql;
            CREATE TABLE mysql.general_log (
                event_time TEXT, thread_id INTEGER, user_host TEXT,
                command_type TEXT, argument TEXT
            );
            ATTACH DATABASE ':memory:' AS gigang_db;
            CREATE TABLE gigang_db.customer_vault (id INTEGER);
        """)
        self.db.executemany("INSERT INTO gigang_db.customer_vault VALUES (?)", [(n,) for n in range(100)])

        def substring_index(value, delimiter, count):
            if value is None:
                return None
            parts = value.split(delimiter)
            return delimiter.join(parts[:count] if count > 0 else parts[count:])

        def regexp_substr(value, pattern, position, occurrence, flags):
            if value is None:
                return None
            pattern = pattern.replace("[[:space:]]", r"\s")
            matches = list(re.finditer(pattern, value[position - 1:], re.I if flags == "i" else 0))
            return matches[occurrence - 1].group() if len(matches) >= occurrence else None

        self.db.create_function("SUBSTRING_INDEX", 3, substring_index)
        self.db.create_function("REGEXP_SUBSTR", 5, regexp_substr)
        self.db.create_function("REGEXP_LIKE", 3, lambda value, pattern, flags:
                                int(regexp_substr(value, pattern, 1, 1, flags) is not None))
        self.db.create_function("LOCATE", 2, lambda needle, value: value.find(needle) + 1)

    def add_log(self, sql, user="sales_user", command="Query", time=None, thread=1):
        self.db.execute("INSERT INTO mysql.general_log VALUES (?, ?, ?, ?, ?)", (
            (time or datetime(2026, 10, 30, 12)).isoformat(sep=" "), thread,
            f"{user}[{user}] @ localhost []", command, sql,
        ))

    def cursor(self, dictionary=False):
        return MemoryCursor(self.db, dictionary)

    def close(self):
        self.closed = True  # DB는 tearDown에서 닫아 여러 HTTP 요청의 상태를 보존한다.


class ViewerDashboardTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real DB forbidden")))
        self.connection = MemoryConnection()
        self.addCleanup(self.connection.db.close)
        self.connect = self.enterContext(patch.object(server, "get_connection", return_value=self.connection))
        self.enterContext(patch.object(server, "API_KEY", "test-key"))
        self.client = server.app.test_client()
        self.enterContext(patch.object(server, "ADMIN_PASSWORD", "fixture-admin-password"))
        self.client.post("/login", data={"username": "admin_user", "password": "fixture-admin-password"})

    def get_page(self, **filters):
        response = self.client.get("/api/viewer/db-logs", query_string=filters)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(self.connection.closed)
        return response.get_json()

    def populate(self):
        for index in range(127):
            user = "cs_user" if index % 2 == 0 else "hr_user"
            excel = "/* GIGANG_EXCEL_DOWNLOAD */ " if index % 3 == 0 else ""
            self.connection.add_log(
                f"SELECT {excel}/* GIGANG_PORTAL_USER={user} */ * FROM customer_vault LIMIT 50",
                time=datetime(2026, 10, 30, 12) + timedelta(seconds=index), thread=index,
            )

    def test_pagination_summary_order_and_out_of_range(self):
        self.populate()
        first = self.get_page()
        second = self.get_page(page=2)
        last = self.get_page(page=999)
        self.assertEqual(first["summary"], {"total": 127, "db_select": 84, "excel_download": 43, "portal_users": 2})
        self.assertEqual((first["page_size"], first["total_pages"], first["count"]), (50, 3, 50))
        self.assertEqual((second["page"], second["count"]), (2, 50))
        self.assertEqual((last["page"], last["count"]), (3, 27))
        self.assertFalse(first["has_previous"])
        self.assertTrue(first["has_next"])
        self.assertFalse(last["has_next"])
        all_logs = first["logs"] + second["logs"] + last["logs"]
        times = [row["event_time"] for row in all_logs]
        self.assertEqual(times, sorted(times, reverse=True))
        self.assertEqual(len(set(times)), 127)
        for row in all_logs:
            self.assertEqual(row["user_name"], "sales_user")
            self.assertEqual(row["db_user"], "sales_user")
            self.assertEqual(row["rows_affected"], 50)

    def test_combined_filters_apply_to_summary_and_page(self):
        self.populate()
        data = self.get_page(portal_user="cs_user", db_user="sales_user", action="EXCEL_DOWNLOAD",
                             legacy="exclude", start="2026-10-30T12:00:06", end="2026-10-30T12:00:18")
        self.assertEqual(data["summary"], {"total": 3, "db_select": 0, "excel_download": 3, "portal_users": 1})
        self.assertEqual(data["count"], 3)
        self.assertEqual(self.get_page(portal_user="cs_user", db_user="report_user")["total"], 0)

    def test_empty_results_and_first_page(self):
        data = self.get_page(page=10)
        self.assertEqual((data["page"], data["total_pages"], data["count"], data["total"]), (1, 1, 0, 0))
        self.assertEqual(data["logs"], [])
        self.assertEqual(data["summary"]["portal_users"], 0)

    def test_legacy_include_exclude_only_without_guessing(self):
        self.connection.add_log("SELECT * FROM customer_vault")
        self.connection.add_log("SELECT /* GIGANG_PORTAL_USER=cs_user */ * FROM customer_vault")
        self.assertEqual(self.get_page()["total"], 2)
        self.assertEqual(self.get_page(legacy="exclude")["total"], 1)
        legacy = self.get_page(legacy="only")
        self.assertEqual(legacy["total"], 1)
        self.assertIsNone(legacy["logs"][0]["portal_user"])
        self.assertEqual(legacy["logs"][0]["db_user"], "sales_user")
        self.assertEqual(legacy["summary"]["portal_users"], 0)

    def test_sql_identity_matches_existing_parser_for_edge_cases(self):
        queries = [
            "SELECT /* GIGANG_PORTAL_USER=cs_user */ * FROM customer_vault",
            "select /* gigang_portal_user=cs_user */ * FROM customer_vault",
            "SELECT /* GIGANG_EXCEL_DOWNLOAD */ /* GIGANG_PORTAL_USER=fin_user */ * FROM customer_vault",
            "SELECT\t/* GIGANG_PORTAL_USER=dev_user\t*/ * FROM customer_vault",
            "SELECT /* GIGANG_PORTAL_USER=CS_USER */ * FROM customer_vault",
            "SELECT /* GIGANG_PORTAL_USER=unknown */ * FROM customer_vault",
            "SELECT '/* GIGANG_PORTAL_USER=hr_user */' FROM customer_vault",
            "SELECT * FROM customer_vault /* GIGANG_PORTAL_USER=hr_user */",
            "SELECT /* GIGANG_PORTAL_USER=report_user */ * FROM customer_vault",
        ]
        for index, sql in enumerate(queries):
            self.connection.add_log(sql, thread=index)
        for row in self.get_page()["logs"]:
            self.assertEqual(row["portal_user"], server.portal_user_from_query(row["query_string"], row["db_user"]))
            self.assertEqual(row["action"], "EXCEL_DOWNLOAD" if "GIGANG_EXCEL_DOWNLOAD" in row["query_string"] else "DB_SELECT")

    def test_allowed_portal_db_mapping_for_all_accounts(self):
        for user in server.ALLOWED_PORTAL_USERS:
            self.connection.add_log(f"SELECT /* GIGANG_PORTAL_USER={user} */ * FROM customer_vault", user=server.db_user_for(user))
        data = self.get_page()
        self.assertEqual(data["summary"]["portal_users"], 7)
        self.assertEqual({row["portal_user"] for row in data["logs"]}, server.ALLOWED_PORTAL_USERS)

    def test_excluded_tables_commands_and_accounts(self):
        self.connection.add_log("SELECT * FROM users")
        self.connection.add_log("SELECT * FROM customer_vault", user="root")
        self.connection.add_log("SELECT * FROM customer_vault", command="Execute")
        self.connection.add_log("UPDATE customer_vault SET id = 1")
        self.assertEqual(self.get_page()["total"], 0)

    def test_invalid_filters_never_connect(self):
        for filters in (
            {"page": "0"}, {"page": "-1"}, {"page": "abc"}, {"page": "1000001"},
            {"portal_user": "cs_user */ DROP TABLE x"}, {"db_user": "root"},
            {"action": "HIGH"}, {"legacy": "delete"}, {"start": "yesterday"},
            {"start": "2026-10-30T00:00:00Z"}, {"end": "0999-01-01"},
            {"start": "2026-10-31", "end": "2026-10-30"},
        ):
            with self.subTest(filters=filters):
                response = self.client.get("/api/viewer/db-logs", query_string=filters)
                self.assertEqual(response.status_code, 400)
        self.connect.assert_not_called()

    def test_authentication_required_before_query(self):
        self.client.get("/logout")
        for headers in ({}, {"X-API-Key": "wrong"}):
            self.assertEqual(self.client.get("/api/viewer/db-logs", headers=headers).status_code, 401)
        self.connect.assert_not_called()

    def test_database_failure_is_isolated_and_does_not_leak_error(self):
        self.connect.side_effect = server.mysql.connector.Error("secret database error")
        response = self.client.get("/api/viewer/db-logs")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("secret", response.get_data(as_text=True))
        self.connect.side_effect = None
        with patch.object(audit_viewer, "load_page", side_effect=server.mysql.connector.Error("failure")):
            self.assertEqual(self.client.get("/api/viewer/db-logs").status_code, 503)
        self.assertTrue(self.connection.closed)
        with patch.object(server, "load_logs", return_value=[]):
            self.assertEqual(self.client.get("/api/db-logs", headers={"X-API-Key": "test-key"}).status_code, 200)

    def test_filters_are_bound_and_page_has_sql_limit(self):
        filters = audit_viewer.parse_filters({"portal_user": "cs_user", "db_user": "sales_user",
                                            "action": "EXCEL_DOWNLOAD", "start": "2026-10-30"},
                                           server.ALLOWED_PORTAL_USERS, server.TARGET_USERS)
        sql, params = audit_viewer.build_query(filters, server.TARGET_USERS,
            {user: server.db_user_for(user) for user in server.ALLOWED_PORTAL_USERS},
            server.SENSITIVE_TABLES, server.PORTAL_USER_PREFIX.pattern)
        self.assertNotIn("cs_user", sql)
        self.assertIn("portal_user = %s", sql)
        self.assertIn("event_time >= %s", sql)
        self.assertEqual(sql.count("%s"), len(params))
        fake = Mock()
        fake.cursor.return_value.fetchone.return_value = {"total": 101, "db_select": 101, "excel_download": 0, "portal_users": 1}
        fake.cursor.return_value.fetchall.return_value = []
        filters["page"] = 2
        audit_viewer.load_page(fake, filters, target_users=server.TARGET_USERS,
            portal_db_users={"cs_user": "sales_user"}, tables=server.SENSITIVE_TABLES,
            prefix_pattern=server.PORTAL_USER_PREFIX.pattern, table_counter=Mock(), row_estimator=Mock())
        page_sql, page_params = fake.cursor.return_value.execute.call_args.args
        self.assertIn("LIMIT %s OFFSET %s", page_sql)
        self.assertEqual(page_params[-2:], [50, 50])

    def test_portal_empty_states_permissions_and_escaped_links(self):
        with self.client.session_transaction() as session:
            session["portal_user"] = "cs_user"
        with patch.object(server, "fetch_customer_rows", return_value=[]) as fetch:
            html = self.client.get("/db?view=dev").get_data(as_text=True)
            self.assertIn("조회할 고객정보를 검색하세요", html)
            self.assertNotIn("주민등록번호", html)
            self.assertNotIn('aria-label="정보 화면"', html)
            fetch.assert_not_called()
            html = self.client.get("/db", query_string={"search": "1", "view": "dev", "keyword": 'a&view=dev<script>'}).get_data(as_text=True)
            self.assertIn("검색 결과가 없습니다", html)
            self.assertNotIn("<script>", html)
            self.assertIn("%26view%3Ddev%3Cscript%3E", html)
            fetch.assert_called_once_with("cs_user", "cs", 'a&view=dev<script>', "name")


if __name__ == "__main__":
    unittest.main()

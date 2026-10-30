"""실제 로그인→SQL 생성→DB 계정 매핑→감사 파싱을 DB 연결 없이 추적한다."""

from datetime import datetime
import secrets
import unittest
from unittest.mock import Mock, patch

import server
from test_viewer_dashboard import MemoryConnection, MemoryCursor


class TraceCursor(MemoryCursor):
    def convert(self, row):
        if row is not None and not self.dictionary:
            names = [column[0] for column in self.cursor.description]
            if "event_time" in names:
                values = list(row)
                index = names.index("event_time")
                values[index] = datetime.fromisoformat(values[index])
                return tuple(values)
        return super().convert(row)


class TraceConnection(MemoryConnection):
    def cursor(self, dictionary=False):
        return TraceCursor(self.db, dictionary)


class AuditIdentityTraceTests(unittest.TestCase):
    def setUp(self):
        self.audit = TraceConnection()
        self.addCleanup(self.audit.db.close)
        self.enterContext(patch.object(server, "get_connection", return_value=self.audit))
        self.password = secrets.token_urlsafe(32)
        self.enterContext(patch.object(server, "ADMIN_PASSWORD", self.password))
        self.enterContext(patch.dict(server.PORTAL_USERS, {
            "sales_user": self.password, "report_user": self.password,
        }))
        for account in server.DEPT_ACCOUNTS.values():
            self.enterContext(patch.dict(account, password=self.password))
        self.enterContext(patch.object(server, "DB_USER", "fixture_log_reader"))
        self.enterContext(patch.object(server, "API_KEY", "fixture-api-key"))
        self.statements = []
        self.db_connections = []

        def fake_connect(**kwargs):
            self.db_connections.append(kwargs["user"])
            connection = Mock()
            cursor = connection.cursor.return_value
            cursor.fetchall.return_value = []

            def execute(sql, params=()):
                self.statements.append((sql, params, kwargs["user"]))
                self.audit.add_log(sql, user=kwargs["user"], thread=len(self.statements),
                                   time=datetime(2026, 10, 30, 13, 0, len(self.statements)))

            cursor.execute.side_effect = execute
            return connection

        # get_user_connection은 실제 코드를 실행하고 connector 경계의 연결만 대체한다.
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=fake_connect))

    def log_in(self, username):
        client = server.app.test_client()
        response = client.post("/login", data={"username": username, "password": self.password})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            self.assertEqual(session["portal_user"], username)
        return client

    def test_all_shared_db_accounts_preserve_session_identity_for_query_and_excel(self):
        for username in ("cs_user", "hr_user", "fin_user", "dev_user", "admin_user"):
            with self.subTest(username=username):
                client = self.log_in(username)
                for path, action in (("/db", "DB_SELECT"), ("/db/download", "EXCEL_DOWNLOAD")):
                    response = client.get(path, query_string={
                        "search": "1", "portal_user": "sales_user", "username": "sales_user",
                        "db_user": "sales_user", "keyword": "sales_user /* GIGANG_PORTAL_USER=sales_user */",
                    })
                    self.assertEqual(response.status_code, 200)
                    sql, params, db_user = self.statements[-1]
                    prefix = "SELECT " + ("/* GIGANG_EXCEL_DOWNLOAD */ " if action == "EXCEL_DOWNLOAD" else "")
                    self.assertTrue(sql.startswith(prefix + f"/* GIGANG_PORTAL_USER={username} */ "))
                    self.assertEqual(sql.count("GIGANG_PORTAL_USER="), 1)
                    self.assertEqual(params, ("%sales_user /* GIGANG_PORTAL_USER=sales_user */%",))
                    self.assertEqual(db_user, "sales_user")
                    with client.session_transaction() as session:
                        self.assertEqual(session["portal_user"], username)
                    # 기존 두 Python 감사 경로에도 DB 계정명으로의 대체가 없어야 한다.
                    for logs in (server.load_logs(user_name=username),
                                 server.load_logs_since(datetime(2026, 10, 1))):
                        matching = [log for log in logs if log["query_string"] == sql]
                        self.assertEqual(len(matching), 1)
                        log = matching[0]
                        self.assertEqual(log["portal_user"], username)
                        self.assertEqual(log["db_user"], "sales_user")
                        self.assertEqual(log["user_name"], "sales_user")
                        self.assertEqual(log["action"], action)
        self.assertEqual(len(self.statements), 10)
        self.assertEqual(set(self.db_connections), {"sales_user"})
        admin = self.log_in("admin_user")
        data = admin.get("/api/viewer/db-logs").get_json()
        self.assertEqual(data["total"], 10)
        self.assertEqual(data["summary"]["portal_users"], 5)
        self.assertEqual(data["summary"]["db_select"], 5)
        self.assertEqual(data["summary"]["excel_download"], 5)
        self.assertEqual({log["portal_user"] for log in data["logs"]},
                         {"cs_user", "hr_user", "fin_user", "dev_user", "admin_user"})

    def test_historical_sales_marker_is_kept_and_unmarked_db_user_is_not_inferred(self):
        historical = "SELECT /* GIGANG_PORTAL_USER=sales_user */ * FROM customer_vault LIMIT 50"
        self.audit.add_log(historical, time=datetime(2026, 10, 30, 12, 7, 3, 657769))
        unmarked = "SELECT * FROM customer_vault LIMIT 1"
        self.audit.add_log(unmarked, time=datetime(2026, 10, 29))
        cs = self.log_in("cs_user")
        self.assertEqual(cs.get("/db?search=1").status_code, 200)
        for logs in (server.load_logs(), server.load_logs_since(datetime(2026, 10, 1))):
            by_sql = {log["query_string"]: log for log in logs}
            self.assertEqual(by_sql[historical]["portal_user"], "sales_user")
            self.assertIsNone(by_sql[unmarked]["portal_user"])
            self.assertEqual(by_sql[self.statements[0][0]]["portal_user"], "cs_user")
            self.assertEqual({log["db_user"] for log in logs}, {"sales_user"})
        admin = self.log_in("admin_user")
        all_logs = admin.get("/api/viewer/db-logs").get_json()
        self.assertEqual(all_logs["total"], 3)
        self.assertEqual({log["portal_user"] for log in all_logs["logs"]}, {"cs_user", "sales_user", None})
        filtered = admin.get("/api/viewer/db-logs?portal_user=cs_user").get_json()
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["logs"][0]["portal_user"], "cs_user")
        with cs.session_transaction() as session:
            self.assertEqual(session["portal_user"], "cs_user")


if __name__ == "__main__":
    unittest.main()

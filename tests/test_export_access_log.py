"""가짜 커서와 임시 폴더만 사용합니다. 실제 DB와 access_log.csv는 건드리지 않습니다."""

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from export_access_log import COLUMNS, TARGET_USERS, is_business_select, save_csv


def log(sql, user="sales_user", command="Query"):
    return ("2026-09-10 12:00:00", f"{user}[{user}] @ localhost []", command, sql)


class Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def fetchmany(self, size):
        batch, self.rows = self.rows[:size], self.rows[size:]
        return batch


class ExportTests(unittest.TestCase):
    def test_system_queries_excluded(self):
        queries = [
            "select @@version_comment limit 1",
            " \nSeLeCt\t@@session.autocommit",
            "SELECT /* client */ @@version",
            "SELECT(@@version)",
            "SELECT VERSION()",
            "SELECT DATABASE()",
            "SELECT USER()",
            "SELECT CURRENT_USER()",
            "SELECT CONNECTION_ID()",
            "SELECT * FROM mysql.user",
            "SELECT * FROM `INFORMATION_SCHEMA` . `TABLES`",
            "SELECT * FROM performance_schema.threads",
            "SELECT * FROM sys.session",
        ]
        for sql in queries:
            with self.subTest(sql=sql):
                self.assertFalse(is_business_select(log(sql)))

    def test_business_queries_kept_for_each_user(self):
        queries = [
            "SELECT * FROM gigang_db.users",
            "select count(*) from users",
            "SELECT name FROM users WHERE id = 1",
            "SELECT 'mysql.user, @@version_comment' AS note FROM users",
            "SELECT name /* mysql.user */ FROM `gigang_db`.`users`",
            "SELECT '한글, 값' AS note\nFROM users",
        ]
        for user in TARGET_USERS:
            for sql in queries:
                with self.subTest(user=user, sql=sql):
                    self.assertTrue(is_business_select(log(sql, user)))

    def test_other_accounts_and_commands_excluded(self):
        self.assertFalse(is_business_select(log("SELECT * FROM users", "root")))
        self.assertFalse(
            is_business_select(log("SELECT * FROM users", "sales_user_extra"))
        )
        self.assertFalse(
            is_business_select(log("SELECT * FROM users", command="Execute"))
        )
        self.assertFalse(is_business_select(log("UPDATE users SET name = 'test'")))

    def test_csv_contains_only_sensitive_business_rows_with_audit_columns(self):
        sql = "SELECT '한글, 값' AS note\nFROM customer_vault LIMIT 5"
        keep = [
            log(sql, user) for user in TARGET_USERS
        ]
        rows = [log("select @@version_comment limit 1"), log("SELECT * FROM users")] + keep
        expected_columns = [
            "event_time", "user_name", "src_ip", "action",
            "target_table", "query_string", "rows_affected",
        ]
        expected_rows = [
            ["2026-09-10T12:00:00", user, "127.0.0.1", "DB_SELECT",
             "customer_vault", sql, "5"]
            for user in TARGET_USERS
        ]
        with patch("export_access_log.TABLE_ROW_COUNTS", {"customer_vault": 10}), \
                tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access_log.csv"
            self.assertEqual(save_csv(Cursor(rows), path), 3)
            with path.open(encoding="utf-8-sig", newline="") as source:
                self.assertEqual(
                    list(csv.reader(source)),
                    [expected_columns] + expected_rows,
                )

    def test_all_system_queries_produce_header_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "access_log.csv"
            self.assertEqual(save_csv(Cursor([log("SELECT @@version")]), path), 0)
            with path.open(encoding="utf-8-sig", newline="") as source:
                self.assertEqual(list(csv.reader(source)), [list(COLUMNS)])


if __name__ == "__main__":
    unittest.main()

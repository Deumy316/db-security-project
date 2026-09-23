"""모든 테스트는 DB 연결과 sleep을 mock 처리합니다."""

from contextlib import redirect_stdout, redirect_stderr
import io
import unittest
from unittest.mock import MagicMock, call, patch
import run_scenarios as runner


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        self.connect = self.enterContext(patch.object(runner, "connect_database"))
        self.sleep = self.enterContext(patch.object(runner.time, "sleep"))
        self.password = self.enterContext(
            patch.object(runner.getpass, "getpass", return_value="test-secret")
        )
        self.connection = self.connect.return_value.__enter__.return_value
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchmany.return_value = []
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(redirect_stderr(self.output))

    def test_agreed_scenarios(self):
        expected = {
            "S_N01": (
                "sales_user",
                "정상",
                "SELECT * FROM gigang_db.users LIMIT 1;",
                5,
                10,
            ),
            "S_N02": (
                "sales_user",
                "정상",
                "SELECT * FROM gigang_db.users LIMIT 5;",
                3,
                20,
            ),
            "S_A01": (
                "sales_user",
                "비정상",
                "SELECT * FROM gigang_db.users LIMIT 1;",
                100,
                0,
            ),
            "S_A02": ("sales_user", "비정상", "SELECT * FROM gigang_db.users;", 5, 1),
            "R_N01": (
                "report_user",
                "정상",
                "SELECT COUNT(*) AS user_count FROM gigang_db.users;",
                3,
                30,
            ),
            "R_N02": (
                "report_user",
                "정상",
                "SELECT * FROM gigang_db.users LIMIT 10;",
                3,
                20,
            ),
            "R_A01": (
                "report_user",
                "비정상",
                "SELECT COUNT(*) AS user_count FROM gigang_db.users;",
                100,
                0,
            ),
            "R_A02": ("report_user", "비정상", "SELECT * FROM gigang_db.users;", 5, 1),
        }
        self.assertEqual(set(runner.SCENARIOS), set(expected))
        for key, values in expected.items():
            item = runner.SCENARIOS[key]
            self.assertEqual(
                (item.account, item.label, item.sql, item.repetitions, item.interval),
                values,
            )

    def test_list_without_connection_or_password(self):
        self.assertEqual(runner.main(["--list"]), 0)
        self.connect.assert_not_called()
        self.password.assert_not_called()
        self.sleep.assert_not_called()
        for key in runner.SCENARIOS:
            self.assertIn(key, self.output.getvalue())

    def test_all_execution_counts_intervals_and_data_privacy(self):
        for key, item in runner.SCENARIOS.items():
            with self.subTest(key=key):
                self.connect.reset_mock()
                self.sleep.reset_mock()
                self.cursor.fetchmany.side_effect = [
                    [("private-user-data",)],
                    [],
                ] * item.repetitions
                self.assertEqual(runner.main(["--scenario", key]), 0)
                self.assertEqual(self.connect.call_args.kwargs["user"], item.account)
                self.assertEqual(
                    self.connect.call_args.kwargs["password"], "test-secret"
                )
                self.assertEqual(
                    self.cursor.execute.call_args_list,
                    [call(item.sql)] * item.repetitions,
                )
                self.assertEqual(self.cursor.fetchmany.call_count, 2 * item.repetitions)
                waits = (
                    [call(item.interval)] * (item.repetitions - 1)
                    if item.interval
                    else []
                )
                self.assertEqual(self.sleep.call_args_list, waits)
                self.connection.cursor.return_value.__exit__.assert_called_once()
                self.connect.return_value.__exit__.assert_called_once()
                self.assertIn(
                    f"[{key}] 계정={item.account} | {item.label}",
                    self.output.getvalue(),
                )
                self.assertIn(
                    f"완료 횟수={item.repetitions}/{item.repetitions}",
                    self.output.getvalue(),
                )
        for text in ("시작 시각=", "종료 시각="):
            self.assertIn(text, self.output.getvalue())
        for text in ("private-user-data", "test-secret"):
            self.assertNotIn(text, self.output.getvalue())

    def test_query_error_stops_and_closes_without_leaking(self):
        self.cursor.execute.side_effect = RuntimeError("test-secret private-user-data")
        self.assertEqual(runner.main(["--scenario", "S_N01"]), 1)
        self.cursor.execute.assert_called_once()
        self.sleep.assert_not_called()
        self.connect.return_value.__exit__.assert_called_once()
        self.assertIn("완료 횟수=0/5", self.output.getvalue())
        self.assertNotIn("test-secret", self.output.getvalue())
        self.assertNotIn("private-user-data", self.output.getvalue())

    def test_connection_failure(self):
        self.connect.side_effect = RuntimeError("test-secret")
        self.assertEqual(runner.main(["--scenario", "R_N01"]), 1)
        self.assertIn("종료 시각=", self.output.getvalue())
        self.assertNotIn("test-secret", self.output.getvalue())
        self.cursor.execute.assert_not_called()

    def test_interrupt_during_wait(self):
        self.sleep.side_effect = KeyboardInterrupt
        self.assertEqual(runner.main(["--scenario", "S_N01"]), 130)
        self.cursor.execute.assert_called_once()
        self.assertIn("완료 횟수=1/5", self.output.getvalue())
        self.assertIn("상태=취소", self.output.getvalue())
        self.connect.return_value.__exit__.assert_called_once()

    def test_password_failure_never_connects(self):
        self.password.side_effect = EOFError
        self.assertEqual(runner.main(["--scenario", "S_N01"]), 1)
        self.connect.assert_not_called()

    def test_invalid_arguments_never_connect(self):
        for args in (
            [],
            ["--scenario", "unknown"],
            ["--list", "--scenario", "S_N01"],
            ["--group", "unknown"],
            ["--group", "abnormal", "--scenario", "S_N01"],
            ["--list", "--group", "abnormal"],
        ):
            with self.subTest(args=args), self.assertRaises(SystemExit):
                runner.main(args)
        self.connect.assert_not_called()
        self.password.assert_not_called()

    def test_abnormal_group_order_accounts_counts_and_output(self):
        managers = [MagicMock(), MagicMock()]
        self.connect.side_effect = managers
        self.password.side_effect = ["sales-secret", "report-secret"]
        events = []
        for account, manager in zip(("sales_user", "report_user"), managers):
            cursor = (
                manager.__enter__.return_value.cursor.return_value.__enter__.return_value
            )
            cursor.execute.side_effect = lambda sql, account=account: events.append(
                (account, sql)
            )
            cursor.fetchmany.side_effect = [[("private-user-data",)], []] * 105
        self.assertEqual(runner.main(["--group", "abnormal"]), 0)
        order = ("S_A01", "S_A02", "R_A01", "R_A02")
        expected = []
        for key in order:
            item = runner.SCENARIOS[key]
            expected.extend([(item.account, item.sql)] * item.repetitions)
        self.assertEqual(events, expected)
        self.assertEqual(
            self.password.call_args_list,
            [call("MySQL sales_user 비밀번호: "), call("MySQL report_user 비밀번호: ")],
        )
        self.assertEqual(
            [c.kwargs["user"] for c in self.connect.call_args_list],
            ["sales_user", "report_user"],
        )
        self.assertEqual(
            [c.kwargs["password"] for c in self.connect.call_args_list],
            ["sales-secret", "report-secret"],
        )
        self.assertEqual(self.sleep.call_args_list, [call(1)] * 8)
        for manager in managers:
            manager.__exit__.assert_called_once()
        lines = self.output.getvalue().splitlines()
        self.assertEqual(
            [line.split("]")[0][1:] for line in lines if "시작 시각=" in line],
            list(order),
        )
        self.assertEqual(
            [line.split("]")[0][1:] for line in lines if "종료 시각=" in line],
            list(order),
        )
        for key in order:
            item = runner.SCENARIOS[key]
            self.assertTrue(
                any(
                    f"[{key}]" in line
                    and f"완료 횟수={item.repetitions}/{item.repetitions}" in line
                    for line in lines
                )
            )
        for secret in ("sales-secret", "report-secret", "private-user-data"):
            self.assertNotIn(secret, self.output.getvalue())

    def test_group_failure_does_not_start_remaining_scenarios(self):
        self.cursor.execute.side_effect = [None] * 100 + [RuntimeError("failed")]
        self.assertEqual(runner.main(["--group", "abnormal"]), 1)
        self.assertEqual(self.cursor.execute.call_count, 101)
        self.password.assert_called_once()
        self.connect.assert_called_once()
        self.connect.return_value.__exit__.assert_called_once()
        self.assertNotIn("[R_A01]", self.output.getvalue())
        self.assertIn("완료 횟수=0/5", self.output.getvalue())

    def test_group_interrupt_stops_remaining_scenarios(self):
        self.sleep.side_effect = KeyboardInterrupt
        self.assertEqual(runner.main(["--group", "abnormal"]), 130)
        self.password.assert_called_once()
        self.assertNotIn("[R_A01]", self.output.getvalue())
        self.connect.return_value.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()

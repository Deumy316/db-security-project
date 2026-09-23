"""등록된 SELECT 시나리오를 실행합니다. 로그 CSV 수집은 별도 프로그램에서 수행합니다."""

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
import getpass
import sys
import time
import warnings


@dataclass(frozen=True)
class Scenario:
    account: str
    label: str
    sql: str
    repetitions: int
    interval: int


# 임의 SQL을 입력받지 않고 합의한 SELECT만 실행합니다.
SCENARIOS = {
    # sales_user 정상 조회
    "S_N01": Scenario(
        "sales_user",
        "정상",
        "SELECT user_id, customer_name " "FROM gigang_db.customer_vault LIMIT 1;",
        5,
        10,
    ),
    "S_N02": Scenario(
        "sales_user",
        "정상",
        "SELECT user_id, customer_name, balance "
        "FROM gigang_db.customer_vault LIMIT 5;",
        3,
        20,
    ),
    # sales_user 비정상 조회
    "S_A01": Scenario(
        "sales_user",
        "비정상",
        "SELECT user_id, customer_name, rrn, credit_card_num, balance "
        "FROM gigang_db.customer_vault LIMIT 10;",
        100,
        0,
    ),
    "S_A02": Scenario(
        "sales_user",
        "비정상",
        "SELECT * FROM gigang_db.customer_vault;",
        5,
        1,
    ),
    # report_user 정상 조회
    "R_N01": Scenario(
        "report_user",
        "정상",
        "SELECT COUNT(*) AS customer_count " "FROM gigang_db.customer_vault;",
        3,
        30,
    ),
    "R_N02": Scenario(
        "report_user",
        "정상",
        "SELECT customer_name, balance " "FROM gigang_db.customer_vault LIMIT 10;",
        3,
        20,
    ),
    # report_user 비정상 조회
    "R_A01": Scenario(
        "report_user",
        "비정상",
        "SELECT user_id, customer_name, rrn, credit_card_num, balance "
        "FROM gigang_db.customer_vault;",
        100,
        0,
    ),
    "R_A02": Scenario(
        "report_user",
        "비정상",
        "SELECT * FROM gigang_db.customer_vault;",
        5,
        1,
    ),
}

GROUPS = {
    "normal": (
        "S_N01",
        "S_N02",
        "R_N01",
        "R_N02",
    ),
    "abnormal": (
        "S_A01",
        "S_A02",
        "R_A01",
        "R_A02",
    ),
}


def timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect_database(**kwargs):
    # 목록 조회와 mock 테스트에는 MySQL 패키지나 서버가 필요하지 않습니다.
    import mysql.connector

    return mysql.connector.connect(**kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--list", action="store_true", help="접속 없이 목록 표시")
    selection.add_argument("--scenario", choices=SCENARIOS, help="실행할 시나리오 ID")
    selection.add_argument(
        "--group", choices=GROUPS, help="시나리오 그룹을 순서대로 실행"
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=3306)
    args = parser.parse_args(argv)
    if args.list:
        for key, item in SCENARIOS.items():
            print(
                f"{key} | {item.account} | {item.label} | {item.repetitions}회 | 대기 {item.interval}초"
            )
            print(f"  {item.sql}")
        return 0

    keys = GROUPS[args.group] if args.group else (args.scenario,)
    # 같은 계정은 연결을 공유합니다. 계정 변경/중단/종료 시 연결을 닫습니다.
    with ExitStack() as connections:
        session = {"account": None, "connection": None}
        for key in keys:
            result = run_scenario(key, args, connections, session)
            if result:
                return result
    return 0


def run_scenario(key, args, connections, session):
    """시나리오별 진행을 기록하고 계정이 바뀔 때만 다시 인증합니다."""
    item = SCENARIOS[key]
    prefix = f"[{key}] 계정={item.account} | {item.label}"
    completed, status = 0, "실패"
    print(f"{prefix} | 시작 시각={timestamp()}")
    print(f"[{key}] SQL={item.sql}")
    print(f"[{key}] 반복={item.repetitions}회 | 대기={item.interval}초")
    try:
        if session["account"] != item.account:
            connections.close()
            session.update(account=None, connection=None)
            # 비밀번호는 연결 직후 버리고 계정 간에 보관하지 않습니다.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass(f"MySQL {item.account} 비밀번호: ")
            connection = connections.enter_context(
                connect_database(
                    host=args.host,
                    port=args.port,
                    user=item.account,
                    password=password,
                    charset="utf8mb4",
                    connection_timeout=10,
                    autocommit=True,
                )
            )
            del password
            session.update(account=item.account, connection=connection)
        with session["connection"].cursor() as cursor:
            for number in range(1, item.repetitions + 1):
                cursor.execute(item.sql)
                # 다음 조회 전에 결과를 끝까지 읽습니다. 데이터는 출력/저장하지 않습니다.
                while cursor.fetchmany(1000):
                    pass
                completed = number
                print(f"{prefix} | {completed}/{item.repetitions} 완료", flush=True)
                # 마지막 조회 뒤에는 대기하지 않습니다.
                if number < item.repetitions and item.interval > 0:
                    time.sleep(item.interval)
        status = "완료"
        return 0
    except (getpass.GetPassWarning, EOFError):
        print(
            "PowerShell에서 비밀번호를 직접 숨김 입력할 수 있는지 확인하세요.",
            file=sys.stderr,
        )
        return 1
    except ImportError:
        print(
            "패키지 설치 필요: py -m pip install -r requirements.txt", file=sys.stderr
        )
        return 1
    except KeyboardInterrupt:
        status = "취소"
        print("사용자가 실행을 취소했습니다.", file=sys.stderr)
        return 130
    except Exception:
        # 접속 정보나 데이터가 포함될 수 있는 예외 원문은 출력하지 않습니다.
        print(
            "실행 실패: 서버, 접속 정보, SELECT 권한을 확인하세요. 자동 재시도하지 않습니다.",
            file=sys.stderr,
        )
        return 1
    finally:
        print(
            f"{prefix} | 종료 시각={timestamp()} | 상태={status} | 완료 횟수={completed}/{item.repetitions}"
        )


if __name__ == "__main__":
    sys.exit(main())

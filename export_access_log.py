"""MySQL general_log에서 실험 계정의 SELECT 요청을 CSV로 저장합니다."""

import argparse
import csv
import getpass
import re
from pathlib import Path
import sys
import tempfile
import warnings

TARGET_USERS = ("sales_user", "report_user", "analyst_user")
COLUMNS = (
    "event_time",
    "user_name",
    "src_ip",
    "action",
    "target_table",
    "query_string",
    "rows_affected",
)
OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "mysql_audit_log.csv"

# user_host는 보통 'sales_user[sales_user] @ localhost [::1]' 형태입니다.
# '[' 앞의 로그인 계정명을 정확히 비교하므로 비슷한 이름은 포함하지 않습니다.
# argument는 BLOB일 수 있어 UTF-8 문자열로 변환한 뒤 SELECT 여부를 검사합니다.
# 현재 연결을 제외하여 수집 프로그램 자체의 조회는 CSV에 넣지 않습니다.
SELECT_LOGS_SQL = """
SELECT event_time,
       user_host,
       command_type,
       CONVERT(argument USING utf8mb4) AS argument
FROM mysql.general_log
WHERE command_type = 'Query'
  AND REGEXP_LIKE(CONVERT(argument USING utf8mb4),
                  '^[[:space:]]*SELECT([[:space:]]|/|[(]|$)', 'i')
  AND TRIM(SUBSTRING_INDEX(user_host, '[', 1)) IN (%s, %s, %s)
  AND thread_id <> CONNECTION_ID()
ORDER BY event_time, thread_id
"""


# 문자열 내용은 필터 판단에서 숨겨 'mysql.users' 같은 텍스트의 오탐을 줄입니다.
# 백틱 식별자는 유지하고 일반 주석은 공백으로 바꿉니다.
SQL_PARTS = re.compile(
    r"'(?:(?:'')|(?:\\.)|[^'\\])*'|\"(?:(?:\"\")|(?:\\.)|[^\"\\])*\""
    r"|`(?:``|[^`])*`|/\*.*?\*/|\#[^\r\n]*|--(?=\s)[^\r\n]*",
    re.DOTALL,
)
SYSTEM_START = re.compile(
    r"^\s*SELECT\b\s*\(*\s*(?:@@|"
    r"(?:VERSION|DATABASE|USER|CURRENT_USER|SESSION_USER|SYSTEM_USER|"
    r"CONNECTION_ID|SCHEMA)\s*\(\s*\))",
    re.IGNORECASE,
)
SYSTEM_SCHEMA = re.compile(
    r"(?<![\w$])(?:mysql|information_schema|performance_schema|sys)\s*\.",
    re.IGNORECASE,
)


def is_business_select(row):
    """알려진 시스템 조회를 제외합니다. 로그만으로 실행 의도는 판별할 수 없습니다."""
    _, user_host, command_type, argument = row
    # DB의 조건에 더해 CSV 저장 직전에도 요청 종류와 계정을 확인합니다.
    if (
        command_type != "Query"
        or user_host.split("[", 1)[0].strip() not in TARGET_USERS
    ):
        return False
    if not isinstance(argument, str) or not re.match(r"^\s*SELECT\b", argument, re.I):
        return False

    def mask_part(match):
        part = match.group()
        if part.startswith("`"):
            return part[1:-1].replace("``", "`")
        if part.startswith(("'", '"')):
            return " STRING_LITERAL "
        return " "

    filtered_sql = SQL_PARTS.sub(mask_part, argument)
    return not (SYSTEM_START.search(filtered_sql) or SYSTEM_SCHEMA.search(filtered_sql))


SENSITIVE_TABLES = (
    "customer_vault",
    "customer_info",
    "corp_strategic_plan",
    "salary_2026",
    "secret_key",
)

TABLE_ROW_COUNTS = {}


def load_table_row_counts(cursor):
    """현재 테스트 테이블의 실제 데이터 건수를 읽습니다."""
    counts = {}

    cursor.execute("SELECT COUNT(*) FROM gigang_db.customer_vault")

    result = cursor.fetchone()
    counts["customer_vault"] = int(result[0])

    return counts


def convert_to_gigang_row(row):
    event_time, user_host, command_type, argument = row

    if hasattr(event_time, "strftime"):
        event_time = event_time.strftime("%Y-%m-%dT%H:%M:%S")
    else:
        event_time = str(event_time).replace(" ", "T").split(".")[0]

    user_name = user_host.split("[", 1)[0].strip()
    src_ip = "127.0.0.1"
    action = "DB_SELECT"

    query_lower = argument.lower()

    target_table = ""

    for table in SENSITIVE_TABLES:
        if table.lower() in query_lower:
            target_table = table
            break

    # 민감 테이블 조회가 아니면 Gigang 전달 CSV에서 제외
    if not target_table:
        return None

    # SELECT 결과 행 수는 general_log 자체에서는 알 수 없으므로
    # 테스트용으로 LIMIT 값이 있으면 사용하고,
    # customer_vault 전체 조회는 현재 테스트 데이터 10건으로 처리
    table_count = TABLE_ROW_COUNTS.get(target_table, 0)

    limit_match = re.search(
        r"\bLIMIT\s+(\d+)",
        argument,
        re.IGNORECASE,
    )

    if limit_match:
        limit_value = int(limit_match.group(1))

        if table_count > 0:
            rows_affected = min(limit_value, table_count)
        else:
            rows_affected = limit_value

    else:
        rows_affected = table_count

    return (
        event_time,
        user_name,
        src_ip,
        action,
        target_table,
        argument,
        rows_affected,
    )


def save_csv(cursor, output_path):
    """메모리를 아끼기 위해 1,000개씩 읽고, 성공한 파일만 최종 경로에 둡니다."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    count = 0
    try:
        # utf-8-sig는 Windows Excel에서도 한글을 읽기 편한 인코딩입니다.
        # csv 모듈이 SQL 안의 쉼표, 줄바꿈, 따옴표를 자동으로 처리합니다.
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            dir=output_path.parent,
            prefix=".access_log_",
            suffix=".tmp",
            delete=False,
        ) as output_file:
            temporary_path = Path(output_file.name)
            writer = csv.writer(output_file)
            writer.writerow(COLUMNS)
            while True:
                rows = cursor.fetchmany(1000)
                if not rows:
                    break
                business_rows = [row for row in rows if is_business_select(row)]
                gigang_rows = []
                for row in business_rows:
                    converted = convert_to_gigang_row(row)

                    if converted is not None:
                        gigang_rows.append(converted)

                writer.writerows(gigang_rows)
                count += len(gigang_rows)
        temporary_path.replace(output_path)
        return count
    finally:
        # 실패했을 때 임시 파일만 정리합니다. 기존 CSV는 유지됩니다.
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost", help="MySQL 서버 주소")
    parser.add_argument("--port", type=int, default=3306, help="MySQL 포트")
    parser.add_argument("--user", default="root", help="로그 조회 권한이 있는 계정")
    args = parser.parse_args()

    try:
        import mysql.connector
    except ImportError:
        print(
            "필요한 패키지를 설치하세요: py -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    try:
        # 비밀번호를 코드, 파일, 명령행 인자에 저장하지 않습니다.
        # 입력을 숨길 수 없는 환경에서는 비밀번호를 받지 않고 중단합니다.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(
                f"MySQL {args.user} 비밀번호 (화면에 표시되지 않음): "
            )

        # 서버의 general_log 설정이나 DB/계정은 변경하지 않습니다.
        # autocommit은 이 연결의 옵션이며, 아래 실행 SQL은 SELECT뿐입니다.
        with mysql.connector.connect(
            host=args.host,
            port=args.port,
            user=args.user,
            password=password,
            charset="utf8mb4",
            connection_timeout=10,
            autocommit=True,
        ) as connection:
            with connection.cursor() as cursor:
                global TABLE_ROW_COUNTS

                TABLE_ROW_COUNTS = load_table_row_counts(cursor)

                print(
                    "customer_vault 데이터:",
                    TABLE_ROW_COUNTS.get("customer_vault", 0),
                    "건",
                )

                cursor.execute(SELECT_LOGS_SQL, TARGET_USERS)
                count = save_csv(cursor, OUTPUT_PATH)
        print(f"저장 완료: {OUTPUT_PATH} ({count:,}건)")
        if count == 0:
            print("조건에 맞는 로그가 없어 컬럼 이름만 저장했습니다.")
        return 0
    except mysql.connector.Error as error:
        print(f"MySQL 오류 ({error.errno}): {error.msg}", file=sys.stderr)
        print(
            "서버 주소, 계정, 비밀번호와 mysql.general_log SELECT 권한을 확인하세요.",
            file=sys.stderr,
        )
        return 1
    except (getpass.GetPassWarning, EOFError):
        print(
            "비밀번호를 안전하게 입력하려면 PowerShell에서 직접 실행하세요.",
            file=sys.stderr,
        )
        return 1
    except OSError as error:
        print(f"파일/연결 오류: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n사용자가 실행을 취소했습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

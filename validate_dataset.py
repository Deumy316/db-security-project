import csv
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

AUDIT_PATH = BASE_DIR / "data" / "mysql_audit_log.csv"
GROUND_TRUTH_PATH = BASE_DIR / "data" / "scenario_ground_truth.csv"

REQUIRED_COLUMNS = {
    "event_time",
    "user_name",
    "src_ip",
    "action",
    "target_table",
    "query_string",
    "rows_affected",
}


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        return reader.fieldnames, list(reader)


def main():
    print("=== Gigang DB 데이터셋 검증 ===")

    # 1. 파일 존재 여부
    if not AUDIT_PATH.exists():
        print("[FAIL] mysql_audit_log.csv 없음")
        return 1

    if not GROUND_TRUTH_PATH.exists():
        print("[FAIL] scenario_ground_truth.csv 없음")
        return 1

    audit_columns, audit_rows = read_csv(AUDIT_PATH)
    _, ground_truth_rows = read_csv(GROUND_TRUTH_PATH)

    failed = False

    # 2. CSV 컬럼 검사
    if set(audit_columns) == REQUIRED_COLUMNS:
        print("[PASS] CSV 컬럼 정상")
    else:
        print("[FAIL] CSV 컬럼 불일치")
        print("실제 컬럼:", audit_columns)
        failed = True

    # 3. Ground Truth 기준 예상 로그 수
    expected_total = sum(int(row["repetitions"]) for row in ground_truth_rows)

    actual_total = len(audit_rows)

    print(f"[INFO] 예상 로그 수: {expected_total}")
    print(f"[INFO] 실제 로그 수: {actual_total}")

    if actual_total == expected_total:
        print("[PASS] 전체 로그 건수 일치")
    else:
        print("[FAIL] 전체 로그 건수 불일치")
        failed = True

    # 4. 사용자별 예상/실제 건수
    expected_users = Counter()

    for row in ground_truth_rows:
        expected_users[row["user_name"]] += int(row["repetitions"])

    actual_users = Counter(row["user_name"] for row in audit_rows)

    print("\n=== 사용자별 로그 ===")

    for user_name in sorted(set(expected_users) | set(actual_users)):
        expected = expected_users[user_name]
        actual = actual_users[user_name]

        print(f"{user_name}: 예상={expected}건 / 실제={actual}건")

        if expected != actual:
            failed = True

    # 5. rows_affected 검사
    zero_rows = []

    for index, row in enumerate(audit_rows, start=2):
        try:
            rows_affected = int(row["rows_affected"])
        except ValueError:
            print(f"[FAIL] {index}행 rows_affected 숫자 변환 실패")
            failed = True
            continue

        if rows_affected <= 0:
            zero_rows.append(index)

    if zero_rows:
        print(f"[FAIL] rows_affected가 0 이하인 로그: " f"{len(zero_rows)}건")
        failed = True
    else:
        print("[PASS] rows_affected 모두 1 이상")

    # 6. 민감 테이블 확인
    invalid_target = [
        row for row in audit_rows if row["target_table"] != "customer_vault"
    ]

    if invalid_target:
        print(f"[FAIL] customer_vault가 아닌 로그: " f"{len(invalid_target)}건")
        failed = True
    else:
        print("[PASS] 전체 로그가 customer_vault 접근")

    # 7. action 검사
    invalid_action = [row for row in audit_rows if row["action"] != "DB_SELECT"]

    if invalid_action:
        print(f"[FAIL] DB_SELECT가 아닌 로그: " f"{len(invalid_action)}건")
        failed = True
    else:
        print("[PASS] 전체 action = DB_SELECT")

    # 8. 조회 규모 분포
    affected_distribution = Counter(int(row["rows_affected"]) for row in audit_rows)

    print("\n=== 조회 규모 분포 ===")

    for rows_affected, count in sorted(affected_distribution.items()):
        print(f"rows_affected={rows_affected}: {count}건")

    # 9. 정상/비정상 설계 건수
    label_counts = Counter()

    for row in ground_truth_rows:
        label_counts[row["label"]] += int(row["repetitions"])

    print("\n=== Ground Truth ===")

    for label, count in label_counts.items():
        print(f"{label}: {count}건")

    print("\n=== 최종 결과 ===")

    if failed:
        print("FAIL - 수정이 필요한 항목이 있습니다.")
        return 1

    print("PASS - Gigang DB 테스트 데이터셋 검증 완료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

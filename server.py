from flask import Flask, jsonify, request, render_template_string
from datetime import datetime
import getpass
import os
import re
import mysql.connector

app = Flask(__name__)

DB_HOST = "localhost"
DB_PORT = 3306
DB_USER = "gigang_reader"

API_KEY = os.environ.get("GIGANG_API_KEY")

TARGET_USERS = (
    "sales_user",
    "report_user",
    "analyst_user",
)

SENSITIVE_TABLES = (
    "customer_vault",
    "customer_info",
    "corp_strategic_plan",
    "salary_2026",
    "secret_key",
)

DB_PASSWORD = None


def check_api_key():
    provided_key = request.headers.get("X-API-Key")

    if not API_KEY:
        return False

    return provided_key == API_KEY


def get_connection():
    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        charset="utf8mb4",
        connection_timeout=10,
        autocommit=True,
    )


def get_table_count(cursor, table_name):
    if table_name == "customer_vault":
        cursor.execute("SELECT COUNT(*) FROM gigang_db.customer_vault")
        result = cursor.fetchone()
        return int(result[0])

    return 0


def detect_target_table(query_string):
    query_lower = query_string.lower()

    for table in SENSITIVE_TABLES:
        if table.lower() in query_lower:
            return table

    return None


def calculate_rows_affected(query_string, table_count):
    limit_match = re.search(
        r"\bLIMIT\s+(\d+)",
        query_string,
        re.IGNORECASE,
    )

    if limit_match:
        limit_value = int(limit_match.group(1))

        if table_count > 0:
            return min(limit_value, table_count)

        return limit_value

    return table_count


def load_logs(limit=None, user_name=None):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        placeholders = ", ".join(["%s"] * len(TARGET_USERS))

        sql = f"""
            SELECT
                event_time,
                user_host,
                CONVERT(argument USING utf8mb4) AS query_string
            FROM mysql.general_log
            WHERE command_type = 'Query'
              AND REGEXP_LIKE(
                    CONVERT(argument USING utf8mb4),
                    '^[[:space:]]*SELECT([[:space:]]|/|[(]|$)',
                    'i'
                  )
              AND TRIM(
                    SUBSTRING_INDEX(user_host, '[', 1)
                  ) IN ({placeholders})
            ORDER BY event_time DESC
        """

        cursor.execute(sql, list(TARGET_USERS))

        # 먼저 결과를 전부 읽은 뒤 가공
        rows = cursor.fetchall()

        logs = []
        table_counts = {}

        for event_time, user_host, query_string in rows:
            db_user = user_host.split("[", 1)[0].strip()

            if user_name and db_user != user_name:
                continue

            target_table = detect_target_table(query_string)

            if not target_table:
                continue

            if target_table not in table_counts:
                table_counts[target_table] = get_table_count(
                    cursor,
                    target_table,
                )

            rows_affected = calculate_rows_affected(
                query_string,
                table_counts[target_table],
            )

            logs.append(
                {
                    "event_time": event_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "user_name": db_user,
                    "src_ip": "127.0.0.1",
                    "action": "DB_SELECT",
                    "target_table": target_table,
                    "query_string": query_string,
                    "rows_affected": rows_affected,
                }
            )

            if limit is not None and len(logs) >= limit:
                break

        return logs

    finally:
        connection.close()


def load_logs_since(after_time, limit=100):
    connection = get_connection()

    try:
        cursor = connection.cursor()

        placeholders = ", ".join(["%s"] * len(TARGET_USERS))

        sql = f"""
            SELECT
                event_time,
                user_host,
                CONVERT(argument USING utf8mb4) AS query_string
            FROM mysql.general_log
            WHERE command_type = 'Query'
              AND event_time > %s
              AND REGEXP_LIKE(
                    CONVERT(argument USING utf8mb4),
                    '^[[:space:]]*SELECT([[:space:]]|/|[(]|$)',
                    'i'
                  )
              AND TRIM(
                    SUBSTRING_INDEX(user_host, '[', 1)
                  ) IN ({placeholders})
            ORDER BY event_time ASC, thread_id ASC
        """

        params = [after_time] + list(TARGET_USERS)

        cursor.execute(sql, params)
        rows = cursor.fetchall()

        logs = []
        table_counts = {}

        for event_time, user_host, query_string in rows:
            db_user = user_host.split("[", 1)[0].strip()

            target_table = detect_target_table(query_string)

            if not target_table:
                continue

            if target_table not in table_counts:
                table_counts[target_table] = get_table_count(
                    cursor,
                    target_table,
                )

            rows_affected = calculate_rows_affected(
                query_string,
                table_counts[target_table],
            )

            logs.append(
                {
                    "event_time": event_time.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    "user_name": db_user,
                    "src_ip": "127.0.0.1",
                    "action": "DB_SELECT",
                    "target_table": target_table,
                    "query_string": query_string,
                    "rows_affected": rows_affected,
                }
            )

            if len(logs) >= limit:
                break

        return logs

    finally:
        connection.close()


@app.route("/health", methods=["GET"])
def health():
    try:
        connection = get_connection()
        connection.close()

        return jsonify(
            {
                "status": "ok",
                "service": "기강 DB Log Server",
                "database": "gigang_db",
                "source": "mysql.general_log",
            }
        )

    except mysql.connector.Error:
        return (
            jsonify(
                {
                    "status": "error",
                    "service": "기강 DB Log Server",
                }
            ),
            500,
        )


@app.route("/api/db-logs", methods=["GET"])
def db_logs():
    if not check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    user_name = request.args.get("user_name")

    logs = load_logs(user_name=user_name)

    return jsonify(
        {
            "count": len(logs),
            "logs": logs,
        }
    )


@app.route("/api/db-logs/latest", methods=["GET"])
def latest_logs():
    if not check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    limit = request.args.get(
        "limit",
        default=20,
        type=int,
    )

    if limit is None:
        return jsonify({"error": "limit must be an integer"}), 400

    if limit < 1:
        limit = 1

    if limit > 1000:
        limit = 1000

    logs = load_logs(limit=limit)

    return jsonify(
        {
            "count": len(logs),
            "logs": logs,
        }
    )


@app.route("/api/db-logs/since", methods=["GET"])
def db_logs_since():
    if not check_api_key():
        return jsonify({"error": "Unauthorized"}), 401

    after = request.args.get("after")

    if not after:
        return jsonify({"error": "after parameter is required"}), 400

    try:
        after_time = datetime.fromisoformat(after)
    except ValueError:
        return jsonify({"error": "Invalid after format"}), 400

    limit = request.args.get(
        "limit",
        default=100,
        type=int,
    )

    if limit is None:
        return jsonify({"error": "limit must be an integer"}), 400

    if limit < 1:
        limit = 1

    if limit > 1000:
        limit = 1000

    logs = load_logs_since(
        after_time,
        limit,
    )

    if logs:
        next_after = logs[-1]["event_time"]
    else:
        next_after = after

    return jsonify(
        {
            "count": len(logs),
            "next_after": next_after,
            "logs": logs,
        }
    )


@app.route("/viewer", methods=["GET"])
def viewer():
    return render_template_string("""
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>기강 DB Log Viewer</title>

    <style>
        body {
            font-family: Arial, sans-serif;
            background: #111827;
            color: #f9fafb;
            margin: 0;
            padding: 30px;
        }

        h1 {
            margin-bottom: 20px;
        }

        .controls {
            margin-bottom: 20px;
        }

        input {
            padding: 10px;
            width: 320px;
            margin-right: 8px;
        }

        button {
            padding: 10px 16px;
            cursor: pointer;
        }

        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            font-size: 14px;
        }

        th, td {
            border: 1px solid #374151;
            padding: 8px;
            text-align: left;
        }

        th {
            background: #1f2937;
        }

        tr:nth-child(even) {
            background: #172033;
        }

        .error {
            color: #f87171;
            margin-top: 15px;
        }
    </style>
</head>

<body>

<h1>기강 DB Log Viewer</h1>

<div class="controls">
    <input
        type="password"
        id="apiKey"
        placeholder="API Key 입력"
    >

    <button onclick="loadLogs()">
        로그 조회
    </button>
</div>

<div id="status"></div>

<table>
    <thead>
        <tr>
            <th>시간</th>
            <th>사용자</th>
            <th>Action</th>
            <th>테이블</th>
            <th>조회량</th>
            <th>SQL</th>
        </tr>
    </thead>

    <tbody id="logBody">
    </tbody>
</table>

<script>
async function loadLogs() {
    const apiKey = document.getElementById("apiKey").value;
    const status = document.getElementById("status");
    const body = document.getElementById("logBody");

    body.innerHTML = "";
    status.innerHTML = "조회 중...";

    try {
        const response = await fetch(
            "/api/db-logs/latest?limit=50",
            {
                headers: {
                    "X-API-Key": apiKey
                }
            }
        );

        if (!response.ok) {
            status.innerHTML =
                '<div class="error">API Key가 올바르지 않습니다.</div>';
            return;
        }

        const data = await response.json();

        status.innerHTML =
            "조회된 로그: " + data.count + "건";

        data.logs.forEach(log => {
            const row = document.createElement("tr");

            row.innerHTML = `
                <td>${log.event_time}</td>
                <td>${log.user_name}</td>
                <td>${log.action}</td>
                <td>${log.target_table}</td>
                <td>${log.rows_affected}</td>
                <td>${log.query_string}</td>
            `;

            body.appendChild(row);
        });

    } catch (error) {
        status.innerHTML =
            '<div class="error">서버 연결 실패</div>';
    }
}
</script>

</body>
</html>
    """)


if __name__ == "__main__":
    if not API_KEY:
        raise RuntimeError("GIGANG_API_KEY 환경변수가 설정되지 않았습니다.")

    DB_PASSWORD = getpass.getpass("MySQL gigang_reader 비밀번호: ")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )

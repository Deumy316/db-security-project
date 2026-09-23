from flask import (
    Flask,
    jsonify,
    request,
    render_template,
    render_template_string,
    redirect,
    url_for,
    session,
    send_file,
)
from datetime import datetime, timedelta
import os
import io
import xlsxwriter
import re
import mysql.connector

app = Flask(__name__)
app.secret_key = os.environ.get("GIGANG_SECRET_KEY", "dev-only-secret")
app.permanent_session_lifetime = timedelta(minutes=15)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

DB_HOST = os.environ.get("GIGANG_DB_HOST", "localhost")
DB_PORT = int(os.environ.get("GIGANG_DB_PORT", "3306"))
DB_USER = os.environ.get("GIGANG_DB_USER", "gigang_reader")
DB_PASSWORD = os.environ.get("GIGANG_DB_PASSWORD")

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

PORTAL_USERS = {
    "sales_user": os.environ.get("GIGANG_SALES_PASSWORD"),
    "report_user": os.environ.get("GIGANG_REPORT_PASSWORD"),
}


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


def get_user_connection(user_name):
    password = PORTAL_USERS.get(user_name)

    if not password:
        raise ValueError("허용되지 않은 사용자입니다.")

    return mysql.connector.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=user_name,
        password=password,
        database="gigang_db",
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

            action = (
                "EXCEL_DOWNLOAD"
                if "GIGANG_EXCEL_DOWNLOAD" in query_string
                else "DB_SELECT"
            )

            logs.append(
                {
                    "event_time": event_time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "user_name": db_user,
                    "src_ip": "127.0.0.1",
                    "action": action,
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

            action = (
                "EXCEL_DOWNLOAD"
                if "GIGANG_EXCEL_DOWNLOAD" in query_string
                else "DB_SELECT"
            )

            logs.append(
                {
                    "event_time": event_time.strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    "user_name": db_user,
                    "src_ip": "127.0.0.1",
                    "action": action,
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


@app.route("/", methods=["GET"])
def index():
    if "portal_user" in session:
        return redirect(url_for("db_portal"))

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        expected_password = PORTAL_USERS.get(username)

        if expected_password and password == expected_password:
            try:
                connection = get_user_connection(username)
                connection.close()

                session.permanent = True
                session["portal_user"] = username

                return redirect(url_for("db_portal"))

            except mysql.connector.Error:
                error = "DB 계정 연결에 실패했습니다."
        else:
            error = "아이디 또는 비밀번호가 올바르지 않습니다."

    return render_template(
        "login.html",
        error=error,
    )


@app.route("/db", methods=["GET"])
def db_portal():
    username = session.get("portal_user")

    if not username:
        return redirect(url_for("login"))

    keyword = request.args.get("keyword", "").strip()
    search_type = request.args.get("search_type", "name")

    rows = []

    if request.args.get("search") == "1":
        connection = get_user_connection(username)

        try:
            cursor = connection.cursor()

            if keyword:
                if search_type == "id" and keyword.isdigit():
                    cursor.execute(
                        """
                        SELECT
                            user_id,
                            customer_name,
                            rrn,
                            phone_number,
                            email,
                            address,
                            account_number,
                            credit_card_num,
                            balance,
                            customer_grade
                        FROM customer_vault
                        WHERE user_id = %s
                        LIMIT 50
                    """,
                        (int(keyword),),
                    )

                else:
                    cursor.execute(
                        """
                        SELECT
                            user_id,
                            customer_name,
                            rrn,
                            phone_number,
                            email,
                            address,
                            account_number,
                            credit_card_num,
                            balance,
                            customer_grade
                        FROM customer_vault
                        WHERE customer_name LIKE %s
                        LIMIT 50
                    """,
                        ("%" + keyword + "%",),
                    )

            else:
                cursor.execute("""
                    SELECT
                        user_id,
                        customer_name,
                        rrn,
                        phone_number,
                        email,
                        address,
                        account_number,
                        credit_card_num,
                        balance,
                        customer_grade
                    FROM customer_vault
                    LIMIT 50
                """)

            rows = cursor.fetchall()

        finally:
            connection.close()

    return render_template(
        "db.html",
        username=username,
        rows=rows,
        keyword=keyword,
        search_type=search_type,
    )


@app.route("/db/download", methods=["GET"])
def download_db_excel():
    username = session.get("portal_user")

    if not username:
        return redirect(url_for("login"))

    keyword = request.args.get("keyword", "").strip()
    search_type = request.args.get("search_type", "name")

    connection = get_user_connection(username)

    try:
        cursor = connection.cursor()

        if keyword:
            if search_type == "id":
                if not keyword.isdigit():
                    rows = []
                else:
                    cursor.execute(
                        """
                        SELECT /* GIGANG_EXCEL_DOWNLOAD */
                            user_id,
                            customer_name,
                            rrn,
                            phone_number,
                            email,
                            address,
                            account_number,
                            credit_card_num,
                            balance,
                            customer_grade
                        FROM customer_vault
                        WHERE user_id = %s
                        LIMIT 50
                    """,
                        (int(keyword),),
                    )

                    rows = cursor.fetchall()

            else:
                cursor.execute(
                    """
                    SELECT /* GIGANG_EXCEL_DOWNLOAD */
                        user_id,
                        customer_name,
                        rrn,
                        phone_number,
                        email,
                        address,
                        account_number,
                        credit_card_num,
                        balance,
                        customer_grade
                    FROM customer_vault
                    WHERE customer_name LIKE %s
                    LIMIT 50
                """,
                    ("%" + keyword + "%",),
                )

                rows = cursor.fetchall()

        else:
            cursor.execute("""
                SELECT /* GIGANG_EXCEL_DOWNLOAD */
                    user_id,
                    customer_name,
                    rrn,
                    phone_number,
                    email,
                    address,
                    account_number,
                    credit_card_num,
                    balance,
                    customer_grade
                FROM customer_vault
                LIMIT 50
            """)

            rows = cursor.fetchall()

    finally:
        connection.close()

    output = io.BytesIO()

    workbook = xlsxwriter.Workbook(output, {"in_memory": True})

    worksheet = workbook.add_worksheet("고객정보")

    headers = [
        "고객번호",
        "고객명",
        "주민등록번호",
        "휴대전화",
        "이메일",
        "주소",
        "계좌번호",
        "신용카드번호",
        "잔액",
        "고객등급",
    ]

    header_format = workbook.add_format(
        {
            "bold": True,
            "border": 1,
            "align": "center",
        }
    )

    money_format = workbook.add_format(
        {
            "num_format": "#,##0.00",
        }
    )

    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)

    for row_index, row in enumerate(rows, start=1):
        worksheet.write(row_index, 0, row[0])
        worksheet.write(row_index, 1, row[1])
        worksheet.write(row_index, 2, row[2])
        worksheet.write(row_index, 3, row[3])
        worksheet.write(row_index, 4, row[4])
        worksheet.write(row_index, 5, row[5])
        worksheet.write(row_index, 6, row[6])
        worksheet.write(row_index, 7, row[7])
        worksheet.write(row_index, 8, row[8], money_format)
        worksheet.write(row_index, 9, row[9])

    worksheet.set_column("A:A", 12)
    worksheet.set_column("B:B", 18)
    worksheet.set_column("C:C", 20)
    worksheet.set_column("D:D", 18)
    worksheet.set_column("E:E", 30)
    worksheet.set_column("F:F", 36)
    worksheet.set_column("G:H", 24)
    worksheet.set_column("I:I", 15)
    worksheet.set_column("J:J", 12)

    workbook.close()

    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="gigang_customer_data.xlsx",
        mimetype=(
            "application/vnd.openxmlformats-officedocument." "spreadsheetml.sheet"
        ),
    )


@app.route("/logout", methods=["GET"])
def logout():
    session.clear()

    return redirect(url_for("login"))


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

    if not DB_PASSWORD:
        raise RuntimeError("GIGANG_DB_PASSWORD 환경변수가 설정되지 않았습니다.")

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
    )

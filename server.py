from flask import (
    Flask,
    jsonify,
    request,
    render_template,
    redirect,
    url_for,
    session,
    send_file,
)
from datetime import datetime, timedelta
import os
import hmac
import secrets
import io
import xlsxwriter
import re
import mysql.connector
import audit_viewer
from notices import NOTICES

app = Flask(__name__)
app.secret_key = os.environ.get("GIGANG_SECRET_KEY", "dev-only-secret")
app.permanent_session_lifetime = timedelta(minutes=15)
app.config["SESSION_REFRESH_EACH_REQUEST"] = True

DB_HOST = os.environ.get("GIGANG_DB_HOST", "localhost")
DB_PORT = int(os.environ.get("GIGANG_DB_PORT", "3306"))
DB_USER = os.environ.get("GIGANG_DB_USER", "gigang_reader")
DB_PASSWORD = os.environ.get("GIGANG_DB_PASSWORD")

API_KEY = os.environ.get("GIGANG_API_KEY")
ADMIN_PASSWORD = os.environ.get("GIGANG_ADMIN_PASSWORD")

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

# 부서별 포털 아이디. 로그인하면 그 부서 화면(view)만 보인다 (탭 없음).
# MySQL 에는 db_user 계정으로 접속하고 SQL 주석으로 포털 사용자를 구분한다.
# 비밀번호 환경변수가 비어 있으면 그 아이디는 로그인할 수 없다.
DEPT_ACCOUNTS = {
    "cs_user": {"password": os.environ.get("GIGANG_CS_PASSWORD"), "db_user": "sales_user", "view": "cs"},
    "hr_user": {"password": os.environ.get("GIGANG_HR_PASSWORD"), "db_user": "sales_user", "view": "hr"},
    "fin_user": {"password": os.environ.get("GIGANG_FIN_PASSWORD"), "db_user": "sales_user", "view": "fin"},
    "dev_user": {"password": os.environ.get("GIGANG_DEV_PASSWORD"), "db_user": "sales_user", "view": "dev"},
}


ALLOWED_PORTAL_USERS = frozenset((*PORTAL_USERS, *DEPT_ACCOUNTS, "admin_user"))
PORTAL_USER_PREFIX = re.compile(
    r"^\s*SELECT\s+(?:/\*\s*GIGANG_EXCEL_DOWNLOAD\s*\*/\s*)?"
    r"/\*\s*GIGANG_PORTAL_USER=([a-z_]+)\s*\*/",
    re.IGNORECASE,
)


def portal_user_from_query(query_string, db_user):
    """문자열 값 속 가짜 주석을 피하도록 SELECT 바로 뒤의 표식만 읽는다."""
    match = PORTAL_USER_PREFIX.match(query_string)
    if match:
        portal_user = match.group(1)
        if portal_user in ALLOWED_PORTAL_USERS and db_user_for(portal_user) == db_user:
            return portal_user
    return None


def portal_password(username):
    if username in DEPT_ACCOUNTS:
        return DEPT_ACCOUNTS[username]["password"]

    return PORTAL_USERS.get(username)


def db_user_for(username):
    """포털 아이디 → MySQL 계정. 기존 계정(sales_user 등)은 그대로."""
    if username == "admin_user":
        return "sales_user"
    if username in DEPT_ACCOUNTS:
        return DEPT_ACCOUNTS[username]["db_user"]

    return username


def fixed_view_for(username):
    """부서 아이디면 그 부서 화면, 기존 계정이면 None (탭으로 모든 화면)."""
    account = DEPT_ACCOUNTS.get(username)
    return account["view"] if account else None


def check_api_key():
    provided_key = request.headers.get("X-API-Key")

    if not API_KEY:
        return False

    return provided_key == API_KEY


def is_viewer_admin():
    auth = session.get("viewer_admin_auth")
    if (not ADMIN_PASSWORD or session.get("portal_user") != "admin_user"
            or not isinstance(auth, str) or len(auth) != 97):
        return False
    nonce, _, signature = auth.partition(":")
    expected = hmac.new(ADMIN_PASSWORD.encode(), nonce.encode(), "sha256").hexdigest()
    return len(nonce) == 32 and hmac.compare_digest(signature.encode(), expected.encode())


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
    user_name = db_user_for(user_name)
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
            portal_user = portal_user_from_query(query_string, db_user)

            if user_name and user_name not in (db_user, portal_user):
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
                    "portal_user": portal_user,
                    "db_user": db_user,
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
            portal_user = portal_user_from_query(query_string, db_user)

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
                    "portal_user": portal_user,
                    "db_user": db_user,
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
    if is_viewer_admin():
        return redirect(url_for("viewer"))
    if "portal_user" in session:
        return redirect(url_for("portal_home"))

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if username == "admin_user":
            if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
                session.clear()
                session.permanent = True
                session["portal_user"] = "admin_user"
                # 포털 ID만 바꾸거나 기존 세션 서명 키만 알아도 관리자 권한은 얻지 못한다.
                nonce = secrets.token_hex(16)
                proof = hmac.new(ADMIN_PASSWORD.encode(), nonce.encode(), "sha256").hexdigest()
                session["viewer_admin_auth"] = f"{nonce}:{proof}"
                return redirect(url_for("viewer"))
            return render_template("login.html", error="아이디 또는 비밀번호가 올바르지 않습니다.")

        expected_password = portal_password(username)

        if expected_password and password == expected_password:
            try:
                connection = get_user_connection(username)
                connection.close()

                session.clear()
                session.permanent = True
                session["portal_user"] = username

                return redirect(url_for("portal_home"))

            except mysql.connector.Error:
                error = "DB 계정 연결에 실패했습니다."
        else:
            error = "아이디 또는 비밀번호가 올바르지 않습니다."

    return render_template(
        "login.html",
        error=error,
    )


# 부서별 조회 화면. 화면마다 업무에 필요한 열만 보여 준다 (엑셀 다운로드도 같은 열).
# 값: (탭 이름, 열 목록). 열 = (키, 머리글, 정렬 종류). "bank" 는 DB 열이 아니라 화면에서 만든다.
VIEWS = {
    "all": ("전체", ["user_id", "customer_name", "rrn", "phone_number", "email", "address",
                   "bank", "account_number", "credit_card_num", "balance", "customer_grade"]),
    "cs": ("고객지원", ["user_id", "customer_name", "phone_number", "email", "address", "customer_grade"]),
    "hr": ("인사", ["user_id", "customer_name", "rrn", "phone_number", "email", "address",
                  "bank", "account_number"]),
    "fin": ("재무·정산", ["user_id", "customer_name", "bank", "account_number", "credit_card_num",
                       "balance", "customer_grade"]),
    # 개발: 운영 장애 점검용으로 전체 열을 본다 (개발자가 고객정보를 반출하면 DEV-1 로 잡히는 시연용)
    "dev": ("개발(운영 점검)", ["user_id", "customer_name", "rrn", "phone_number", "email", "address",
                           "bank", "account_number", "credit_card_num", "balance", "customer_grade"]),
}

COLUMNS = {
    "user_id": ("고객번호", "number"),
    "customer_name": ("고객명", "string"),
    "rrn": ("주민등록번호", "string"),
    "phone_number": ("휴대전화", "string"),
    "email": ("이메일", "string"),
    "address": ("주소", "string"),
    "bank": ("은행", "string"),
    "account_number": ("계좌번호", "string"),
    "credit_card_num": ("신용카드번호", "string"),
    "balance": ("잔액", "number"),
    "customer_grade": ("고객등급", "grade"),
}

# 계좌번호는 DB에 숫자 14자리로 저장돼 있다. 화면·엑셀에 낼 때 고객마다 은행을 정하고
# 그 은행의 실제 하이픈 형식으로 보여 준다 (실제 시스템도 하이픈은 표시할 때 붙인다).
BANKS = (
    ("국민은행", (6, 2, 6)),
    ("하나은행", (3, 6, 5)),
    ("기업은행", (3, 6, 2, 3)),
)


def bank_for(user_id):
    return BANKS[int(user_id) % len(BANKS)]


def format_account(user_id, account_number):
    digits = re.sub(r"\D", "", str(account_number or ""))
    _, parts = bank_for(user_id)

    if len(digits) != sum(parts):
        return account_number

    pieces, pos = [], 0
    for size in parts:
        pieces.append(digits[pos:pos + size])
        pos += size

    return "-".join(pieces)


def get_view(view_key):
    return view_key if view_key in VIEWS else "all"


def fetch_customer_rows(username, view_key, keyword, search_type, excel=False):
    """화면(view)에 맞는 열만 조회해서 [열 순서대로 값] 목록을 돌려준다."""
    if username not in ALLOWED_PORTAL_USERS:
        raise ValueError("허용되지 않은 포털 사용자입니다.")

    columns = VIEWS[view_key][1]
    # 은행·계좌 형식을 만들려면 고객번호가 필요하므로 user_id 는 늘 조회한다
    sql_columns = ["user_id"] + [c for c in columns if c not in ("user_id", "bank")]
    marker = " /* GIGANG_EXCEL_DOWNLOAD */" if excel else ""
    marker += f" /* GIGANG_PORTAL_USER={username} */"
    sql = f"SELECT{marker} {', '.join(sql_columns)} FROM customer_vault"
    params = ()

    if keyword:
        if search_type == "id" and keyword.isdigit():
            sql += " WHERE user_id = %s"
            params = (int(keyword),)
        elif search_type == "id" and excel:
            return []   # 원래 동작: 엑셀은 숫자 아닌 고객번호면 빈 파일, 화면은 이름 검색으로 처리
        else:
            sql += " WHERE customer_name LIKE %s"
            params = ("%" + keyword + "%",)

    sql += " LIMIT 50"

    connection = get_user_connection(username)

    try:
        cursor = connection.cursor()
        cursor.execute(sql, params)
        fetched = cursor.fetchall()
    finally:
        connection.close()

    rows = []
    for record in fetched:
        values = dict(zip(sql_columns, record))
        user_id = values["user_id"]
        values["bank"] = bank_for(user_id)[0]
        if "account_number" in values:
            values["account_number"] = format_account(user_id, values["account_number"])
        rows.append([values[c] for c in columns])

    return rows


def view_headers(view_key):
    return [(COLUMNS[c][0], COLUMNS[c][1]) for c in VIEWS[view_key][1]]


def portal_profile(username):
    view = fixed_view_for(username) or "all"
    department = {
        "cs_user": "고객지원", "hr_user": "인사", "fin_user": "재무·정산",
        "dev_user": "개발/운영점검", "admin_user": "시스템 관리자",
    }.get(username, "공통 업무")
    return {"department": department, "headers": view_headers(view),
            "full_access": fixed_view_for(username) is None,
            "audit_access": username == "admin_user"}


@app.route("/home", methods=["GET"])
def portal_home():
    username = session.get("portal_user")
    if not username:
        return redirect(url_for("login"))
    if username not in ALLOWED_PORTAL_USERS or (username == "admin_user" and not is_viewer_admin()):
        return "Forbidden", 403

    activity = None
    connection = None
    try:
        connection = get_connection()
        filters = audit_viewer.parse_filters(
            {"portal_user": username, "legacy": "exclude"}, ALLOWED_PORTAL_USERS, TARGET_USERS,
        )
        activity = audit_viewer.load_page(
            connection, filters, target_users=TARGET_USERS,
            portal_db_users={user: db_user_for(user) for user in ALLOWED_PORTAL_USERS},
            tables=SENSITIVE_TABLES, prefix_pattern=PORTAL_USER_PREFIX.pattern,
            table_counter=get_table_count, row_estimator=calculate_rows_affected,
        )
    except mysql.connector.Error:
        pass  # 감사 조회 장애가 업무 포털 홈 접근을 막지 않도록 한다.
    finally:
        if connection is not None:
            connection.close()

    return render_template("home.html", username=username, profile=portal_profile(username),
                           activity=activity, notices=NOTICES)


@app.route("/notices/<slug>", methods=["GET"])
def portal_notice(slug):
    username = session.get("portal_user")
    if not username:
        return redirect(url_for("login"))
    if username not in ALLOWED_PORTAL_USERS or (username == "admin_user" and not is_viewer_admin()):
        return "Forbidden", 403
    notice = NOTICES.get(slug)
    if notice is None:
        return "Not Found", 404
    return render_template("notice.html", notice=notice)


@app.route("/permissions", methods=["GET"])
def portal_permissions():
    username = session.get("portal_user")
    if not username:
        return redirect(url_for("login"))
    if username not in ALLOWED_PORTAL_USERS or (username == "admin_user" and not is_viewer_admin()):
        return "Forbidden", 403
    return render_template("permissions.html", username=username, profile=portal_profile(username))


@app.route("/db", methods=["GET"])
def db_portal():
    username = session.get("portal_user")

    if not username:
        return redirect(url_for("login"))
    if username == "admin_user" and not is_viewer_admin():
        return "Forbidden", 403

    keyword = request.args.get("keyword", "").strip()
    search_type = request.args.get("search_type", "name")
    fixed_view = fixed_view_for(username)
    view = fixed_view or get_view(request.args.get("view", "all"))

    rows = []

    if request.args.get("search") == "1":
        rows = fetch_customer_rows(username, view, keyword, search_type)

    return render_template(
        "db.html",
        username=username,
        rows=rows,
        keyword=keyword,
        search_type=search_type,
        view=view,
        view_label=VIEWS[view][0],
        views={} if fixed_view else VIEWS,   # 부서 아이디는 탭 없이 자기 화면만
        headers=view_headers(view),
    )


@app.route("/db/download", methods=["GET"])
def download_db_excel():
    username = session.get("portal_user")

    if not username:
        return redirect(url_for("login"))
    if username == "admin_user" and not is_viewer_admin():
        return "Forbidden", 403

    keyword = request.args.get("keyword", "").strip()
    search_type = request.args.get("search_type", "name")
    view = fixed_view_for(username) or get_view(request.args.get("view", "all"))

    rows = fetch_customer_rows(username, view, keyword, search_type, excel=True)
    headers = view_headers(view)

    output = io.BytesIO()

    workbook = xlsxwriter.Workbook(output, {"in_memory": True})

    worksheet = workbook.add_worksheet("고객정보")

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

    for col, (header, _) in enumerate(headers):
        worksheet.write(0, col, header, header_format)
        worksheet.set_column(col, col, 36 if header == "주소" else 30 if header == "이메일" else 20)

    for row_index, row in enumerate(rows, start=1):
        for col, value in enumerate(row):
            if headers[col][0] == "잔액":
                worksheet.write(row_index, col, value, money_format)
            else:
                worksheet.write(row_index, col, value)

    workbook.close()

    output.seek(0)

    return send_file(
        output,
        as_attachment=True,
        download_name="gigang_customer_data.xlsx" if view == "all" else f"gigang_customer_data_{view}.xlsx",
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
    if not session.get("portal_user"):
        return redirect(url_for("login"))
    if not is_viewer_admin():
        return "Forbidden", 403
    return render_template(
        "viewer.html", portal_users=sorted(ALLOWED_PORTAL_USERS), db_users=TARGET_USERS,
        username=session["portal_user"],
    )


@app.route("/api/viewer/db-logs", methods=["GET"])
def viewer_db_logs():
    if not session.get("portal_user"):
        return jsonify({"error": "Unauthorized"}), 401
    if not is_viewer_admin():
        return jsonify({"error": "Forbidden"}), 403
    try:
        filters = audit_viewer.parse_filters(request.args, ALLOWED_PORTAL_USERS, TARGET_USERS)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    connection = None
    try:
        connection = get_connection()
        data = audit_viewer.load_page(
            connection, filters, target_users=TARGET_USERS,
            portal_db_users={user: db_user_for(user) for user in ALLOWED_PORTAL_USERS},
            tables=SENSITIVE_TABLES, prefix_pattern=PORTAL_USER_PREFIX.pattern,
            table_counter=get_table_count, row_estimator=calculate_rows_affected,
        )
        response = jsonify(data)
        response.headers["Cache-Control"] = "no-store"
        return response
    except mysql.connector.Error:
        return jsonify({"error": "Audit logs temporarily unavailable"}), 503
    finally:
        if connection is not None:
            connection.close()


@app.route("/api/viewer/auth-events", methods=["GET"])
def viewer_auth_events():
    if not session.get("portal_user"):
        return jsonify({"error": "Unauthorized"}), 401
    if not is_viewer_admin():
        return jsonify({"error": "Forbidden"}), 403
    try:
        import auth_events_viewer
    except Exception:
        return jsonify({"error": "Authentication audit temporarily unavailable"}), 503
    try:
        filters = auth_events_viewer.parse_filters(request.args)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    try:
        data = auth_events_viewer.load_page(app.config.get("AUTH_AUDIT_PATH"), filters)
    except Exception:
        return jsonify({"error": "Authentication audit temporarily unavailable"}), 503
    response = jsonify(data)
    response.headers["Cache-Control"] = "no-store"
    return response


try:
    import auth_audit
    auth_audit.register(app)
except Exception:
    try:
        app.logger.warning("Authentication audit initialization failed; continuing without audit.")
    except Exception:
        pass


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

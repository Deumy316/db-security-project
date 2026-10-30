"""Viewer 전용 읽기 경로. 기존 SOC API/수집 경로와 독립적으로 동작한다."""

from datetime import datetime

PAGE_SIZE = 50


def parse_filters(args, portal_users, db_users):
    filters = {}
    for name, allowed in (
        ("portal_user", portal_users), ("db_user", db_users),
        ("action", ("DB_SELECT", "EXCEL_DOWNLOAD")),
        ("legacy", ("include", "exclude", "only")),
    ):
        value = args.get(name, "include" if name == "legacy" else "")
        if value and value not in allowed:
            raise ValueError(f"Invalid {name}")
        filters[name] = value
    try:
        page = int(args.get("page", "1"))
    except (ValueError, TypeError):
        raise ValueError("Invalid page") from None
    if not 1 <= page <= 1_000_000:
        raise ValueError("page must be between 1 and 1000000")
    filters["page"] = page
    for name in ("start", "end"):
        value = args.get(name, "")
        try:
            parsed = datetime.fromisoformat(value) if value else None
        except ValueError:
            raise ValueError(f"Invalid {name}") from None
        # general_log DATETIME와 같은 DB 세션 기준 시간. 시간대 추정을 하지 않는다.
        if parsed and (parsed.tzinfo is not None or parsed.year < 1000):
            raise ValueError(f"{name} must be a DB-local datetime without timezone")
        filters[name] = parsed
    if filters["start"] and filters["end"] and filters["start"] > filters["end"]:
        raise ValueError("start must not be after end")
    return filters


def build_query(filters, target_users, portal_db_users, tables, prefix_pattern):
    """허용된 정의만 SQL 구조에 사용하고 필터 값은 모두 바인딩한다."""
    placeholders = ", ".join(["%s"] * len(target_users))
    raw_conditions = [
        "command_type = 'Query'",
        "REGEXP_LIKE(CONVERT(argument USING utf8mb4), %s, 'i')",
        f"TRIM(SUBSTRING_INDEX(user_host, '[', 1)) IN ({placeholders})",
    ]
    params = [r"^[[:space:]]*SELECT([[:space:]]|/|[(]|$)", *target_users]
    for name, operator in (("start", ">="), ("end", "<=")):
        if filters[name]:
            raw_conditions.append(f"event_time {operator} %s")
            params.append(filters[name])

    # Python의 기존 prefix 파서와 같은 패턴. SQL 문자열 리터럴 대신 바인딩한다.
    params.append(prefix_pattern.replace(r"\s", "[[:space:]]"))
    identity_cases = []
    for portal_user, db_user in sorted(portal_db_users.items()):
        identity_cases.append(
            "WHEN CAST(candidate AS BINARY) = CAST(%s AS BINARY) "
            "AND db_user = %s THEN %s"
        )
        params.extend((portal_user, db_user, portal_user))
    table_cases = []
    for table in tables:
        table_cases.append("WHEN LOCATE(%s, LOWER(query_string)) > 0 THEN %s")
        params.extend((table.lower(), table))
    conditions = ["target_table IS NOT NULL"]
    for name in ("portal_user", "db_user", "action"):
        if filters[name]:
            conditions.append(f"{name} = %s")
            params.append(filters[name])
    if filters["legacy"] == "exclude":
        conditions.append("portal_user IS NOT NULL")
    elif filters["legacy"] == "only":
        conditions.append("portal_user IS NULL")

    sql = f"""
        WITH raw_logs AS (
            SELECT event_time, thread_id,
                   TRIM(SUBSTRING_INDEX(user_host, '[', 1)) AS db_user,
                   CONVERT(argument USING utf8mb4) AS query_string
            FROM mysql.general_log WHERE {' AND '.join(raw_conditions)}
        ), marked AS (
            SELECT raw_logs.*, REGEXP_SUBSTR(query_string, %s, 1, 1, 'i') AS marker
            FROM raw_logs
        ), candidates AS (
            SELECT marked.*,
                   REGEXP_SUBSTR(SUBSTRING_INDEX(marker, '=', -1), '^[a-z_]+', 1, 1, 'i') AS candidate
            FROM marked
        ), classified AS (
            SELECT event_time, thread_id, db_user, query_string,
                   CASE {' '.join(identity_cases)} ELSE NULL END AS portal_user,
                   CASE {' '.join(table_cases)} ELSE NULL END AS target_table,
                   CASE WHEN LOCATE(CAST('GIGANG_EXCEL_DOWNLOAD' AS BINARY),
                                         CAST(query_string AS BINARY)) > 0
                        THEN 'EXCEL_DOWNLOAD' ELSE 'DB_SELECT' END AS action
            FROM candidates
        ), filtered AS (
            SELECT * FROM classified WHERE {' AND '.join(conditions)}
        )
    """
    return sql, params


def load_page(connection, filters, *, target_users, portal_db_users, tables,
              prefix_pattern, table_counter, row_estimator):
    sql, params = build_query(filters, target_users, portal_db_users, tables, prefix_pattern)
    cursor = connection.cursor(dictionary=True)
    count_cursor = None
    try:
        cursor.execute(sql + """
            SELECT /*+ MAX_EXECUTION_TIME(5000) */ COUNT(*) AS total,
                   COALESCE(SUM(CASE WHEN action = 'DB_SELECT' THEN 1 ELSE 0 END), 0) AS db_select,
                   COALESCE(SUM(CASE WHEN action = 'EXCEL_DOWNLOAD' THEN 1 ELSE 0 END), 0) AS excel_download,
                   COUNT(DISTINCT portal_user) AS portal_users
            FROM filtered
        """, params)
        summary = {key: int(value) for key, value in cursor.fetchone().items()}
        total_pages = max(1, (summary["total"] + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(filters["page"], total_pages)
        cursor.execute(sql + """
            SELECT /*+ MAX_EXECUTION_TIME(5000) */ event_time, portal_user, db_user, action, target_table, query_string
            FROM filtered ORDER BY event_time DESC, thread_id DESC LIMIT %s OFFSET %s
        """, [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE])
        rows = cursor.fetchall()
        table_counts = {}
        if rows:
            count_cursor = connection.cursor()
        for row in rows:
            table = row["target_table"]
            if table not in table_counts:
                table_counts[table] = table_counter(count_cursor, table)
            row["event_time"] = row["event_time"].isoformat(timespec="microseconds")
            row["user_name"] = row["db_user"]
            row["src_ip"] = "127.0.0.1"
            row["rows_affected"] = row_estimator(row["query_string"], table_counts[table])
        return {
            "count": len(rows), "logs": rows, "summary": summary,
            "total": summary["total"], "page": page, "page_size": PAGE_SIZE,
            "total_pages": total_pages, "has_previous": page > 1,
            "has_next": page < total_pages,
        }
    finally:
        cursor.close()
        if count_cursor is not None:
            count_cursor.close()

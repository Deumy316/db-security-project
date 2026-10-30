"""인증 감사 Viewer 전용 읽기 경로. 인증·감사 저장·MySQL에 의존하지 않는다."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
import ipaddress
import json
from pathlib import Path
import sqlite3
import time

PAGE_SIZE = 50
READ_BUDGET_SECONDS = 0.025
KST = timezone(timedelta(hours=9))
EVENT_TYPES = ("LOGIN_SUCCESS", "LOGIN_FAILED", "LOGOUT")


def parse_filters(args):
    user = args.get("portal_user", "").strip()
    event = args.get("event_type", "")
    if len(user) > 128:
        raise ValueError("portal_user must be at most 128 characters")
    if event and event not in EVENT_TYPES:
        raise ValueError("Invalid event_type")
    try:
        page = int(args.get("page", "1"))
    except (ValueError, TypeError):
        raise ValueError("Invalid page") from None
    if not 1 <= page <= 1_000_000:
        raise ValueError("page must be between 1 and 1000000")
    return {"portal_user": user, "event_type": event, "page": page}


def _forwarded_ips(value):
    try:
        values = json.loads(value[:2048])
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    addresses = []
    for value in values[:8]:
        if isinstance(value, str):
            try:
                address = ipaddress.ip_address(value)
                addresses.append(str(ipaddress.ip_address(address.packed)))
            except ValueError:
                pass
    return addresses


def load_page(path, filters):
    conditions, params = [], []
    for name in ("portal_user", "event_type"):
        if filters[name]:
            conditions.append(f"{name} = ?")
            params.append(filters[name])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=0.02)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        deadline = time.monotonic() + READ_BUDGET_SECONDS
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        # 짧은 읽기 트랜잭션으로 집계와 페이지의 기준을 맞추고 즉시 연결을 해제한다.
        with connection:
            connection.execute("BEGIN")
            total = connection.execute("SELECT COUNT(*) FROM auth_events" + where, params).fetchone()[0]
            total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(filters["page"], total_pages)
            rows = connection.execute("""
                SELECT id, event_time, portal_user, event_type, client_ip, client_ip_source,
                       SUBSTR(user_agent, 1, 512) AS user_agent,
                       SUBSTR(session_id, 1, 12) AS session_id_prefix,
                       SUBSTR(forwarded_for_untrusted, 1, 2048) AS forwarded_for_untrusted,
                       forwarded_for_present, proxy_headers_trusted
                FROM auth_events
            """ + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()

    events = []
    for row in rows:
        event = dict(row)
        timestamp = datetime.fromisoformat(event["event_time"])
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        event["event_time_kst"] = timestamp.astimezone(KST).isoformat(timespec="seconds")
        event["forwarded_for_untrusted"] = _forwarded_ips(event["forwarded_for_untrusted"])
        event["forwarded_for_present"] = bool(event["forwarded_for_present"])
        event["proxy_headers_trusted"] = bool(event["proxy_headers_trusted"])
        events.append(event)
    return {
        "events": events, "count": len(events), "total": total, "page": page,
        "page_size": PAGE_SIZE, "total_pages": total_pages,
        "has_previous": page > 1, "has_next": page < total_pages,
    }

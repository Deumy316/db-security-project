"""인증 결과만 관찰하는 선택적 감사 기능. 기존 인증·Flask 세션은 변경하지 않는다."""

from contextlib import closing
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3

from flask import g, request, session
from itsdangerous import BadData, URLSafeSerializer

COOKIE_NAME = "gigang_audit_id"


def _warn(app):
    try:
        app.logger.warning("Authentication audit unavailable; authentication continues unchanged.")
    except Exception:
        pass


def _append(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    with closing(sqlite3.connect(path, timeout=0.05)) as connection:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS auth_events (
                    id INTEGER PRIMARY KEY,
                    event_time TEXT NOT NULL,
                    portal_user TEXT,
                    event_type TEXT NOT NULL CHECK (event_type IN ('LOGIN_SUCCESS', 'LOGIN_FAILED', 'LOGOUT')),
                    client_ip TEXT,
                    client_ip_source TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    session_id TEXT,
                    forwarded_for_untrusted TEXT NOT NULL,
                    forwarded_for_present INTEGER NOT NULL,
                    proxy_headers_trusted INTEGER NOT NULL
                )
            """)
            connection.execute("""
                INSERT INTO auth_events (
                    event_time, portal_user, event_type, client_ip, client_ip_source,
                    user_agent, session_id, forwarded_for_untrusted, forwarded_for_present,
                    proxy_headers_trusted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event["event_time"], event["portal_user"], event["event_type"],
                event["client_ip"], event["client_ip_source"], event["user_agent"],
                event["session_id"], json.dumps(event["forwarded_for_untrusted"]),
                int(event["forwarded_for_present"]), 0,
            ))


def record_event(app, event):
    try:
        _append(app.config["AUTH_AUDIT_PATH"], event)
    except Exception:
        # 오류 원문에는 경로나 요청 정보가 포함될 수 있으므로 고정 메시지만 남긴다.
        _warn(app)


def _ip(value):
    try:
        if not value:
            return None
        address = ipaddress.ip_address(value.strip())
        return str(ipaddress.ip_address(address.packed))
    except ValueError:
        return None


def _cookie_id(app):
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    try:
        value = URLSafeSerializer(app.secret_key, salt="gigang-auth-audit").loads(cookie)
    except BadData:
        return None
    if isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value):
        return value
    return None


def _event(event_type, portal_user, session_id):
    forwarded = request.headers.get("X-Forwarded-For", "")
    addresses = [_ip(part) for part in forwarded[:2048].split(",")[:8]]
    return {
        "event_time": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "portal_user": portal_user[:128] if isinstance(portal_user, str) else None,
        "event_type": event_type,
        "client_ip": _ip(request.remote_addr),
        "client_ip_source": "remote_addr",
        "user_agent": request.headers.get("User-Agent", "")[:512],
        "session_id": session_id,
        "forwarded_for_untrusted": [value for value in addresses if value is not None],
        "forwarded_for_present": bool(forwarded),
        "proxy_headers_trusted": False,
    }


def register(app):
    if "auth_audit" in app.extensions:
        return
    app.config.setdefault("AUTH_AUDIT_PATH", str(Path(app.root_path) / "auth-audit" / "auth_audit.sqlite3"))

    @app.before_request
    def observe_auth_request():
        try:
            if request.endpoint == "login" and request.method == "POST":
                g.auth_audit = {"attempted_user": request.form.get("username", "").strip()}
            elif request.endpoint == "logout" and session.get("portal_user"):
                g.auth_audit = {"portal_user": session.get("portal_user"), "session_id": _cookie_id(app)}
        except Exception:
            _warn(app)

    @app.after_request
    def observe_auth_response(response):
        try:
            observed = getattr(g, "auth_audit", None)
            if observed is None:
                return response
            if request.endpoint == "login":
                success = response.status_code == 302 and bool(session.get("portal_user"))
                if not success and response.status_code != 200:
                    return response
                event_type = "LOGIN_SUCCESS" if success else "LOGIN_FAILED"
                portal_user = session.get("portal_user") if success else observed["attempted_user"]
                session_id = secrets.token_hex(16) if success else None
            else:
                if response.status_code != 302 or session.get("portal_user"):
                    return response
                event_type = "LOGOUT"
                portal_user = observed["portal_user"]
                session_id = observed["session_id"]

            record_event(app, _event(event_type, portal_user, session_id))
            if event_type == "LOGIN_SUCCESS":
                # 인증용 쿠키/Flask 세션과 별개의 상관관계 ID이며 권한 판단에 사용하지 않는다.
                cookie = URLSafeSerializer(app.secret_key, salt="gigang-auth-audit").dumps(session_id)
                response.set_cookie(COOKIE_NAME, cookie, httponly=True, samesite="Lax", secure=request.is_secure)
            elif event_type == "LOGOUT":
                response.delete_cookie(COOKIE_NAME, httponly=True, samesite="Lax", secure=request.is_secure)
        except Exception:
            _warn(app)
        return response

    app.extensions["auth_audit"] = True

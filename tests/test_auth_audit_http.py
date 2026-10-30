"""실제 WSGI/HTTP/쿠키 왕복 회귀 테스트. 운영 Docker·MySQL·감사 파일은 사용하지 않는다."""

from contextlib import closing
from http.cookiejar import CookieJar
from pathlib import Path
import secrets
import sqlite3
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, HTTPRedirectHandler, HTTPCookieProcessor, ProxyHandler, build_opener

from flask import g, request, session
from werkzeug.serving import make_server, WSGIRequestHandler

import auth_audit
import server


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


class QuietHandler(WSGIRequestHandler):
    def log(self, type, message, *args):
        pass


class AuthenticationAuditHTTPTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "http-audit.sqlite3"
        self.enterContext(patch.dict(server.app.config, {"AUTH_AUDIT_PATH": str(self.path)}))
        self.enterContext(patch.object(server.mysql.connector, "connect", side_effect=AssertionError("Real MySQL forbidden")))
        self.enterContext(patch.object(server, "get_user_connection"))
        self.warning = self.enterContext(patch.object(server.app.logger, "warning"))
        self.password = secrets.token_urlsafe(32)
        self.enterContext(patch.dict(server.DEPT_ACCOUNTS["cs_user"], password=self.password))
        self.stages = {}

        def before_snapshot():
            if request.endpoint == "logout":
                observed = getattr(g, "auth_audit", None)
                self.stages.update(
                    before_user=session.get("portal_user"),
                    session_cookie_present="session" in request.cookies,
                    audit_cookie_present=auth_audit.COOKIE_NAME in request.cookies,
                    observed_user=observed.get("portal_user") if observed else None,
                    observed_id_present=bool(observed and observed.get("session_id")),
                )

        def after_snapshot(response):
            if request.endpoint == "logout":
                self.stages.update(after_called=True, after_user=session.get("portal_user"),
                                   response_status=response.status_code)
            return response

        # 기존 훅을 그대로 실행하고 테스트 전용 관찰자만 임시로 덧붙인다.
        self.enterContext(patch.dict(server.app.before_request_funcs, {
            None: [*server.app.before_request_funcs.get(None, []), before_snapshot],
        }))
        self.enterContext(patch.dict(server.app.after_request_funcs, {
            None: [*server.app.after_request_funcs.get(None, []), after_snapshot],
        }))
        self.http_server = make_server("127.0.0.1", 0, server.app,
                                       threaded=True, request_handler=QuietHandler)
        self.thread = Thread(target=self.http_server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base_url = f"http://127.0.0.1:{self.http_server.server_port}"
        self.cookies = CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies), NoRedirect())

    def stop_server(self):
        self.http_server.shutdown()
        self.http_server.server_close()
        self.thread.join(timeout=2)

    def http(self, path, data=None):
        payload = urlencode(data).encode() if data is not None else None
        request = Request(self.base_url + path, data=payload, headers={
            "User-Agent": "Mozilla/5.0 GIGANG-local-http-test",
            "X-Forwarded-Proto": "https", "X-Forwarded-For": "100.64.10.20",
        })
        try:
            response = self.opener.open(request, timeout=5)
        except HTTPError as error:
            response = error  # 302 응답을 따라가지 않고 실제 쿠키 처리 결과를 검증한다.
        with closing(response):
            response.read()
            return response.code, response.headers

    def login(self):
        status, headers = self.http("/login", {"username": "cs_user", "password": self.password})
        self.assertEqual((status, headers["Location"]), (302, "/home"))

    def rows(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute("SELECT * FROM auth_events ORDER BY id")]

    def remove_cookie(self, name):
        for cookie in list(self.cookies):
            if cookie.name == name:
                self.cookies.clear(cookie.domain, cookie.path, cookie.name)

    def test_real_http_logout_observes_session_then_commits_before_cookie_deletion(self):
        self.login()
        self.assertEqual({cookie.name for cookie in self.cookies}, {"session", auth_audit.COOKIE_NAME})
        original_record = auth_audit.record_event
        with patch.object(auth_audit, "record_event", wraps=original_record) as record:
            status, headers = self.http("/logout")
            record.assert_called_once()
            self.assertEqual(record.call_args.args[1]["event_type"], "LOGOUT")
        self.assertEqual((status, headers["Location"]), (302, "/login"))
        self.assertEqual(self.stages, {
            "before_user": "cs_user", "session_cookie_present": True, "audit_cookie_present": True,
            "observed_user": "cs_user", "observed_id_present": True,
            "after_called": True, "after_user": None, "response_status": 302,
        })
        success, logout = self.rows()
        self.assertEqual((success["event_type"], logout["event_type"]), ("LOGIN_SUCCESS", "LOGOUT"))
        self.assertEqual(logout["portal_user"], "cs_user")
        self.assertEqual(logout["session_id"], success["session_id"])
        self.assertEqual(list(self.cookies), [])
        self.assertTrue(any(value.startswith(auth_audit.COOKIE_NAME + "=") and "Max-Age=0" in value
                            for value in headers.get_all("Set-Cookie")))
        self.warning.assert_not_called()

    def test_real_http_logout_without_audit_cookie_still_records_user(self):
        self.login()
        self.remove_cookie(auth_audit.COOKIE_NAME)
        status, headers = self.http("/logout")
        self.assertEqual((status, headers["Location"]), (302, "/login"))
        self.assertTrue(self.stages["session_cookie_present"])
        self.assertFalse(self.stages["audit_cookie_present"])
        self.assertEqual(self.stages["observed_user"], "cs_user")
        logout = self.rows()[-1]
        self.assertEqual((logout["event_type"], logout["portal_user"]), ("LOGOUT", "cs_user"))
        self.assertIsNone(logout["session_id"])
        self.assertEqual(list(self.cookies), [])

    def test_real_http_storage_failure_keeps_logout_redirect_and_session_deletion(self):
        self.login()
        with patch.object(auth_audit, "_append", side_effect=OSError("fixture storage failure")):
            status, headers = self.http("/logout")
        self.assertEqual((status, headers["Location"]), (302, "/login"))
        self.assertEqual(self.stages["before_user"], "cs_user")
        self.assertIsNone(self.stages["after_user"])
        self.assertEqual(list(self.cookies), [])
        self.assertEqual(len(self.rows()), 1)
        self.warning.assert_called_once()

    def test_real_http_missing_flask_session_does_not_infer_logout_from_audit_cookie(self):
        self.login()
        self.remove_cookie("session")
        with patch.object(auth_audit, "record_event") as record:
            status, headers = self.http("/logout")
            record.assert_not_called()
        self.assertEqual((status, headers["Location"]), (302, "/login"))
        self.assertFalse(self.stages["session_cookie_present"])
        self.assertTrue(self.stages["audit_cookie_present"])
        self.assertIsNone(self.stages["observed_user"])
        self.assertEqual(len(self.rows()), 1)


if __name__ == "__main__":
    unittest.main()

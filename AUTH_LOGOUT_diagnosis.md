# 실제 브라우저 LOGOUT 누락 보고 조사

## 확인된 원인

제공된 `2026-10-30 06:12:22 UTC` 사례는 저장 누락이 아니었다. Docker 실행 이력에서 동일한 SQLite 조회가 실제 로그아웃 전에 실행·완료된 것을 확인했다. 기존 인증 감사 판정 및 저장 로직은 수정하지 않았다.

| UTC 시각 | 확인한 사실 |
| --- | --- |
| 06:11:01.217722 | 실제 브라우저의 cs_user LOGIN_SUCCESS 행 |
| 06:11:42 | 제공된 SELECT와 일치하는 Docker exec 조회 시작·종료, exit 0 |
| 06:11:59 | 같은 조회의 마지막 시작·종료, exit 0 |
| 06:12:22.188465 | SQLite에 저장된 cs_user LOGOUT 행의 event_time |
| 06:12:22.195264010 | Docker가 기록한 실제 GET /logout → 302 access log 시각 |
| 06:12:51 | test_client 격리 테스트 Docker exec 실행 |

조회 식별에는 `auth_events ORDER BY id DESC LIMIT 10`을 포함한 명령인지 여부만 사용했고 명령 본문·자격증명은 출력하지 않았다. exec_start와 동일 execID의 exec_die를 대조하여 조회 완료 시각까지 확인했다. 표의 시간대는 모두 UTC이며 한국시간은 9시간을 더한다.

현재 같은 컨테이너의 `/app/auth-audit/auth_audit.sqlite3`에는 LOGIN_FAILED 3건, LOGIN_SUCCESS 1건, LOGOUT 1건이 있다. LOGOUT의 User-Agent는 브라우저 종류이고, 피어는 loopback이 아니며, 감사 session_id가 존재한다. 이는 제공된 test_client 결과(session_id=None)와 다른 행이다. 실제 세션·감사 쿠키 원문과 session_id 값은 조회 출력하지 않았다.

따라서 ‘test_client는 정상인데 브라우저에서는 저장되지 않는다’는 차이는 이 사례에서 확인되지 않았다. 초기 조회에 LOGIN_SUCCESS까지만 있었던 것은 그 조회가 LOGOUT 발생보다 약 23초 먼저 완료됐기 때문이다.

## 실제 실행 코드 및 단계별 판단

- 실행 컨테이너의 `server.py`, `auth_audit.py` 해시가 작업 폴더와 일치했다.
- `AUTH_AUDIT_PATH`는 `/app/auth-audit/auth_audit.sqlite3`, 감사 extension과 before/after 훅은 정상 등록됐으며 중복 훅은 없었다.
- 실제 SQLite 스키마의 session_id는 nullable이며 파일 쓰기 권한도 확인됐다. 컨테이너 로그에서 감사 warning은 발견되지 않았다.
- 과거 요청의 g/session 메모리는 이미 소멸했으므로 직접 읽을 수 없다. 다음 사항은 동일한 실행 코드와 실제 저장 결과에서 도출한 판단이다.
  - before 훅의 포털 사용자는 cs_user였으며 g.auth_audit에 보존됐다. LOGOUT 행의 portal_user는 이 스냅샷에서만 가져온다.
  - 저장된 session_id가 있으므로 해당 요청의 감사 쿠키가 검증되어 ID를 추출했다.
  - after 훅과 record_event가 실행되어 SQLite 행이 커밋됐다. after 훅이 판별하는 처리 후 포털 세션은 비어 있었다.
- record_event의 SQLite 쓰기·커밋은 동기 처리다. response.delete_cookie는 그 다음이다. 삭제 쿠키 처리에 오류가 있더라도 이미 커밋된 행을 제거하지 않는다.
- session.clear는 Flask 세션을 비우며 g의 별도 스냅샷을 지우지 않는다. 코드에서 비동기 저장·지연 큐는 사용하지 않는다.
- 순서 검증을 위해 로그아웃을 추정 기록하거나 감사 쿠키를 인증 근거로 사용하는 fallback은 추가하지 않았다.

## 추가한 회귀 테스트

`tests/test_auth_audit_http.py`는 test_client 대신 실제 localhost WSGI 서버와 HTTP 클라이언트·CookieJar를 사용한다. 프록시 전달 헤더를 포함하고 기존 Flask 훅/세션/리다이렉트 코드를 실행한다. MySQL 연결은 가짜로 대체하고, 감사 저장소는 임시 SQLite로 격리한다. 운영 Docker 또는 감사 volume에 테스트 이벤트를 쓰지 않는다.

1. 실제 HTTP 로그인 후 두 쿠키가 전달되는지, logout before 시점의 사용자와 g 스냅샷, after 시점의 세션 삭제, record_event 호출, SQLite 커밋과 쿠키 삭제를 확인한다.
2. 감사 쿠키가 없어도 Flask 세션이 유효하면 사용자 LOGOUT을 session_id=None으로 기록한다.
3. 저장 오류가 있어도 302 /login 이동과 Flask 세션·감사 쿠키 삭제가 유지된다.
4. Flask 세션 쿠키가 없으면 감사 쿠키만으로 사용자 LOGOUT을 추정하지 않는다.

전체 테스트 결과: **91개 통과 / 0개 실패**(기존 87개 + 실제 HTTP 4개). Windows 샌드박스는 localhost 접속을 제한해 처음 HTTP 테스트 4개가 환경 오류로 중단됐으며, localhost 통신을 허용한 실행 환경에서 전체를 다시 실행해 모두 통과했다. 테스트 서버와 파일은 테스트 종료 시 정리된다.

## 변경 범위

추가한 파일은 HTTP 테스트와 이 조사 문서이며, 인증 감사 설명 문서에 검증 결과를 갱신했다. 인증 감사 런타임 코드, 로그인/로그아웃, 기존 세션·DB Portal·Viewer·DB 로그 API·SOC, Docker/Tailscale 설정과 실제 `.env`는 변경하지 않았다.

실행 중인 Docker는 소스/설정/스키마/이벤트 메타데이터/로그/과거 실행 이력을 읽기 전용으로 조사했다. 컨테이너 재시작·배포·설정 변경, MySQL 연결·변경, 기존 감사 행 수정·삭제는 하지 않았다.

# 관리자 Viewer 로그인 이력 탭

## 변경 파일

| 파일 | 변경 |
| --- | --- |
| `auth_events_viewer.py` | 인증 감사 전용 읽기·필터·페이지·KST 표시값 생성 |
| `server.py` | 신규 관리자 API `/api/viewer/auth-events`만 추가 |
| `templates/viewer.html` | 기존 DB 영역을 기본 활성 패널로 감싸고 탭과 로그인 패널 include 추가 |
| `templates/auth_events_panel.html` | 독립 로그인 필터·테이블·페이지 UI |
| `static/auth-events.js` | 탭·지연 조회·로그인 필터/페이지·안전한 텍스트 렌더링 |
| `static/app.css` | 새 탭·로그인 테이블·배지 스타일만 추가 |
| `tests/test_auth_events_viewer.py` | 신규 12개 테스트 |
| 이 문서 | 동작과 검증 범위 |

기존 서버 함수 33개는 현재 HEAD와 AST가 동일하다. `static/viewer.js`, `audit_viewer.py`, `auth_audit.py` 내용도 동일하다. 기존 로그인/로그아웃·감사 저장·MySQL·부서 권한·SOC·웹훅·기존 API는 수정하지 않았다. Docker/Tailscale·실제 `.env`도 변경하지 않았다.

## API

`GET /api/viewer/auth-events`

유효한 관리자 세션만 허용한다. 미로그인 401, 일반 사용자와 유효하지 않은 관리자 세션은 403이다. API Key로 관리자 세션을 대체할 수 없다.

| 파라미터 | 기본값 | 의미 |
| --- | --- | --- |
| `portal_user` | 전체 | 정확히 일치하는 계정명. 최대 128자. 실패 로그인에 입력된 미등록 계정명도 가능 |
| `event_type` | 전체 | LOGIN_SUCCESS / LOGIN_FAILED / LOGOUT |
| `page` | 1 | 1~1000000 정수. 마지막 페이지를 초과하면 마지막 페이지로 조정 |

사용자·이벤트 조건은 AND로 조합하고 SQL 파라미터로 바인딩한다. 페이지 크기는 50 고정이며 id 내림차순으로 최신 기록부터 반환한다. 잘못된 필터는 400이다.

응답은 `events`, `count`(현재 페이지), `total`, `page`, `page_size`, `total_pages`, `has_previous`, `has_next`를 포함한다. 결과가 없으면 0건과 1/1페이지다. 성공 응답은 `Cache-Control: no-store`다.

이벤트 반환 필드는 id, 원본 event_time, KST용 event_time_kst, portal_user, event_type, client_ip, client_ip_source, user_agent, session_id_prefix, forwarded_for_untrusted, forwarded_for_present, proxy_headers_trusted로 한정했다.

전체 session_id는 SELECT에서도 SUBSTR(...,1,12)로 축약하며 API나 HTML에 반환하지 않는다. 비밀번호·Flask 세션/감사 쿠키 원문·API Key·인증 토큰을 반환하는 필드나 요청은 없다. User-Agent는 최대 512자로 제한한다.

## 읽기 전용과 독립성

기존 AUTH_AUDIT_PATH를 사용하되 URI의 `mode=ro`로 파일을 열고 `PRAGMA query_only=ON`을 설정한다. 디렉터리·파일·테이블·인덱스를 생성하지 않는다. 원본 인증 이벤트를 수정하지 않는다. SQLite가 없거나 손상·잠금·읽기 실패·스키마 오류인 경우 내부 경로나 오류 원문을 숨긴 503 응답을 반환한다. 인증 저장 함수를 호출하거나 MySQL로 대체하지 않는다.

조회 모듈은 신규 API가 호출될 때만 import한다. 모듈 자체의 오류도 신규 API의 503으로 격리한다. `/viewer` 렌더링과 기존 DB API에서는 새 저장소를 열지 않는다. 원래 인증 감사 저장 훅은 이 GET 요청을 인증 이벤트로 기록하지 않는다.

COUNT와 최대 50건 조회 후 즉시 연결을 닫고, 시간·표시값 가공은 연결 해제 후 수행한다. SQLite 잠금 대기는 20ms이며 읽기 트랜잭션에는 25ms 기준의 VM 진행 콜백을 둬 과도한 스캔을 중단한다. 이 수치는 파일시스템 정지까지 포함한 전체 응답시간 보장이 아니다. 읽기 전용도 SQLite 읽기 잠금과 호스트 I/O를 사용하므로 자원 비용 자체가 0이라는 의미는 아니다. 저널 모드나 기존 저장 로직은 변경하지 않았다.

## 화면

- 기본은 **DB 접근 이력**. 기존 필터·집계·SQL 상세·페이지 컨트롤과 JavaScript는 그대로다.
- **로그인 이력**을 처음 선택할 때만 SQLite API를 호출한다. 탭별 결과·필터·페이지 상태가 분리된다. 이후 조회 버튼이나 페이지 이동으로 새로 조회한다.
- 로그인 탭에는 시간(KST), 포털 사용자, 이벤트 배지, 직접 연결 IP, 전달 IP(미신뢰), User-Agent 요약/원문 펼치기, session_id 앞 12자만 표시한다.
- UTC 원본 event_time은 유지하고 별도의 event_time_kst를 +09:00으로 생성한다. 기존 DB 탭의 시각 표시는 변경하지 않았다.
- 전달 IP는 유효한 IP 문자열만 별도 배열로 다루며 실제 이용자 IP나 신원으로 단정하지 않는다. 직접 연결 IP에도 Docker·프록시 주소가 포함될 수 있음을 안내한다.
- User-Agent 요약은 클라이언트 제공 문자열에서 브라우저·OS를 간략 추출한 표시용 값이다. 인증·위험 판정에 사용하지 않는다. 원문은 클릭해서 볼 수 있으며 모든 동적 값은 textContent로 렌더링한다.
- 조회 실패 시 로그인 패널의 결과와 상태만 갱신한다. 기존 DB 접근 패널에는 영향을 주지 않는다. 모바일에서는 필터가 세로로 배치되고 테이블은 내부 가로 스크롤을 제공한다.

## 검증

`python -W error::ResourceWarning run_tests.py`

**전체 103개 통과 / 0개 실패**(기존 91개 + 신규 12개).

관리자 세션·401/403, 이벤트 3종, 미등록 사용자와 조합 필터, SQL 바인딩, 50/50/27건 페이지, KST 변환·원본 유지, session_id/민감 필드 미노출, ro/query_only와 파일 해시·mtime 유지, 파일 없음·손상·잠금·조회 중단 시 기존 DB Viewer 정상 응답을 검증했다.

격리된 로컬 미리보기에서는 기본 DB 탭에서 인증 조회 0회, 로그인 탭 첫 진입/페이지/필터 적용에만 조회 발생을 확인했다. 테스트용 인증 기록 128건의 첫 페이지 50건·마지막 28건, 조합 필터 21건, UA 원문 펼치기, KST·축약 ID·IP 라벨과 DB 탭 상태 보존을 확인했다. 모바일 페이지 전체의 가로 넘침은 없고 테이블 내부 스크롤이 동작했다. 브라우저 JavaScript 오류는 없었다.

실제 운영 Docker·MySQL·인증 SQLite volume은 이번 작업에서 조회하거나 변경하지 않았다. 운영 배포도 수행하지 않았다. 미리보기는 임시 SQLite와 메모리 MySQL 대체 데이터를 사용했고 종료했다.

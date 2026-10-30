# GIGANG Portal / Audit Viewer UI 개선

## 변경 범위와 호환성

- `templates/base.html`, `static/app.css`에 공통 반응형 레이아웃과 다크 테마를 모았다.
- 로그인 인증과 부서 계정 구조는 유지한다. 포털 검색/다운로드는 기존 `fetch_customer_rows()`와 부서별 컬럼 정의를 그대로 사용한다. 조회 전/빈 결과 안내, 권한 범위, 사용자 배지와 테이블 정렬을 제공한다.
- Viewer HTML과 JavaScript를 `templates/viewer.html`, `static/viewer.js`로 분리했다. 위험도 값이나 SOC 판정을 생성하지 않는다.
- 기존 `/api/db-logs`, `/api/db-logs/latest`, `/api/db-logs/since`의 처리 함수와 응답 구조는 변경하지 않았다. `user_name`, `portal_user`, `db_user` 및 모든 기존 필드를 유지한다.
- CSV 수집기, 시나리오 실행기, `.env`, 기존 환경변수, Docker/Tailscale 설정, MySQL 계정과 데이터는 변경하지 않았다. 로그 삭제/초기화 기능은 없다. 탐지/분석/등급 판정, 웹훅과 외부 SOC 소비자 코드는 변경하지 않는다.

## 신규 Viewer 전용 API

`GET /api/viewer/db-logs`는 기존과 동일한 `X-API-Key` 헤더 인증을 사용한다. 키를 URL이나 브라우저 저장소에 저장하지 않는다. 성공 응답은 `Cache-Control: no-store`다. 인증 실패는 401, 잘못된 필터는 400, DB 오류/시간 초과는 내부 오류 원문을 숨긴 503이다.

| 파라미터 | 기본값 | 의미 |
| --- | --- | --- |
| `page` | `1` | 1~1000000 정수. 마지막 페이지를 넘으면 마지막 페이지 반환 |
| `portal_user` | 전체 | 기존 허용 포털 계정 목록 중 하나 |
| `db_user` | 전체 | 기존 TARGET_USERS 목록 중 하나 |
| `action` | 전체 | DB_SELECT / EXCEL_DOWNLOAD |
| `start`, `end` | 제한 없음 | DB 세션 기준 ISO 날짜/시간. 양 끝 포함. 시간대 오프셋은 받지 않음 |
| `legacy` | `include` | include: 전체, exclude: 식별 불가 숨김, only: 식별 불가만 |

필터는 AND로 조합한다. `portal_user`와 `legacy=only`를 함께 지정하면 결과가 없다. 잘못된 필터는 DB 연결 전에 거부한다. 모든 필터 값과 계정·테이블 매칭 값은 파라미터로 바인딩한다.

응답에는 `logs`, `count`(현재 페이지), `total`, `page`, `page_size`(50 고정), `total_pages`, `has_previous`, `has_next`, `summary`가 있다. 결과가 없으면 0건, 1/1페이지를 반환한다. `summary`의 `total`, `db_select`, `excel_download`, `portal_users`는 현재 페이지가 아닌 현재 필터 전체 기준이다. 포털 사용자 집계에서 식별 불가 값은 제외한다. 로그 레코드의 필드 의미와 조회량 추정 방식은 기존 API와 같다.

`audit_viewer.py`는 MySQL 8 CTE와 정규식으로 식별·분류·필터링을 수행하고 DB에서 COUNT/SUM/COUNT DISTINCT와 LIMIT/OFFSET을 적용한다. 브라우저로 전체 로그를 보내지 않는다. 최신 `event_time`, 같은 시각이면 `thread_id` 내림차순이다. 같은 시각·같은 스레드에 완전히 구분되지 않는 로그에는 general_log의 고유 ID가 없으므로 완전한 순서를 보장하지 않는다.

식별은 기존 Python 파서와 동일한 SELECT 접두 주석 패턴 및 허용 계정→DB 계정 매핑을 따른다. SQL 문자열 값 속 표식, 미허용 계정과 매핑 불일치를 포털 사용자로 인정하지 않는다. 주석 없는 과거 로그는 `portal_user=null`, 화면에는 `기존 로그(식별 불가)`로 표시한다. DB 사용자를 포털 사용자로 추정하지 않는다. Action은 기존처럼 대소문자를 구분한 `GIGANG_EXCEL_DOWNLOAD` 포함 여부로 결정한다. SQL 원문과 두 감사 주석은 보존한다.

## 조회 및 운영 특성

- 신규 집계와 페이지 조회 각각에 `MAX_EXECUTION_TIME(5000)` 힌트를 적용한다. 기존 API 쿼리나 서버 전역 설정에는 적용하지 않는다. 브라우저의 요청 대기는 30초로 제한한다.
- CTE와 실행 시간 힌트의 문법은 [MySQL 공식 WITH 문서](https://dev.mysql.com/doc/refman/8.0/en/with.html), 정규식 추출은 [MySQL 공식 정규식 문서](https://dev.mysql.com/doc/refman/8.0/en/regexp.html)를 기준으로 작성했다.
- 원본 general_log 전체 집계에는 DB 스캔 비용이 든다. DB 인덱스·엔진·로그 보관 정책을 바꾸지 않았으므로 대량 로그의 실제 실행 계획과 지연은 운영 환경에서 별도 검증해야 한다. 실행 시간 힌트는 부하가 전혀 없다는 보장이 아니다.
- general_log는 실시간 변경되는 원본이며 집계와 페이지는 별도의 SELECT다. 그 사이 신규 이벤트가 들어오면 총계·페이지 경계가 변할 수 있다. 페이지 이동 중 신규 이벤트에 의한 중복/이동을 막는 스냅샷 또는 수집 저장소는 이번 범위에 추가하지 않았다.
- 시간대는 기존 로그의 DB 세션 기준을 유지하며 임의 변환하지 않는다.
- UI 오류/필터 오류를 처리하는 경로는 기존 SOC API 호출 경로와 분리했다. 신규 경로 실패가 기존 API 핸들러를 호출하거나 변경하지 않는다.

## 검증

`python -m unittest discover -s tests -v`

기존 23개 + 신규 12개 = 총 35개 통과. 신규 테스트는 메모리 SQLite에 MySQL 전용 함수만 번역해 필터·집계·페이지 쿼리를 실행한다. 실제 MySQL 연결은 차단한다. 127건의 페이지 분할, 조합 필터, 과거 로그, 인증, 파라미터 검증, SQL 바인딩, 오류 격리, 부서 권한과 URL/HTML 이스케이프를 검증한다. 기존 Viewer 테스트는 외부 JavaScript 파일을 확인하도록 조정했다.

브라우저에서는 운영 환경과 분리된 메모리 테스트 데이터로 로그인, 포털 빈 화면/조회 결과, Viewer 조회·집계·페이지 이동·조합 필터·초기화·SQL 펼치기 및 PC/모바일 레이아웃을 확인했다. JavaScript 문법 검사도 통과했다.

기존 버전과 AST를 비교해 인증·권한·SELECT 생성·감사 파싱·세 로그 API 관련 기존 함수 18개의 동작 코드가 동일함을 확인했다. 잘못된 키의 브라우저 오류 표시와 결과 초기화도 확인했으며 브라우저 JavaScript 오류는 없었다. 검증용 로컬 서버는 종료했다.

실제 MySQL의 신규 CTE/정규식 문법·실행 계획과 운영 SOC, Docker, Tailscale 접속은 이번 작업에서 실행하지 않았다. 운영 배포도 수행하지 않았다.

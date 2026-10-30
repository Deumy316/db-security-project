# 감사 로그 portal_user 추적 결과

## 결론

현재 저장소의 운영 코드에서 `cs_user` 세션으로 생성한 SQL 주석을 DB 연결 계정인 `sales_user`로 바꾸는 경로는 확인되지 않았다. 운영 코드와 UI는 수정하지 않았다. 테스트와 이 추적 문서만 추가했다.

제공된 행:

- 시간: `2026-10-30 12:07:03.657769`
- portal_user: `sales_user`
- db_user: `sales_user`
- SQL 접두 주석: `/* GIGANG_PORTAL_USER=sales_user */`

이 행은 원본 SQL 자체가 sales_user로 식별된 기록이다. cs_user 주석을 파서나 화면이 sales_user로 변환한 사례가 아니다. 현재 포털 코드가 이 SQL을 생성했다는 전제에서는 해당 요청의 세션 포털 ID가 sales_user였다는 의미다. 하지만 기록의 시간·주석만으로 당시의 실제 인증 세션, 브라우저, 배포 코드 버전 또는 외부 SQL 실행 여부는 확정할 수 없다. SQL 주석은 인증 증명이 아니므로 과거의 정상 로그인에서 생성됐다고 단정하거나 데이터를 정정하지 않는다.

Viewer의 기본 필터는 전체 포털 사용자·전체 DB 사용자다. 현재 사용하는 포털 계정에 맞춰 기존 행을 재귀속하지 않는다. 따라서 기존 sales_user 포털 기록과 다른 포털 사용자 기록이 함께 표시되는 것은 가능한 정상 동작이다. 특정 행의 생성 출처까지 확정하려면 해당 시각의 실제 세션/배포 이력이 추가로 필요하다. 실제 DB나 컨테이너를 조회하지 않았으므로 배포된 코드가 현재 작업 폴더와 같은지도 이번 점검으로 확정하지 않는다.

## 생성 및 표시 경로

1. 일반 로그인은 검증된 로그인 ID를, 관리자 로그인은 admin_user를 `session["portal_user"]`에 저장한다. DB 연결 ID를 세션에 저장하지 않는다.
2. `/db`의 `db_portal()`과 `/db/download`의 `download_db_excel()`이 각각 `session.get("portal_user")`를 읽어 `fetch_customer_rows(username, ...)`에 전달한다. 포털 고객정보 SQL 생성기의 운영 코드 호출자는 이 두 곳이다.
3. `fetch_customer_rows()`가 허용 계정을 검증한 뒤 SELECT 접두 주석을 만드는 지점은 한 곳이다. 일반 조회와 엑셀 다운로드가 이 지점을 공유하고 엑셀은 기존 GIGANG_EXCEL_DOWNLOAD 주석도 유지한다.
4. `get_user_connection()`은 함수 내부의 지역 변수만 `db_user_for()`를 통해 MySQL 계정으로 매핑한다. 호출자의 username이나 Flask 세션을 변경하지 않는다. cs/hr/fin/dev/admin은 기존 sales_user로 연결된다.
5. 기존 두 로그 경로 `load_logs()` / `load_logs_since()`는 원본 SQL의 유효한 접두 주석을 파싱한다. 유효한 표식이 없으면 portal_user는 None이다. DB 계정명 fallback은 없다.
6. Viewer용 `audit_viewer.py`도 같은 접두 패턴과 허용 포털→DB 계정 매핑을 적용한다. 정확하게 일치한 포털 계정을 반환하고, 나머지는 NULL로 둔다. DB 계정명으로 대체하지 않는다.
7. `static/viewer.js`의 포털 사용자 셀은 `log.portal_user`를 표시하고 없으면 `기존 로그(식별 불가)`로 표시한다. DB 사용자 셀의 `db_user ?? user_name` fallback은 별도 DB 사용자 열에만 적용된다.

`DB_USER`는 감사 수집용 연결에 사용되며 포털 식별 주석에 사용되지 않는다. 홈의 최근 활동도 기존 감사 조회 모듈을 읽기 전용으로 사용한다. 테이블 건수 조회·감사 수집 SELECT, CSV 수집기, 시나리오 실행기, 수동 DB 점검 스크립트에는 포털 식별 주석을 생성하는 코드가 없다. 특히 시나리오 실행기의 sales_user 연결만으로 portal_user=sales_user가 생기지는 않는다.

기존 API의 `user_name`은 호환성을 위해 DB 계정명을 유지한다. 부서별 구분에는 `portal_user`를 사용해야 한다. 또한 기존 `user_name=sales_user` 필터는 DB 계정 기준 조회도 허용하므로 다른 부서 기록을 포함할 수 있다. 이 동작도 변경하지 않았다.

## 원본 주석별 판단

| 원본 SELECT 접두 주석 | 현재 코드의 portal_user | 해석 |
| --- | --- | --- |
| GIGANG_PORTAL_USER=cs_user | cs_user | sales_user DB 연결이어도 cs_user 유지 |
| GIGANG_PORTAL_USER=sales_user | sales_user | SQL 자체에 sales_user가 기록됨. 생성 세션/버전은 별도 확인 필요 |
| 유효한 식별 주석 없음 | null | 과거/직접 DB 조회 등일 수 있으나 포털 사용자를 추정하지 않음 |

## 회귀 검증

`tests/test_audit_identity_trace.py`에서 실제 로그인·세션·SQL 생성·DB 계정 매핑·Python 파서·Viewer SQL 파서를 연결해 검증했다. MySQL connector 경계만 가짜 연결로 바꾸고 감사 저장소는 메모리 SQLite를 사용했다. 실제 MySQL 서버 문법/실행 환경 검증을 대체하지는 않는다.

- cs/hr/fin/dev/admin × 일반 조회/엑셀 = 10개 조합에서 주석과 portal_user가 실제 로그인 ID와 일치한다.
- 모든 연결의 실제 MySQL 사용자 인수와 파싱 결과 db_user는 sales_user다.
- DB_USER를 별도 값으로 설정해도 포털 주석에 섞이지 않는다.
- 요청 파라미터와 검색어에 sales_user 및 가짜 식별 주석을 넣어도 세션 ID를 덮어쓰지 않는다.
- DB 계정 매핑 뒤에도 세션 ID는 변경되지 않는다.
- 유효한 sales_user 원본 주석은 유지하고, 식별 없는 sales_user DB 로그는 portal_user=null로 유지한다.
- 전체 Viewer에서 서로 다른 계정의 행이 함께 보이고 portal_user=cs_user 필터에서는 sales_user 행이 제외된다.

전체 테스트 **70개 통과 / 0개 실패**. 기존 API, DB 구조·계정·데이터, Docker와 실제 `.env`는 변경하지 않았다. 과거 감사 로그도 수정하거나 삭제하지 않았다.

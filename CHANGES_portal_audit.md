# 포털 사용자 감사 로그 식별

부서별 로그인과 MySQL 연결 계정 매핑은 그대로 유지한다. 화면 조회와 엑셀 다운로드가 공통으로 사용하는 `fetch_customer_rows()`에서 허용 목록(`PORTAL_USERS`, `DEPT_ACCOUNTS`)에 있는 세션 사용자만 SQL 주석에 넣는다. 검색어는 기존처럼 SQL 파라미터로 전달한다.

```sql
SELECT /* GIGANG_PORTAL_USER=cs_user */ ... FROM customer_vault LIMIT 50
SELECT /* GIGANG_EXCEL_DOWNLOAD */ /* GIGANG_PORTAL_USER=cs_user */ ... FROM customer_vault LIMIT 50
```

`/api/db-logs`, `/api/db-logs/latest`, `/api/db-logs/since`는 기존 응답 필드를 유지하고 다음 두 필드를 추가한다.

```json
{
  "user_name": "sales_user",
  "portal_user": "cs_user",
  "db_user": "sales_user",
  "action": "DB_SELECT"
}
```

- `user_name`: 기존 SOC 연동 호환을 위해 DB 로그인 계정이라는 의미를 유지한다.
- `portal_user`: SELECT 바로 뒤(엑셀 표식 다음 허용)에 있는 식별 주석에서 추출한다. 허용된 포털 계정이며 실제 DB 계정 매핑과 일치할 때만 사용한다. 주석 없는 과거 로그와 잘못된 표식은 `null`이다.
- `db_user`: 기존 `user_host`에서 추출한 DB 로그인 계정이다.
- `user_name=sales_user` 필터는 기존대로 해당 DB 계정의 모든 로그를 반환한다. `user_name=cs_user`는 해당 포털 사용자 로그를 반환한다.
- `DB_SELECT` / `EXCEL_DOWNLOAD` 판정, SQL 원문, 시간 형식, 정렬, 제한, `/since` 커서는 유지한다.
- `/viewer`는 포털 사용자와 DB 사용자를 별도 열로 표시한다. 포털 사용자를 알 수 없으면 `—`를 표시한다. SQL은 HTML이 아닌 텍스트로 렌더링한다.
- CSV 수집기의 스키마와 동작은 변경하지 않는다. SOC에서 부서 구분이 필요하면 API의 `portal_user`를 사용한다.

SQL 주석은 포털의 식별 메타데이터이며 DB 인증 증명이 아니다. 공유 DB 계정으로 직접 SQL을 실행할 수 있는 사용자는 같은 주석을 작성할 수 있다. 과거 로그의 부서를 소급해서 추정하지 않는다.

## 검증

```powershell
python -m unittest discover -s tests -p test_portal_audit.py -v
python -m unittest discover -s tests -v
```

신규 테스트는 MySQL 연결 함수를 차단하고 가짜 커서를 사용한다. 기존/부서별 포털 로그인, 조회 및 다운로드 표식, 입력값 격리, 미허용 계정 거부, 로그 파싱, 세 API 경로, 기존 필터, action과 커서 유지, 뷰어 열을 검증한다.

`.env`, Docker 설정, MySQL 계정과 데이터는 변경하지 않는다. 실제 DB 및 운영 SOC 연결 검증은 수행하지 않는다.

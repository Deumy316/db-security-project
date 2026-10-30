# 기강 DB Portal 변경 (2026-10-06) — 부서별 화면 + 은행별 계좌 형식

바뀐 파일 2개: `server.py`, `templates/db.html` (DB·`.env`·Docker 설정은 그대로)

## 바뀐 것

1. **부서별 화면 탭** — `/db?view=all|cs|hr|fin` (기본 `all` = 예전 화면과 같은 열 + 은행)
   | 탭 | 보이는 열 |
   |---|---|
   | 전체 | 고객번호, 고객명, 주민등록번호, 휴대전화, 이메일, 주소, 은행, 계좌번호, 신용카드번호, 잔액, 고객등급 |
   | 고객지원 | 고객번호, 고객명, 휴대전화, 이메일, 주소, 고객등급 |
   | 인사 | 고객번호, 고객명, 주민등록번호, 휴대전화, 이메일, 주소, 은행, 계좌번호 |
   | 재무·정산 | 고객번호, 고객명, 은행, 계좌번호, 신용카드번호, 잔액, 고객등급 |
   - 조회 SQL 도 그 탭의 열만 가져옴. 엑셀 다운로드도 같은 열 (`gigang_customer_data_cs.xlsx` 등, 전체는 이름 그대로)
2. **은행 열 + 은행별 계좌 형식** — DB 데이터는 그대로. 화면·엑셀에 낼 때만 고객번호로 은행(국민·하나·기업)을 정하고
   숫자 14자리를 그 은행 형식으로 하이픈을 다시 붙임 (국민 6-2-6, 하나 3-6-5, 기업 3-6-2-3).
   예전 3-3-8 은 실제 은행 형식이 아니라 GIGANG 계좌 규칙에 안 잡혔음.
3. 같은 SQL 3벌(화면) + 3벌(엑셀) 을 `fetch_customer_rows()` 하나로 합침.

## 그대로인 것

- 로그인 계정·비밀번호, MySQL 데이터, 감사 로그 API (`/api/db-logs*`, `/viewer`)
- 감사 로그 조건: SQL 은 여전히 `SELECT … FROM customer_vault … LIMIT 50`, 엑셀은 `/* GIGANG_EXCEL_DOWNLOAD */` 표시
- 주민번호·카드번호 마스킹

## 반영 방법 (DESKTOP-OLI)

```
# db-security-project 폴더에 server.py, templates/db.html 덮어쓰기 (기존 파일은 백업)
docker compose up -d --build
```

## 확인한 것

가짜 DB 연결로 탭 5개(전체·고객지원·인사·재무·잘못된 값) 화면·엑셀을 만들어 GIGANG 규칙 검사기로 확인.
계좌번호 5줄 → 5건 탐지 (예전 0건). 실제 MySQL 연결로는 아직 확인 안 함.

---

# 추가 변경 (2026-10-06 15시) — 부서별 로그인 아이디

바뀐 파일: `server.py`, `templates/db.html`, `.env.example`

| 아이디 | 보이는 화면 | 비밀번호 환경변수 |
|---|---|---|
| `cs_user` | 고객지원 (휴대전화·이메일·주소) | `GIGANG_CS_PASSWORD` |
| `hr_user` | 인사 (+ 주민번호·은행·계좌) | `GIGANG_HR_PASSWORD` |
| `fin_user` | 재무·정산 (은행·계좌·카드·잔액) | `GIGANG_FIN_PASSWORD` |
| `dev_user` | 개발(운영 점검) — 전체 열 | `GIGANG_DEV_PASSWORD` |
| `sales_user`, `report_user` | 예전처럼 탭으로 모든 화면 | (기존 그대로) |

- 부서 아이디는 탭 없이 자기 화면만 보임. 주소에 `view=` 를 바꿔 넣어도 자기 화면, 엑셀도 자기 화면 열만.
- **MySQL 계정은 새로 만들지 않음.** 부서 아이디도 MySQL 에는 `sales_user` 로 접속 → DB 감사 로그에는 `sales_user` 로 남음.
- 비밀번호 환경변수가 비어 있으면 그 아이디는 로그인 불가.

## 반영 방법 추가

`.env` 에 네 줄 추가 (비밀번호는 팀에서 정하기):

```
GIGANG_CS_PASSWORD=...
GIGANG_HR_PASSWORD=...
GIGANG_FIN_PASSWORD=...
GIGANG_DEV_PASSWORD=...
```

그다음 `docker compose up -d --build` (`.env` 를 바꿨으므로 다시 띄워야 반영)

## 개발(dev_user) 화면이 전체 열인 이유

- 설정: 개발자가 운영 장애 점검을 위해 운영 DB 를 넓게 조회하는 상황 (실제 회사에서도 흔함).
- 시연 목적: GIGANG 규칙 DEV-1 ("개발 부서는 고객 데이터를 1건만 반출해도 WATCH") 을 보여 주려면 화면에 고객 데이터가 있어야 함.
- 최소 권한 원칙과는 맞지 않음 → 발표 때 "넓은 권한을 가진 개발자의 반출을 GIGANG 이 잡는 장면" 으로 설명.
  줄이려면 `server.py` 의 `VIEWS["dev"]` 열 목록만 바꾸면 됨 (예: 고객번호·고객명·이메일·고객등급).

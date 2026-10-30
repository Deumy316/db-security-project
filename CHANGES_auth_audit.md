# 독립 인증 감사 로그

## 현재 구조 분석과 저장 방식

기존 일반 로그인은 포털 비밀번호 검증과 MySQL 연결 확인 후 세션을 생성한다. 관리자는 환경변수 비밀번호 검증 후 관리자 인증 표식을 세션에 생성한다. 성공 응답은 302, 기존 실패 응답은 200의 로그인 화면이다. 로그아웃은 기존대로 `session.clear()` 후 302 응답을 반환한다. 이 인증·세션·로그아웃 함수 본문은 수정하지 않았다.

기존 `compose.yaml`은 API 서비스 한 개와 `5000:5000` 포트만 정의하고 volume은 없다. Dockerfile은 `/app`에서 `python server.py`를 실행한다. 컨테이너의 쓰기 계층에만 파일을 두면 재시작에는 남지만 컨테이너 재생성 시 보존되지 않는다. 기존 `data/`는 CSV 파일 용도이며 영속 마운트가 아니다.

인증 이벤트는 MySQL·기존 CSV와 분리된 SQLite의 `auth_events` 테이블에 저장한다. JSONL보다 트랜잭션과 동시 쓰기, 추후 조건 조회를 다루기 쉽고 별도 서버나 새 pip 의존성이 필요하지 않다. SQLite는 Python 배포판의 표준 모듈을 사용하며, 누락되어 모듈 초기화가 실패해도 기존 서버는 고정 warning 후 계속 실행한다.

## 변경 파일

| 파일 | 역할 |
| --- | --- |
| `auth_audit.py` (신규) | 인증 요청/결과 관찰 훅, IP·메타데이터 수집, 감사 전용 ID, SQLite 저장, fail-open |
| `server.py` | 모든 기존 핸들러 뒤에 선택적 모듈 등록 코드만 추가 |
| `compose.auth-audit.yaml` (신규) | 기존 Compose와 함께 적용할 독립 인증 감사 volume |
| `.gitignore`, `.dockerignore` | 감사 파일을 Git 및 이미지 빌드에서 제외 |
| `tests/test_auth_audit.py` (신규) | 저장·비밀값 배제·IP 처리·장애 격리 테스트 |
| `tests/test_auth_audit_http.py` (신규) | 실제 localhost HTTP/쿠키를 이용한 로그아웃 회귀 검증 |
| `run_tests.py` (신규) | 기존 로그와 분리된 임시 감사 저장소에서 전체 테스트, 실제 MySQL 연결 차단 |
| 이 문서 | 운영 위치·수집 범위·검증·한계 |

기존 테스트 파일, 인증 판정, 세션 설정, 업무·Viewer 화면, DB SQL, MySQL general_log, DB 감사 API, Viewer API, SOC·위험 등급·웹훅, MySQL 계정/데이터, Dockerfile·원본 Compose·Tailscale 설정·실제 `.env`는 이번 작업에서 변경하지 않았다.

## 저장 위치와 영속성

- 컨테이너 내: `/app/auth-audit/auth_audit.sqlite3`
- 로컬 실행 기본값: 프로젝트 폴더의 `auth-audit/auth_audit.sqlite3`
- Flask 설정 `AUTH_AUDIT_PATH`로 테스트용 경로를 주입할 수 있다. 새 환경변수나 실제 `.env` 수정은 필요하지 않다.
- 최초 이벤트에서만 디렉터리·파일·테이블을 생성한다. 시작 시 저장소 연결을 요구하지 않는다. 새 디렉터리는 POSIX 0700, 새 파일은 0600을 요청하며 실제 접근 제어는 호스트/volume 권한에도 따른다.
- 원본 `compose.yaml`은 유지했다. **volume은 아직 적용되지 않았다.** 향후 배포 승인 후 다음처럼 추가 파일을 함께 사용해야 한다.

```powershell
# 이번 작업에서는 실행하지 않은 향후 배포 예시
docker compose -f compose.yaml -f compose.auth-audit.yaml up -d --build
```

이 설정은 논리 이름 `auth-audit`의 Docker named volume을 `/app/auth-audit`에 마운트한다. 실제 이름에는 Compose 프로젝트 접두사가 붙는다. 네트워크·포트·기존 서비스 옵션은 바꾸지 않는다. volume은 같은 프로젝트로 컨테이너를 재시작/재생성해도 유지되지만, volume 삭제·프로젝트 변경·호스트 디스크 장애는 보존을 보장하지 않는다. volume을 삭제하는 운영 명령은 실행하지 않았다. [Docker 공식 volume 문서](https://docs.docker.com/engine/storage/volumes/)

기본 Compose 파일만 사용하면 일반 컨테이너 재시작에는 파일이 남더라도 재생성 시 영속성은 확보되지 않는다. 전용 volume이 필요하다. Docker 엔진의 마운트 실패 등 프로세스 시작 전 인프라 오류는 애플리케이션 fail-open의 범위가 아니다.

## 기록 방식

기존 로그인 결과를 별도 `before_request` / `after_request` 훅이 관찰한다. 인증 성공 여부를 감사 저장 결과로 판단하지 않는다. 기존 Flask 세션에는 감사 필드를 추가하지 않는다.

| 이벤트 | 관찰 조건 | portal_user | session_id |
| --- | --- | --- | --- |
| LOGIN_SUCCESS | POST `/login`이 기존 302 성공 응답과 포털 세션을 생성 | 인증된 세션의 포털 ID | 매번 새 128비트 랜덤 ID |
| LOGIN_FAILED | POST `/login`이 기존 200 실패 화면 반환 | 입력한 로그인 ID(최대 128자) | null |
| LOGOUT | 로그인 상태에서 `/logout` 처리 후 기존 세션이 비고 302 반환 | 세션 삭제 전에 관찰한 포털 ID | 감사 쿠키가 유효하면 로그인 시 ID, 없거나 잘못됐으면 null |

LOGIN_FAILED는 기존 인증 흐름이 실패 화면을 반환했다는 뜻이며, 잘못된 비밀번호 외에 기존 MySQL 연결 실패도 포함한다. 비정상 500 응답을 비밀번호 실패로 추정하지 않는다. GET `/login`, 미로그인 로그아웃, 업무 조회·기존 API 요청은 인증 이벤트를 만들지 않는다. 브라우저 종료·세션 만료는 명시적 LOGOUT과 구별되며 자동 추정 기록하지 않는다.

`event_time`은 UTC ISO 8601 마이크로초와 `+00:00` 오프셋을 기록한다. 기존 DB 감사 로그는 DB 세션 기준 시각을 그대로 사용하므로, 양쪽을 시간으로 비교하려면 DB 시간대를 먼저 확인하고 정규화해야 한다. 기존 DB 이벤트에 인증 session_id를 추가하지 않았다.

기록 필드: `id`, `event_time`, `portal_user`, `event_type`, `client_ip`, `client_ip_source`, `user_agent`, `session_id`, `forwarded_for_untrusted`, `forwarded_for_present`, `proxy_headers_trusted`.

비밀번호·실패 비밀번호, 실제 Flask 세션 쿠키, API Key, Authorization 헤더, 관리자 인증 표식, 전체 요청/헤더/세션 dict는 수집하지 않는다. User-Agent는 512자로 제한한다. SQL 값은 모두 바인딩하며 저장 오류 원문도 warning에 포함하지 않는다.

## 감사 전용 ID와 쿠키

`secrets.token_hex(16)`으로 감사 ID를 만들고, 기존 Flask 비밀키와 전용 salt로 서명한 `gigang_audit_id` 쿠키에 보관한다. 저장소에는 서명·쿠키 원문 없이 랜덤 ID만 남긴다. 이 쿠키는 인증 토큰이 아니며 로그인·업무 권한·관리자 판정에 사용하지 않는다.

별도 쿠키만 응답에 추가하며 HttpOnly·SameSite=Lax를 적용한다. Flask가 HTTPS 요청으로 인식한 경우에만 Secure를 설정한다. 프록시의 X-Forwarded-Proto를 임의 신뢰하지 않는다. 브라우저 세션 쿠키이므로 기존 Flask 세션의 갱신·만료와는 별개다. 새 로그인에서 ID를 바꾸고 명시적 로그아웃에서 감사 쿠키를 제거한다. 쿠키 누락/변조나 브라우저 종료 후에는 로그인-로그아웃 연결이 불가능할 수 있다. 감사 쿠키 처리 실패도 기존 인증 결과를 바꾸지 않는다.

## IP 기록 원칙

저장소에는 신뢰할 reverse proxy, ProxyFix, Tailscale Serve/Funnel 설정이 없다. 확인된 구성은 Docker 포트 직접 노출뿐이다. 실제 서비스의 요청 헤더·피어 주소·Tailscale 경로는 이번에 관측하지 않았으므로 IP 출처를 단정할 수 없다.

- `client_ip`: 유효한 `request.remote_addr`만 기록. 없거나 IP 형식이 아니면 null이다.
- `client_ip_source`: `remote_addr`. 이는 앱의 직접 피어이며 최종 이용자의 실제 IP라는 보장이 아니다.
- `X-Forwarded-For`: 최대 2,048자에서 처음 8개 항목의 유효한 IP만 `forwarded_for_untrusted` JSON 배열로 별도 저장한다. client_ip를 덮어쓰지 않는다. 형식에 맞지 않는 원문은 저장하지 않는다.
- `forwarded_for_present`: 해당 헤더 존재 여부. `proxy_headers_trusted`는 항상 false다.
- X-Real-IP 등 다른 전달 헤더는 IP 선택에 사용하지 않는다.

Docker NAT, 프록시 또는 Tailscale 경로에 따라 피어 주소가 중계 주소일 수 있다. Tailscale처럼 보이는 대역이어도 실제 사람의 신원을 단정하지 않는다. 신뢰할 proxy의 경로·개수·직접 접근 차단을 확인하기 전에는 ProxyFix를 추가하지 않는다. [Flask 공식 프록시 설명](https://flask.palletsprojects.com/en/stable/deploying/proxy_fix/)

## 장애 격리와 한계

등록 오류는 서버의 선택적 등록 guard에서, 수집·쿠키·저장 오류는 모듈의 훅과 저장 함수 내부에서 처리한다. 고정 warning 자체가 실패해도 삼킨다. 오류 시 로그인 성공/실패 화면·리다이렉트·Flask 세션·로그아웃은 기존대로 유지된다. MySQL 연결이나 기존 API/SOC의 판단에 감사 저장 결과를 전달하지 않는다.

SQLite 잠금 대기는 50ms로 제한한다. 이벤트마다 트랜잭션을 커밋하고 연결을 닫는다. 재시도 큐·백그라운드 서비스는 추가하지 않았다. 동기 파일 쓰기이므로 소량의 지연이 추가되며 파일시스템 자체의 장기 정지까지 완전히 격리하지는 않는다. [Python SQLite timeout 설명](https://docs.python.org/3.14/library/sqlite3.html#sqlite3.connect)

Fail-open이므로 실패한 이벤트는 누락될 수 있고 자동 재전송하지 않는다. 저장소는 기존 MySQL과 논리적으로 분리되지만 호스트 CPU·물리 디스크까지 분리되는 것은 아니다. 보관주기·자동 삭제·위험 판정·알림은 이번 범위에 없으며 디스크 사용량과 백업은 별도 운영 관리가 필요하다.

Flask 응답 처리 단계에서 서버가 확정한 결과를 관찰하며, 브라우저가 쿠키를 실제 수신·저장했다는 증명은 아니다. 이 구조는 변조 방지 전용 외부 감사 장비를 대체하지 않는다.

## Viewer와 향후 연동

이번에는 저장만 추가했다. 기존 Viewer·DB 감사 API 응답은 그대로이며 인증 이벤트를 DB_SELECT/EXCEL_DOWNLOAD에 섞지 않는다. 추후 별도 관리자 API가 SQLite를 읽도록 확장할 수 있지만, 이번 단계에서 새 API나 UI 탭은 추가하지 않았다.

## 테스트

```powershell
python -W error::ResourceWarning run_tests.py
```

전체 91개 통과 / 0개 실패(기존 70개 + 인증 감사 17개 + 실제 HTTP 4개). 실제 MySQL 연결은 차단하고 전체 테스트의 감사 저장소는 임시 폴더에 둔다. 운영/로컬 기본 감사 파일은 생성하지 않는다. 기존 unittest를 서버 설정 그대로 실행하면 감사 이벤트가 기본 저장소로 기록될 수 있으므로 격리된 실행기를 사용한다. HTTP 테스트에는 localhost 소켓 통신이 가능한 환경이 필요하다.

로그인 성공·비밀번호 실패·DB 인증 실패·로그아웃, 비밀값 미수집, 랜덤 ID 재사용/교체, 변조 쿠키, 잠금·손상·저장 함수·수집·warning 오류, 헤더 위조, 입력 크기 제한, 기존 API 무변경을 검증했다. SQLite 파일을 닫고 다시 열어 커밋 데이터가 유지되는 것도 확인했다.

실제 Docker 실행·배포·volume 재시작 검증, 운영 요청 IP 관측, 실제 MySQL 접속·변경은 수행하지 않았다. 실제 `.env`, Tailscale과 네트워크도 변경하지 않았다.

후속 LOGOUT 조사에서는 이미 실행 중인 컨테이너와 SQLite를 읽기 전용으로 확인했다. 실제 브라우저 LOGOUT 저장을 확인했고 초기 조회가 로그아웃 이전에 완료된 사실을 Docker 실행 이력으로 검증했다. 자세한 타임라인은 `AUTH_LOGOUT_diagnosis.md`에 정리했다. 런타임 코드·배포 설정은 수정하지 않았다.

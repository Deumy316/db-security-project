# MySQL 접근 로그 CSV 수집

## SELECT 시나리오 실행

비정상 시나리오 4개를 한 번에 실행하려면 다음 명령을 사용합니다(실제 DB 연결).

```powershell
py run_scenarios.py --group abnormal
```

`S_A01 → S_A02 → R_A01 → R_A02` 순서로 실행합니다. 처음 sales_user 비밀번호를 입력하고, report_user로 바뀔 때 한 번 더 입력합니다. 같은 계정의 시나리오는 연결을 공유하며 비밀번호를 파일에 저장하지 않습니다. 각 시나리오의 시작/종료 시각과 완료 횟수를 따로 출력하고 실제 사용자 데이터는 출력하지 않습니다. 오류나 Ctrl+C가 발생하면 남은 시나리오는 실행하지 않습니다. 그룹 전환을 위한 추가 대기는 없으며 각 시나리오의 기존 반복 횟수와 대기시간을 유지합니다.

기존 `--scenario`와 `--list`도 그대로 사용할 수 있습니다. `--group`, `--scenario`, `--list`는 한 번에 하나만 지정합니다.

`run_scenarios.py`는 실험 SELECT를 실행하고 `export_access_log.py`는 로그를 수집합니다. 실행기는 CSV를 변경하지 않습니다.

목록 확인은 DB 접속이나 비밀번호 입력 없이 가능합니다.

```powershell
py run_scenarios.py --list
```

실제 실험 시 아래처럼 하나를 선택합니다. 다음 명령은 실제 DB에 연결합니다.

```powershell
py run_scenarios.py --scenario S_N01
py run_scenarios.py --scenario R_A01 --host localhost --port 3306
```

시나리오의 계정 비밀번호를 PowerShell에서 숨김 입력합니다. 비밀번호와 조회 결과 데이터는 출력하거나 파일에 저장하지 않습니다. ID, 계정, 정상/비정상 구분, 시작/종료 시각(현지 시간과 UTC 오프셋), 진행 횟수를 표시합니다. 시작 시각에는 비밀번호 입력과 연결 준비 시간이 포함됩니다.

| ID | 계정 | 구분 | 조회 | 반복 | 조회 사이 대기 |
|---|---|---|---|---|---|
| S_N01 | sales_user | 정상 | users LIMIT 1 | 5회 | 10초 |
| S_N02 | sales_user | 정상 | users LIMIT 5 | 3회 | 20초 |
| S_A01 | sales_user | 비정상 | users LIMIT 1 | 100회 | 없음 |
| S_A02 | sales_user | 비정상 | users 전체 | 5회 | 1초 |
| R_N01 | report_user | 정상 | users COUNT(*) | 3회 | 30초 |
| R_N02 | report_user | 정상 | users LIMIT 10 | 3회 | 20초 |
| R_A01 | report_user | 비정상 | users COUNT(*) | 100회 | 없음 |
| R_A02 | report_user | 비정상 | users 전체 | 5회 | 1초 |

모든 대상은 기존 `gigang_db.users`이며 정확한 SQL은 `--list`로 확인합니다. 결과를 끝까지 읽은 뒤 기다리며 마지막 조회 뒤에는 대기하지 않습니다. 오류는 재시도 없이 중단하고 Ctrl+C로 취소할 수 있습니다. 완료 횟수는 결과를 끝까지 읽은 조회 수이므로 실패/취소 시 서버의 요청 로그 수와 다를 수 있습니다.

시나리오 SQL은 SELECT뿐입니다. 드라이버는 연결 초기화 과정에서 세션 옵션을 설정할 수 있지만 DB 데이터, 계정, 서버 전역 설정을 변경하는 코드는 없습니다.

실제 DB 없이 연결과 sleep을 mock 처리한 실행기 테스트만 수행하려면:

```powershell
py -m unittest discover -s tests -p test_run_scenarios.py -v
```

## 로그 수집 개요

기존 `mysql.general_log`에서 `sales_user`, `report_user`, `analyst_user`의 SELECT 요청을 읽어 `data/access_log.csv`로 저장합니다. DB/테이블/계정을 생성하지 않으며 서버의 로그 설정도 변경하지 않습니다.

## 준비와 실행

PowerShell에서 프로젝트 폴더로 이동합니다.

```powershell
cd C:\Users\User\Desktop\db-security-project
py -m pip install -r requirements.txt
py export_access_log.py --user root
```

`root` 대신 기존의 로그 조회 가능 계정을 지정할 수도 있습니다. 접속 계정에는 `mysql.general_log`의 SELECT 권한이 필요합니다. 실험용 세 계정이 로그 조회 권한까지 가지고 있을 필요는 없습니다. 프로그램은 권한을 자동 변경하지 않습니다.

비밀번호 입력 안내가 나오면 터미널에 직접 입력하고 Enter를 누르세요. 입력 문자가 보이지 않는 것은 정상입니다. 비밀번호는 소스코드나 파일에 저장하지 않습니다.

주소나 포트가 다르면 다음처럼 지정합니다.

```powershell
py export_access_log.py --host localhost --port 3306 --user root
```

실제 DB에 접속하지 않고 실행 옵션만 확인하려면 `py export_access_log.py --help`를 사용합니다.

## 저장 내용

| 컬럼 | 의미 |
|---|---|
| event_time | MySQL 세션 시간대 기준 요청 시각 |
| user_host | 로그인 계정, 인증 계정 및 접속 출처 |
| command_type | 이 파일에서는 Query |
| argument | 서버가 받은 SQL 문자열 |

`user_host`가 `sales_user[sales_user] @ localhost [::1]`라면 `[` 앞의 `sales_user`로 사용자를 구분합니다. CSV에는 위 네 컬럼만 저장합니다.

조건은 `command_type = 'Query'`, 앞 공백을 제외하고 SELECT 키워드로 시작하는 SQL, 지정한 세 로그인 계정입니다. 대소문자는 구분하지 않습니다. 현재 수집 연결의 로그는 제외합니다. 정확한 조회 SQL은 `export_access_log.py`의 `SELECT_LOGS_SQL`에 있습니다.

이 첫 버전은 SQL 앞에 주석이 있는 조회, WITH로 시작하는 조회, command_type이 Execute인 준비된 문 실행은 포함하지 않습니다. 전체 SQL 문법을 해석하는 파서는 아닙니다. general_log는 요청 로그이므로 SELECT 성공 여부나 반환 행 수를 의미하지 않습니다.

## 파일 저장 방식

CSV 저장 직전에 Python의 `is_business_select` 함수로 시스템 조회를 추가 제외합니다.

- SELECT 다음에 시스템 변수(`@@version_comment`, `@@session...` 등)가 나오는 조회
- SELECT 다음에 `VERSION()`, `DATABASE()`, `USER()`, `CURRENT_USER()`, `SESSION_USER()`, `SYSTEM_USER()`, `CONNECTION_ID()`, `SCHEMA()`가 나오는 조회
- `mysql.`, `information_schema.`, `performance_schema.`, `sys.`로 한정된 이름을 사용하는 조회

대소문자, 공백, 일반 주석과 백틱을 고려하며 문자열 안의 시스템 이름은 제외 근거로 사용하지 않습니다. 원본 SQL은 수정하지 않고 CSV에 저장합니다. 업무 테이블 전체 조회와 COUNT 조회도 유지합니다.

이 필터는 알려진 시스템 조회를 제거하는 휴리스틱입니다. 사람이 직접 실행한 시스템 조회도 제외하며, 자동화가 실행한 업무 SQL은 구분할 수 없습니다. 시스템 이름과 같은 테이블 별칭도 제외될 수 있습니다. 완전한 SQL 파서가 아니며 기본적인 MySQL 인용 규칙을 가정합니다. 실행 주체를 확실히 구분하려면 별도의 실행 기록 등이 필요합니다.

DB에 접속하지 않는 테스트: `py -m unittest discover -s tests -v`

- CSV는 스크립트 위치를 기준으로 `data/access_log.csv`에 저장합니다.
- 실행할 때마다 조건에 맞는 현재 로그 전체를 다시 저장합니다. 기존 CSV에 추가하지 않고 교체합니다.
- 결과가 0건이면 헤더만 있는 CSV로 교체합니다.
- 임시 파일에 먼저 저장하므로 수집/저장 실패 시 기존 CSV를 보존합니다.
- UTF-8 BOM 인코딩을 사용하며 SQL의 쉼표와 줄바꿈은 CSV 규칙에 맞게 보존합니다.
- DB 접속 성공 전에는 실제 로그 CSV를 만들지 않습니다. `data/.gitkeep`은 빈 폴더를 유지하기 위한 파일입니다.

## 문제가 생기면

- 패키지를 찾지 못하면 위의 pip 설치 명령을 실행하세요.
- Access denied라면 계정과 비밀번호를 확인하세요.
- SELECT command denied라면 로그 조회 권한이 있는 기존 계정으로 실행하세요.
- CSV 저장이 거부되면 Excel에서 해당 CSV를 닫고 다시 실행하세요.
- 0건이면 세 계정으로 실행한 일반 SELECT가 general_log에 남아 있는지 확인하세요.

프로그램은 Connector/Python으로 접속하며 드라이버가 연결 초기화를 위해 세션 옵션을 설정할 수 있습니다. 서버 전역 설정은 바꾸지 않습니다. 기존 general_log가 ON이므로 접속과 수집 조회 자체는 서버에 자동 기록될 수 있습니다.

공식 참고: [Connector/Python 설치](https://dev.mysql.com/doc/connector-python/en/ch04s04s01.html), [접속 방법](https://dev.mysql.com/doc/connector-python/en/connector-python-example-connecting.html)

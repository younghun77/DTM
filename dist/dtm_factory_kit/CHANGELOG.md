# DTM Factory Kit — 작업 내역 (Changelog)

문서 작성일: 2026-05-26  
대상 배포 패키지: `c:\Users\USER\direct_test_mode\dist\dtm_factory_kit\` (zip: `dtm_factory_kit.zip`)

이 문서는 nRF52840 Dongle용 DTM RX 자동 테스트 패키지를 **nRF Connect Desktop /
별도 SDK 없이** 사용할 수 있도록 만들기 위해 진행한 작업 전체를 시간순으로
정리한 기록입니다.

---

## 1. 배포 패키지 구조

```
dtm_factory_kit/
├─ flash_dongle.bat          ← dongle 펌웨어 플래시 (nrfutil 사용)
├─ run_gui.bat               ← RX 테스트 GUI 실행 (pip 자동 설치 포함)
├─ README.md
├─ CHANGELOG.md              ← (이 문서)
├─ bin/
│   └─ nrfutil.exe           ← Nordic 통합 도구 (DFU 플래시용)
├─ firmware/
│   └─ dtm_dongle.zip        ← dongle 펌웨어 DFU 패키지
└─ tools/
    ├─ dtm_rx_runner.py      ← 통합 GUI (PC ↔ dongle ↔ DUT)
    ├─ dut_control.py        ← DUT 제어 라이브러리 (+ CLI)
    ├─ _ppk_to_pem.py        ← (선택) PuTTY .ppk → OpenSSH .pem 변환
    └─ private_key.pem       ← DUT SSH 키 (OpenSSH 포맷)
```

배포 ZIP: `c:\Users\USER\direct_test_mode\dist\dtm_factory_kit.zip` (~2.5 MB)

---

## 2. Dongle 펌웨어 (nRF52840 Dongle)

- 보드 오버레이/Kconfig 추가:
  - `boards/nrf52840dongle_nrf52840.overlay`
  - `boards/nrf52840dongle_nrf52840.conf`
- **Legacy USB CDC-ACM** 스택을 사용하도록 구성하여 별도 드라이버 설치
  없이 Windows에 "Nordic DTM USB" COM 포트로 인식되도록 했음.
- `west build` 결과물(merged.hex 등)을 `nrfutil pkg generate`로 묶어
  `firmware/dtm_dongle.zip` 생성.
- 플래시 절차: dongle RESET → `flash_dongle.bat COMxx` → 재인식.

`flash_dongle.bat`의 핵심 로직 (요약):

```bat
"%~dp0bin\nrfutil.exe" pkg display "%~dp0firmware\dtm_dongle.zip"
"%~dp0bin\nrfutil.exe" dfu usb-serial -pkg "%~dp0firmware\dtm_dongle.zip" -p %COM%
```

---

## 3. DUT 제어 라이브러리 — `tools/dut_control.py`

### 3.1 통신 채널
| 용도 | 프로토콜 | 비고 |
|---|---|---|
| BT TX 스크립트 실행 (`bt_tx_test_39ch.sh`, `bt_test_off.sh`) | SSH (paramiko) | OpenSSH PEM 키 사용 |
| Reboot (`SERVICE_ENABLE` → `REBOOT_FRAME`) | TCP 20000 (KU 바이너리 프레임) | 0x7E 종결 |

### 3.2 SSH 키 처리
- `private_key.pem` (OpenSSH PEM) / `id_ed25519` / `id_rsa` 중 첫 발견 파일 사용.
- `private_key.ppk`가 있으면 명확한 오류 메시지로 변환 안내.
- `_ppk_to_pem.py`: PuTTY 키를 paramiko 호환 PEM으로 변환하는 헬퍼.

### 3.3 SSH 사용자 fallback
- 기본 `SSH_USER = "root"` 이지만 환경변수 `DUT_SSH_USER`로 덮어쓰기 가능.
- 인증 실패 시 `("root", "ubuntu", "admin", "factory")` 순으로 재시도.

### 3.4 영구 SSH 세션 — `SSHSession` 클래스
GUI가 반복 작업을 할 때마다 SSH를 다시 여는 비용을 없애기 위해 도입.

```python
ssh = dut_control.SSHSession()
ssh.connect()           # 최초 1회 (또는 끊겼을 때 자동 재연결)
ssh.exec("…command…")   # 이후 재사용
ssh.is_alive()          # transport.is_active() 확인
ssh.close()
```

- `run_bt_tx_test(script, session=…)` / `bt_test_off(session=…)` 둘 다
  세션을 인자로 받아 재사용.
- 인자를 주지 않으면 기존 일회성 SSH 동작(레거시 CLI 호환).

### 3.5 Reboot 시 TimeoutError 방어
- DUT가 reboot 프레임 직후 네트워크를 끊기 때문에 `socket.create_connection`,
  `recv` 등에서 `TimeoutError` / `OSError`가 발생하던 문제 발생.
- `reboot_dut()`이:
  - reboot 프레임을 보낸 *이후*에 발생한 모든 I/O 오류를 **정상 종료로 간주**
  - reboot 프레임을 보내기 *전*에 연결 자체가 안 되면 그대로 예외 raise
  - `finally`에서 소켓 정리

### 3.6 CLI 서브커맨드
```
python dut_control.py test       # bt_tx_test_39ch.sh 실행
python dut_control.py tx_off     # bt_test_off.sh 실행
python dut_control.py reboot     # 리부트 프레임
python dut_control.py rx_start   # = test (의미상 별칭)
python dut_control.py rx_end     # = reboot (의미상 별칭)
python dut_control.py shell "ls -al"
python dut_control.py gui        # 미니 Tk GUI
```

---

## 4. 통합 GUI — `tools/dtm_rx_runner.py`

### 4.1 DTM 2-wire 프로토콜
- 19200 8N1, 2바이트 명령 / 2바이트 이벤트
- 명령 비트: `[CMD:2][freq:6][length:6][PKT:2]`
- 이벤트: bit7=0 → status(success/error), bit7=1 → packet_count `((b0&0x7F)<<8)|b1`

### 4.2 화면 구성
| 영역 | 컨트롤 |
|---|---|
| 상단 | Dongle COM 자동탐색 / Refresh / Open, Channel(0~39), Length(기본 37) |
| 메인 버튼 | **START RX TEST**(녹), **END RX TEST**(빨), **REBOOT DUT**(주황), **DTM Reset**, **Open CSV folder** |
| 자동화 행 | Iterations, RX duration(s), Cooldown(s), **AUTO RUN**(파랑), **AUTO RUN (no reboot)**(녹), **STOP**, **Plot CSV** |
| 로그 | ScrolledText (모든 SSH/DTM I/O 출력) |

### 4.3 버튼 동작 요약

#### START RX TEST
1. `dut_control.run_bt_tx_test("bt_tx_test_39ch.sh", session=…)` 를 비동기로
   호출 → DUT TX 시작.
2. dongle에 `CMD_RECEIVER` 송신.

#### END RX TEST  (← 사용자 요청으로 reboot 분리)
1. `CMD_END` 송신 → packet count 수신.
2. `D:\factory\YY-MM-DD\rx_result.csv` 에 저장.
3. **같은 SSH 세션**으로 `bt_test_off.sh` 실행 (재접속 X).

CSV 형식:

| test_index | timestamp | channel | length | rx_count |
|---|---|---|---|---|

#### REBOOT DUT  (신규 버튼)
- TCP로 `SERVICE_ENABLE`→`REBOOT_FRAME` 전송.
- DUT 네트워크가 끊기므로 호출 후 `self.ssh.close()` → 다음 호출 때 자동 재연결.

#### DTM Reset
- dongle에 `CMD_RESET` 전송 (상태기계 정렬).

### 4.4 AUTO RUN  (with reboot)
1. DUT TX 시작 (`run_bt_tx_test`, async)
2. `startup_delay = 3s` 대기
3. dongle `CMD_RESET` → `CMD_RECEIVER`
4. RX duration 동안 측정 (interruptible)
5. `CMD_END` → packet count 수신, CSV append
6. `reboot_dut()` → cooldown 동안 DUT 복귀 대기
7. STOP 버튼으로 즉시 중단 가능

### 4.5 AUTO RUN (no reboot)  (신규 버튼)
- 기존 AUTO RUN과 동일하나 매 iteration 끝에서 **reboot 대신 `bt_test_off.sh`**
  실행 → DUT TX만 정지 후 다음 iteration 진행.
- **SSH 세션을 끊지 않고 계속 재사용**하므로 매우 빠른 반복이 가능.
- 로그상 "cooldown" 대신 "gap" 으로 표기.

### 4.6 packet count = 0 문제 수정
초기 AUTO 구현에서 `subprocess.run([... dut_control.py test])`이
`bt_tx_test_39ch.sh`가 종료될 때까지 stdout을 읽으며 블로킹 → RX 명령이
타임아웃 이후에야 송신되며 RX 윈도우가 0초가 되던 문제.

**조치**
1. GUI가 `dut_control`을 **모듈 import** 하여 `SSHSession` 직접 사용 (subprocess 제거).
2. AUTO 루프는 `_dut_test_async()`로 fire-and-forget 호출.
3. DUT TX 안정화를 위해 `startup_delay=3s` 후 dongle `CMD_RESET → CMD_RECEIVER`.

### 4.7 Plot CSV  (신규 버튼)
- 당일 `rx_result.csv` 를 읽어 `test_index` vs `rx_count` 그래프.
- **matplotlib** 가 있으면 라인 + 평균선(`avg=…`) 그래프.
- 없으면 순수 Tk Canvas 막대그래프로 자동 폴백 (배포본은 추가 설치 불필요).

### 4.8 윈도우 종료 처리
- `WM_DELETE_WINDOW` 핸들러에서 AUTO 중지, SSH 세션 close, 시리얼 close.

---

## 5. 배포/실행 스크립트

### 5.1 `flash_dongle.bat`
- 인자로 COM 포트 받음 (`flash_dongle.bat COM9`) 또는 대화형 입력.
- `bin\nrfutil.exe`를 사용해 `firmware\dtm_dongle.zip` 을 DFU로 굽기.

### 5.2 `run_gui.bat`
```bat
%PY% -m pip show pyserial >nul 2>nul || pip install pyserial
%PY% -m pip show paramiko >nul 2>nul || pip install paramiko
%PY% "%HERE%tools\dtm_rx_runner.py"
```
- `py -3`을 우선 시도, 없으면 `python` 폴백.
- 최초 실행시 `pyserial`, `paramiko` 자동 설치.

---

## 6. 트러블슈팅 가이드 (반영된 케이스)

| 증상 | 원인 | 해결 |
|---|---|---|
| GUI Open 시 "Cannot communicate with the device" | nRF DTM 앱이 같은 dongle을 잡고 있음 | nRF DTM 앱 종료 후 dongle 재연결 |
| flash_dongle 후 SDFU 포트 안 보임 | 부트로더 미진입 | RESET 버튼 한 번 더 눌러 SDFU 모드 |
| `private_key.ppk` 만 있음 | paramiko가 .ppk 미지원 | `_ppk_to_pem.py` 또는 PuTTYgen 으로 PEM 변환 후 `tools/private_key.pem` 으로 저장 |
| Test End 시 `TimeoutError: timed out` | reboot 후 link 끊김 | `reboot_dut()`이 reboot 이후 I/O 오류를 정상으로 처리하도록 수정 |
| AUTO 시 rx_count 가 계속 0 | subprocess의 SSH stdout이 블로킹되며 RX 명령이 지연 | GUI를 모듈 import + async SSH 세션 사용으로 전환 |
| packet count = -1 | dongle 무응답 | "DTM Reset" 후 재시도 |
| END RX TEST 마다 SSH 재접속 | 매번 새 SSH | `SSHSession` 영구 세션 도입, END→`bt_test_off.sh`는 세션 재사용 |

---

## 7. 단독 사용 (CLI)

GUI 없이도 동일 동작을 명령행에서 수행 가능:

```cmd
python tools\dut_control.py test     :: DUT TX on (bt_tx_test_39ch.sh)
python tools\dut_control.py tx_off   :: DUT TX off (bt_test_off.sh)
python tools\dut_control.py reboot   :: DUT 리부트
python tools\dut_control.py gui      :: DUT 단독 GUI
```

---

## 8. 주요 변경 이력 (요약)

| 일자 | 변경 내용 |
|---|---|
| 초기 | dongle 펌웨어/오버레이, `dtm_rx_runner.py` 1차 GUI, `dut_control.py` 1차 |
| 중간 | 배포 패키지화(`dtm_factory_kit.zip`), `nrfutil.exe` 내장, `run_gui.bat` 의존성 자동설치 |
| 중간 | OpenSSH PEM 지원, `.ppk → .pem` 변환 헬퍼, SSH 사용자 fallback |
| 후반 | `reboot_dut()` TimeoutError 방어, **AUTO RUN / STOP / Plot CSV** 추가 |
| 후반 | END RX TEST에서 reboot 분리, **REBOOT DUT** 버튼 신설 |
| 후반 | END RX TEST에 `bt_test_off.sh` 실행 추가 |
| 후반 | **`SSHSession` 영구 세션** 도입(재접속 제거), **AUTO RUN (no reboot)** 버튼 추가, packet count=0 문제 해결 |
| 2026-05-26 | **Standalone(switch) 모드** 추가 — dongle 단독 RX 측정. Standalone에서는 AUTO/REBOOT 버튼 자동 숨김. |
| 2026-05-26 | **자동 실패 정지 + 로그 수집** — AUTO 중 `rx_count==0` 또는 알려진 실패 패턴 발견 시 루프 중단, `bt_test.log`/`bt_bootstrap.log` SFTP 다운로드, `cat /proc/tty/driver/serial` 덤프, `FAILURE_SUMMARY.txt` 자동 작성. |
| 2026-05-26 | 실패 패턴 추가: `[ERROR][BT]` (bt_test.log), `ff fd 01 08 55 00` firmware-crash 시그니처 (bt_test.log / bt_bootstrap.log, 텍스트·바이너리 모두). |

---

## 9. Standalone 모드 (dongle only) 동작 표

| 액션 | DUT-LINK | STANDALONE |
|---|---|---|
| START RX TEST | SSH `bt_tx_test_39ch.sh` + dongle RX | dongle RX만 |
| END RX TEST | `CMD_END` + CSV + `bt_test_off.sh` | `CMD_END` + CSV만 |
| REBOOT DUT | TCP reboot 프레임 | 버튼 비활성화 |
| AUTO RUN | 표시됨 | 화면에서 숨김 |
| AUTO RUN (no reboot) | 표시됨 | 화면에서 숨김 |
| Plot CSV | 표시됨 | 표시됨 |

---

## 10. 실패 자동 감지 / 로그 수집

| 트리거 | 동작 |
|---|---|
| `rx_count == 0` (END RX TEST) | 루프 중단 → 아티팩트 수집 |
| 로그에서 `[ERROR][BT]` 검출 | 루프 중단 → 아티팩트 수집 |
| 로그에서 `Fail, total boot time` 검출 | 루프 중단 → 아티팩트 수집 |
| 로그에서 `ff fd 01 08 55 00` 검출 (FW crash) | 루프 중단 → 아티팩트 수집 |

수집 폴더: `D:\factory\YY-MM-DD\logs\HHMMSS_iterN_<reason>\`
- `bt_test.log`, `bt_bootstrap.log` (SFTP)
- `FAILURE_SUMMARY.txt` (timestamp, channel, length, findings, `/proc/tty/driver/serial` 덤프)

---

## 11. 향후 개선 후보 (참고)

- `bt_bootstrap.log` 의 STOP 문자열 감지 시 자동 정지 (`run_test_loop_with_log_check.py` 와 동일 로직)를 GUI에 옵션 토글로 통합.
- CSV 결과를 채널별로 분리한 다중 그래프.
- DUT IP/포트, SSH 사용자, RX 채널 기본값을 `config.ini`로 외부화.
- 로그를 파일로도 동시에 저장(`logs/YYYY-MM-DD.log`).

---

(이 문서는 `dist/dtm_factory_kit/CHANGELOG.md` 로 배포됩니다.)

# DTM Factory Kit

nRF52840 Dongle용 Direct Test Mode(DTM) RX 자동 테스트 배포 패키지입니다.  
**별도 SDK / nRF Connect Desktop 설치 없이** 동작합니다.

## 폴더 구성
```
dtm_factory_kit/
├─ flash_dongle.bat          ← dongle에 펌웨어 플래시
├─ run_gui.bat               ← RX 테스트 GUI 실행
├─ README.md
├─ bin/
│   └─ nrfutil.exe           ← Nordic 통합 도구 (DFU 플래시용)
├─ firmware/
│   └─ dtm_dongle.zip        ← dongle 펌웨어 DFU 패키지
└─ tools/
    ├─ dtm_rx_runner.py      ← 통합 GUI (PC ↔ dongle ↔ DUT)
    ├─ dut_control.py        ← DUT 제어 라이브러리
    └─ private_key.ppk       ← (선택) DUT SSH 키
```

## 필수 사전 조건
- Windows 10/11
- **Python 3.8 이상**이 PATH에 있어야 함 (없으면 https://www.python.org/downloads/ 에서 설치, "Add Python to PATH" 체크)
- 네트워크에서 DUT(`160.48.249.98:20000`) 접근 가능 (RX 테스트 자동화시)

## 1) Dongle 펌웨어 플래시
1. PC에 nRF52840 Dongle을 꽂는다.
2. dongle 옆면의 작은 **RESET 버튼**을 한 번 눌러 빨간 LED가 깜빡(부트로더 = SDFU 모드)이 되게 한다.
3. Windows 장치 관리자에서 새로 잡힌 **COM 번호** (예: COM9) 확인.
4. `flash_dongle.bat` 더블클릭 → COM 번호 입력  
   또는 명령창에서:
   ```cmd
   flash_dongle.bat COM9
   ```
5. `[OK] Dongle programmed successfully.` 가 보이면 완료. dongle을 뺐다 다시 꽂으면 "Nordic DTM USB"라는 새 CDC ACM 포트로 잡힌다.

## 2) RX 테스트 GUI 실행
1. `run_gui.bat` 더블클릭. (최초 실행 시 `pyserial`, `paramiko`가 자동 설치됨)
2. GUI 창에서
   - **Dongle COM**: "Nordic DTM USB" 포트가 자동 선택됨 → **Open** 클릭
   - **Channel** (0~39), **Length** (기본 37) 설정
3. **START RX TEST** (녹색 버튼) → 
   - SSH로 DUT의 `bt_tx_test_39ch.sh` 자동 실행 (DUT가 BT TX 시작)
   - Dongle은 RX 명령 수신
4. 측정 완료 후 **END RX TEST** (빨간 버튼) →
   - Dongle로부터 수신 패킷 수(RX count) 수신
   - `D:\factory\YY-MM-DD\rx_result.csv` 에 자동 저장
   - DUT는 reboot되어 TX 정상 종료
5. **Open CSV folder** 버튼으로 결과 폴더 바로 열기.

### 저장되는 CSV 형식
| test_index | timestamp | channel | length | rx_count |
|---|---|---|---|---|
| 1 | 2026-05-20 16:42:38 | 39 | 37 | 1234 |

## 트러블슈팅
| 증상 | 해결 |
|---|---|
| "Cannot communicate with the device" (nRF DTM 앱) / GUI Open 실패 | dongle을 뺐다 다시 꽂기. nRF DTM 앱과 우리 GUI는 **동시에 못 켭니다**. |
| flash_dongle 후 SDFU 포트가 안 보임 | dongle RESET 버튼을 다시 눌러 부트로더 진입. |
| SSH 실패 | `tools/private_key.ppk` 위치/사용자명 확인. `tools/dut_control.py` 상단의 `SSH_USER` 값 변경 가능. |
| GUI에서 packet count = -1 | dongle이 응답하지 않음. "DTM Reset" 버튼 누른 뒤 재시도. |
| Python 설치되어 있는데 실행 안 됨 | `python --version` 으로 PATH 확인. PATH가 안 잡혔으면 Python 재설치 시 "Add to PATH" 체크. |

## 단독 사용 (수동)
GUI 없이 명령행에서도 사용 가능:
```cmd
python tools\dut_control.py rx_start   :: DUT TX 켜기
python tools\dut_control.py rx_end     :: DUT 리부트로 TX 끄기
python tools\dut_control.py gui        :: DUT 단독 GUI
```

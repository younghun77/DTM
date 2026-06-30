# DTM RX Runner — Session Changelog (2026-06-01)

> 본 문서는 `tools/dtm_rx_runner.py` 와 `tools/dut_control.py` 에 대해
> 이번 세션에서 적용한 **기능 추가 / 버그 수정 / 운영 절차**를 정리한 변경 이력이다.
> Confluence 게시용 시각화 파일은 `docs/DTM_RX_Runner_Confluence.html` 참조.

---

## 0. 화면 캡처

| GUI 메인 화면 | RX counts plot |
|---|---|
| ![GUI](images/rx_runner_gui.png) | ![Plot](images/rx_counts_plot.png) |

- **GUI**: AUTO RUN 22/1000 진행 중 화면. `STAGE` 인디케이터, 컬러 로그(녹/적/주/청), DUT-LINK 모드, REBOOT DUT/DTM Reset 버튼 등.
- **Plot**: 43회 실행 결과. `min=-1`(동글 응답 실패 1회), 정상 구간 `avg≈4899`, 최대 `15103`. 좌측 첫 두 막대는 워밍업/실패 구간으로 별도 분석 대상.

---

## 1. MANUAL 모드 확장 — 커스텀 로그 & 실패 패턴

OEM 마다 테스트 로그 경로와 실패 메시지 포맷이 다르므로, **MANUAL 모드**에서 사용자가 직접 지정할 수 있도록 확장.

### UI
- `MANUAL DUT CONNECTION` 다이얼로그 하단에 **`Logs & errors…`** 버튼 추가.
- 서브 다이얼로그:
  - **Remote DUT log paths** (한 줄에 하나) — SFTP로 추가 다운로드할 절대 경로.
  - **Custom failure patterns** (한 줄에 하나의 정규식, `|` 로 OR, `#` 주석).  → dlt filter / logcat 형식 그대로 입력 가능.
  - **Active error messages** 리뷰 패널 — built-in + user 패턴/경로를 한 곳에서 확인.
  - `Refresh review` / `Save` / `Cancel` 버튼, 정규식 유효성 검사 포함.

### 백엔드 (`tools/dut_control.py`)
- `fetch_dut_logs(..., extra_paths=None)` — 추가 경로도 SFTP 다운로드, 이름 충돌 시 자동 suffix.
- `analyze_dut_logs(..., extra_patterns=None)` — 각 user 라인을 **case-insensitive 정규식**으로 컴파일, 라인 단위 매칭 후 finding 출력.

### Fail 판정
- AUTO 루프의 `_collect_failure_artifacts()` / `_scan_logs_quick()` 가
  MANUAL 모드일 때 위 사용자 값을 자동 전달 → built-in 패턴(`[ERROR][BT]`, `Fail, total boot time`, FW crash 바이트 시퀀스) + user 패턴 매칭 결과를 모두 합산해 실패로 판정.

---

## 2. AUTO RUN — Dongle 무응답 자동 복구

### 증상
```
[TX-DTM] pre-RX reset: 00 00
[RX-DTM] <timeout> partial=
[TX-DTM] Receiver Test: 67 94
[RX-DTM] <timeout> partial=
[AUTO] Dongle did not accept RX: None
…
[RESULT] iter=2 rx_count=-1
```
DUT 리부트 직후 dongle의 **USB CDC-ACM endpoint가 멈춤** → 모든 DTM 프레임이 타임아웃.
이전에는 무한히 `-1` 만 누적되며 루프가 의미 없이 계속 진행됨.

### 수정
- `_reopen_dongle()` 신설 — 시리얼 핸들 close → 0.5 s 대기 → 재오픈 → `DTM Reset` 프로브.
- `_auto_worker()`:
  - `pre-RX reset` 응답이 `None` 이면 **자동 복구 1회 시도**. 실패하면 `_collect_failure_artifacts(reason="dongle_unresponsive")` 후 루프 종료.
  - `rx_count == -1` 도 실패로 처리 (기존엔 `0` 만 실패) → 동글 복구 시도, 실패 시 아티팩트 수집.
  - 헤더 상태등을 복구 결과(`recovered` / `stuck`)에 맞춰 갱신.

---

## 3. 동글 펌웨어 재플래시 절차 (운영 메모)

USB 리셋으로도 복구되지 않을 때, 부트로더 모드에서 재플래시.

```powershell
# 1) 동글이 nRF52 SDFU (VID:PID 1915:521F) 로 enumerate 되는지 확인
python -c "import serial.tools.list_ports as lp; [print(p.device,'|',p.description,'|',p.hwid) for p in lp.comports()]"

# 2) DFU 패키지 플래시
& "C:\Users\USER\.nrfutil\bin\nrfutil.exe" nrf5sdk-tools dfu usb-serial `
    -pkg "c:\Users\USER\direct_test_mode\dtm_dongle.zip" -p COM9
```
- 결과 `Device programmed.` 가 나오면 동글을 한 번 뽑았다 다시 꽂는다.
- GUI 재실행 시 `[OK] Auto-opened dongle on COMx @ 19200 8N1` 가 떠야 정상.
- 동글이 일반 모드일 때 부트로더로 다시 진입하려면 **RESET 버튼 살짝 짧게 누름**.

---

## 4. Plot 해석 가이드

캡처된 plot (`n=43`, `min=-1`, `avg=4899.09`, `max=15103`) 기준:

| 영역 | 의미 | 조치 |
|---|---|---|
| 좌측 1~2번째 막대 (값≈0~수백) | 워밍업 / DUT TX 미가동 / 동글 무응답 | 첫 1~2 iteration은 burn-in 으로 제외하거나, MANUAL `Cooldown` 을 늘려 검토 |
| 두 개의 최고점 (≈15103) | DUT TX와 RX 윈도가 완전히 정렬된 “이상치 상한” 케이스 | 정상. 회귀 시 평균 상승의 상한값으로 사용 |
| 안정 구간 (≈4000~5000) | 정상 동작 구간 | spec baseline 으로 채택 |
| `min=-1` 1점 | dongle Test-End 응답 미수신 (자동복구가 작동했어야 함) | 본 세션의 dongle 복구 패치로 향후 자동 복구 |

---

## 5. 적용 파일 요약

| 파일 | 변경 요지 |
|---|---|
| `tools/dtm_rx_runner.py` | MANUAL 다이얼로그 확장(`Logs & errors…`), `_open_logs_errors_dialog`, custom log/pattern StringVar, `_reopen_dongle`, AUTO 루프 무응답 복구/`-1` 실패 처리, 실패 아티팩트 수집 시 user 값 전달 |
| `tools/dut_control.py` | `fetch_dut_logs(..., extra_paths=)`, `analyze_dut_logs(..., extra_patterns=)` |
| `dist/dtm_factory_kit/` & `dist/dtm_factory_kit.zip` | 위 파일 동기화 및 재패키징 |
| `docs/DTM_RX_Runner_Changelog_2026-06-01.md` | 본 문서 |
| `docs/DTM_RX_Runner_Confluence.html` | Confluence Storage Format / HTML Macro 버전 |

---

## 6. 향후 후보

- CSV에 **상태 컬럼** (`ok` / `dongle_stuck` / `dut_fw_crash` / …) 추가.
- AUTO 결과 요약 다이얼로그 (성공률, 최저/평균/최대, 실패 iteration 목록).
- 운영자 프로파일 저장/로드 (OEM 별 MANUAL 설정 한꺼번에 import/export).

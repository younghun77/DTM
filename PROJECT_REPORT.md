# DTM Factory Kit — 과제 보고서

> nRF52840 동글 + DUT 기반 Bluetooth Direct Test Mode 자동화 툴킷
> Repository: <https://github.com/younghun77/DTM>

---

## ■ 과제 참여 인원
- 남영훈 (younghun.nam@lge.com)

---

## ■ 과제 내용

### ▸ 배경
- 본인은 BT(Bluetooth) S/W 개발자로서, **TX command를 발행했을 때 실제 RF TX signal이 정상적으로 출력되는지 개발 단계에서 직접 검증할 수 있는 수단이 부재**했다.
- 양산 공정에서는 LG 공정 PC에 설치된 전용 프로그램이 R&S **CMP180** 계측기와 USB로, DUT(test sample)와는 별도 통신 채널로 연결되어 다음과 같이 동작한다.
  - 공정 PC → DUT : TX command 송신
  - DUT → CMP180 : RF signal 송출
  - CMP180 → 공정 PC : 측정값 회신 → 공정 프로그램이 PASS/FAIL 판정
- 이 구조는 라인 검증용으로는 동작하나, R&D 관점에서는 다음 한계가 있었다.
  1. CMP180 + 공정 PC + 공정 프로그램이 함께 있어야만 검증 가능 → **개발자가 책상에서 즉시 reproduce 불가**, 라인 점유 필요.
  2. 합/불 결과만 표시되고, **테스트 중 실패 발생 시 로그는 tester가 별도로 DUT에 SSH 접속하여 수동으로 추출**해야 함. 야간/장시간 반복 시험에서 원인 분석 데이터 확보가 사실상 불가능.
  3. 채널/길이/반복 횟수 등 시험 조건을 개발자가 자유롭게 바꿀 수 없음.

### ▸ 목적
- CMP180 + 공정 PC 환경에 의존하지 않고, **개발자 PC + 저가의 nRF52840 동글(약 1만원대) + DUT** 만으로 TX 송출 여부를 검증하고, 실패 시 DUT 로그까지 자동 수집·분석할 수 있는 **Direct Test Mode 기반 RX 검증 툴킷(DTM Factory Kit)** 을 구축한다.

### ▸ 상세 내용
1. **DTM RX Runner GUI 신규 개발** (`tools/dtm_rx_runner.py`)
   - Bluetooth Core Spec Vol.6 Part F의 **DTM 2-wire 프로토콜 직접 구현** → nRF52840 동글을 RX 측정기로 사용 (CMP180 대체).
   - 동글 CDC-ACM 포트(Nordic VID `0x1915`) **자동 감지·자동 오픈**, 실패 시에만 수동 선택 카드 노출.
   - 다크 테마 카드 UI, STAGE 인디케이터 + 활성 버튼 컬러 링 하이라이트로 현재 단계 시각화.
   - 표준 DTM 명령: `Reset`, `Receiver Test`, `Test End` + 패킷 카운트 이벤트 파싱 (19200 8N1).

2. **DUT 제어 모듈** (`tools/dut_control.py`)
   - `paramiko` 기반 **영구 SSH 세션(SSHSession)** 으로 매 회 핸드셰이크 비용 제거.
   - `bt_tx_test_39ch.sh` 시작 / `bt_test_off.sh` 정지 / 캡처된 TCP 프레임 기반 reboot 시퀀스를 단일 API로 제공.
   - SFTP로 `bt_test.log`, `bt_bootstrap.log` 자동 수집, `/proc/tty/driver/serial` 덤프.
   - SSH 인증 일시 거부에 대비한 **3회 재시도 + 2초 백오프**.

3. **AUTO RUN 자동화 루프 + 자동 실패 진단**
   - 사용자가 지정한 횟수·RX duration·cooldown으로 무인 반복 실행 (with reboot / no reboot 2종).
   - 각 회차: `DUT TX 시작 → 동글 RX → END → CSV 저장 → bt_test_off.sh → 1s settle → reboot → 로그 자동 스캔`.
   - **실패 자동 정지**: `rx_count == 0` 또는 DUT 로그에서
     - `[ERROR][BT]`
     - `Fail, total boot time`
     - F/W 크래시 시그니처 `ff fd 01 08 55 00`

     중 하나라도 검출 시 즉시 정지하고, 로그·시리얼 드라이버 상태·요약 리포트를 `results/YY-MM-DD/logs/<HHMMSS>_iter<N>_<reason>/`에 자동 보관.

4. **결과 보존 & 분석**
   - 모든 측정 결과는 `<launcher_dir>/results/YY-MM-DD/rx_result.csv`로 누적 저장 (또는 환경변수 `DTM_RX_RESULT_BASE`로 오버라이드).
   - `Plot CSV` 기능: matplotlib 사용 가능 시 회차별 RX 카운트 라인 차트 + `min / max / avg` 기준선 + 통계 박스, 미설치 시 Tk 캔버스 폴백 차트.

5. **운영자 UX 현대화**
   - 다크 슬레이트 팔레트(`#0f172a` / `#1e293b`) + ttk `clam` 테마.
   - 카드 레이아웃: **TEST CONFIG / DONGLE PORT (manual, fallback) / ACTIONS / STAGE / LOG**.
   - 큰 primary 버튼(▶ START / ■ END / ⟳ AUTO / ⟳ AUTO no-reboot / STOP), 보조 버튼(REBOOT, DTM Reset, Plot CSV, Open results folder).
   - 현재 활성 동작에 따라 STAGE 텍스트와 버튼 컬러 링이 동기화되어, 운영자가 어느 단계인지 한눈에 파악 가능.
   - DUT 연결 없이 동작 가능한 **STANDALONE 모드** 토글 (외부 TX 소스와 함께 사용).

6. **배포 패키지화 + GitHub 공개**
   - `dist/dtm_factory_kit.zip` 한 파일로 BT 개발자/시험자 누구나 즉시 사용.
   - `run_gui.bat`, `flash_dongle.bat`, 펌웨어 hex, Python 도구, SSH key 포함.
   - `publish_to_github.bat` 한 번으로 소스 + 릴리스 zip을 <https://github.com/younghun77/DTM> 으로 자동 배포.

---

## ■ 주요 기술 결정

| 항목 | 결정 | 이유 |
|------|------|------|
| RX 측정기 | nRF52840 Dongle | 1만원대, USB-Only, DTM 2-wire 펌웨어(Nordic 공식 샘플) 지원 |
| 통신 방식 | DTM 2-wire (Bluetooth Core Spec) | 표준 명세, 추가 SDK 없이 19200 8N1로 직접 제어 |
| DUT 제어 | 영구 SSH 세션 + TCP 캡처 프레임 | 매 회 SSH 재핸드셰이크 비용 제거(~1.5s/회), reboot은 캡처된 raw frame 사용 |
| GUI | Tkinter/ttk (다크 테마) | 추가 런타임 의존성 없이 표준 Python으로 배포 가능 |
| 실패 자동 진단 | 로그 패턴 매칭 + 시리얼 드라이버 덤프 | 산발적 결함의 사후 분석 데이터 자동 확보 |
| 결과 경로 | `<launcher>/results/YY-MM-DD/` | 런처 위치 기준 → 어디서 실행해도 즉시 발견 가능 |

---

## ■ 워크플로우

```
[ 개발자 PC ]
    │  (USB CDC-ACM, 19200 8N1, DTM 2-wire)
    ├──────────────►  [ nRF52840 Dongle ]  ◄── RF (2.4 GHz, ch=0..39)
    │                                                │
    │  (SSH, paramiko, persistent)                   │
    └──────────────►  [        DUT        ] ─── TX ──┘
                       bt_tx_test_39ch.sh
                       bt_test_off.sh
                       reboot (TCP frame)
```

AUTO RUN 1 회차:

```
DUT TX 시작 ─► (3s 안정화) ─► Dongle RX ─► (N s 측정) ─► END ─► rx_count 저장
                                                                  │
              ┌── rx_count == 0 ──► 실패 아티팩트 수집 후 정지
              │
              ▼
   bt_test_off.sh ─► 1s settle ─► reboot ─► 로그 스캔 ─► 다음 회차
                                                 │
                                 ┌── 패턴 검출 ──► 실패 아티팩트 수집 후 정지
                                 ▼
                              계속
```

---

## ■ 기대 효과

- **(개발 즉시성)** CMP180 + 공정 PC + 공정 프로그램 + 라인 점유 없이, BT 개발자가 자기 자리에서 동글 하나로 TX 송출 정상 여부를 즉시 검증할 수 있어 디버깅 사이클이 대폭 단축된다.
- **(무인 신뢰성 시험)** 야간·주말 무인으로 수백~수천 회 반복 RX 시험이 가능하고, 실패 발생 시점의 DUT 로그·시리얼 상태가 자동 보관되어 산발적 결함의 재현·근본 원인 분석이 가능해진다.
- **(전사 표준화)** 패키지화된 툴킷과 GitHub 배포로 다른 BT 개발자/시험자도 동일한 환경을 1분 안에 갖출 수 있어 검증 결과의 일관성과 재사용성이 확보된다.
- **(비용 절감)** 1만원대 동글로 수천만원대 CMP180 환경을 대체할 수 있어, BT R&D 단계에서 별도 계측 장비 투자 없이 신규 펌웨어/HCI 변경 검증이 가능하다.

---

## ■ 정량적 지표 (ROI)

| 항목 | Before (CMP180 + 공정 PC 수동) | After (DTM Factory Kit) | 절감/개선 |
|------|---------------------|----------------------|----------|
| **TX 송출 검증 1회 소요 시간** | 약 10분 (라인 예약·세팅·수동 합/불 확인) | 약 30초 (`START RX` → `END RX`) | **약 95% 단축** |
| **100회 반복 RX 신뢰성 시험** | 약 16시간 (수동 반복·인력 상주) | 약 1.5시간 (AUTO RUN 무인 실행) | **약 90% 단축, 인력 상주 0** |
| **실패 발생 시 로그 확보 시간** | 약 15분/건 (SSH 접속·경로 탐색·수동 다운로드·grep) | 자동 수집·자동 분석·리포트 생성 → **0분 (즉시)** | **100% 자동화** |
| **장비 비용 (개발자 1인 환경)** | CMP180 + 공정 PC 라인 점유 (수천만원, 공유 자원) | nRF52840 동글 1개 (약 1만원) | **장비 비용 99% 이상 절감** |
| **신규 BT 펌웨어 검증 투입 인력** | 1.0 MM (수동 시험·로그 수집 전담 인력 필요) | **0.3 MM** (무인 시험·자동 리포트) | **약 0.7 MM 절감 / 사이클** |
| **BT 개발자 디버깅 사이클(코드 수정 → TX 검증)** | 약 1일 (라인 일정 대기) | **약 30분** (책상에서 즉시 검증) | **약 95% 단축** |

---

## ■ 변경 이력 요약

- **GUI 현대화**: 다크 테마 카드 레이아웃, STAGE 인디케이터, 활성 버튼 컬러 링 하이라이트.
- **동글 자동 감지/오픈**: Nordic VID 매칭, 실패 시 수동 picker fallback.
- **결과 저장 경로**: 런처 디렉토리 기준 `results/YY-MM-DD/` (환경변수 오버라이드 가능).
- **AUTO RUN 자동 정지**: `rx_count==0` / 로그 패턴 검출 시 정지 + 아티팩트 자동 수집.
- **AUTO RUN reboot 안정화**: `bt_test_off.sh → 1s settle → reboot` 순서로 수정하여 BT TX 점유 상태의 reboot 실패 문제 해결.
- **로그 스캔 견고화**: SSH 일시 단절 대비 3회 재시도 + 백오프, 연속 실패 누적 알림.
- **Plot CSV 강화**: min / max / avg 기준선 + 통계 박스 (matplotlib 및 Tk 폴백 모두).
- **배포 자동화**: `publish_to_github.bat`로 소스 + zip 일괄 push.

---

_Last updated: 2026-05-28_

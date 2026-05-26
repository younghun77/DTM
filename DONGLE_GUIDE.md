# nRF52840 Dongle — Direct Test Mode (DTM) 빌드/패키지/플래시 가이드

본 문서는 `direct_test_mode` 샘플을 **nRF52840 Dongle (PCA10059)** 용으로 빌드하고, USB DFU 패키지를 만들어 COM 포트로 굽는 전체 워크플로우를 정리한 것입니다.

작업 환경: Windows 11 / PowerShell / nRF Connect SDK **v3.3.0** (`C:\ncs\v3.3.0`) / 툴체인 `C:\ncs\toolchains\936afb6332`.

---

## 1. 문제 배경

`direct_test_mode` 샘플은 기본적으로 **UART 2-wire** 인터페이스로 PC와 통신합니다.
- **nRF52840 DK**: 온보드 J-Link IF가 USB↔UART 브리지 역할을 해주므로 그대로 동작합니다.
- **nRF52840 Dongle (PCA10059)**: USB-UART 브리지가 **없습니다**. USB는 nRF52840 칩 자체에 직결됩니다.

→ Dongle에서는 DTM 2-wire 트랜스포트를 **USB CDC ACM** 위로 매핑해야 PC의 *Direct Test Mode* 앱이 통신할 수 있습니다.

---

## 2. 추가/수정한 파일

### 2.1 `boards/nrf52840dongle_nrf52840.overlay` (신규)

`chosen ncs,dtm-uart`을 보드 기본 제공 CDC ACM 노드(`&board_cdc_acm_uart`, Zephyr `boards/common/usb/cdc_acm_serial.dtsi` 제공)로 매핑하고, 사용하지 않는 UART0을 비활성화합니다.

```dts
/ {
    chosen {
        ncs,dtm-uart = &board_cdc_acm_uart;
    };
};

&uart0 {
    status = "disabled";
};

&board_cdc_acm_uart {
    current-speed = <19200>;
};
```

### 2.2 `boards/nrf52840dongle_nrf52840.conf` (신규)

USB 디바이스 스택(새 스택)과 CDC ACM 클래스를 활성화합니다.

```ini
CONFIG_USB_DEVICE_STACK_NEXT=y
CONFIG_CDC_ACM_SERIAL_INITIALIZE_AT_BOOT=y
CONFIG_CDC_ACM_SERIAL_PRODUCT_STRING="Nordic DTM USB"
CONFIG_CDC_ACM_SERIAL_VID=0x1915
CONFIG_CDC_ACM_SERIAL_PID=0x520F

CONFIG_SERIAL=y
CONFIG_UART_LINE_CTRL=y
CONFIG_UART_INTERRUPT_DRIVEN=y
```

### 2.3 `src/transport/dtm_uart_twowire.c` (수정)

기존에는 nRF54H20에서만 `uart_irq_rx_enable()`을 호출했는데, 새 USB 스택의 CDC ACM(`CONFIG_USBD_CDC_ACM_CLASS`)을 사용하는 nRF52840 Dongle에서도 호출해야 PC→Dongle 방향 RX가 동작합니다.

```c
#if (defined(CONFIG_DTM_USB) && defined(CONFIG_SOC_NRF54H20_CPURAD)) || \
    (defined(CONFIG_USBD_CDC_ACM_CLASS) && defined(CONFIG_SOC_NRF52840))
    /* Enable RX path for the USB CDC ACM. */
    uart_irq_rx_enable(dtm_uart);
#endif
```

> **왜 `USBD_CDC_ACM_CLASS`인가?** 새 USB 스택(`USB_DEVICE_STACK_NEXT`)을 쓰면 Kconfig 심볼이 `USBD_CDC_ACM_CLASS`로 정의됩니다. 레거시 스택의 `USB_CDC_ACM`은 정의되지 않으므로 두 스택 모두 커버하려면 이 심볼을 사용해야 합니다.

### 2.4 `_build.bat` (헬퍼)

cc1.exe 메모리 부족을 피하기 위해 `-j2`로 빌드합니다.

```bat
@echo off
cd /d C:\ncs\v3.3.0
west build -b nrf52840dongle/nrf52840 -p always --sysbuild ^
  -d C:\Users\USER\direct_test_mode\build C:\Users\USER\direct_test_mode -o=-j2
```

---

## 3. 워크플로우 (수동 실행)

### 3.1 빌드

`direct_test_mode` 폴더 자체는 west workspace가 아니므로 **NCS 워크스페이스 안에서** `-d`/source path를 명시해 빌드합니다.

```powershell
& "C:\ncs\toolchains\936afb6332\nrfutil\bin\nrfutil.exe" toolchain-manager launch `
    --ncs-version v3.3.0 -- C:\Users\USER\direct_test_mode\_build.bat
```

결과 산출물:
- `build\direct_test_mode\zephyr\zephyr.hex`
- 메모리: FLASH ≈ 7.6 % / RAM ≈ 8.2 %

### 3.2 DFU 패키지 생성

Dongle의 온보드 부트로더(Nordic Open Bootloader, signature-less)에 호환되는 zip을 만듭니다.

```powershell
& "C:\ncs\toolchains\936afb6332\nrfutil\bin\nrfutil.exe" pkg generate `
    --hw-version 52 --sd-req=0x00 `
    --application build\direct_test_mode\zephyr\zephyr.hex `
    --application-version <N> `
    dtm_dongle.zip
```
- `<N>`은 매 빌드마다 증가 권장 (1, 2, 3, ...).

### 3.3 Dongle을 부트로더 모드로

Dongle의 측면 **RESET 버튼**을 한 번 짧게 누릅니다 → 빨간 LED 깜빡임.
Windows 장치 관리자에서 `nRF52 SDFU USB (COMx)`로 잡힙니다 (보통 새 COM 번호).

### 3.4 플래시 (DFU)

```powershell
# COM 포트 자동 감지 후 플래시
$port = (Get-CimInstance Win32_PnPEntity |
         Where-Object Name -match "nRF52 SDFU USB\(COM\d+\)" |
         Select-Object -First 1).Name -replace '.*\((COM\d+)\).*','$1'

& "C:\ncs\toolchains\936afb6332\nrfutil\bin\nrfutil.exe" dfu usb-serial `
    -pkg dtm_dongle.zip -p $port
```

성공 시 `Device programmed.` 출력.
새 펌웨어 부팅 후 `USB 직렬 장치 (COMy)` (VID 0x1915 / PID 0x520F, 제품명 *Nordic DTM USB*)가 enumerate됩니다.

### 3.5 PC측 DTM 앱

- nRF Connect for Desktop → **Direct Test Mode** 앱
- 새로 잡힌 COM 포트 선택, **Baud rate: 19200**
- *Running device setup* → 정상 → TRANSMITTER/RECEIVER 테스트 사용 가능

---

## 4. 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| DTM 앱: *Cannot communicate with the device* | CDC ACM RX 미활성. `dtm_uart_twowire.c`의 `uart_irq_rx_enable()` 가드에 `USBD_CDC_ACM_CLASS && SOC_NRF52840` 포함되었는지 확인 |
| 빌드: `cc1.exe: out of memory` | `-o=-j2`로 ninja 병렬 수 제한 (`_build.bat`에 적용됨) |
| 빌드: `multiple definition of __device_dts_ord_...` | 레거시(`USB_DEVICE_STACK`)와 새(`USB_DEVICE_STACK_NEXT`) USB 스택이 동시에 켜진 경우. 보드 기본인 새 스택만 사용 |
| 빌드: `undefined node label 'cdc_acm_uart0'` | Dongle 보드는 `&board_cdc_acm_uart` 라벨을 사용. overlay에서 이 라벨을 참조 |
| DFU: `could not open port 'COMxx'` | `COMxx`는 예시. 실제 부트로더 COM 포트(예: `nRF52 SDFU USB(COM9)`)로 교체 |
| 부트로더 포트가 안 보임 | RESET 버튼을 다시 짧게 눌러 부트로더 모드 진입 |

---

## 5. 자주 쓰는 명령 모음

```powershell
# 1) 빌드
& "C:\ncs\toolchains\936afb6332\nrfutil\bin\nrfutil.exe" toolchain-manager launch --ncs-version v3.3.0 -- C:\Users\USER\direct_test_mode\_build.bat

# 2) DFU 패키지 생성
& "C:\ncs\toolchains\936afb6332\nrfutil\bin\nrfutil.exe" pkg generate --hw-version 52 --sd-req=0x00 --application build\direct_test_mode\zephyr\zephyr.hex --application-version 1 dtm_dongle.zip

# 3) 부트로더 COM 포트 확인 (RESET 후)
Get-CimInstance Win32_PnPEntity | Where-Object Name -match "nRF52 SDFU USB"

# 4) 플래시
& "C:\ncs\toolchains\936afb6332\nrfutil\bin\nrfutil.exe" dfu usb-serial -pkg dtm_dongle.zip -p COM9

# 5) 동작중 펌웨어 COM 포트 확인 (VID 0x1915 PID 0x520F)
Get-CimInstance Win32_PnPEntity | Where-Object DeviceID -match "VID_1915&PID_520F"
```

---

## 6. Copilot에게 이 작업을 요청하는 방법 (트리거 문구)

아래 문구를 그대로 또는 비슷하게 입력하면 한 번에 처리합니다. 각각 단계별로 요청해도 되고, 한 줄로 요청해도 됩니다.

| 원하는 동작 | Copilot에게 보낼 메시지 예시 |
|---|---|
| **빌드만** | "dongle용으로 빌드해줘" / "DTM을 nRF52840 Dongle로 다시 빌드" |
| **DFU 패키지만 생성** | "nrfutil로 dongle용 DFU 패키지(dtm_dongle.zip)를 만들어줘" |
| **플래시까지** | "dongle에 새 펌웨어 플래시해줘. 부트로더 COM은 자동 감지" |
| **전부 (빌드 → 패키지 → 플래시)** | "dongle용으로 빌드하고 DFU 패키지 만들어서 플래시까지 해줘" |
| **버전 증가** | "application-version을 N으로 올려서 패키지 다시 만들어줘" |
| **현재 동작중 COM 확인** | "Nordic DTM USB가 잡힌 COM 포트가 뭐야?" |

> 💡 한 줄 추천:
> **"dongle용 DTM 전체 워크플로우 실행해줘 (빌드 → pkg generate → DFU flash, COM 자동 감지)"**
>
> 이렇게 요청하면 본 문서의 3.1 → 3.2 → 3.4 단계를 자동으로 수행합니다. RESET 버튼 누르는 것만 사용자가 해주시면 됩니다.

---

## 7. 산출물 위치 요약

| 파일 | 경로 |
|---|---|
| 빌드된 HEX | `c:\Users\USER\direct_test_mode\build\direct_test_mode\zephyr\zephyr.hex` |
| DFU 패키지 | `c:\Users\USER\direct_test_mode\dtm_dongle.zip` |
| Overlay | `c:\Users\USER\direct_test_mode\boards\nrf52840dongle_nrf52840.overlay` |
| Kconfig 보드 추가 옵션 | `c:\Users\USER\direct_test_mode\boards\nrf52840dongle_nrf52840.conf` |
| 빌드 헬퍼 | `c:\Users\USER\direct_test_mode\_build.bat` |
| 빌드 로그 (디버깅용) | `c:\Users\USER\direct_test_mode\build.log` |

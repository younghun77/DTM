@echo off
setlocal
REM ============================================================
REM  Flash the nRF52840 Dongle with the DTM firmware.
REM  Usage: double-click, or pass a COM port:  flash_dongle.bat COM9
REM ============================================================

set HERE=%~dp0
set NRFUTIL=%HERE%bin\nrfutil.exe
set PKG=%HERE%firmware\dtm_dongle.zip

echo.
echo === DTM Dongle Flasher ===
echo.
echo  1) Insert the nRF52840 Dongle into a USB port.
echo  2) Press the small RESET button on the dongle so that the
echo     red LED starts pulsing (DFU / SDFU bootloader mode).
echo  3) Wait until Windows finishes enumerating "nRF52 SDFU USB"
echo     (Device Manager shows a new COMx port).
echo.

if "%~1"=="" (
    set /p PORT=Enter SDFU COM port (e.g. COM9): 
) else (
    set PORT=%~1
)

echo.
echo Flashing %PKG% to %PORT% ...
"%NRFUTIL%" nrf5sdk-tools dfu usb-serial -pkg "%PKG%" -p %PORT%
set RC=%ERRORLEVEL%

echo.
if "%RC%"=="0" (
    echo [OK] Dongle programmed successfully.
    echo Unplug and replug the dongle to start the new firmware.
) else (
    echo [FAIL] nrfutil exit code %RC%
)
echo.
pause
endlocal

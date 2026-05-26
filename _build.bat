@echo off
cd /d C:\ncs\v3.3.0
west build -b nrf52840dongle/nrf52840 -p always --sysbuild -d C:\Users\USER\direct_test_mode\build C:\Users\USER\direct_test_mode -o=-j2

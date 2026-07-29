@echo off
rem 매일 단타 스캐너 1사이클 (예약작업 AIDailyScan이 1시간마다 호출)
cd /d D:\ai\01_trading\pattern_trader
set PYTHONIOENCODING=utf-8
echo. >> logs\daily_scan.log
python tools\daily_scan.py --notify --trade --live >> logs\daily_scan.log 2>&1

@echo off
rem OBV_DIV 페이퍼 봇 1사이클 (예약작업 AIPatternBot이 1시간마다 호출)
cd /d D:\ai\01_trading\pattern_trader
set PYTHONIOENCODING=utf-8
echo. >> logs\obvbot.log
python run.py bot --config config_obvbot.yaml >> logs\obvbot.log 2>&1

@echo off
rem Starts the Wi-Fi Sense dashboard. Extra arguments are passed through (e.g. --no-browser).
rem The MySQL password and other secrets are read from .env in this folder (see .env.example).
cd /d "%~dp0"
title Wi-Fi Sense
if not exist ".env" echo [tip] No .env file found. Copy .env.example to .env and set WIFI_SENSE_DB_PASSWORD.
python wifi_web.py %*
if errorlevel 1 pause

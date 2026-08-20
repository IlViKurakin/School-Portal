@echo off
cd /d %~dp0

if "%PORTAL_SECRET_KEY%"=="" (
    echo ERROR: PORTAL_SECRET_KEY is not configured.
    exit /b 1
)

set PORTAL_HTTPS=1

venv\Scripts\waitress-serve.exe ^
  --listen=127.0.0.1:8080 ^
  app:app

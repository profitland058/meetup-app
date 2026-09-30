@echo off
chcp 65001 >nul
REM 모임 신청 프로그램 실행 (Windows)
REM 첫 실행 시 자동으로 Flask를 설치합니다.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [오류] Python 이 설치되어 있지 않습니다.
    echo https://www.python.org/downloads/ 에서 설치 후 다시 실행해 주세요.
    pause
    exit /b 1
)

python -c "import flask" >nul 2>nul
if errorlevel 1 (
    echo [안내] Flask 를 처음 사용하므로 설치 중입니다...
    python -m pip install --upgrade pip
    python -m pip install flask
)

echo.
echo ============================================
echo   모임 신청 프로그램을 시작합니다.
echo   브라우저에서 http://127.0.0.1:5000 을 열어 주세요.
echo   종료하려면 이 창에서 Ctrl + C 를 누르세요.
echo ============================================
echo.
start "" http://127.0.0.1:5000
python app.py
pause

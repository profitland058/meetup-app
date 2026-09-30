#!/usr/bin/env bash
# 모임 신청 프로그램 실행 (macOS / Linux)
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[오류] Python3 이 설치되어 있지 않습니다."
  exit 1
fi

if ! python3 -c "import flask" >/dev/null 2>&1; then
  echo "[안내] Flask 를 처음 사용하므로 설치 중입니다..."
  python3 -m pip install --upgrade pip
  python3 -m pip install flask
fi

echo "============================================"
echo "  모임 신청 프로그램을 시작합니다."
echo "  브라우저에서 http://127.0.0.1:5000 을 열어 주세요."
echo "  종료하려면 Ctrl + C 를 누르세요."
echo "============================================"
python3 app.py

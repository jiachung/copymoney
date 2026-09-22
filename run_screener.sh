#!/usr/bin/env bash
# crontab 에서 호출되는 래퍼. 프로젝트 디렉토리로 이동 후 스크리너 실행.
# 로그는 logs/YYYYMMDD.log 로 저장 (일별 파일).

set -euo pipefail

PROJECT_DIR="/Users/jiachung/Projects/copymoney"
PYTHON="/Users/jiachung/.pyenv/shims/python3"
LOG_DIR="$PROJECT_DIR/logs"

mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/$(date +%Y%m%d).log"

cd "$PROJECT_DIR"

{
  echo ""
  echo "=================================================="
  echo "실행 시각: $(date '+%Y-%m-%d %H:%M:%S %Z')"
  echo "=================================================="
  "$PYTHON" stock_screener.py 2>&1
} >> "$LOG_FILE"

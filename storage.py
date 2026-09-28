"""신호 이력 저장소 (SQLite).

매 스크리닝의 결과 신호를 시각과 함께 저장.
Backtest / Forward tracking 의 기반 데이터.
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = "data/signals.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,          -- 스크리닝 실행 시각 (ISO)
    code TEXT NOT NULL,            -- 종목코드
    name TEXT NOT NULL,            -- 종목명
    current_price INTEGER NOT NULL,
    signal_strength INTEGER,
    weekly_signal TEXT,
    weekly_5d INTEGER,
    daily_signal TEXT,
    daily_5d INTEGER,
    daily_20d INTEGER,
    price_vs_5d REAL,
    vwap_30min INTEGER,
    price_vs_vwap REAL,
    resistance INTEGER,
    support INTEGER,
    resistance_dist REAL,
    support_dist REAL,
    stop_loss INTEGER,
    risk_pct REAL,
    UNIQUE(run_at, code)
);

CREATE INDEX IF NOT EXISTS idx_signals_code ON signals(code);
CREATE INDEX IF NOT EXISTS idx_signals_run_at ON signals(run_at);
CREATE INDEX IF NOT EXISTS idx_signals_code_time ON signals(code, run_at);
"""


def get_db(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """DB 연결. 없으면 생성 + 스키마 초기화."""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_signals(signals: List[Dict], db_path: str = DEFAULT_DB_PATH) -> int:
    """신호 목록을 DB 에 저장. 중복(같은 run_at + code) 은 무시."""
    if not signals:
        return 0

    # run_at 은 모두 동일한 실행 시각으로 통일
    run_at = signals[0].get("timestamp") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = get_db(db_path)
    try:
        rows = []
        for s in signals:
            rows.append((
                run_at,
                s.get("code"),
                s.get("name"),
                s.get("current_price"),
                s.get("signal_strength"),
                s.get("weekly_signal"),
                s.get("weekly_5d"),
                s.get("daily_signal"),
                s.get("daily_5d"),
                s.get("daily_20d"),
                s.get("price_vs_5d"),
                s.get("vwap_30min"),
                s.get("price_vs_vwap"),
                s.get("resistance"),
                s.get("support"),
                s.get("resistance_dist"),
                s.get("support_dist"),
                s.get("stop_loss"),
                s.get("risk_pct"),
            ))
        cur = conn.executemany("""
            INSERT OR IGNORE INTO signals (
                run_at, code, name, current_price, signal_strength,
                weekly_signal, weekly_5d,
                daily_signal, daily_5d, daily_20d, price_vs_5d,
                vwap_30min, price_vs_vwap,
                resistance, support, resistance_dist, support_dist,
                stop_loss, risk_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)
        conn.commit()
        inserted = cur.rowcount
        logger.info(f"[storage] {inserted}개 신호 저장 (run_at={run_at}, db={db_path})")
        return inserted
    finally:
        conn.close()


def stats_summary(db_path: str = DEFAULT_DB_PATH) -> Dict:
    """저장된 신호 요약 통계."""
    if not os.path.exists(db_path):
        return {"total": 0, "runs": 0, "unique_codes": 0}
    conn = get_db(db_path)
    try:
        row = conn.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(DISTINCT run_at) AS runs,
                   COUNT(DISTINCT code) AS unique_codes,
                   MIN(run_at) AS first_run,
                   MAX(run_at) AS last_run
            FROM signals
        """).fetchone()
        return dict(row)
    finally:
        conn.close()

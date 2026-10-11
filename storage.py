"""신호 이력 저장소 (SQLite).

매 스크리닝의 결과 신호를 시각과 함께 저장.
Backtest / Forward tracking 의 기반 데이터.
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict, Optional

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

-- 성과 추적: (종목, 신호일자, N일후) 단위. 신호일자당 최초 신호의 진입가 사용.
CREATE TABLE IF NOT EXISTS performance (
    code TEXT NOT NULL,
    signal_date TEXT NOT NULL,      -- YYYYMMDD
    entry_price INTEGER NOT NULL,
    signal_strength INTEGER,
    horizon_days INTEGER NOT NULL,  -- 1, 3, 5, 10
    exit_date TEXT,                 -- YYYYMMDD (실제 도달한 거래일)
    exit_price INTEGER,
    return_pct REAL,
    computed_at TEXT,
    PRIMARY KEY (code, signal_date, horizon_days)
);

CREATE INDEX IF NOT EXISTS idx_perf_signal_date ON performance(signal_date);
CREATE INDEX IF NOT EXISTS idx_perf_horizon ON performance(horizon_days);
"""

HORIZONS = (1, 3, 5, 10)


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


def get_codes_alerted_today(db_path: str, today_date: str,
                            exclude_run_at: Optional[str] = None) -> set:
    """오늘 (today_date = 'YYYY-MM-DD') 이미 저장된 신호의 종목코드 집합.
    exclude_run_at 지정 시 해당 run 은 제외."""
    if not os.path.exists(db_path):
        return set()
    conn = get_db(db_path)
    try:
        if exclude_run_at:
            rows = conn.execute("""
                SELECT DISTINCT code FROM signals
                WHERE substr(run_at, 1, 10) = ? AND run_at != ?
            """, (today_date, exclude_run_at)).fetchall()
        else:
            rows = conn.execute("""
                SELECT DISTINCT code FROM signals
                WHERE substr(run_at, 1, 10) = ?
            """, (today_date,)).fetchall()
        return {r["code"] for r in rows}
    finally:
        conn.close()


def get_first_signal_per_day(db_path: str, since_date: str) -> List[Dict]:
    """(code, signal_date) 별 최초 신호의 진입가.
    since_date: YYYYMMDD, 이 날짜 이후(포함) 신호만."""
    conn = get_db(db_path)
    try:
        rows = conn.execute("""
            SELECT code, substr(run_at, 1, 10) AS signal_date,
                   MIN(run_at) AS first_run_at,
                   current_price AS entry_price,
                   signal_strength
            FROM signals
            WHERE substr(replace(run_at,'-',''), 1, 8) >= ?
            GROUP BY code, signal_date
            ORDER BY signal_date, code
        """, (since_date,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_computed_performance(db_path: str) -> set:
    """이미 계산된 (code, signal_date, horizon) 집합."""
    if not os.path.exists(db_path):
        return set()
    conn = get_db(db_path)
    try:
        rows = conn.execute(
            "SELECT code, signal_date, horizon_days FROM performance"
        ).fetchall()
        return {(r["code"], r["signal_date"], r["horizon_days"]) for r in rows}
    finally:
        conn.close()


def save_performance_row(db_path: str, row: Dict):
    conn = get_db(db_path)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO performance
            (code, signal_date, entry_price, signal_strength, horizon_days,
             exit_date, exit_price, return_pct, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row["code"], row["signal_date"], row["entry_price"],
            row.get("signal_strength"), row["horizon_days"],
            row.get("exit_date"), row.get("exit_price"),
            row.get("return_pct"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        conn.commit()
    finally:
        conn.close()


def weekly_performance_stats(db_path: str, since_date: str, until_date: str) -> Dict:
    """지정 기간 신호에 대한 성과 통계.
    since_date, until_date: YYYYMMDD (signal_date 기준)."""
    if not os.path.exists(db_path):
        return {}
    conn = get_db(db_path)
    try:
        # 신호 개수 & 강도별
        row = conn.execute("""
            SELECT COUNT(DISTINCT code || signal_date) AS total_signals
            FROM performance
            WHERE signal_date BETWEEN ? AND ?
        """, (since_date, until_date)).fetchone()
        total = row["total_signals"] if row else 0

        # 강도 분포 (신호 단위 - horizon=1 기준으로 unique)
        strength_rows = conn.execute("""
            SELECT signal_strength, COUNT(*) AS cnt
            FROM performance
            WHERE signal_date BETWEEN ? AND ? AND horizon_days = 1
            GROUP BY signal_strength
            ORDER BY signal_strength DESC
        """, (since_date, until_date)).fetchall()

        # horizon 별 통계
        horizon_stats = {}
        for h in HORIZONS:
            stats_row = conn.execute("""
                SELECT COUNT(*) AS n,
                       AVG(return_pct) AS avg_return,
                       SUM(CASE WHEN return_pct > 0 THEN 1 ELSE 0 END) AS wins,
                       MAX(return_pct) AS best_return,
                       MIN(return_pct) AS worst_return
                FROM performance
                WHERE signal_date BETWEEN ? AND ?
                  AND horizon_days = ?
                  AND return_pct IS NOT NULL
            """, (since_date, until_date, h)).fetchone()
            horizon_stats[h] = dict(stats_row) if stats_row else {}

        # Top winners/losers (1일 후 기준)
        top_win = conn.execute("""
            SELECT p.code, s.name, p.return_pct
            FROM performance p
            LEFT JOIN signals s ON s.code = p.code
            WHERE p.signal_date BETWEEN ? AND ?
              AND p.horizon_days = 3
              AND p.return_pct IS NOT NULL
            GROUP BY p.code, p.signal_date
            ORDER BY p.return_pct DESC LIMIT 3
        """, (since_date, until_date)).fetchall()

        top_lose = conn.execute("""
            SELECT p.code, s.name, p.return_pct
            FROM performance p
            LEFT JOIN signals s ON s.code = p.code
            WHERE p.signal_date BETWEEN ? AND ?
              AND p.horizon_days = 3
              AND p.return_pct IS NOT NULL
            GROUP BY p.code, p.signal_date
            ORDER BY p.return_pct ASC LIMIT 3
        """, (since_date, until_date)).fetchall()

        return {
            "total_signals": total,
            "by_strength": [dict(r) for r in strength_rows],
            "by_horizon": horizon_stats,
            "top_winners": [dict(r) for r in top_win],
            "top_losers": [dict(r) for r in top_lose],
        }
    finally:
        conn.close()

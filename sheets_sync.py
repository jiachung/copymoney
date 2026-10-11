"""Google Sheets 동기화 (Apps Script Web App 엔드포인트에 POST).

- 미전송 (synced_at IS NULL) 행만 전송 후 synced_at 업데이트
- signals, performance 각각 batching (500행 단위)
- 환경변수 SHEETS_WEBHOOK_URL 없으면 조용히 스킵
"""

import logging
import sqlite3
from datetime import datetime
from typing import List

import requests

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
TIMEOUT_SEC = 30

SIGNAL_FIELDS = [
    "run_at", "code", "name", "current_price", "signal_strength",
    "weekly_signal", "weekly_5d",
    "daily_signal", "daily_5d", "daily_20d", "price_vs_5d",
    "vwap_30min", "price_vs_vwap",
    "resistance", "support", "resistance_dist", "support_dist",
    "stop_loss", "risk_pct",
]

PERF_FIELDS = [
    "code", "signal_date", "entry_price", "signal_strength",
    "horizon_days", "exit_date", "exit_price", "return_pct", "computed_at",
]


def _ensure_synced_column(conn: sqlite3.Connection, table: str):
    """synced_at 컬럼 없으면 추가 (idempotent)."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if "synced_at" not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN synced_at TEXT")
        conn.commit()
        logger.info(f"[sheets] {table}.synced_at 컬럼 추가")


def _post_batch(webhook_url: str, sheet_name: str, rows: List[list]) -> int:
    """한 배치 전송. 실패 시 예외."""
    resp = requests.post(
        webhook_url,
        json={"sheet": sheet_name, "rows": rows},
        timeout=TIMEOUT_SEC,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        raise RuntimeError(f"Sheets 응답 실패: {result}")
    return result.get("appended", len(rows))


def _sync_table(conn: sqlite3.Connection, webhook_url: str,
                sheet_name: str, pk: str, fields: List[str]) -> int:
    _ensure_synced_column(conn, sheet_name)
    cur = conn.execute(
        f"SELECT {pk}, {', '.join(fields)} FROM {sheet_name} "
        f"WHERE synced_at IS NULL ORDER BY {pk}"
    )
    all_rows = cur.fetchall()
    if not all_rows:
        logger.info(f"[sheets] {sheet_name}: 신규 행 없음")
        return 0

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_sent = 0
    for i in range(0, len(all_rows), BATCH_SIZE):
        batch = all_rows[i:i + BATCH_SIZE]
        ids = [r[0] for r in batch]
        payload_rows = [[r[j + 1] for j in range(len(fields))] for r in batch]
        try:
            n = _post_batch(webhook_url, sheet_name, payload_rows)
            total_sent += n
        except Exception as e:
            logger.error(f"[sheets] {sheet_name} 배치 전송 실패: {e}")
            raise
        # 성공한 배치만 synced 마킹
        conn.executemany(
            f"UPDATE {sheet_name} SET synced_at = ? WHERE {pk} = ?",
            [(now, i_) for i_ in ids],
        )
        conn.commit()

    logger.info(f"[sheets] {sheet_name} {total_sent}건 동기화 완료")
    return total_sent


def sync_all(db_path: str, webhook_url: str) -> dict:
    """signals + performance 둘 다 동기화."""
    if not webhook_url:
        return {"skipped": "webhook URL 없음"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "signals": _sync_table(conn, webhook_url, "signals", "id", SIGNAL_FIELDS),
            "performance": _sync_table(conn, webhook_url, "performance", "rowid", PERF_FIELDS),
        }
    finally:
        conn.close()

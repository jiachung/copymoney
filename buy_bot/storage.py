"""buy_bot/data/orders.db 관리.

주문 이력 저장 + 중복 체크 + 하루 매수 건수 집계.
"""

import sqlite3
from datetime import date
from pathlib import Path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    signal_run_at TEXT,
    order_price INTEGER,
    order_shares INTEGER,
    kis_order_no TEXT,
    status TEXT,
    filled_price INTEGER,
    filled_shares INTEGER,
    filled_at TEXT,
    error_msg TEXT,
    stop_loss INTEGER,
    resistance INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_code_day
    ON orders (code, substr(submitted_at, 1, 10));
CREATE INDEX IF NOT EXISTS idx_orders_day
    ON orders (substr(submitted_at, 1, 10));
CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders (status);
"""


def _migrate(con: sqlite3.Connection) -> None:
    """기존 DB에 신규 컬럼 추가 (하위호환)."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(orders)")}
    for col in ("stop_loss", "resistance"):
        if col not in cols:
            con.execute(f"ALTER TABLE orders ADD COLUMN {col} INTEGER")


def _conn(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _conn(db_path) as con:
        con.executescript(_SCHEMA)
        _migrate(con)


def has_order_today(db_path: Path, code: str, today: str | None = None) -> bool:
    today = today or date.today().isoformat()
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT 1 FROM orders "
            "WHERE code = ? AND substr(submitted_at, 1, 10) = ? "
            "LIMIT 1",
            (code, today),
        ).fetchone()
    return row is not None


def count_today(db_path: Path, today: str | None = None) -> int:
    today = today or date.today().isoformat()
    with _conn(db_path) as con:
        row = con.execute(
            "SELECT COUNT(*) AS n FROM orders "
            "WHERE substr(submitted_at, 1, 10) = ? AND status != 'FAILED'",
            (today,),
        ).fetchone()
    return int(row["n"])


def active_holdings(db_path: Path) -> set[str]:
    """매도 로직 없는 단계에서 보유 포지션 근사 — SUBMITTED/FILLED 상태 전체."""
    with _conn(db_path) as con:
        rows = con.execute(
            "SELECT DISTINCT code FROM orders WHERE status IN ('SUBMITTED', 'FILLED')"
        ).fetchall()
    return {r["code"] for r in rows}


def insert_order(db_path: Path, *, submitted_at: str, code: str, name: str,
                 signal_run_at: str, order_price: int, order_shares: int,
                 kis_order_no: str | None, status: str,
                 error_msg: str | None = None,
                 stop_loss: int | None = None,
                 resistance: int | None = None) -> None:
    with _conn(db_path) as con:
        con.execute(
            "INSERT INTO orders "
            "(submitted_at, code, name, signal_run_at, order_price, order_shares, "
            " kis_order_no, status, error_msg, stop_loss, resistance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (submitted_at, code, name, signal_run_at, order_price, order_shares,
             kis_order_no, status, error_msg, stop_loss, resistance),
        )

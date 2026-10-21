"""copymoney의 signals.db 리더 + 티어 등급 계산.

grade 로직은 copymoney/stock_screener.py의 _grade_signal과 동일.
"""

import sqlite3
from datetime import date
from typing import Any


def _grade(s: dict) -> int:
    """🏆 Best 4/4 = 4, 🥇 3/4 = 3, 그 이하는 2/1/0."""
    score = 0
    if s.get("signal_strength") == 4:
        score += 1
    pv5 = s.get("price_vs_5d")
    if pv5 is not None and 2 <= pv5 < 5:
        score += 1
    pvw = s.get("price_vs_vwap")
    if pvw is not None and 1 <= pvw < 3:
        score += 1
    rp = s.get("risk_pct")
    if rp is not None and 3 <= rp < 5:
        score += 1
    return score


def load_candidates(db_path: str, today: str | None = None,
                    min_grade: int = 3) -> list[dict[str, Any]]:
    """오늘 신호 중 grade >= min_grade만 반환. 티어 → 강도 순 정렬.

    today: YYYY-MM-DD (None이면 로컬 오늘).
    각 종목은 당일 가장 최신 run_at 신호 하나만 선택 (여러 번 재발사된 경우).
    """
    if today is None:
        today = date.today().isoformat()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT * FROM signals "
        "WHERE substr(run_at, 1, 10) = ? "
        "ORDER BY id",
        (today,),
    ).fetchall()
    con.close()

    # 종목별 최신 신호 하나만 (같은 날 재발사 처리)
    latest: dict[str, dict] = {}
    for r in rows:
        s = dict(r)
        latest[s["code"]] = s  # ORDER BY id → 뒤가 최신

    graded = [{**s, "grade": _grade(s)} for s in latest.values()]
    passed = [s for s in graded if s["grade"] >= min_grade]
    passed.sort(key=lambda s: (s["grade"], s.get("signal_strength") or 0), reverse=True)
    return passed

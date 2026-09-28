"""신호 성과 계산 & 주간 리포트."""

import logging
from datetime import datetime, timedelta
from typing import Optional

from storage import (
    HORIZONS,
    get_computed_performance,
    get_first_signal_per_day,
    save_performance_row,
    weekly_performance_stats,
    DEFAULT_DB_PATH,
)

logger = logging.getLogger(__name__)


def compute_pending_performance(api, db_path: str = DEFAULT_DB_PATH,
                                lookback_days: int = 20) -> int:
    """
    최근 lookback_days 이내 신호에 대해 미계산 (code, signal_date, horizon)
    성과를 KIS 일봉 API 로 계산해 저장.

    Returns: 새로 계산된 행 수.
    """
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y%m%d")
    signals = get_first_signal_per_day(db_path, since)
    if not signals:
        logger.info("[perf] 계산 대상 신호 없음")
        return 0

    computed = get_computed_performance(db_path)

    # code 별로 그룹핑해서 일봉 API 호출 1회씩만
    by_code = {}
    for s in signals:
        by_code.setdefault(s["code"], []).append(s)

    total_new = 0
    for code, code_signals in by_code.items():
        # 필요한 max horizon 만큼 뒤 데이터 필요 → 넉넉히 30일봉
        try:
            daily = api.get_daily_candles(code, days_back=30)
        except Exception as e:
            logger.warning(f"[perf] {code} 일봉 조회 실패: {e}")
            continue
        if not daily:
            continue
        # daily 는 오래된 것부터 정렬. date 는 YYYYMMDD 문자열.
        dates_sorted = [d["date"] for d in daily]

        for sig in code_signals:
            signal_date = sig["signal_date"].replace("-", "")  # YYYY-MM-DD → YYYYMMDD
            entry_price = sig["entry_price"]

            # signal_date 이후 거래일 인덱스 찾기
            try:
                # signal_date 이후 첫 거래일부터
                future_dates = [(i, d) for i, d in enumerate(dates_sorted) if d > signal_date]
            except Exception:
                continue
            if not future_dates:
                continue  # 아직 다음 거래일 종가 없음

            for h in HORIZONS:
                key = (code, signal_date, h)
                if key in computed:
                    continue
                if len(future_dates) < h:
                    continue  # 아직 h 거래일 지나지 않음
                idx, exit_date = future_dates[h - 1]
                exit_price = daily[idx]["close"]
                return_pct = (exit_price - entry_price) / entry_price * 100
                save_performance_row(db_path, {
                    "code": code,
                    "signal_date": signal_date,
                    "entry_price": entry_price,
                    "signal_strength": sig.get("signal_strength"),
                    "horizon_days": h,
                    "exit_date": exit_date,
                    "exit_price": exit_price,
                    "return_pct": return_pct,
                })
                total_new += 1

    logger.info(f"[perf] 신규 계산 {total_new}건")
    return total_new


def build_weekly_report(db_path: str = DEFAULT_DB_PATH) -> Optional[str]:
    """지난 7일치 성과 요약을 텔레그램 HTML 문자열로 반환.
    데이터 없으면 None."""
    today = datetime.now()
    # 지난주 월요일 ~ 금요일
    weekday = today.weekday()  # Mon=0
    days_since_monday = weekday
    this_monday = today - timedelta(days=days_since_monday)
    last_monday = this_monday - timedelta(days=7)
    last_friday = last_monday + timedelta(days=4)

    since = last_monday.strftime("%Y%m%d")
    until = last_friday.strftime("%Y%m%d")

    stats = weekly_performance_stats(db_path, since, until)
    total = stats.get("total_signals", 0)
    if total == 0:
        return None

    lines = []
    lines.append("📊 <b>주간 리포트</b>")
    lines.append(f"기간: {last_monday.strftime('%m/%d')}({['월','화','수','목','금','토','일'][last_monday.weekday()]})"
                 f" ~ {last_friday.strftime('%m/%d')}({['월','화','수','목','금','토','일'][last_friday.weekday()]})")
    lines.append(f"총 신호: {total}개")
    lines.append("")

    # 강도별
    by_str = stats.get("by_strength", [])
    if by_str:
        lines.append("<b>신호 강도별</b>")
        for s in by_str:
            stars = "⭐" * (s.get("signal_strength") or 3)
            lines.append(f"  {stars}: {s['cnt']}개")
        lines.append("")

    # horizon 별 성과
    lines.append("<b>Horizon 별 성과</b>")
    for h in HORIZONS:
        hs = stats["by_horizon"].get(h, {})
        n = hs.get("n") or 0
        if n == 0:
            lines.append(f"  {h}일 후: 데이터 없음")
            continue
        wins = hs.get("wins") or 0
        avg = hs.get("avg_return") or 0.0
        best = hs.get("best_return") or 0.0
        worst = hs.get("worst_return") or 0.0
        win_rate = wins / n * 100
        lines.append(f"  {h}일 후 (n={n}): 승률 {win_rate:.0f}%, "
                     f"평균 {avg:+.2f}%, 최고 {best:+.2f}%, 최저 {worst:+.2f}%")
    lines.append("")

    # Top winners / losers (3일 후 기준)
    tw = stats.get("top_winners") or []
    tl = stats.get("top_losers") or []
    if tw:
        lines.append("<b>🏆 Top 3 (3일 후)</b>")
        for r in tw:
            lines.append(f"  {r.get('name','')}({r['code']}): {r['return_pct']:+.2f}%")
        lines.append("")
    if tl:
        lines.append("<b>💀 Worst 3 (3일 후)</b>")
        for r in tl:
            lines.append(f"  {r.get('name','')}({r['code']}): {r['return_pct']:+.2f}%")

    return "\n".join(lines)

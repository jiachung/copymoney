"""코스닥 지수 regime 판단.

지수 흐름에 따라 매수 정책을 조정하기 위한 모듈.

Regime:
  🟢 강세 (bull):  지수 > 20MA + 20MA 상향  → 평상시 매수
  🟡 혼조 (neutral): 지수 ≈ 20MA (±1%) 또는 20MA 횡보 → 신중 (🏆만)
  🔴 약세 (bear):  지수 < 20MA (-1% 이상) 또는 20MA 하향  → 매수 중단
"""

import logging
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# KIS 지수 조회 TR_ID (일별)
TR_INDEX_DAILY = "FHKUP03500100"
INDEX_KOSDAQ = "1001"
INDEX_KOSPI = "0001"


def get_index_closes(api, index_code: str = INDEX_KOSDAQ, n: int = 30) -> List[float]:
    """지수 종가 n개 반환 (오래된 순)."""
    from datetime import datetime, timedelta
    fetch_days = n * 2 + 20  # 거래일 기준 넉넉히
    end_dt = datetime.now().strftime("%Y%m%d")
    start_dt = (datetime.now() - timedelta(days=fetch_days)).strftime("%Y%m%d")

    path = "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price"
    params = {
        "fid_cond_mrkt_div_code": "U",
        "fid_input_iscd": index_code,
        "fid_input_date_1": start_dt,
        "fid_input_date_2": end_dt,
        "fid_period_div_code": "D",
        "fid_org_adj_prc": "0",
    }
    try:
        result = api._get(path, TR_INDEX_DAILY, params)
    except Exception as e:
        logger.warning(f"[market_index] 지수 조회 실패: {e}")
        return []

    rows = result.get("output2") or []
    closes = []
    for row in rows:
        try:
            closes.append(float(row.get("bstp_nmix_prpr", 0)))
        except (TypeError, ValueError):
            pass
    closes.reverse()  # 오래된 순
    return closes[-n:]


def _sma(xs: List[float], period: int) -> Optional[float]:
    if len(xs) < period:
        return None
    return sum(xs[-period:]) / period


def classify_regime(closes: List[float], name: str = "지수") -> Dict:
    """
    종가 리스트로 regime 판단.

    Returns:
        {
            "code": "bull" | "neutral" | "bear",
            "label": "🟢 강세" | "🟡 혼조" | "🔴 약세",
            "comment": str,              # Telegram 발송용 짧은 코멘트
            "current": float | None,     # 현재가
            "sma_20": float | None,
            "sma_5": float | None,
            "gap_vs_20ma": float | None, # (current - sma20) / sma20 * 100
            "trend_20ma": float | None,  # 20MA 모멘텀 (오늘 - 5일전) / 5일전 * 100
            "allow_buy": bool,
            "min_grade": int,            # 매수 최소 티어 (3=🥇이상, 4=🏆만, 99=금지)
        }
    """
    if not closes or len(closes) < 25:
        return {
            "code": "unknown", "label": "❓ 데이터 부족",
            "comment": "지수 데이터 부족으로 판단 불가 — 평상시로 처리.",
            "current": None, "sma_20": None, "sma_5": None,
            "gap_vs_20ma": None, "trend_20ma": None,
            "allow_buy": True, "min_grade": 3,
        }

    current = closes[-1]
    sma_20 = _sma(closes, 20)
    sma_5 = _sma(closes, 5)
    gap = (current - sma_20) / sma_20 * 100
    # 20MA 모멘텀 (오늘 20MA vs 5거래일 전 20MA)
    sma_20_now = _sma(closes[-20:], 20)
    sma_20_past = _sma(closes[-25:-5], 20)
    trend = (sma_20_now - sma_20_past) / sma_20_past * 100 if sma_20_past else 0.0

    # 분류
    if gap >= 1.0 and trend > 0:
        code = "bull"
        label = "🟢 강세"
        comment = (f"{name} {current:,.1f} (+{gap:.2f}% vs 20MA), 20MA 상향 중. "
                   f"평상시 매수 OK.")
        allow_buy = True
        min_grade = 3
    elif gap <= -1.0 or trend < -1.0:
        code = "bear"
        label = "🔴 약세"
        comment = (f"{name} {current:,.1f} ({gap:+.2f}% vs 20MA, 20MA {trend:+.2f}%). "
                   f"⚠️ 매수 자제. 보유 종목 포지션 축소/손절 검토 권장.")
        allow_buy = False
        min_grade = 99  # 모든 매수 차단
    else:
        code = "neutral"
        label = "🟡 혼조"
        comment = (f"{name} {current:,.1f} ({gap:+.2f}% vs 20MA, 20MA {trend:+.2f}%). "
                   f"🏆 Best 4/4 만 매수 권장, 신규 포지션 축소.")
        allow_buy = True
        min_grade = 4  # 🏆만

    return {
        "code": code, "label": label, "comment": comment,
        "current": current, "sma_20": sma_20, "sma_5": sma_5,
        "gap_vs_20ma": gap, "trend_20ma": trend,
        "allow_buy": allow_buy, "min_grade": min_grade,
    }


_SEVERITY = {"bull": 0, "neutral": 1, "bear": 2, "unknown": 0}


def get_market_regime(api) -> Dict:
    """코스피 + 코스닥 둘 다 조회해서 더 보수적인 쪽을 overall regime 으로.

    Returns:
        {
            ...regime 필드들 (더 나쁜 쪽 복사)...
            "kospi": {regime dict},
            "kosdaq": {regime dict},
        }
    """
    kospi_closes = get_index_closes(api, INDEX_KOSPI, n=30)
    kosdaq_closes = get_index_closes(api, INDEX_KOSDAQ, n=30)
    kospi = classify_regime(kospi_closes, "코스피")
    kosdaq = classify_regime(kosdaq_closes, "코스닥")

    # 더 심각한 쪽 선택 (severity 큰 쪽)
    if _SEVERITY.get(kospi["code"], 0) >= _SEVERITY.get(kosdaq["code"], 0):
        worse = kospi
        worse_name = "코스피"
    else:
        worse = kosdaq
        worse_name = "코스닥"

    # overall regime 은 더 나쁜 쪽을 기준으로, 코멘트에 두 지수 상태 모두 표시
    overall = dict(worse)
    overall["comment"] = (
        f"코스피 {kospi['label']} ({kospi.get('gap_vs_20ma', 0):+.1f}% vs 20MA) / "
        f"코스닥 {kosdaq['label']} ({kosdaq.get('gap_vs_20ma', 0):+.1f}% vs 20MA)\n"
        f"→ 보수적 기준({worse_name}): {worse['comment']}"
    )
    overall["kospi"] = kospi
    overall["kosdaq"] = kosdaq
    overall["worse_name"] = worse_name
    return overall

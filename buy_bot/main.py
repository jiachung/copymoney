"""매수 자동화 봇 — Phase 1a Entry Only.

흐름:
  1. config 로드 + 환경변수 검증
  2. 오늘 copymoney 신호 로드 → grade >= BOT_MIN_GRADE 필터
  3. 종목별로:
       - orders.db 당일 중복? 스킵
       - 이미 보유 (orders.db SUBMITTED/FILLED)? 스킵
       - 하루 매수 건수 >= BOT_MAX_DAILY_ORDERS? 중단
       - 예수금 부족? 중단
       - 지정가 산정 → buy_limit_order → orders.db 기록 → 텔레그램

실행:
  python -m buy_bot.main
"""

import sys
import traceback
from datetime import date, datetime

from buy_bot import config as cfg
from buy_bot.kis_client import (
    buy_limit_order, floor_to_tick,
    get_account_balance, get_current_price_info,
    sell_market_order,
)
from buy_bot.notifier import notify
from buy_bot.signals import load_candidates
from buy_bot.storage import (
    active_holdings, active_positions, count_today, has_order_today,
    init_db, insert_order, mark_sold,
)


_GRADE_TAG = {4: "🏆 Best 4/4", 3: "🥇 3/4"}


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def _past_buy_cutoff(now: datetime) -> bool:
    """매수 금지 시각 지났는지. 15:00 이후면 True."""
    try:
        hh, mm = map(int, cfg.BOT_BUY_CUTOFF_HHMM.split(":"))
    except Exception:
        hh, mm = 15, 0
    return (now.hour, now.minute) >= (hh, mm)


def _run_stop_loss_check() -> int:
    """현재 보유 포지션에 대해 -X% 손절 체크 → 시장가 매도.
    Returns: 매도 체결 건수."""
    if not cfg.BOT_SELL_ENABLED:
        _log("[sell] BOT_SELL_ENABLED=false — 매도 체크 스킵")
        return 0

    positions = active_positions(cfg.ORDERS_DB_PATH)
    if not positions:
        _log("[sell] 보유 포지션 없음")
        return 0

    _log(f"[sell] 보유 {len(positions)}건 — 손절 -{cfg.BOT_STOP_LOSS_PCT}% 체크")
    sold_count = 0

    for pos in positions:
        code = pos["code"]; name = pos["name"]; entry = pos["entry"]; shares = pos["shares"]
        try:
            info = get_current_price_info(code)
            cur = float(info["current"])
        except Exception as e:
            _log(f"  [{code}] 현재가 조회 실패: {e}")
            continue

        ret_pct = (cur - entry) / entry * 100
        if ret_pct > -cfg.BOT_STOP_LOSS_PCT:
            _log(f"  [{code}] {name} 평가손 {ret_pct:+.2f}% — 손절선 안쪽 (유지)")
            continue

        # 손절 발동
        _log(f"  [{code}] {name} 평가손 {ret_pct:+.2f}% — 손절 발동 (entry {entry:,} / 현재 {cur:,.0f})")
        try:
            res = sell_market_order(code, int(shares))
            out = res.get("output", {}) if isinstance(res, dict) else {}
            kis_no = f"{out.get('KRX_FWDG_ORD_ORGNO', '')}/{out.get('ODNO', '')}".strip("/")
            sold_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 시장가라 체결가는 사후 조회해야 정확하나, 일단 호가 기준 current 로 기록
            mark_sold(cfg.ORDERS_DB_PATH, pos["id"],
                      sold_at=sold_at, sold_price=int(cur),
                      sold_shares=int(shares), sold_reason="STOP_LOSS")
            pnl = (cur - entry) * shares
            notify(
                f"🛑 [손절 매도] {sold_at} (모의)\n\n"
                f"📌 {name} ({code})\n"
                f"  매수가: {entry:,}원 × {shares}주 = {entry*shares:,}원\n"
                f"  매도가(시장가): 약 {cur:,.0f}원\n"
                f"  평가손익: {pnl:+,.0f}원 ({ret_pct:+.2f}%)\n"
                f"  손절선 설정: -{cfg.BOT_STOP_LOSS_PCT}%\n"
                f"  KIS 주문번호: {kis_no or 'N/A'}"
            )
            sold_count += 1
        except Exception as e:
            err = str(e)
            _log(f"  [{code}] 매도 실패: {err}")
            notify(f"❌ [손절 매도 실패] {name} ({code})\n이유: {err}\n수동 확인 필요")

    _log(f"[sell] 손절 매도 완료 {sold_count}건")
    return sold_count


def _place_order(sig: dict, available_cash: float) -> dict | None:
    """단일 매수 시도. 성공 시 dict 반환, 실패/스킵 시 None."""
    code = sig["code"]
    name = sig.get("name", "")

    try:
        info = get_current_price_info(code)
        live_price = float(info["current"])
    except Exception as e:
        _log(f"[스킵] {code} 현재가 조회 실패: {e}")
        notify(f"❌ [현재가 조회 실패] {name} ({code})\n이유: {e}")
        return None

    limit_price = floor_to_tick(live_price * cfg.BOT_LIMIT_PRICE_MARGIN)
    shares = cfg.BOT_PER_POSITION // limit_price
    if shares < 1:
        _log(f"[스킵] {code} 고가주 (1주 {limit_price:,}원 > {cfg.BOT_PER_POSITION:,}원)")
        notify(
            f"⚠️ [고가주 스킵] {name} ({code})\n"
            f"1주 {limit_price:,}원 > 종목당 한도 {cfg.BOT_PER_POSITION:,}원"
        )
        return None

    cost = limit_price * shares
    if cost > available_cash:
        _log(f"[스킵] {code} 예수금 부족 (필요 {cost:,} / 가용 {available_cash:,.0f})")
        notify(
            f"❌ [예수금 부족] {name} ({code})\n"
            f"요청 {cost:,}원 / 보유 {available_cash:,.0f}원\n"
            f"오늘 매수 중단합니다."
        )
        return {"stop": True}

    submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stop_loss  = sig.get("stop_loss")
    resistance = sig.get("resistance")
    try:
        res = buy_limit_order(code, limit_price, shares)
        out = res.get("output", {}) if isinstance(res, dict) else {}
        kis_order_no = f"{out.get('KRX_FWDG_ORD_ORGNO', '')}/{out.get('ODNO', '')}".strip("/")
        insert_order(
            cfg.ORDERS_DB_PATH,
            submitted_at=submitted_at, code=code, name=name,
            signal_run_at=sig.get("run_at", ""),
            order_price=limit_price, order_shares=int(shares),
            kis_order_no=kis_order_no or None, status="SUBMITTED",
            stop_loss=stop_loss, resistance=resistance,
        )
        tag = _GRADE_TAG.get(sig["grade"], f"{sig['grade']}/4")
        strength = sig.get("signal_strength") or 0
        sl_line = (f"\n  손절가(참고): {stop_loss:,}원 (-{sig.get('risk_pct') or 0:.1f}%)"
                   if stop_loss else "")
        rs_line = (f"\n  저항선(참고): {resistance:,}원 (+{sig.get('resistance_dist') or 0:.1f}%)"
                   if resistance else "")
        notify(
            f"🤖 [매수 주문] {submitted_at} (모의)\n\n"
            f"📌 {name} ({code})\n"
            f"  신호: {tag} {'⭐' * strength}\n"
            f"  지정가: {limit_price:,}원 × {shares}주 = {cost:,}원\n"
            f"  예수금 잔액: {available_cash:,.0f} → {available_cash - cost:,.0f} (예상)"
            f"{sl_line}{rs_line}\n"
            f"  KIS 주문번호: {kis_order_no or 'N/A'}"
        )
        _log(f"[매수] {code} {name} {shares}주 @ {limit_price:,}원 "
             f"손절 {stop_loss or 'N/A'} 저항 {resistance or 'N/A'} (주문번호 {kis_order_no})")
        return {"cost": cost}
    except Exception as e:
        err = str(e)
        insert_order(
            cfg.ORDERS_DB_PATH,
            submitted_at=submitted_at, code=code, name=name,
            signal_run_at=sig.get("run_at", ""),
            order_price=limit_price, order_shares=int(shares),
            kis_order_no=None, status="FAILED", error_msg=err,
            stop_loss=stop_loss, resistance=resistance,
        )
        _log(f"[실패] {code} {err}")
        notify(f"❌ [매수 실패] {name} ({code})\n이유: {err}")
        return {"failed": True}


def main() -> None:
    cfg.validate()
    init_db(cfg.ORDERS_DB_PATH)

    if not cfg.BOT_ENABLED:
        _log("BOT_ENABLED=false — 비활성 상태로 종료")
        return

    today = date.today().isoformat()
    now = datetime.now()
    _log(f"봇 시작 — {today} {now.strftime('%H:%M')} (TRADE_MODE=paper)")

    # 1) 매도 로직 (손절 체크) — 매수 전에 먼저 실행 (포지션 정리 후 매수)
    try:
        _run_stop_loss_check()
    except Exception as e:
        _log(f"[sell] 매도 체크 전체 실패: {e}")
        notify(f"❌ [매도 체크 실패] {e}")

    # 2) 매수 시간 cutoff 체크 — 15:00 이후면 매수 스킵
    if _past_buy_cutoff(now):
        _log(f"매수 금지 시각 ({cfg.BOT_BUY_CUTOFF_HHMM}) 이후 — 매수 스킵, 종료")
        return

    # 지수 regime 조회 (copymoney 스크리너가 저장한 최신 스냅샷)
    regime_min_grade = cfg.BOT_MIN_GRADE
    if cfg.BOT_USE_REGIME_FILTER:
        try:
            from storage import get_latest_regime
            regime = get_latest_regime(cfg.SIGNALS_DB_PATH, max_age_hours=8)
        except Exception as e:
            _log(f"regime 조회 실패 (필터 미적용): {e}")
            regime = None

        if regime is None:
            _log("regime 스냅샷 없음/만료 — 지수 필터 생략")
        else:
            _log(f"지수 regime: {regime['overall_label']} (allow_buy={bool(regime['allow_buy'])}, "
                 f"min_grade={regime['min_grade']}, 코스피 {regime['kospi_gap']:+.2f}%, "
                 f"코스닥 {regime['kosdaq_gap']:+.2f}%)")
            if not regime["allow_buy"]:
                notify(
                    f"🚨 [매수 중단 — 지수 약세] {today}\n\n"
                    f"{regime['overall_label']}\n"
                    f"코스피 {regime['kospi_gap']:+.2f}% / 코스닥 {regime['kosdaq_gap']:+.2f}% (20MA 대비)\n\n"
                    f"⚠️ 보유 종목 포지션 축소/손절 검토 권장."
                )
                _log("지수 약세 — 매수 전면 중단하고 종료")
                return
            # 혼조면 min_grade 상향
            if (regime["min_grade"] or 0) > regime_min_grade:
                regime_min_grade = regime["min_grade"]
                _log(f"지수 혼조 — 매수 티어 상향: min_grade {cfg.BOT_MIN_GRADE} → {regime_min_grade}")

    try:
        # 1차: grade 만 통과 (주봉 상한 체크 전) — 로깅용
        all_graded = load_candidates(cfg.SIGNALS_DB_PATH, today, regime_min_grade)
        # 2차: 주봉 상한 포함
        candidates = load_candidates(
            cfg.SIGNALS_DB_PATH, today, regime_min_grade,
            max_weekly_gap_pct=cfg.BOT_MAX_WEEKLY_GAP_PCT,
        )
    except Exception as e:
        _log(f"신호 로드 실패: {e}")
        notify(f"❌ [신호 로드 실패] {e}")
        return

    # 주봉 과열로 걸러진 종목 알림
    passed_codes = {s["code"] for s in candidates}
    for s in all_graded:
        if s["code"] in passed_codes:
            continue
        w = s.get("weekly_gap_pct")
        name = s.get("name", "")
        _log(f"[주봉 과열 스킵] {s['code']} {name} 주봉 5선위 {w:.1f}% > {cfg.BOT_MAX_WEEKLY_GAP_PCT:.0f}%")
        notify(
            f"⚠️ [주봉 과열 스킵] {name} ({s['code']})\n"
            f"주봉 5선위 {w:.1f}% > 상한 {cfg.BOT_MAX_WEEKLY_GAP_PCT:.0f}%\n"
            f"(일봉/VWAP/위험도 조건은 통과했지만 주봉 과열로 매수 안 함)"
        )

    _log(f"신호 후보 {len(candidates)}건 (grade ≥ {cfg.BOT_MIN_GRADE}, 주봉≤{cfg.BOT_MAX_WEEKLY_GAP_PCT:.0f}%)")
    if not candidates:
        _log("매수 대상 없음 — 종료")
        return

    held = active_holdings(cfg.ORDERS_DB_PATH)
    already_today = count_today(cfg.ORDERS_DB_PATH, today)
    _log(f"보유중(orders.db 기준) {len(held)}종목 / 오늘 주문 {already_today}건")

    if len(held) >= cfg.BOT_MAX_POSITIONS:
        _log(f"포지션 가득 ({len(held)}/{cfg.BOT_MAX_POSITIONS}) — 종료")
        return

    try:
        cash = get_account_balance()
    except Exception as e:
        _log(f"예수금 조회 실패: {e}")
        notify(f"❌ [예수금 조회 실패] {e}")
        return

    _log(f"예수금: {cash:,.0f}원")

    daily_limit = cfg.BOT_MAX_DAILY_ORDERS - already_today
    slot_limit  = cfg.BOT_MAX_POSITIONS - len(held)
    budget_slots = min(daily_limit, slot_limit)

    bought = 0
    for sig in candidates:
        if bought >= budget_slots:
            _log(f"슬롯 소진 ({bought}건) — 중단")
            break
        code = sig["code"]
        if code in held:
            _log(f"[스킵] {code} 이미 보유중")
            continue
        if has_order_today(cfg.ORDERS_DB_PATH, code, today):
            _log(f"[스킵] {code} 오늘 이미 주문")
            continue

        result = _place_order(sig, cash)
        if result is None:
            continue
        if result.get("stop"):
            break
        if result.get("failed"):
            continue
        bought += 1
        cash -= result["cost"]

    _log(f"매수봇 종료 — 신규 매수 {bought}건, 잔여 예수금 {cash:,.0f}원")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        notify(f"❌ [봇 크래시] {type(e).__name__}: {e}")
        sys.exit(1)

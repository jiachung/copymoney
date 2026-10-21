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
)
from buy_bot.notifier import notify
from buy_bot.signals import load_candidates
from buy_bot.storage import (
    active_holdings, count_today, has_order_today, init_db, insert_order,
)


_GRADE_TAG = {4: "🏆 Best 4/4", 3: "🥇 3/4"}


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


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
    _log(f"매수봇 시작 — {today} (TRADE_MODE=paper)")

    try:
        candidates = load_candidates(cfg.SIGNALS_DB_PATH, today, cfg.BOT_MIN_GRADE)
    except Exception as e:
        _log(f"신호 로드 실패: {e}")
        notify(f"❌ [신호 로드 실패] {e}")
        return

    _log(f"신호 후보 {len(candidates)}건 (grade ≥ {cfg.BOT_MIN_GRADE})")
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

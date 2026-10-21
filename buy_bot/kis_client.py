"""KIS OpenAPI 모의계좌 매수봇 전용 클라이언트.

bg_bot/order.py에서 매수에 필요한 함수만 self-contained로 분리.
토큰 캐시 파일은 GH runner에선 ephemeral이라 매 실행 새로 발급.
"""

import json
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

from buy_bot.config import (
    KIS_APP_KEY, KIS_APP_SECRET, KIS_ACCOUNT_NO, KIS_ACCOUNT_PROD_CODE,
    KIS_BASE_URL,
)


TOKEN_CACHE_FILE = Path(__file__).parent / "data" / "kis_token.json"

# 모의계좌(VTTC) 전용 TR
_TR = {"buy": "VTTC0802U", "balance": "VTTC8908R"}


class NoPositionError(RuntimeError):
    """잔고 없음."""
    pass


def _get_token() -> str:
    if TOKEN_CACHE_FILE.exists():
        try:
            cached = json.loads(TOKEN_CACHE_FILE.read_text())
            expires_at = datetime.fromisoformat(cached["expires_at"])
            if datetime.now() < expires_at - timedelta(minutes=10):
                return cached["access_token"]
        except Exception:
            pass

    res = requests.post(
        f"{KIS_BASE_URL}/oauth2/tokenP",
        json={
            "grant_type": "client_credentials",
            "appkey": KIS_APP_KEY,
            "appsecret": KIS_APP_SECRET,
        },
        timeout=10,
    ).json()

    token = res.get("access_token")
    if not token:
        raise RuntimeError(f"토큰 발급 실패: {res.get('msg1', res)}")

    expires_at = datetime.now() + timedelta(seconds=int(res.get("expires_in", 86400)))
    try:
        TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_CACHE_FILE.write_text(json.dumps({
            "access_token": token,
            "expires_at": expires_at.isoformat(),
        }))
    except Exception:
        pass  # ephemeral runner면 못 써도 OK
    return token


def _headers(tr_id: str) -> dict:
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {_get_token()}",
        "appkey": KIS_APP_KEY,
        "appsecret": KIS_APP_SECRET,
        "tr_id": tr_id,
    }


def get_account_balance() -> float:
    """모의계좌 예수금."""
    params = {
        "CANO": KIS_ACCOUNT_NO,
        "ACNT_PRDT_CD": KIS_ACCOUNT_PROD_CODE,
        "AFHR_FLPR_YN": "N", "OFL_YN": "",
        "INQR_DVSN": "02", "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
        "PDNO": "", "ORD_UNPR": "0", "ORD_DVSN": "00",
        "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N",
    }
    res = requests.get(
        f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/inquire-balance",
        headers=_headers(_TR["balance"]),
        params=params,
        timeout=10,
    ).json()
    return float(res["output"]["ord_psbl_cash"])


def get_current_price_info(ticker: str, retries: int = 3) -> dict:
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}
    for attempt in range(retries):
        try:
            res = requests.get(
                f"{KIS_BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price",
                headers=_headers("FHKST01010100"),
                params=params,
                timeout=10,
            ).json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise RuntimeError(f"현재가 조회 실패 [{ticker}]: {e}")
        if res.get("rt_cd") == "0" and "output" in res:
            out = res["output"]
            return {"current": float(out["stck_prpr"]),
                    "open":    float(out["stck_oprc"]),
                    "high":    float(out["stck_hgpr"])}
        msg = res.get("msg1", "")
        if attempt < retries - 1:
            time.sleep(1.0 * (attempt + 1))
            continue
        raise RuntimeError(f"현재가 조회 실패 [{ticker}]: {msg or res}")


def _tick_size(price: float) -> int:
    if price < 2_000:    return 1
    if price < 5_000:    return 5
    if price < 20_000:   return 10
    if price < 50_000:   return 50
    if price < 200_000:  return 100
    if price < 500_000:  return 500
    return 1_000


def floor_to_tick(price: float) -> int:
    tick = _tick_size(price)
    return int(price // tick) * tick


def _post_order(tr_id: str, body: dict, action: str, ticker: str,
                retries: int = 3) -> dict:
    for attempt in range(retries):
        res = requests.post(
            f"{KIS_BASE_URL}/uapi/domestic-stock/v1/trading/order-cash",
            headers=_headers(tr_id),
            json=body,
            timeout=10,
        ).json()
        if res.get("rt_cd") == "0":
            return res
        msg = res.get("msg1", "")
        if "초당 거래건수" in msg and attempt < retries - 1:
            time.sleep(1.0 * (attempt + 1))
            continue
        if "잔고" in msg:
            raise NoPositionError(f"{action} 주문 실패 [{ticker}]: {msg}")
        raise RuntimeError(f"{action} 주문 실패 [{ticker}]: {msg}")
    raise RuntimeError(f"{action} 주문 실패 [{ticker}]: 재시도 {retries}회 초과")


def buy_limit_order(ticker: str, price: float, shares: int) -> dict:
    body = {
        "CANO": KIS_ACCOUNT_NO,
        "ACNT_PRDT_CD": KIS_ACCOUNT_PROD_CODE,
        "PDNO": ticker,
        "ORD_DVSN": "00",
        "ORD_QTY": str(shares),
        "ORD_UNPR": str(floor_to_tick(price)),
    }
    return _post_order(_TR["buy"], body, "매수", ticker)

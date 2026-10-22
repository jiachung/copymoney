"""buy_bot 환경변수 로드 + 검증.

copymoney의 .env는 실전 KIS 키용. buy_bot은 KIS_PAPER_* 접두사로
모의계좌 전용 키를 격리 로드. GH Actions에선 Secrets로 주입.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # GH runner에선 dotenv 없이도 Secrets로 들어옴


# ── KIS 모의계좌 (buy_bot 전용) ─────────────────────────
KIS_APP_KEY           = os.getenv("KIS_PAPER_APP_KEY")
KIS_APP_SECRET        = os.getenv("KIS_PAPER_APP_SECRET")
KIS_ACCOUNT_NO        = os.getenv("KIS_PAPER_ACCOUNT_NO")
KIS_ACCOUNT_PROD_CODE = os.getenv("KIS_PAPER_ACCOUNT_PROD_CODE", "01")
KIS_BASE_URL          = "https://openapivts.koreainvestment.com:29443"
TRADE_MODE            = "paper"  # 하드코딩 — 실전 전환 금지

# ── Telegram ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ── BOT 파라미터 (spec 2026-10-20 사용자 확정) ──────────
BOT_ENABLED            = os.getenv("BOT_ENABLED", "true").lower() == "true"
BOT_CAPITAL            = int(os.getenv("BOT_CAPITAL", "50000000"))
BOT_PER_POSITION       = int(os.getenv("BOT_PER_POSITION", "10000000"))
BOT_MAX_POSITIONS      = int(os.getenv("BOT_MAX_POSITIONS", "5"))
BOT_MAX_DAILY_ORDERS   = int(os.getenv("BOT_MAX_DAILY_ORDERS", "5"))
BOT_LIMIT_PRICE_MARGIN = float(os.getenv("BOT_LIMIT_PRICE_MARGIN", "1.002"))
BOT_MIN_GRADE          = int(os.getenv("BOT_MIN_GRADE", "3"))
# 주봉 5선 이격도 상한 (%): 이보다 멀면 과열로 간주해 매수 스킵.
# 데이터 분석(2026-10-22): 20-30% 구간 승률 40% / 5일 -7.85%, HT로보틱스 35.8% 급락 사례.
BOT_MAX_WEEKLY_GAP_PCT = float(os.getenv("BOT_MAX_WEEKLY_GAP_PCT", "20.0"))

# ── 경로 (same-repo 접근) ────────────────────────────
REPO_ROOT       = Path(__file__).parent.parent
SIGNALS_DB_PATH = str(REPO_ROOT / "data" / "signals.db")
ORDERS_DB_PATH  = Path(__file__).parent / "data" / "orders.db"


def validate() -> None:
    """필수 env + signals.db 존재 체크. 봇 시작 시 반드시 호출."""
    missing = []
    for name, val in [
        ("KIS_PAPER_APP_KEY",    KIS_APP_KEY),
        ("KIS_PAPER_APP_SECRET", KIS_APP_SECRET),
        ("KIS_PAPER_ACCOUNT_NO", KIS_ACCOUNT_NO),
    ]:
        if not val:
            missing.append(name)
    if missing:
        raise RuntimeError(f"환경변수 미설정: {missing}")
    if not Path(SIGNALS_DB_PATH).exists():
        raise RuntimeError(f"signals.db 없음: {SIGNALS_DB_PATH}")

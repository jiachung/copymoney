"""buy_bot 전용 텔레그램 알림 — [매수봇] prefix."""

import requests

from buy_bot.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


_PREFIX = "[매수봇]"


def _send(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram 미설정] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print(f"[Telegram 전송 실패] {e}\n메시지: {message}")


def notify(message: str) -> None:
    _send(f"{_PREFIX} {message}")

#!/usr/bin/env python3
"""
30분봉 VWAP + 이평선 정배열 자동 스크리닝 시스템
한국투자증권 OpenAPI 기반
"""

import os
import json
import sys
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class KoreaInvestmentAPI:
    """한국투자증권 OpenAPI 클래스"""

    REAL_URL = "https://openapi.koreainvestment.com:9443"
    PAPER_URL = "https://openapivts.koreainvestment.com:29443"

    # KIS OpenAPI tr_id (필수 헤더). 시세 조회는 실전/모의 공통 사용 가능.
    TR_VOLUME_RANK = "FHPST01710000"
    TR_DAILY_CHART = "FHKST03010100"   # 국내주식 기간별 시세(일/주/월/년)
    TR_MINUTE_CHART = "FHKST03010200"  # 국내주식 시간별(분봉) 시세

    def __init__(self, api_key: str, api_secret: str, account: str, mode: str = "real"):
        self.api_key = api_key
        self.api_secret = api_secret
        self.account = account
        self.mode = mode
        self.base_url = self.PAPER_URL if mode == "paper" else self.REAL_URL
        self.access_token: Optional[str] = None
        self.token_expired_time: Optional[datetime] = None
        self._get_access_token()

    def _get_access_token(self):
        """접근 토큰 발급"""
        url = f"{self.base_url}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.api_key,
            "appsecret": self.api_secret,
        }
        try:
            response = requests.post(url, json=body, timeout=10)
            response.raise_for_status()
            result = response.json()
            self.access_token = result["access_token"]
            expires_in = int(result.get("expires_in", 3600))
            self.token_expired_time = datetime.now() + timedelta(seconds=expires_in - 60)
            logger.info("접근 토큰 발급 성공")
        except Exception as e:
            logger.error(f"토큰 발급 실패: {e}")
            raise

    def _refresh_token_if_needed(self):
        if self.token_expired_time and datetime.now() >= self.token_expired_time:
            self._get_access_token()

    def _headers(self, tr_id: str) -> Dict[str, str]:
        """API 요청 헤더 (tr_id 필수). GET 조회에는 content-type 붙이지 않음
        — 붙이면 KIS 서버가 500 반환."""
        self._refresh_token_if_needed()
        return {
            "authorization": f"Bearer {self.access_token}",
            "appkey": self.api_key,
            "appsecret": self.api_secret,
            "tr_id": tr_id,
            "custtype": "P",  # P=개인, B=법인
        }

    def _get(self, path: str, tr_id: str, params: Dict) -> Dict:
        """공통 GET 호출 + rt_cd 검사"""
        url = f"{self.base_url}{path}"
        response = requests.get(url, headers=self._headers(tr_id), params=params, timeout=10)
        if response.status_code >= 400:
            # 오류 응답 body를 함께 노출해서 진단 쉽게
            body_preview = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"HTTP {response.status_code} on {path}: {body_preview}")
        result = response.json()
        # KIS는 HTTP 200에도 rt_cd로 실패 반환
        rt_cd = result.get("rt_cd")
        if rt_cd is not None and rt_cd != "0":
            raise RuntimeError(f"KIS API 오류: rt_cd={rt_cd}, msg={result.get('msg1')}")
        return result

    def get_daily_volume_ranking(self, limit: int = 100) -> List[Dict]:
        """거래대금 상위 종목 조회 (KOSPI+KOSDAQ 통합)"""
        path = "/uapi/domestic-stock/v1/quotations/volume-rank"
        params = {
            "fid_cond_mrkt_div_code": "J",   # J=주식 (KOSPI/KOSDAQ 통합)
            "fid_cond_scr_div_code": "20171",
            "fid_input_iscd": "0000",        # 0000=전체, 0001=KOSPI, 1001=KOSDAQ
            "fid_div_cls_code": "0",         # 0=전체
            "fid_blng_cls_code": "3",        # 3=거래금액순
            "fid_trgt_cls_code": "111111111",
            "fid_trgt_exls_cls_code": "000000",
            "fid_input_price_1": "",
            "fid_input_price_2": "",
            "fid_vol_cnt": "",
            "fid_input_date_1": "",
        }
        try:
            result = self._get(path, self.TR_VOLUME_RANK, params)
            stocks = []
            for item in (result.get("output") or [])[:limit]:
                stocks.append({
                    "code": item.get("mksc_shrn_iscd"),
                    "name": item.get("hts_kor_isnm"),
                    "volume": item.get("acml_vol"),
                })
            logger.info(f"거래대금 상위 {len(stocks)}개 종목 조회 완료")
            return stocks
        except Exception as e:
            logger.error(f"거래대금 조회 실패: {e}")
            return []

    def get_30min_candles(self, stock_code: str) -> List[Dict]:
        """
        30분봉 캔들 데이터 조회.
        KIS 분봉 API는 1회 최대 30건 반환. 30분봉은 1분봉을 30개씩 집계.
        여기서는 시간별 API를 여러 시각에 호출해 1분봉을 모은 뒤 30분 단위로 리샘플링.
        """
        # 하루치 1분봉 확보를 위해 15:30, 14:30, 13:30, ... 순으로 호출
        # KIS 분봉 API는 fid_input_hour_1(HHMMSS) 기준 이전 30건 반환
        query_times = ["153000", "143000", "133000", "123000", "113000", "103000", "093000"]
        raw_minutes: List[Dict] = []

        for hour in query_times:
            path = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
            params = {
                "fid_etc_cls_code": "",
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": stock_code,
                "fid_input_hour_1": hour,   # HHMMSS
                "fid_pw_data_incu_yn": "N",  # N=장중데이터만
            }
            try:
                result = self._get(path, self.TR_MINUTE_CHART, params)
                for item in (result.get("output2") or []):
                    close = int(item.get("stck_prpr", 0) or 0)
                    if close == 0:
                        continue
                    raw_minutes.append({
                        "time": f"{item.get('stck_bsop_date','')}{item.get('stck_cntg_hour','')}",
                        "open": int(item.get("stck_oprc", 0) or 0),
                        "high": int(item.get("stck_hgpr", 0) or 0),
                        "low": int(item.get("stck_lwpr", 0) or 0),
                        "close": close,
                        "volume": int(item.get("cntg_vol", 0) or 0),
                    })
            except Exception as e:
                logger.warning(f"{stock_code} 분봉 조회 실패({hour}): {e}")
                continue

        if not raw_minutes:
            return []

        # 시간 오름차순 정렬 후 30개씩 묶어 30분봉 생성
        raw_minutes.sort(key=lambda x: x["time"])
        candles: List[Dict] = []
        for i in range(0, len(raw_minutes), 30):
            chunk = raw_minutes[i:i + 30]
            if not chunk:
                continue
            candles.append({
                "time": chunk[-1]["time"],
                "open": chunk[0]["open"],
                "high": max(c["high"] for c in chunk),
                "low": min(c["low"] for c in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(c["volume"] for c in chunk),
            })
        logger.debug(f"{stock_code}: 30분봉 {len(candles)}개 생성")
        return candles

    def _get_period_candles(self, stock_code: str, period_code: str, count: int) -> List[Dict]:
        """일/주/월봉 공통 조회"""
        path = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
        today = datetime.now().strftime("%Y%m%d")
        # 넉넉히 과거 기간 커버
        days_span = count * (7 if period_code == "W" else 2)
        start = (datetime.now() - timedelta(days=days_span)).strftime("%Y%m%d")
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_input_iscd": stock_code,
            "fid_input_date_1": start,
            "fid_input_date_2": today,
            "fid_period_div_code": period_code,
            "fid_org_adj_prc": "0",  # 0=수정주가
        }
        try:
            result = self._get(path, self.TR_DAILY_CHART, params)
            candles = []
            for item in (result.get("output2") or [])[:count]:
                close = int(item.get("stck_clpr", 0) or 0)
                if close == 0:
                    continue
                candles.append({
                    "date": item.get("stck_bsop_date"),
                    "open": int(item.get("stck_oprc", 0) or 0),
                    "high": int(item.get("stck_hgpr", 0) or 0),
                    "low": int(item.get("stck_lwpr", 0) or 0),
                    "close": close,
                    "volume": int(item.get("acml_vol", 0) or 0),
                })
            candles.reverse()  # 오래된 것부터
            return candles
        except Exception as e:
            logger.error(f"{stock_code} {period_code}봉 조회 실패: {e}")
            return []

    def get_daily_candles(self, stock_code: str, days_back: int = 100) -> List[Dict]:
        return self._get_period_candles(stock_code, "D", days_back)

    def get_weekly_candles(self, stock_code: str, weeks_back: int = 50) -> List[Dict]:
        return self._get_period_candles(stock_code, "W", weeks_back)


class TechnicalAnalyzer:
    """기술적 분석 클래스"""

    @staticmethod
    def calculate_vwap(candles: List[Dict]) -> List[float]:
        """VWAP 계산 (typical_price * volume 누적 / volume 누적)"""
        vwap = []
        cumsum_pv = 0.0
        cumsum_volume = 0
        for candle in candles:
            typical_price = (candle["high"] + candle["low"] + candle["close"]) / 3
            cumsum_pv += typical_price * candle["volume"]
            cumsum_volume += candle["volume"]
            vwap.append(cumsum_pv / cumsum_volume if cumsum_volume > 0 else 0)
        return vwap

    @staticmethod
    def calculate_sma(prices: List[float], period: int) -> List[Optional[float]]:
        """단순이동평균"""
        sma: List[Optional[float]] = []
        for i in range(len(prices)):
            if i < period - 1:
                sma.append(None)
            else:
                sma.append(sum(prices[i - period + 1:i + 1]) / period)
        return sma

    @staticmethod
    def check_trend_alignment(sma_5: Optional[float], sma_20: Optional[float],
                              sma_60: Optional[float]) -> Tuple[bool, str]:
        """추세 정배열 판단"""
        if sma_5 is None or sma_20 is None or sma_60 is None:
            return False, "이평선_데이터부족"
        if sma_5 > sma_20 > sma_60:
            return True, "완벽한_정배열"
        if sma_5 > sma_20 and sma_20 >= sma_60 * 0.98:
            return True, "초기진입_신호"
        return False, "정배열_아님"

    @staticmethod
    def check_weekly_trend(candles_weekly: List[Dict], sma_5_w: Optional[float],
                           current_price: float) -> Tuple[bool, str]:
        """주봉 추세 확인: 종가 > 5주선 + 5주선 상향"""
        if len(candles_weekly) < 6 or sma_5_w is None:
            return False, "주봉_데이터부족"

        closes_recent = [c["close"] for c in candles_weekly[-5:]]
        closes_prev = [c["close"] for c in candles_weekly[-6:-1]]
        sma5_recent = sum(closes_recent) / 5
        sma5_prev = sum(closes_prev) / 5

        if current_price <= sma_5_w:
            return False, "주봉_5선아래"
        if sma5_recent <= sma5_prev:
            return False, "주봉_5선하향"

        distance = (current_price - sma_5_w) / sma_5_w * 100
        return True, f"봉_5선위_{distance:.1f}%"

    @staticmethod
    def check_daily_trend(sma_5_d: Optional[float], sma_20_d: Optional[float],
                          current_price: float) -> Tuple[bool, str]:
        """일봉 추세 확인: 정배열(5>20) + 종가 ≥ 5일선 근처"""
        if sma_5_d is None or sma_20_d is None:
            return False, "일봉_데이터부족"
        if sma_5_d <= sma_20_d:
            return False, "일봉_역배열"
        if current_price > sma_5_d:
            distance = (current_price - sma_5_d) / sma_5_d * 100
            return True, f"정배열_{distance:.1f}%"
        if current_price >= sma_5_d * 0.98:
            return True, "정배열_근처"
        return False, "일봉_약세"

    @staticmethod
    def calculate_key_levels(candles_daily: List[Dict], current_price: float,
                             sma_5_d: float, sma_20_d: float) -> Dict:
        """주요 저항/지지/손절 레벨"""
        if len(candles_daily) < 5:
            return {}
        recent = candles_daily[-20:]
        highest = max(c["high"] for c in recent)
        lowest = min(c["low"] for c in recent)
        resistance = max(highest, sma_20_d)
        support = min(lowest, sma_5_d)
        stop_loss = sma_5_d * 0.98
        return {
            "resistance": round(resistance),
            "support": round(support),
            "resistance_dist": round((resistance - current_price) / current_price * 100, 2),
            "support_dist": round((current_price - support) / current_price * 100, 2),
            "stop_loss": round(stop_loss),
            "risk_pct": round((current_price - stop_loss) / current_price * 100, 2),
        }


class StockScreener:
    """메인 스크리닝 엔진"""

    def __init__(self, api: KoreaInvestmentAPI):
        self.api = api
        self.analyzer = TechnicalAnalyzer()
        self.signals: List[Dict] = []

    def screen(self, limit: int = 100) -> List[Dict]:
        logger.info("=== 스크리닝 시작 ===")
        stocks = self.api.get_daily_volume_ranking(limit=limit)
        logger.info(f"검사 대상: {len(stocks)}개 종목")

        for idx, stock in enumerate(stocks):
            code = stock.get("code")
            name = stock.get("name")
            if not code:
                continue
            logger.info(f"[{idx + 1}/{len(stocks)}] {name} ({code}) 검사 중...")

            try:
                candles_30min = self.api.get_30min_candles(code)
                candles_daily = self.api.get_daily_candles(code, days_back=100)
                candles_weekly = self.api.get_weekly_candles(code, weeks_back=50)

                if len(candles_30min) < 5 or len(candles_daily) < 70 or len(candles_weekly) < 10:
                    logger.debug(f"{code}: 데이터 부족 (30m={len(candles_30min)}, "
                                 f"D={len(candles_daily)}, W={len(candles_weekly)})")
                    continue

                # 30분봉
                vwap_30min = self.analyzer.calculate_vwap(candles_30min)[-1]
                current_price = candles_30min[-1]["close"]

                # 일봉
                closes_daily = [c["close"] for c in candles_daily]
                sma_5_d = self.analyzer.calculate_sma(closes_daily, 5)[-1]
                sma_20_d = self.analyzer.calculate_sma(closes_daily, 20)[-1]
                sma_60_d = self.analyzer.calculate_sma(closes_daily, 60)[-1]

                # 주봉
                closes_weekly = [c["close"] for c in candles_weekly]
                sma_5_w = self.analyzer.calculate_sma(closes_weekly, 5)[-1]

                # STEP 1: 주봉
                weekly_ok, weekly_signal = self.analyzer.check_weekly_trend(
                    candles_weekly, sma_5_w, current_price
                )
                if not weekly_ok:
                    continue

                # STEP 2: 일봉
                daily_ok, daily_signal = self.analyzer.check_daily_trend(
                    sma_5_d, sma_20_d, current_price
                )
                if not daily_ok:
                    continue

                # STEP 3: 30분봉 VWAP
                if current_price <= vwap_30min:
                    continue

                # 신호 강도
                signal_strength = 3  # 3단계 통과 기본
                if sma_60_d is not None and sma_5_d > sma_20_d > sma_60_d:
                    signal_strength += 1

                levels = self.analyzer.calculate_key_levels(
                    candles_daily, current_price, sma_5_d, sma_20_d
                )

                signal = {
                    "code": code,
                    "name": name,
                    "current_price": current_price,
                    "signal_strength": signal_strength,
                    "weekly_signal": weekly_signal,
                    "weekly_5d": round(sma_5_w),
                    "daily_signal": daily_signal,
                    "daily_5d": round(sma_5_d),
                    "daily_20d": round(sma_20_d),
                    "price_vs_5d": round((current_price - sma_5_d) / sma_5_d * 100, 2),
                    "vwap_30min": round(vwap_30min),
                    "price_vs_vwap": round((current_price - vwap_30min) / vwap_30min * 100, 2),
                    **levels,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.signals.append(signal)
                logger.info(f"신호 포착: {name} (주:{weekly_signal} / 일:{daily_signal})")

            except Exception as e:
                logger.error(f"{code} 분석 실패: {e}")
                continue

        logger.info(f"=== 스크리닝 완료: {len(self.signals)}개 신호 발견 ===")
        return self.signals


class TelegramNotifier:
    """Telegram 알림"""

    # Telegram 메시지 4096자 제한
    MAX_LEN = 3800

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    def send_signals(self, signals: List[Dict]):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not signals:
            self._send_message(f"📊 {now}\n신호 없음")
            return

        signals_sorted = sorted(signals, key=lambda x: x.get("signal_strength", 0), reverse=True)

        header = (
            f"🚨 상승초기 종목 ({len(signals_sorted)}개)\n"
            f"⏰ {now}\n"
            + "=" * 30 + "\n\n"
        )
        buf = header

        for sig in signals_sorted:
            block = self._format_signal(sig)
            if len(buf) + len(block) > self.MAX_LEN:
                self._send_message(buf)
                buf = ""
            buf += block

        if buf:
            self._send_message(buf)

    @staticmethod
    def _format_signal(s: Dict) -> str:
        stars = "⭐" * s.get("signal_strength", 3)
        weekly = s['weekly_signal'].replace('주봉_', '').replace('_', ' ')
        daily = s['daily_signal'].replace('5일선', '5D')
        vwap_gap = s['price_vs_vwap']
        price_vs_5d = s['price_vs_5d']

        if price_vs_5d < 2:
            entry_state = "⬆️ 충분히 떨어짐"
        elif price_vs_5d < 5:
            entry_state = "→ 적당"
        else:
            entry_state = "⬇️ 약간 높음"

        risk = s.get("risk_pct", 0)
        if risk < 3:
            risk_state = "🟢 낮음"
        elif risk < 5:
            risk_state = "🟡 중간"
        else:
            risk_state = "🔴 높음"

        return (
            f"📌 <b>{s['name']}</b> ({s['code']}) {stars}\n"
            f"└─ {weekly} • {daily} • VWAP {vwap_gap:+.1f}%\n\n"
            f"<b>진입</b>\n"
            f"  일봉 정배열: 5D {s['daily_5d']:,} > 20D {s['daily_20d']:,} ✓\n"
            f"  주봉 5선: {s['weekly_5d']:,}원\n"
            f"  추천가: 5D 근처 ({s['daily_5d']:,}원)\n"
            f"  현재: 5D에서 {price_vs_5d:+.1f}%\n"
            f"  상태: {entry_state}\n\n"
            f"<b>목표/리스크</b>\n"
            f"  다음 저항: {s.get('resistance', 0):,}원 (+{s.get('resistance_dist', 0):.1f}%)\n"
            f"  손절: {s.get('stop_loss', 0):,}원 (-{risk:.1f}%)\n"
            f"  위험도: {risk_state}\n"
            + "─" * 30 + "\n\n"
        )

    def _send_message(self, text: str):
        url = f"{self.base_url}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Telegram 알림 전송 완료")
        except Exception as e:
            logger.error(f"Telegram 전송 실패: {e}")


def _load_dotenv(path: str = ".env"):
    """로컬 실행 시 .env 파일이 있으면 환경변수로 로드 (덮어쓰지 않음)"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def main():
    _load_dotenv()

    api_key = os.getenv("KIS_APP_KEY")
    api_secret = os.getenv("KIS_APP_SECRET")
    account_no = os.getenv("KIS_ACCOUNT_NO")
    account_prod = os.getenv("KIS_ACCOUNT_PROD_CODE", "01")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not all([api_key, api_secret, account_no, telegram_token, telegram_chat_id]):
        logger.error("필수 환경변수 누락: KIS_APP_KEY/SECRET, KIS_ACCOUNT_NO, "
                     "TELEGRAM_BOT_TOKEN/CHAT_ID")
        sys.exit(1)

    account = f"{account_no}-{account_prod}"
    mode = os.getenv("TRADE_MODE", "real").lower()  # real | paper

    limit = int(os.getenv("SCREEN_LIMIT", "150"))

    try:
        api = KoreaInvestmentAPI(api_key, api_secret, account)
        screener = StockScreener(api)
        signals = screener.screen(limit=limit)

        output_file = os.getenv("OUTPUT_FILE", "stock_signals.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(signals, f, ensure_ascii=False, indent=2)
        logger.info(f"결과 저장: {output_file}")

        notifier = TelegramNotifier(telegram_token, telegram_chat_id)
        notifier.send_signals(signals)

    except Exception as e:
        logger.error(f"실행 중 오류: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

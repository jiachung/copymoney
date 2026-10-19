# 매수 자동화 봇 — 1단계 Entry Only 스펙

## 목적

**Copymoney 스크리닝 신호 → 모의계좌에서 자동 매수**를 수행하는 봇.
이번 단계는 **매수만** 집중, 매도/청산 로직은 2단계로 분리. 2주간 모의 운영으로 자동매매 파이프라인 검증 후 실전 전환 여부 결정.

**비목적 (하지 말 것):**
- 실전 매매 (TRADE_MODE=paper 고정)
- 매도/익절/손절 로직 (별도 2단계)
- 포지션 사이즈 동적 조정
- 신호 로직 자체 수정 (copymoney 그대로 사용)

---

## 아키텍처

```
copymoney (스크리닝)
  └─ data/signals.db  (SQLite, 자동 git commit)
         ↓
      [buy_bot]
         ├─ 신호 로드 (오늘의 🏆 Best 4/4 티어)
         ├─ 중복 체크 (DB: orders 테이블)
         ├─ KIS 모의계좌 지정가 주문
         ├─ 주문 기록 + Telegram 알림
         └─ 로그
```

**봇 위치:** `/Users/jiachung/Projects/copymoney/buy_bot/` (copymoney 내부 하위 디렉토리)
**실행 환경:** 로컬 crontab (매일 10:05, 12:35, 15:40 — copymoney 스크리닝 완료 5분 후)
**모드:** `TRADE_MODE=paper` 고정 (실전 모드 전환은 수동 승인 후)

---

## 입력 데이터

### copymoney의 signals DB

`/Users/jiachung/Projects/copymoney/data/signals.db` (SQLite)

**signals 테이블 (주요 필드):**
- `id`, `run_at` (YYYY-MM-DD HH:MM:SS), `code`, `name`
- `current_price` (신호 발생 시 가격)
- `signal_strength` (3 or 4)
- `price_vs_5d`, `price_vs_vwap`, `risk_pct` (%)
- 기타 signal 분석 필드

**매수 대상: 🏆 Best 4/4 + 🥇 3/4 티어** (Python 필터로 처리, SQL은 당일 신호 전체):

```sql
-- 1단계: 오늘 신호 전부 로드
SELECT * FROM signals
WHERE substr(run_at, 1, 10) = DATE('now', 'localtime')
ORDER BY id
```

```python
# 2단계: 티어 점수 계산 + 필터
def grade(s):
    score = 0
    if s['signal_strength'] == 4: score += 1
    if 2 <= s['price_vs_5d'] < 5: score += 1
    if 1 <= s['price_vs_vwap'] < 3: score += 1
    if 3 <= s['risk_pct'] < 5: score += 1
    return score

# 🥇 3/4 이상만 (= 3 또는 4)
candidates = [s for s in signals if grade(s) >= 3]

# 우선순위: 티어 → 강도 순 (🏆 먼저)
candidates.sort(key=lambda s: (grade(s), s['signal_strength']), reverse=True)
```

**4개 Best 조건:**
- ⭐⭐⭐⭐ (`signal_strength == 4`)
- 5D 이격 중간 (`2 ≤ price_vs_5d < 5`)
- VWAP 이격 1-3% (`1 ≤ price_vs_vwap < 3`)
- 위험도 🟡 (`3 ≤ risk_pct < 5`)

→ 분석 검증: 🏆 4/4 상승장 승률 93-100%, 🥇 3/4 우수 등급.
→ 🥇 3/4 포함 시 `signal_strength=3 (⭐⭐⭐)` 신호도 통과 가능 (예: ⭐⭐⭐ + 다른 3개 조건).

### 중복 방지 (buy_bot 자체 DB)

새 테이블 생성: `/Users/jiachung/Projects/copymoney/buy_bot/data/orders.db`

```sql
CREATE TABLE orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at TEXT NOT NULL,         -- 주문 요청 시각
    code TEXT NOT NULL,
    name TEXT,
    signal_run_at TEXT,                 -- 어느 신호 기반인지 (signals.run_at)
    order_price INTEGER,                -- 지정가
    order_shares INTEGER,
    kis_order_no TEXT,                  -- KIS 주문번호
    status TEXT,                        -- SUBMITTED / FILLED / FAILED / CANCELLED
    filled_price INTEGER,               -- 체결가 (확인 후)
    filled_shares INTEGER,
    filled_at TEXT,
    error_msg TEXT,
    UNIQUE(code, substr(submitted_at,1,10))  -- 종목+일자 중복 방지
);
```

---

## 매수 조건

### 티어 필터 (가장 중요)

**🏆 Best 4/4 티어만** 매수. 다음 4개 조건을 전부 충족:

| 조건 | 범위 |
|---|---|
| 신호 강도 | ⭐⭐⭐⭐ (= `signal_strength == 4`) |
| 5D 이격도 | `2% ≤ price_vs_5d < 5%` |
| VWAP 이격도 | `1% ≤ price_vs_vwap < 3%` |
| 위험도 | `3% ≤ risk_pct < 5%` |

**참고:** copymoney의 `stock_screener.py` 의 `TelegramNotifier._grade_signal()` 함수와 동일 로직. 재사용 가능.

### 추가 가드레일

1. **이미 보유 중인 종목 제외** — KIS 잔고 조회로 보유 종목 확인, 포함되어 있으면 스킵
2. **당일 중복 매수 금지** — orders.db 의 UNIQUE 제약으로 자동 처리
3. **하루 최대 N건** — N = 5 (아래 "포지션 사이징" 참고)
4. **예수금 부족** — 매수 금액이 예수금 초과하면 스킵 (경고 로그)
5. **종목 코드 유효성** — 숫자 6자리 아니면 스킵
6. **시간 체크** — 장중 (09:00 ~ 15:20) 아니면 스킵. 15:30 신호는 다음날 09:05 재발사 OR 즉시 체결 안되면 취소

---

## 포지션 사이징 (사용자 확정값)

| 파라미터 | 값 |
|---|---|
| **총 투입 자본 (모의)** | **5,000만원** |
| **종목당 투자 금액** | **1,000만원** (총자본의 20%) |
| **최대 보유 종목 수** | **5개** (풀 포지션 = 5종목 × 1,000만원 = 5,000만원) |
| **하루 최대 신규 매수** | **5건** (풀 포지션까지 하루에 채울 수 있음) |

**주문 수량 계산:**
```python
target_amount = 10_000_000  # 종목당 1천만원
shares = target_amount // order_price  # 소수점 버림 (정수 주식 수)
# 1주 가격이 1,000만원 초과하면 스킵 (고가주 제외)
if shares < 1:
    skip "고가주 (1주 > 1,000만원)"
```

**예시:**
- 하나마이크론 46,000원 → 1천만원 / 46,000 = 217주 → 217 × 46,000 = 998만원 매수
- 삼성전기 1,500,000원 → 1천만원 / 1,500,000 = 6주 → 6 × 1,500,000 = 900만원 매수
- 가상 고가주 15,000,000원 → shares=0 → **스킵**

**포지션 확장 희망 시 (추후):**
- 5-6종목 분산 유지 희망 → 종목당 833만원 × 6종목 조정 가능 (env 변경)
- 더 분산 → 500만원 × 10종목 (안전)
- 집중 → 2,500만원 × 2종목 (고위험)

---

## 주문 방식

### 지정가 매수 (시장가 X)

**이유:** 모의 환경에서도 슬리피지 통제 필요. 시장가는 체결은 보장되나 고점에 걸릴 위험.

**지정가 산정:**
- `signal.current_price` (신호 발생 시점의 가격) 사용
- bg_bot 의 `floor_to_tick()` 로 호가단위 맞춤
- **지정가 = current_price × 1.002** (0.2% 여유, 체결 유도)
- 10분 내 체결 안 되면 자동 취소 (옵션 — 2단계에서 구현)

### KIS API (bg_bot/order.py 재사용)

**함수:**
```python
from bg_bot.order import (
    get_account_balance,
    get_current_price_info,
    buy_limit_order,
    floor_to_tick,
    TRADE_MODE,
)

# 1. 예수금 확인
cash = get_account_balance()

# 2. 현재가 조회 (옵션 — 신호의 current_price와 큰 차이 있으면 재검증)
info = get_current_price_info(ticker)
live_price = info['stck_prpr']

# 3. 지정가 계산
limit_price = floor_to_tick(live_price * 1.002)

# 4. 주문 (지정가, 매수, 수량)
result = buy_limit_order(ticker=code, price=limit_price, shares=shares)
# result 에서 KIS 주문번호 추출 → orders.db 저장
```

**bg_bot/order.py 그대로 복사 (또는 import)** — 토큰 관리, rate limit, 호가단위 모두 검증된 코드.

### 체결 확인 (필수)

주문 직후 체결 안됨 (지정가라). 체결 확인은 **다음 실행 때** or **별도 5분 뒤 체크 스크립트**:

```python
# 5분 후 주문 상태 조회 → 체결 여부 확인
# KIS: /uapi/domestic-stock/v1/trading/inquire-daily-ccld
# status 업데이트: SUBMITTED → FILLED / CANCELLED
```

**간단한 1단계:** 주문만 넣고 체결 확인은 다음 cron 실행에서. 2단계에서 정교화.

---

## 알림 (Telegram)

**copymoney의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 재사용.**
구분 위해 prefix 추가:

### 주문 발생 시 (즉시)
```
🤖 [매수 주문] 2026-10-21 10:07:15 (모의)

📌 하나마이크론 (067310)
  신호: 🏆 Best 4/4 ⭐⭐⭐⭐
  지정가: 45,600원 × 11주 = 501,600원
  예수금 잔액: 4,498,400원 → 3,996,800원 (예상)
  KIS 주문번호: 1234567890
```

### 체결 확인 시 (다음 실행 때 체크)
```
✅ [체결 완료] 하나마이크론 (067310)
  체결가: 45,550원 × 11주 = 501,050원 (지정가보다 -50원)
  현재 포지션: 1/10 종목
```

### 에러/실패 시
```
❌ [매수 실패] 하나마이크론 (067310)
  이유: 예수금 부족 (요청 501,600원 / 보유 42,000원)
  오늘 매수 중단 권장
```

### 일일 요약 (15:40 실행 끝)
```
📊 [일일 매수 요약] 2026-10-21

신규 매수: 3건
  하나마이크론 (067310): 501,050원 체결
  삼성전기 (009150): 505,000원 체결
  LG전자 (066570): 미체결 (지정가 미달)

보유 종목: 3/10
예수금: 3,493,950원
오늘 신호: 15건 중 🏆 3건 → 매수 시도 3건
```

---

## 긴급 중단 (Kill Switch)

### 자동 중단 조건

1. **KIS API 토큰 발급 실패 3회 연속** → 중단, 수동 복구 알림
2. **매수 실패 3회 연속** → 중단, 수동 복구 알림
3. **예수금 음수 / 포지션 수 11개 이상** → 로직 버그 가능성, 즉시 중단

### 수동 중단 (환경변수)

```bash
# .env 에 추가하면 봇 비활성화 (crontab 은 돌지만 주문 안 함)
BOT_ENABLED=false
```

---

## 환경변수 (.env)

copymoney의 .env 기반 + 추가:

```bash
# 기존 (copymoney)
KIS_APP_KEY=...
KIS_APP_SECRET=...
KIS_ACCOUNT_NO=50182013       # ⚠️ 모의계좌 번호 (실전과 다름!)
KIS_ACCOUNT_PROD_CODE=01
TRADE_MODE=paper              # ⚠️ paper 고정, 변경 시 수동 검토 필수

TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...

# 신규 (buy_bot) — 사용자 확정값
BOT_ENABLED=true              # false 로 설정 시 봇 비활성
BOT_CAPITAL=50000000          # 총 투입 자본 5,000만원
BOT_PER_POSITION=10000000     # 종목당 1,000만원 (총자본 × 20%)
BOT_MAX_POSITIONS=5           # 최대 5종목 (풀 포지션)
BOT_MAX_DAILY_ORDERS=5        # 하루 최대 신규 매수 5건
BOT_LIMIT_PRICE_MARGIN=1.002  # 지정가 = 현재가 × 1.002
BOT_MIN_GRADE=3               # 매수 티어 최소값 (3 = 🥇, 4 = 🏆만)
```

⚠️ **중요:** 지금 copymoney 의 `.env` 는 **실전 계좌** (73865321, TRADE_MODE=real) 로 설정되어 있음.
매수 봇 테스트 시 **반드시 모의계좌 (50182013) + TRADE_MODE=paper 로 전환** 하거나, 별도 `.env.bot` 로 분리.

---

## 실행 방식

### 로컬 crontab 등록

```cron
# copymoney 스크리닝 완료 후 5분 뒤 매수 봇 실행
5 10 * * 1-5 /Users/jiachung/Projects/copymoney/buy_bot/run_bot.sh
35 12 * * 1-5 /Users/jiachung/Projects/copymoney/buy_bot/run_bot.sh
40 15 * * 1-5 /Users/jiachung/Projects/copymoney/buy_bot/run_bot.sh
```

### run_bot.sh 래퍼
```bash
#!/usr/bin/env bash
set -euo pipefail
cd /Users/jiachung/Projects/copymoney
LOG_DIR=buy_bot/logs
mkdir -p "$LOG_DIR"
{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
  /Users/jiachung/.pyenv/shims/python3 -m buy_bot.main
} >> "$LOG_DIR/$(date +%Y%m%d).log" 2>&1
```

---

## 디렉토리 구조 (목표)

```
copymoney/
├── stock_screener.py        # 기존 (수정 X)
├── storage.py, performance.py, sheets_sync.py   # 기존
├── data/signals.db          # 기존 (buy_bot 이 읽기만)
├── buy_bot/
│   ├── __init__.py
│   ├── main.py              # 메인 엔트리 (신호 로드 → 필터 → 주문 → 기록)
│   ├── order_client.py      # bg_bot/order.py 복사 or import 래퍼
│   ├── storage.py           # orders.db 관리
│   ├── notifier.py          # Telegram 알림 (copymoney 와 공유)
│   ├── config.py            # 환경변수 로드
│   ├── data/orders.db       # 주문 이력 (SQLite, git commit 금지)
│   ├── logs/YYYYMMDD.log    # 일별 실행 로그
│   └── run_bot.sh           # crontab 래퍼
└── .gitignore               # buy_bot/data/, buy_bot/logs/ 추가
```

---

## 구현 체크리스트

### Phase 1a: 뼈대 (반드시)
- [ ] `buy_bot/` 디렉토리 생성 + `.gitignore` 업데이트
- [ ] `config.py`: 환경변수 로드 + 검증 (TRADE_MODE=paper 확인)
- [ ] `order_client.py`: bg_bot/order.py 를 copy or import (경로 조정)
- [ ] `storage.py`: orders.db 스키마 + CRUD
- [ ] `notifier.py`: Telegram prefix "[매수 주문]/[체결]/[실패]"
- [ ] `main.py`:
  - 신호 로드 (오늘 날짜, Best 4/4 티어)
  - 중복 체크 (orders.db + KIS 잔고)
  - 포지션 사이징 (환경변수 기반)
  - 주문 발사 (지정가, bg_bot 재사용)
  - orders.db 저장 + Telegram 알림
- [ ] `run_bot.sh` 작성 + crontab 등록

### Phase 1b: 체결 확인 (중요)
- [ ] 다음 실행 시, 지난 실행의 SUBMITTED 주문들 체결 확인
- [ ] FILLED / CANCELLED 상태 업데이트
- [ ] Telegram 체결 알림

### Phase 1c: 안전장치
- [ ] 긴급 중단 로직 (3회 연속 실패 → 중단)
- [ ] 일일 요약 알림 (15:40 실행 끝에)
- [ ] 로그 관리 (일별, 자동 로테이션)

---

## 테스트 플랜 (2주)

### Week 1
- Day 1-2: 모의계좌 세팅 + 봇 배포, 수동 1회 실행
- Day 3-5: 자동 실행 관찰, 주문/체결 로직 확인
- Day 5 끝: 중간 리뷰 — 몇 건 주문? 체결율? 오류?

### Week 2
- Day 6-10: 지속 운영, 매일 요약 확인
- Day 10 끝: 성과 분석 — 매수 가격 vs copymoney performance 데이터 매칭

### 2주 후 결정
- **모의 성적이 copymoney forward tracking 과 유사** → 실전 소액 전환 검토
- **괴리 크면** → 원인 분석 (슬리피지? 체결 실패?) → Phase 1 수정
- **매도 로직 2단계** 설계 시작

---

## 사용자 확정 사항 (2026-10-20)

| # | 항목 | 확정값 |
|---|---|---|
| 1 | 총 투입 자본 (모의) | **5,000만원** |
| 2 | 종목당 투자 금액 | **1,000만원** (5-6종목 분산) → 종목당 1천만원 × 5종목 선택 |
| 3 | 지정가 margin | **×1.002** (현재가 대비 0.2% 위) |
| 4 | 매수 티어 | **🥇 3/4 + 🏆 4/4** (grade ≥ 3) |
| 5 | 고가주 처리 | **스킵** (1주 > 종목당 금액이면 매수 안 함) |
| 6 | cron 시각 | **10:05 / 12:35 / 15:40** (copymoney 스크리닝 5분 후) |

→ 구현 착수 가능.

---

## 참고 자료

- **copymoney 신호 로직**: `stock_screener.py` 의 `TelegramNotifier._grade_signal()`
- **copymoney 분석 결과**: 대화 로그 (2026-10-19 기준 가설 1-11)
- **bg_bot order.py 함수**: `buy_limit_order`, `get_account_balance`, `get_current_price_info`, `floor_to_tick`, `NoPositionError`
- **KIS OpenAPI 문서**: https://apiportal.koreainvestment.com

---

**작성일:** 2026-10-20
**버전:** 1.0 (Entry Only Phase 1)
**다음 버전 예정:** Phase 2 — 매도/익절/손절 자동화 (2주 모의 운영 후)

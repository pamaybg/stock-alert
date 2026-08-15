"""
RSI(14) 미국 주식 모니터링 스크립트
---------------------------------
- config/tickers.json 에 있는 티커들을 순회하며 일봉(daily close) 기준 RSI(14)를 계산합니다.
- RSI < 20 이면 텔레그램으로 알림을 보냅니다.
- 같은 티커는 하루(Asia/Seoul 기준 날짜)에 한 번만 알림을 보냅니다.
- RSI 계산은 TA-Lib(업계 표준 레퍼런스 구현체)을 사용합니다.

이 스크립트는 GitHub Actions에서 주기적으로 실행되는 것을 전제로 작성되었습니다.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import talib
import yfinance as yf

try:
    from zoneinfo import ZoneInfo
except ImportError:  # 매우 오래된 파이썬 대비 (일반적으로 필요 없음)
    ZoneInfo = None  # type: ignore

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config" / "tickers.json"
STATE_PATH = BASE_DIR / "state" / "last_alert.json"

RSI_PERIOD = 14
RSI_THRESHOLD = 20
# RSI(Wilder's smoothing)는 초기 구간이 불안정하므로 넉넉하게 6개월치 데이터를 받아옵니다.
HISTORY_PERIOD = "6mo"

# true면 각 티커마다 최근 20거래일 종가 + RSI 값 전체를 로그에 찍어서
# 다른 앱/사이트의 RSI와 직접 검산할 수 있게 해줍니다.
# GitHub Actions Secrets/Variables 에서 RSI_DEBUG=false 로 바꾸면 끌 수 있습니다.
DEBUG = os.environ.get("RSI_DEBUG", "true").lower() not in ("0", "false", "no")
DEBUG_TAIL_ROWS = 20

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("rsi-monitor")


def today_kst_str() -> str:
    now = datetime.now(KST) if KST else datetime.utcnow()
    return now.strftime("%Y-%m-%d")


def load_tickers() -> list[str]:
    if not CONFIG_PATH.exists():
        log.error(f"설정 파일이 없습니다: {CONFIG_PATH}")
        return []
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    tickers = [t.strip().upper() for t in data.get("tickers", []) if str(t).strip()]
    # 중복 제거 (순서 유지)
    seen = set()
    unique_tickers = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            unique_tickers.append(t)
    return unique_tickers


def load_state() -> dict:
    today = today_kst_str()

    state = {}
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                state = json.load(f)
        except json.JSONDecodeError:
            log.warning("state 파일이 손상되어 초기화합니다.")
            state = {}

    # 날짜가 바뀌었으면(한국시간 기준) 알림 기록을 초기화합니다.
    if state.get("date") != today:
        state = {"date": today, "alerted": []}

    state.setdefault("alerted", [])
    return state


def save_state(state: dict) -> None:
    state["last_checked_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def get_latest_rsi(ticker: str) -> Optional[float]:
    try:
        # auto_adjust=False: 배당/분할로 보정되지 않은 "원래 종가"를 사용합니다.
        # (대부분의 주식 앱/증권사 HTS가 RSI 계산에 쓰는 값과 기준을 맞추기 위함.
        #  yfinance 기본값(auto_adjust=True)을 쓰면 최근 배당이 있었던 종목은
        #  과거 종가가 소급 보정되어 RSI가 눈에 띄게 달라질 수 있습니다.)
        hist = yf.Ticker(ticker).history(
            period=HISTORY_PERIOD, interval="1d", auto_adjust=False
        )
    except Exception as e:
        log.warning(f"[{ticker}] 데이터 조회 실패: {e}")
        return None

    if hist is None or hist.empty or "Close" not in hist.columns:
        log.warning(f"[{ticker}] 유효한 가격 데이터가 없습니다. 티커명을 확인하세요.")
        return None

    closes = hist["Close"].dropna()
    if len(closes) < RSI_PERIOD + 1:
        log.warning(f"[{ticker}] RSI 계산에 데이터가 부족합니다 ({len(closes)}개).")
        return None

    close_values = closes.to_numpy(dtype="float64")
    rsi_series = talib.RSI(close_values, timeperiod=RSI_PERIOD)
    latest_rsi = rsi_series[-1]

    if DEBUG:
        log.info(f"[{ticker}] ===== 검산용 상세 로그 (최근 {DEBUG_TAIL_ROWS}거래일) =====")
        log.info(
            f"[{ticker}] 데이터소스=yfinance, 조회기간={HISTORY_PERIOD}, "
            f"총 {len(closes)}개 일봉, 원가(비조정) 종가 사용"
        )

        tail_dates = closes.index[-DEBUG_TAIL_ROWS:]
        tail_closes = close_values[-DEBUG_TAIL_ROWS:]
        tail_rsi = rsi_series[-DEBUG_TAIL_ROWS:]
        for d, c, r in zip(tail_dates, tail_closes, tail_rsi):
            r_str = f"{r:.4f}" if not pd.isna(r) else "N/A(초기구간)"
            log.info(f"[{ticker}]   {d.date()}  종가={c:.4f}  RSI14={r_str}")

        last_bar_date = closes.index[-1]
        exchange_tz = last_bar_date.tzinfo
        if exchange_tz is not None:
            now_at_exchange = datetime.now(exchange_tz)
            if last_bar_date.date() == now_at_exchange.date():
                log.info(
                    f"[{ticker}] ⚠️ 마지막 봉({last_bar_date.date()})은 오늘자 장중 데이터입니다. "
                    f"정규장 마감(미국 동부시간 16:00) 전이면 아직 확정된 종가가 아니라서, "
                    f"장 마감 후 다시 계산되는 값/다른 앱과 다를 수 있습니다. "
                    f"(현재 거래소 기준 시각: {now_at_exchange.strftime('%Y-%m-%d %H:%M %Z')})"
                )
            else:
                log.info(
                    f"[{ticker}] 마지막 봉({last_bar_date.date()})은 정규장 마감 기준 확정값입니다."
                )
        log.info(f"[{ticker}] ===== 최종 RSI14 = {latest_rsi:.4f} =====")

    if pd.isna(latest_rsi):
        log.warning(f"[{ticker}] RSI 값이 유효하지 않습니다(NaN).")
        return None

    return float(latest_rsi)


def send_telegram(message: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수(GitHub Secrets)가 설정되지 않았습니다."
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=15,
        )
    except requests.RequestException as e:
        log.error(f"텔레그램 요청 중 오류: {e}")
        return False

    if resp.status_code != 200:
        log.error(f"텔레그램 발송 실패: {resp.status_code} {resp.text}")
        return False

    log.info("텔레그램 알림 발송 완료")
    return True


def main() -> None:
    tickers = load_tickers()
    if not tickers:
        log.warning("감시할 티커가 없습니다. config/tickers.json 을 확인하세요.")
        return

    state = load_state()
    alerted_today = set(state.get("alerted", []))

    log.info(f"감시 티커 {len(tickers)}개: {', '.join(tickers)}")

    newly_alerted = []
    for ticker in tickers:
        if ticker in alerted_today:
            log.info(f"[{ticker}] 오늘 이미 알림을 보냈습니다 -> 스킵")
            continue

        rsi = get_latest_rsi(ticker)
        if rsi is None:
            continue

        log.info(f"[{ticker}] RSI(14) = {rsi:.2f}")

        if rsi < RSI_THRESHOLD:
            message = (
                f"⚠️ <b>{ticker}</b> RSI(14) 과매도 구간 진입\n"
                f"RSI: {rsi:.2f} (기준: {RSI_THRESHOLD} 이하)\n"
                f"기준일: {state['date']} (KST)"
            )
            if send_telegram(message):
                newly_alerted.append(ticker)

    state["alerted"] = sorted(alerted_today | set(newly_alerted))
    save_state(state)

    if newly_alerted:
        log.info(f"신규 알림 발송 티커: {newly_alerted}")
    else:
        log.info("신규 알림 없음")


if __name__ == "__main__":
    main()

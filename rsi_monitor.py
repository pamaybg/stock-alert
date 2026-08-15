"""
RSI(14) 미국 주식 모니터링 스크립트
---------------------------------
- config/tickers.json 에 있는 티커들을 순회하며 두 가지 기준으로 RSI(14)를 계산합니다.
    1) 일봉(daily close) 기준 RSI(14)
    2) 주봉(weekly close) 기준 RSI(14)
- 둘 중 하나라도 RSI < RSI_THRESHOLD 이면 텔레그램으로 알림을 보냅니다.
- 같은 실행에서 일봉/주봉이 동시에 조건을 만족하면 알림 문구를 하나로 합쳐서 보냅니다.
- 같은 티커·같은 기준(일봉/주봉)은 하루(Asia/Seoul 기준 날짜)에 한 번만 알림을 보냅니다.
- 알림이 실제로 발송될 때마다 state/alert_history.jsonl 에 이력이 한 줄씩 누적됩니다(초기화되지 않음).
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
# 알림이 발송될 때마다 한 줄씩 쌓이는 이력 파일 (JSON Lines).
# last_alert.json과 달리 매일 초기화되지 않고 계속 누적됩니다.
HISTORY_PATH = BASE_DIR / "state" / "alert_history.jsonl"

RSI_PERIOD = 14
RSI_THRESHOLD = 60  # 일봉/주봉 공통 기준

# RSI(Wilder's smoothing)는 초기 구간이 불안정하므로 넉넉한 기간의 데이터를 받아옵니다.
DAILY_HISTORY_PERIOD = "6mo"    # 약 125개 일봉
WEEKLY_HISTORY_PERIOD = "2y"    # 약 104개 주봉

# true면 각 티커·각 기준(일봉/주봉)마다 최근 값들을 로그에 찍어서
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
        state = {"date": today, "alerted": {}}

    alerted = state.get("alerted", {})
    # 이전 버전(일봉 전용) 호환: alerted가 리스트 형태였다면 {"티커": ["daily"]} 형태로 변환
    if isinstance(alerted, list):
        alerted = {ticker: ["daily"] for ticker in alerted}
    state["alerted"] = alerted
    return state


def save_state(state: dict) -> None:
    state["last_checked_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def append_alert_history(entries: list[dict]) -> None:
    """실제 알림이 발송됐을 때만 호출됩니다. 파일을 매번 새로 쓰지 않고 뒤에 이어붙이기만 해서,
    깃 커밋 diff도 작고 이전 이력이 절대 사라지지 않습니다."""
    if not entries:
        return
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_PATH, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")
    log.info(f"알림 이력 {len(entries)}건을 {HISTORY_PATH.name} 에 기록했습니다.")


def compute_rsi(ticker: str, interval: str, period: str, label: str) -> Optional[float]:
    """
    interval: "1d"(일봉) 또는 "1wk"(주봉)
    label: 로그에 찍을 한글 라벨 ("일봉" / "주봉")
    """
    try:
        # auto_adjust=False: 배당/분할로 보정되지 않은 "원래 종가"를 사용합니다.
        # (대부분의 주식 앱/증권사 HTS가 RSI 계산에 쓰는 값과 기준을 맞추기 위함.
        #  yfinance 기본값(auto_adjust=True)을 쓰면 최근 배당이 있었던 종목은
        #  과거 종가가 소급 보정되어 RSI가 눈에 띄게 달라질 수 있습니다.)
        hist = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False)
    except Exception as e:
        log.warning(f"[{ticker}][{label}] 데이터 조회 실패: {e}")
        return None

    if hist is None or hist.empty or "Close" not in hist.columns:
        log.warning(f"[{ticker}][{label}] 유효한 가격 데이터가 없습니다. 티커명을 확인하세요.")
        return None

    closes = hist["Close"].dropna()
    if len(closes) < RSI_PERIOD + 1:
        log.warning(f"[{ticker}][{label}] RSI 계산에 데이터가 부족합니다 ({len(closes)}개).")
        return None

    close_values = closes.to_numpy(dtype="float64")
    rsi_series = talib.RSI(close_values, timeperiod=RSI_PERIOD)
    latest_rsi = rsi_series[-1]

    if DEBUG:
        log.info(f"[{ticker}][{label}] ===== 검산용 상세 로그 (최근 {DEBUG_TAIL_ROWS}개 {label}) =====")
        log.info(
            f"[{ticker}][{label}] 데이터소스=yfinance, 조회기간={period}, "
            f"총 {len(closes)}개 {label}, 원가(비조정) 종가 사용"
        )

        tail_dates = closes.index[-DEBUG_TAIL_ROWS:]
        tail_closes = close_values[-DEBUG_TAIL_ROWS:]
        tail_rsi = rsi_series[-DEBUG_TAIL_ROWS:]
        for d, c, r in zip(tail_dates, tail_closes, tail_rsi):
            r_str = f"{r:.4f}" if not pd.isna(r) else "N/A(초기구간)"
            log.info(f"[{ticker}][{label}]   {d.date()}  종가={c:.4f}  RSI14={r_str}")

        last_bar_date = closes.index[-1]
        exchange_tz = last_bar_date.tzinfo
        if exchange_tz is not None:
            now_at_exchange = datetime.now(exchange_tz)
            if interval == "1wk":
                is_forming = last_bar_date.date().isocalendar()[:2] == now_at_exchange.date().isocalendar()[:2]
                unclosed_note = "이번 주가 아직 끝나지 않아 확정되지 않은 봉"
                closed_note = "지난 주까지 마감 확정된 봉"
            else:
                is_forming = last_bar_date.date() == now_at_exchange.date()
                unclosed_note = "정규장 마감(미국 동부시간 16:00) 전이라 확정되지 않은 봉"
                closed_note = "정규장 마감 기준 확정된 봉"

            if is_forming:
                log.info(
                    f"[{ticker}][{label}] ⚠️ 마지막 {label}({last_bar_date.date()})은 {unclosed_note}입니다. "
                    f"다른 앱/사이트가 마감 확정값만 보여준다면 값이 다를 수 있습니다. "
                    f"(현재 거래소 기준 시각: {now_at_exchange.strftime('%Y-%m-%d %H:%M %Z')})"
                )
            else:
                log.info(f"[{ticker}][{label}] 마지막 {label}({last_bar_date.date()})은 {closed_note}입니다.")
        log.info(f"[{ticker}][{label}] ===== 최종 RSI14({label}) = {latest_rsi:.4f} =====")

    if pd.isna(latest_rsi):
        log.warning(f"[{ticker}][{label}] RSI 값이 유효하지 않습니다(NaN).")
        return None

    return float(latest_rsi)


def get_daily_rsi(ticker: str) -> Optional[float]:
    return compute_rsi(ticker, interval="1d", period=DAILY_HISTORY_PERIOD, label="일봉")


def get_weekly_rsi(ticker: str) -> Optional[float]:
    return compute_rsi(ticker, interval="1wk", period=WEEKLY_HISTORY_PERIOD, label="주봉")


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


def build_message(ticker: str, date_str: str, daily_rsi: Optional[float], weekly_rsi: Optional[float]) -> str:
    if daily_rsi is not None and weekly_rsi is not None:
        return (
            f"⚠️ <b>{ticker}</b> 일봉+주봉 RSI(14) 동시 과매도\n"
            f"일봉 RSI: {daily_rsi:.2f} / 주봉 RSI: {weekly_rsi:.2f} (기준: {RSI_THRESHOLD} 이하)\n"
            f"기준일: {date_str} (KST)"
        )
    if daily_rsi is not None:
        return (
            f"⚠️ <b>{ticker}</b> 일봉 RSI(14) 과매도 구간 진입\n"
            f"RSI: {daily_rsi:.2f} (기준: {RSI_THRESHOLD} 이하)\n"
            f"기준일: {date_str} (KST)"
        )
    return (
        f"⚠️ <b>{ticker}</b> 주봉 RSI(14) 과매도 구간 진입\n"
        f"RSI: {weekly_rsi:.2f} (기준: {RSI_THRESHOLD} 이하)\n"
        f"기준일: {date_str} (KST)"
    )


def main() -> None:
    tickers = load_tickers()
    if not tickers:
        log.warning("감시할 티커가 없습니다. config/tickers.json 을 확인하세요.")
        return

    state = load_state()
    alerted = state["alerted"]  # {"AAPL": ["daily", "weekly"], ...}

    log.info(f"감시 티커 {len(tickers)}개: {', '.join(tickers)}")

    any_new_alert = False
    history_entries: list[dict] = []

    for ticker in tickers:
        sent_types = set(alerted.get(ticker, []))
        need_daily = "daily" not in sent_types
        need_weekly = "weekly" not in sent_types

        if not need_daily and not need_weekly:
            log.info(f"[{ticker}] 오늘 일봉/주봉 모두 이미 알림을 보냈습니다 -> 스킵")
            continue

        daily_rsi = get_daily_rsi(ticker) if need_daily else None
        weekly_rsi = get_weekly_rsi(ticker) if need_weekly else None

        if not need_daily:
            log.info(f"[{ticker}] 일봉은 오늘 이미 알림을 보냈습니다 -> 스킵")
        elif daily_rsi is not None:
            log.info(f"[{ticker}] 일봉 RSI(14) = {daily_rsi:.2f}")

        if not need_weekly:
            log.info(f"[{ticker}] 주봉은 오늘 이미 알림을 보냈습니다 -> 스킵")
        elif weekly_rsi is not None:
            log.info(f"[{ticker}] 주봉 RSI(14) = {weekly_rsi:.2f}")

        daily_triggered = need_daily and daily_rsi is not None and daily_rsi < RSI_THRESHOLD
        weekly_triggered = need_weekly and weekly_rsi is not None and weekly_rsi < RSI_THRESHOLD

        if not daily_triggered and not weekly_triggered:
            continue

        message = build_message(
            ticker,
            state["date"],
            daily_rsi if daily_triggered else None,
            weekly_rsi if weekly_triggered else None,
        )

        if send_telegram(message):
            sent_types_updated = set(alerted.get(ticker, []))
            triggered_types = []
            if daily_triggered:
                sent_types_updated.add("daily")
                triggered_types.append("daily")
            if weekly_triggered:
                sent_types_updated.add("weekly")
                triggered_types.append("weekly")
            alerted[ticker] = sorted(sent_types_updated)
            any_new_alert = True

            now_utc = datetime.now(timezone.utc)
            history_entries.append(
                {
                    "sent_at_kst": now_utc.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S") if KST else None,
                    "sent_at_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S"),
                    "ticker": ticker,
                    "triggered": triggered_types,  # ["daily"], ["weekly"], 또는 ["daily", "weekly"]
                    "daily_rsi": round(daily_rsi, 4) if daily_triggered else None,
                    "weekly_rsi": round(weekly_rsi, 4) if weekly_triggered else None,
                    "threshold": RSI_THRESHOLD,
                    "message": message,
                }
            )

    state["alerted"] = alerted
    save_state(state)
    append_alert_history(history_entries)

    if any_new_alert:
        log.info(f"신규 알림 발송 결과: {json.dumps(alerted, ensure_ascii=False)}")
    else:
        log.info("신규 알림 없음")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Receive TradingView webhook alerts and forward them to Telegram.

This service is intentionally small and dependency-free so it can run with the
same Python setup already used by the Telegram project in this folder.
"""

from __future__ import annotations

import argparse
import csv
import math
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import io
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram_meme_call_bot import ensure_utf8_stdio, get_json, load_env_file, post_telegram, safe_float


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
DEFAULT_PATH = "/webhook/tradingview"
DEFAULT_XAU_STATE_FILE = Path(__file__).with_name("xau_signal_state.json")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def format_number(value: Any) -> str:
    if value in (None, ""):
        return "n/a"
    if isinstance(value, str):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(numeric) >= 1000:
        return f"{numeric:,.2f}"
    return f"{numeric:.6f}".rstrip("0").rstrip(".")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_api_timestamp(value: str) -> datetime:
    normalized = value.strip().replace(" ", "T")
    return datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)


def floor_time(value: datetime, minutes: int) -> datetime:
    minute = (value.minute // minutes) * minutes
    return value.replace(minute=minute, second=0, microsecond=0)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


@dataclass
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SignalConfig:
    timeframe_name: str
    interval_minutes: int
    higher_timeframe_minutes: int
    fast_ema: int
    slow_ema: int
    trend_ema: int
    rsi_period: int
    rsi_long_min: float
    rsi_long_max: float
    rsi_short_min: float
    rsi_short_max: float
    adx_period: int
    adx_min: float
    breakout_lookback: int
    atr_period: int
    stop_atr: float
    tp1_atr: float
    tp2_atr: float
    max_stretch_atr: float
    candle_body_min: float
    candle_close_zone: float
    max_candle_atr: float
    min_score: int
    session_start_utc: int
    session_end_utc: int


XAU_M5_CONFIG = SignalConfig(
    timeframe_name="M5",
    interval_minutes=5,
    higher_timeframe_minutes=15,
    fast_ema=18,
    slow_ema=50,
    trend_ema=200,
    rsi_period=14,
    rsi_long_min=52.5,
    rsi_long_max=64.5,
    rsi_short_min=35.5,
    rsi_short_max=47.5,
    adx_period=14,
    adx_min=18.0,
    breakout_lookback=4,
    atr_period=14,
    stop_atr=1.2,
    tp1_atr=1.7,
    tp2_atr=2.8,
    max_stretch_atr=0.9,
    candle_body_min=0.40,
    candle_close_zone=0.62,
    max_candle_atr=2.4,
    min_score=6,
    session_start_utc=6,
    session_end_utc=21,
)


XAU_M15_CONFIG = SignalConfig(
    timeframe_name="M15",
    interval_minutes=15,
    higher_timeframe_minutes=60,
    fast_ema=21,
    slow_ema=55,
    trend_ema=200,
    rsi_period=14,
    rsi_long_min=52.0,
    rsi_long_max=67.5,
    rsi_short_min=32.5,
    rsi_short_max=48.0,
    adx_period=14,
    adx_min=18.0,
    breakout_lookback=4,
    atr_period=14,
    stop_atr=1.6,
    tp1_atr=2.0,
    tp2_atr=3.4,
    max_stretch_atr=1.25,
    candle_body_min=0.45,
    candle_close_zone=0.68,
    max_candle_atr=2.8,
    min_score=6,
    session_start_utc=6,
    session_end_utc=21,
)


def ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    alpha = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    prev = seed
    for index in range(period, len(values)):
        prev = values[index] * alpha + prev * (1.0 - alpha)
        result[index] = prev
    return result


def rsi(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    rs = avg_gain / avg_loss if avg_loss > 0 else math.inf
    result[period] = 100.0 - (100.0 / (1.0 + rs))
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else math.inf
        result[index] = 100.0 - (100.0 / (1.0 + rs))
    return result


def true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    ranges = [
        true_range(highs[index], lows[index], closes[index - 1] if index > 0 else None)
        for index in range(len(closes))
    ]
    seed = sum(ranges[1 : period + 1]) / period
    result[period] = seed
    prev = seed
    for index in range(period + 1, len(closes)):
        prev = ((prev * (period - 1)) + ranges[index]) / period
        result[index] = prev
    return result


def macd(values: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> tuple[list[float | None], list[float | None], list[float | None]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    line: list[float | None] = [None] * len(values)
    macd_values: list[float] = []
    macd_indices: list[int] = []
    for index in range(len(values)):
        if ema_fast[index] is None or ema_slow[index] is None:
            continue
        current = ema_fast[index] - ema_slow[index]
        line[index] = current
        macd_values.append(current)
        macd_indices.append(index)
    signal_full: list[float | None] = [None] * len(values)
    hist_full: list[float | None] = [None] * len(values)
    signal_values = ema(macd_values, signal_period)
    for idx, candle_index in enumerate(macd_indices):
        signal_value = signal_values[idx]
        signal_full[candle_index] = signal_value
        if signal_value is not None and line[candle_index] is not None:
            hist_full[candle_index] = line[candle_index] - signal_value
    return line, signal_full, hist_full


def adx(highs: list[float], lows: list[float], closes: list[float], period: int) -> tuple[list[float | None], list[float | None], list[float | None]]:
    size = len(closes)
    plus_di: list[float | None] = [None] * size
    minus_di: list[float | None] = [None] * size
    adx_values: list[float | None] = [None] * size
    if size <= period * 2:
        return plus_di, minus_di, adx_values

    tr_list = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size

    for index in range(1, size):
        up_move = highs[index] - highs[index - 1]
        down_move = lows[index - 1] - lows[index]
        plus_dm[index] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[index] = down_move if down_move > up_move and down_move > 0 else 0.0
        tr_list[index] = true_range(highs[index], lows[index], closes[index - 1])

    tr14 = sum(tr_list[1 : period + 1])
    plus14 = sum(plus_dm[1 : period + 1])
    minus14 = sum(minus_dm[1 : period + 1])
    dx_values: list[float] = []

    for index in range(period, size):
        if index > period:
            tr14 = tr14 - (tr14 / period) + tr_list[index]
            plus14 = plus14 - (plus14 / period) + plus_dm[index]
            minus14 = minus14 - (minus14 / period) + minus_dm[index]

        if tr14 <= 0:
            continue
        plus = (plus14 / tr14) * 100.0
        minus = (minus14 / tr14) * 100.0
        plus_di[index] = plus
        minus_di[index] = minus
        denominator = plus + minus
        dx = 0.0 if denominator <= 0 else abs(plus - minus) / denominator * 100.0
        dx_values.append(dx)
        if len(dx_values) == period:
            adx_values[index] = sum(dx_values) / period
        elif len(dx_values) > period and adx_values[index - 1] is not None:
            adx_values[index] = ((adx_values[index - 1] * (period - 1)) + dx) / period

    return plus_di, minus_di, adx_values


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"signals": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"signals": {}}
    if not isinstance(raw, dict):
        return {"signals": {}}
    raw.setdefault("signals", {})
    return raw


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_yahoo_timestamp(value: int | float) -> datetime:
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def fetch_twelvedata_candles(api_key: str, symbol: str, interval: str, outputsize: int = 260) -> list[Candle]:
    query_symbol = urllib.parse.quote(symbol, safe="/")
    url = (
        "https://api.twelvedata.com/time_series"
        f"?symbol={query_symbol}&interval={interval}&outputsize={outputsize}&timezone=UTC&apikey={api_key}"
    )
    data = get_json(url)
    if isinstance(data, dict) and data.get("status") == "error":
        raise RuntimeError(f"Twelve Data error: {data.get('message', 'unknown_error')}")
    values = data.get("values") if isinstance(data, dict) else None
    if not isinstance(values, list) or not values:
        raise RuntimeError("Twelve Data returned no candle data.")

    candles: list[Candle] = []
    for row in reversed(values):
        ts = parse_api_timestamp(str(row["datetime"]))
        open_price = safe_float(row.get("open"))
        high_price = safe_float(row.get("high"))
        low_price = safe_float(row.get("low"))
        close_price = safe_float(row.get("close"))
        if None in (open_price, high_price, low_price, close_price):
            continue
        candles.append(
            Candle(
                ts=ts,
                open=float(open_price),
                high=float(high_price),
                low=float(low_price),
                close=float(close_price),
            )
        )
    return candles


def drop_incomplete_candle(candles: list[Candle], interval_minutes: int) -> list[Candle]:
    if not candles:
        return candles
    latest = candles[-1]
    if latest.ts + timedelta(minutes=interval_minutes) > utc_now() - timedelta(seconds=10):
        return candles[:-1]
    return candles


def aggregate_candles(candles: list[Candle], interval_minutes: int) -> list[Candle]:
    aggregated: list[Candle] = []
    current_bucket: datetime | None = None
    bucket_open = bucket_high = bucket_low = bucket_close = None

    for candle in candles:
        bucket_ts = floor_time(candle.ts, interval_minutes)
        if current_bucket != bucket_ts:
            if current_bucket is not None and None not in (bucket_open, bucket_high, bucket_low, bucket_close):
                aggregated.append(
                    Candle(
                        ts=current_bucket,
                        open=float(bucket_open),
                        high=float(bucket_high),
                        low=float(bucket_low),
                        close=float(bucket_close),
                    )
                )
            current_bucket = bucket_ts
            bucket_open = candle.open
            bucket_high = candle.high
            bucket_low = candle.low
            bucket_close = candle.close
        else:
            bucket_high = max(float(bucket_high), candle.high)
            bucket_low = min(float(bucket_low), candle.low)
            bucket_close = candle.close

    if current_bucket is not None and None not in (bucket_open, bucket_high, bucket_low, bucket_close):
        aggregated.append(
            Candle(
                ts=current_bucket,
                open=float(bucket_open),
                high=float(bucket_high),
                low=float(bucket_low),
                close=float(bucket_close),
            )
        )
    return aggregated


def in_session(ts: datetime, start_hour: int, end_hour: int) -> bool:
    if ts.weekday() >= 5:
        return False
    return start_hour <= ts.hour < end_hour


def build_reason_lines(side: str, trend_ok: bool, htf_ok: bool, adx_ok: bool, macd_ok: bool, rsi_ok: bool, breakout_ok: bool) -> str:
    labels: list[str] = []
    if trend_ok:
        labels.append("EMA trend aligned")
    if htf_ok:
        labels.append("higher timeframe agrees")
    if adx_ok:
        labels.append("ADX confirms strength")
    if macd_ok:
        labels.append("MACD momentum supports move")
    if rsi_ok:
        labels.append("RSI in sweet spot")
    if breakout_ok:
        labels.append("recent breakout close")
    if not labels:
        labels.append(f"{side} setup passed system filters")
    return ", ".join(labels)


def evaluate_signal(candles: list[Candle], htf_candles: list[Candle], config: SignalConfig) -> dict[str, Any] | None:
    min_bars = max(config.trend_ema, config.slow_ema, config.atr_period, config.adx_period + 10, config.rsi_period + 5) + 5
    if len(candles) < min_bars or len(htf_candles) < config.trend_ema // 2:
        return None

    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]

    htf_closes = [c.close for c in htf_candles]
    htf_fast = ema(htf_closes, max(8, config.fast_ema))
    htf_slow = ema(htf_closes, config.slow_ema)
    htf_trend = ema(htf_closes, config.trend_ema)

    fast = ema(closes, config.fast_ema)
    slow = ema(closes, config.slow_ema)
    trend = ema(closes, config.trend_ema)
    rsi_values = rsi(closes, config.rsi_period)
    macd_line, macd_signal, macd_hist = macd(closes)
    atr_values = atr(highs, lows, closes, config.atr_period)
    plus_di, minus_di, adx_values = adx(highs, lows, closes, config.adx_period)

    index = len(candles) - 1
    latest = candles[index]
    htf_latest = htf_candles[-1]

    required = [
        fast[index],
        slow[index],
        trend[index],
        rsi_values[index],
        macd_line[index],
        macd_signal[index],
        macd_hist[index],
        atr_values[index],
        plus_di[index],
        minus_di[index],
        adx_values[index],
        htf_fast[-1],
        htf_slow[-1],
        htf_trend[-1],
    ]
    if any(value is None for value in required):
        return None

    atr_now = float(atr_values[index])
    candle_range = max(latest.high - latest.low, 1e-9)
    body_ratio = abs(latest.close - latest.open) / candle_range
    bull_close_ratio = (latest.close - latest.low) / candle_range
    bear_close_ratio = (latest.high - latest.close) / candle_range
    price_stretch_atr = abs(latest.close - float(fast[index])) / atr_now if atr_now > 0 else 999.0
    range_atr = candle_range / atr_now if atr_now > 0 else 999.0

    trend_up = latest.close > float(fast[index]) > float(slow[index]) > float(trend[index])
    trend_down = latest.close < float(fast[index]) < float(slow[index]) < float(trend[index])
    htf_trend_up = htf_latest.close > float(htf_fast[-1]) > float(htf_slow[-1]) > float(htf_trend[-1])
    htf_trend_down = htf_latest.close < float(htf_fast[-1]) < float(htf_slow[-1]) < float(htf_trend[-1])
    adx_bull = float(adx_values[index]) >= config.adx_min and float(plus_di[index]) > float(minus_di[index])
    adx_bear = float(adx_values[index]) >= config.adx_min and float(minus_di[index]) > float(plus_di[index])
    macd_bull = float(macd_line[index]) > float(macd_signal[index]) and float(macd_hist[index]) > 0
    macd_bear = float(macd_line[index]) < float(macd_signal[index]) and float(macd_hist[index]) < 0
    rsi_bull = config.rsi_long_min <= float(rsi_values[index]) <= config.rsi_long_max
    rsi_bear = config.rsi_short_min <= float(rsi_values[index]) <= config.rsi_short_max
    candle_bull = latest.close > latest.open and body_ratio >= config.candle_body_min and bull_close_ratio >= config.candle_close_zone
    candle_bear = latest.close < latest.open and body_ratio >= config.candle_body_min and bear_close_ratio >= config.candle_close_zone
    stretch_ok = price_stretch_atr <= config.max_stretch_atr
    session_ok = in_session(latest.ts, config.session_start_utc, config.session_end_utc)
    range_ok = range_atr <= config.max_candle_atr

    recent_high = max(highs[index - config.breakout_lookback : index])
    recent_low = min(lows[index - config.breakout_lookback : index])
    breakout_bull = latest.close > recent_high
    breakout_bear = latest.close < recent_low

    long_score = sum([trend_up, htf_trend_up, adx_bull, macd_bull, rsi_bull, candle_bull, stretch_ok, session_ok])
    short_score = sum([trend_down, htf_trend_down, adx_bear, macd_bear, rsi_bear, candle_bear, stretch_ok, session_ok])

    if range_ok and breakout_bull and long_score >= config.min_score:
        return {
            "timeframe": config.timeframe_name,
            "side": "BUY",
            "timestamp": latest.ts,
            "price": latest.close,
            "stop": latest.close - atr_now * config.stop_atr,
            "tp1": latest.close + atr_now * config.tp1_atr,
            "tp2": latest.close + atr_now * config.tp2_atr,
            "score": long_score,
            "confidence": long_score * 100.0 / 8.0,
            "atr": atr_now,
            "reason": build_reason_lines("BUY", trend_up, htf_trend_up, adx_bull, macd_bull, rsi_bull, breakout_bull),
        }

    if range_ok and breakout_bear and short_score >= config.min_score:
        return {
            "timeframe": config.timeframe_name,
            "side": "SELL",
            "timestamp": latest.ts,
            "price": latest.close,
            "stop": latest.close + atr_now * config.stop_atr,
            "tp1": latest.close - atr_now * config.tp1_atr,
            "tp2": latest.close - atr_now * config.tp2_atr,
            "score": short_score,
            "confidence": short_score * 100.0 / 8.0,
            "atr": atr_now,
            "reason": build_reason_lines("SELL", trend_down, htf_trend_down, adx_bear, macd_bear, rsi_bear, breakout_bear),
        }

    return None


def build_xau_message(signal: dict[str, Any]) -> str:
    timestamp = signal["timestamp"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    title = str(signal.get("title") or "XAUUSD Focus Signal")
    instrument = signal.get("instrument")
    return "\n".join(
        [
            title,
            f"Instrument: {instrument}" if instrument else "Instrument: XAU/USD",
            f"Timeframe: {signal['timeframe']}",
            f"Side: {signal['side']}",
            f"Time: {timestamp}",
            "",
            f"Entry: {format_number(signal['price'])}",
            f"Stop: {format_number(signal['stop'])}",
            f"TP1: {format_number(signal['tp1'])}",
            f"TP2: {format_number(signal['tp2'])}",
            "",
            f"Score: {signal['score']}/8 | Confidence: {format_number(signal['confidence'])}%",
            f"ATR: {format_number(signal['atr'])}",
            f"Reason: {signal['reason']}",
        ]
    )


class XAUSignalBot:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = env_bool("XAU_SIGNAL_BOT_ENABLED", False)
        self.provider = env_str("XAU_DATA_PROVIDER", "yahoo").strip().lower()
        self.api_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
        self.stooq_api_key = os.environ.get("STOOQ_API_KEY", "").strip()
        self.symbol = env_str("XAU_SIGNAL_SYMBOL", "XAU/USD")
        self.stooq_symbol = env_str("STOOQ_SYMBOL", "xauusd")
        self.yahoo_symbol = env_str("YAHOO_GOLD_SYMBOL", "GC=F")
        self.state_file = Path(os.environ.get("XAU_SIGNAL_STATE_FILE", DEFAULT_XAU_STATE_FILE))
        self.poll_offset_seconds = env_int("XAU_SIGNAL_OFFSET_SECONDS", 20)
        self.backfill_hours = env_int("XAU_SIGNAL_BACKFILL_HOURS", 6)
        self.max_send_per_run = env_int("XAU_SIGNAL_MAX_SEND_PER_RUN", 6)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.last_run_at: str | None = None
        self.state = load_state(self.state_file)

    def _provider_ready(self) -> bool:
        if self.provider == "twelvedata":
            return bool(self.api_key)
        if self.provider == "stooq":
            return bool(self.stooq_api_key)
        if self.provider == "yahoo":
            return bool(self.yahoo_symbol)
        return False

    def _display_symbol(self) -> str:
        if self.provider == "yahoo":
            return f"{self.yahoo_symbol} (gold futures proxy)"
        if self.provider == "stooq":
            return self.stooq_symbol
        return self.symbol

    def _prepare_signal(self, signal: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(signal)
        prepared["instrument"] = self._display_symbol()
        prepared["title"] = "Gold Proxy Focus Signal" if self.provider == "yahoo" else "XAUUSD Focus Signal"
        return prepared

    def start(self) -> None:
        if not self.enabled:
            return
        if self.provider == "twelvedata" and not self.api_key:
            print("XAU signal bot is enabled but TWELVEDATA_API_KEY is missing.")
            return
        if self.provider == "stooq" and not self.stooq_api_key:
            print("XAU signal bot is enabled but STOOQ_API_KEY is missing.")
            return
        if self.provider == "yahoo" and not self.yahoo_symbol:
            print("XAU signal bot is enabled but YAHOO_GOLD_SYMBOL is missing.")
            return
        if self.provider not in {"twelvedata", "stooq", "yahoo"}:
            print(f"XAU signal bot provider '{self.provider}' is not supported.")
            return
        self.thread = threading.Thread(target=self._run_loop, name="xau-signal-bot", daemon=True)
        self.thread.start()
        print("XAU signal bot: started")

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled and self._provider_ready(),
            "provider": self.provider,
            "symbol": self._display_symbol(),
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
        }

    def _run_loop(self) -> None:
        # Run once shortly after startup, then every five minutes shortly after bar close.
        while not self.stop_event.is_set():
            try:
                self.run_once()
                self.last_error = None
            except Exception as exc:  # pragma: no cover - long-running guard.
                self.last_error = str(exc)
                print(f"XAU signal bot error: {exc}", file=sys.stderr)
            wait_seconds = self._seconds_until_next_run()
            self.stop_event.wait(wait_seconds)

    def _seconds_until_next_run(self) -> float:
        now = utc_now()
        next_boundary = floor_time(now, 5) + timedelta(minutes=5, seconds=self.poll_offset_seconds)
        if next_boundary <= now:
            next_boundary += timedelta(minutes=5)
        return max(5.0, (next_boundary - now).total_seconds())

    def _already_sent(self, timeframe: str, ts: datetime, side: str) -> bool:
        signal_key = f"{timeframe}:{iso_utc(ts)}:{side}"
        return signal_key in (self.state.get("signals") or {})

    def _mark_sent(self, timeframe: str, ts: datetime, side: str) -> None:
        signal_key = f"{timeframe}:{iso_utc(ts)}:{side}"
        self.state.setdefault("signals", {})[signal_key] = {"sent_at": iso_utc(utc_now())}
        # Keep state file compact.
        items = sorted((self.state.get("signals") or {}).items(), key=lambda item: item[0])
        self.state["signals"] = dict(items[-200:])
        save_state(self.state_file, self.state)

    def _fetch_candle_sets(self) -> tuple[list[Candle], list[Candle], list[Candle], list[Candle]]:
        if self.provider == "stooq":
            candles_5m = fetch_stooq_candles(self.stooq_api_key, self.stooq_symbol, "5")
        elif self.provider == "yahoo":
            candles_5m = fetch_yahoo_candles(self.yahoo_symbol, interval="5m", range_name="5d")
            candles_15m = fetch_yahoo_candles(self.yahoo_symbol, interval="15m", range_name="1mo")
            candles_60m = fetch_yahoo_candles(self.yahoo_symbol, interval="60m", range_name="1mo")
        else:
            candles_5m = fetch_twelvedata_candles(self.api_key, self.symbol, "5min", outputsize=320)
        candles_5m = drop_incomplete_candle(candles_5m, 5)
        if len(candles_5m) < 240:
            raise RuntimeError(f"Not enough XAU/USD 5-minute candles returned from {self.provider}.")

        candles_15m_for_m5 = aggregate_candles(candles_5m, 15)
        if self.provider == "yahoo":
            candles_15m = drop_incomplete_candle(candles_15m, 15)
            candles_60m = drop_incomplete_candle(candles_60m, 60)
        else:
            candles_15m = candles_15m_for_m5
            candles_60m = aggregate_candles(candles_5m, 60)

        return candles_5m, candles_15m_for_m5, candles_15m, candles_60m

    def _collect_recent_candidates(
        self,
        candles: list[Candle],
        htf_candles: list[Candle],
        config: SignalConfig,
    ) -> list[dict[str, Any]]:
        if not candles or not htf_candles:
            return []
        lookback_bars = max(1, int(math.ceil((self.backfill_hours * 60) / config.interval_minutes)))
        start_index = max(1, len(candles) - lookback_bars + 1)
        seen_keys: set[str] = set()
        candidates: list[dict[str, Any]] = []
        for end_index in range(start_index, len(candles) + 1):
            latest_ts = candles[end_index - 1].ts
            htf_prefix = [candle for candle in htf_candles if candle.ts <= latest_ts]
            signal = evaluate_signal(candles[:end_index], htf_prefix, config)
            if not signal:
                continue
            signal_key = f"{signal['timeframe']}:{iso_utc(signal['timestamp'])}:{signal['side']}"
            if signal_key in seen_keys:
                continue
            seen_keys.add(signal_key)
            candidates.append(signal)
        return candidates

    def run_once(self) -> None:
        candles_5m, candles_15m_for_m5, candles_15m, candles_60m = self._fetch_candle_sets()
        self.last_run_at = iso_utc(utc_now())

        signals = self._collect_recent_candidates(candles_15m, candles_60m, XAU_M15_CONFIG)
        signals.extend(self._collect_recent_candidates(candles_5m, candles_15m_for_m5, XAU_M5_CONFIG))
        signals.sort(key=lambda signal: (signal["timestamp"], 0 if signal["timeframe"] == "M15" else 1))

        pending_signals = [
            signal
            for signal in signals
            if not self._already_sent(signal["timeframe"], signal["timestamp"], signal["side"])
        ]
        if len(pending_signals) > self.max_send_per_run:
            pending_signals = pending_signals[-self.max_send_per_run :]

        sent_count = 0
        for signal in pending_signals:
            prepared_signal = self._prepare_signal(signal)
            post_telegram(self.bot_token, self.chat_id, build_xau_message(prepared_signal))
            self._mark_sent(signal["timeframe"], signal["timestamp"], signal["side"])
            sent_count += 1
            print(f"Sent XAU {signal['timeframe']} {signal['side']} signal at {iso_utc(signal['timestamp'])}")
            if sent_count >= self.max_send_per_run:
                break


def parse_stooq_timestamp(date_value: str, time_value: str | None) -> datetime:
    date_value = date_value.strip()
    if not time_value:
        return datetime.fromisoformat(date_value).replace(tzinfo=timezone.utc)
    normalized = f"{date_value} {time_value.strip()}"
    for fmt in ("%Y-%m-%d %H:%M", "%Y%m%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(normalized, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported Stooq datetime format: {normalized}")


def fetch_stooq_candles(api_key: str, symbol: str, interval: str) -> list[Candle]:
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(symbol)}&i={interval}&apikey={urllib.parse.quote(api_key)}"
    request = urllib.request.Request(url, headers={"User-Agent": "codex-xau-bot/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode("utf-8", errors="replace")
    if text.strip().startswith("Get your apikey"):
        raise RuntimeError("Stooq API key is missing or invalid. Revisit the get_apikey page and copy the new key.")
    reader = csv.DictReader(io.StringIO(text))
    candles: list[Candle] = []
    for row in reader:
        if not row:
            continue
        date_value = row.get("Date") or row.get("date")
        if not date_value:
            continue
        time_value = row.get("Time") or row.get("time")
        open_price = safe_float(row.get("Open") or row.get("open"))
        high_price = safe_float(row.get("High") or row.get("high"))
        low_price = safe_float(row.get("Low") or row.get("low"))
        close_price = safe_float(row.get("Close") or row.get("close"))
        if None in (open_price, high_price, low_price, close_price):
            continue
        candles.append(
            Candle(
                ts=parse_stooq_timestamp(str(date_value), str(time_value) if time_value else None),
                open=float(open_price),
                high=float(high_price),
                low=float(low_price),
                close=float(close_price),
            )
        )
    if not candles:
        raise RuntimeError("Stooq returned no candles.")
    candles.sort(key=lambda candle: candle.ts)
    return candles


def fetch_yahoo_candles(symbol: str, interval: str = "5m", range_name: str = "5d") -> list[Candle]:
    encoded_symbol = urllib.parse.quote(symbol, safe="")
    urls = [
        (
            f"https://query2.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
            f"?interval={urllib.parse.quote(interval)}&range={urllib.parse.quote(range_name)}&includePrePost=true"
        ),
        (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded_symbol}"
            f"?interval={urllib.parse.quote(interval)}&range={urllib.parse.quote(range_name)}&includePrePost=true"
        ),
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/135.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://finance.yahoo.com/quote/{encoded_symbol}",
        "Origin": "https://finance.yahoo.com",
    }
    data: dict[str, Any] | None = None
    last_error: str | None = None
    for url in urls:
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read().decode("utf-8")
            data = json.loads(payload)
            break
        except Exception as exc:
            last_error = str(exc)
            continue
    if data is None:
        raise RuntimeError(f"Yahoo Finance request failed: {last_error or 'unknown_error'}")
    if not isinstance(data, dict):
        raise RuntimeError("Yahoo Finance returned an unexpected response.")
    chart = data.get("chart")
    if not isinstance(chart, dict):
        raise RuntimeError("Yahoo Finance response missing chart data.")
    error = chart.get("error")
    if error:
        message = error.get("description") if isinstance(error, dict) else str(error)
        raise RuntimeError(f"Yahoo Finance error: {message}")
    result = chart.get("result")
    if not isinstance(result, list) or not result:
        raise RuntimeError("Yahoo Finance returned no chart result.")
    series = result[0]
    timestamps = series.get("timestamp")
    indicators = series.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        raise RuntimeError("Yahoo Finance response missing timestamps or indicators.")
    quote_list = indicators.get("quote")
    if not isinstance(quote_list, list) or not quote_list:
        raise RuntimeError("Yahoo Finance response missing OHLC quote data.")
    quote = quote_list[0]
    opens = quote.get("open") if isinstance(quote, dict) else None
    highs = quote.get("high") if isinstance(quote, dict) else None
    lows = quote.get("low") if isinstance(quote, dict) else None
    closes = quote.get("close") if isinstance(quote, dict) else None
    if not all(isinstance(values, list) for values in [opens, highs, lows, closes]):
        raise RuntimeError("Yahoo Finance response missing OHLC arrays.")

    candles: list[Candle] = []
    for index, ts_value in enumerate(timestamps):
        open_price = safe_float(opens[index]) if index < len(opens) else None
        high_price = safe_float(highs[index]) if index < len(highs) else None
        low_price = safe_float(lows[index]) if index < len(lows) else None
        close_price = safe_float(closes[index]) if index < len(closes) else None
        if None in (open_price, high_price, low_price, close_price):
            continue
        candles.append(
            Candle(
                ts=parse_yahoo_timestamp(ts_value),
                open=float(open_price),
                high=float(high_price),
                low=float(low_price),
                close=float(close_price),
            )
        )
    if not candles:
        raise RuntimeError("Yahoo Finance returned no complete candles.")
    candles.sort(key=lambda candle: candle.ts)
    return candles


def build_message(payload: Any) -> str:
    if isinstance(payload, str):
        return payload.strip() or "TradingView alert"

    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)

    raw_message = payload.get("message")
    if isinstance(raw_message, str) and len(payload) == 1:
        return raw_message.strip() or "TradingView alert"

    ticker = str(payload.get("ticker") or payload.get("symbol") or "UNKNOWN")
    interval = str(payload.get("interval") or payload.get("timeframe") or "")
    side = str(payload.get("side") or payload.get("action") or "SIGNAL").upper()
    script_name = str(payload.get("script") or "TradingView")
    price = format_number(payload.get("price"))
    stop = format_number(payload.get("stop"))
    tp1 = format_number(payload.get("tp1"))
    tp2 = format_number(payload.get("tp2"))
    score = format_number(payload.get("score"))
    confidence = format_number(payload.get("confidence"))
    volume_ratio = format_number(payload.get("volume_ratio"))
    atr = format_number(payload.get("atr"))

    lines = [
        "TradingView Alert",
        f"{ticker} {interval}".strip(),
        f"Side: {side}",
        f"Setup: {script_name}",
        f"Entry: {price}",
        f"Stop: {stop}",
        f"TP1: {tp1}",
        f"TP2: {tp2}",
        f"Score: {score} | Confidence: {confidence}%",
        f"ATR: {atr} | Volume ratio: {volume_ratio}x",
    ]
    return "\n".join(lines)


class WebhookHandler(BaseHTTPRequestHandler):
    server: "WebhookServer"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> str:
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        return body.decode("utf-8", errors="replace")

    def _parse_payload(self, body_text: str) -> Any:
        stripped = body_text.lstrip()
        if stripped.startswith("{") or stripped.startswith("["):
            return json.loads(body_text)
        return body_text

    def _authorized(self, payload: Any) -> bool:
        if not self.server.webhook_secret:
            return True
        if isinstance(payload, dict) and payload.get("passphrase") == self.server.webhook_secret:
            return True
        if self.headers.get("X-Webhook-Secret") == self.server.webhook_secret:
            return True
        return False

    def do_GET(self) -> None:
        if self.path == self.server.health_path:
            self._write_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "tradingview-telegram-webhook",
                    "path": self.server.webhook_path,
                    "dry_run": self.server.dry_run,
                    "xau_signal_bot": self.server.xau_bot.status(),
                },
            )
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != self.server.webhook_path:
            self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        try:
            body_text = self._read_body()
            payload = self._parse_payload(body_text)
        except json.JSONDecodeError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"invalid_json: {exc}"})
            return

        if not self._authorized(payload):
            self._write_json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return

        message = build_message(payload)

        try:
            if self.server.dry_run:
                print("\n--- TradingView webhook dry-run ---")
                print(message)
            else:
                post_telegram(self.server.bot_token, self.server.chat_id, message)
        except Exception as exc:  # pragma: no cover - best effort runtime guard.
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})
            return

        self._write_json(
            HTTPStatus.OK,
            {"ok": True, "dry_run": self.server.dry_run, "forwarded_to": self.server.chat_id},
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stdout.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))


class WebhookServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        *,
        bot_token: str,
        chat_id: str,
        webhook_path: str,
        health_path: str,
        webhook_secret: str,
        dry_run: bool,
        xau_bot: XAUSignalBot,
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.webhook_path = webhook_path
        self.health_path = health_path
        self.webhook_secret = webhook_secret
        self.dry_run = dry_run
        self.xau_bot = xau_bot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Forward TradingView webhook alerts to Telegram.")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--host", default=env_str("TRADINGVIEW_WEBHOOK_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=env_int("PORT", env_int("TRADINGVIEW_WEBHOOK_PORT", DEFAULT_PORT)))
    parser.add_argument("--webhook-path", default=env_str("TRADINGVIEW_WEBHOOK_PATH", DEFAULT_PATH))
    parser.add_argument("--health-path", default="/healthz")
    parser.add_argument("--dry-run", action="store_true", help="Print formatted alerts instead of sending to Telegram.")
    parser.add_argument("--secret", default=os.environ.get("TRADINGVIEW_WEBHOOK_SECRET", ""))
    return parser


def main() -> int:
    ensure_utf8_stdio()
    env_arg = "--env-file"
    if env_arg in sys.argv:
        env_index = sys.argv.index(env_arg)
        if env_index + 1 < len(sys.argv):
            load_env_file(Path(sys.argv[env_index + 1]))
    else:
        load_env_file(Path(__file__).with_name(".env"))

    parser = build_parser()
    args = parser.parse_args()
    load_env_file(args.env_file)

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not args.dry_run and (not bot_token or not chat_id):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env before starting the webhook.")

    xau_bot = XAUSignalBot(bot_token=bot_token, chat_id=chat_id or "dry-run")
    xau_bot.start()

    server = WebhookServer(
        (args.host, args.port),
        WebhookHandler,
        bot_token=bot_token,
        chat_id=chat_id or "dry-run",
        webhook_path=args.webhook_path,
        health_path=args.health_path,
        webhook_secret=args.secret,
        dry_run=args.dry_run,
        xau_bot=xau_bot,
    )

    mode = "DRY-RUN" if args.dry_run else "LIVE"
    print(f"TradingView Telegram webhook listening on http://{args.host}:{args.port}{args.webhook_path} [{mode}]")
    print(f"Health endpoint: http://{args.host}:{args.port}{args.health_path}")
    if args.secret:
        print("Webhook secret: enabled")
    else:
        print("Webhook secret: disabled")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping webhook server...")
    finally:
        xau_bot.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

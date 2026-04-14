#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scan Solana meme-token feeds and publish conditional call alerts to Telegram.

The bot is intentionally conservative:
- It posts watchlist-style calls with TP/SL and expiry, not guaranteed outcomes.
- It defaults to dry-run mode so public posts require an explicit --send flag.
- It keeps a cooldown file to avoid repeating the same token too often.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEX_API = "https://api.dexscreener.com"
DEFAULT_STATE_FILE = Path(__file__).with_name("sent_calls.json")
USER_AGENT = "telegram-meme-call-bot/1.0"


@dataclass
class Candidate:
    score: int
    symbol: str
    name: str
    token: str
    dex: str
    price: float
    m5: float | None
    h1: float | None
    h6: float | None
    h24: float | None
    buys_1h: int
    sells_1h: int
    buy_sell_ratio: float
    vol_1h: float
    vol_24h: float
    liquidity: float
    market_cap: float | None
    pair_created_at: str
    dexscreener_url: str

    @property
    def tp10(self) -> float:
        return self.price * 1.10

    @property
    def tp15(self) -> float:
        return self.price * 1.15

    @property
    def sl6(self) -> float:
        return self.price * 0.94

    @property
    def gmgn_url(self) -> str:
        return f"https://gmgn.ai/sol/token/{self.token}"


def ensure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_json(url: str, timeout: int = 25) -> Any:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {body[:300]}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error from {url}: {exc.reason}") from exc


def post_telegram(bot_token: str, chat_id: str, text: str) -> None:
    payload = urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        },
    )
    with urlopen(request, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram sendMessage failed: {result}")


def rows_from_response(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("value"), list):
        return data["value"]
    if isinstance(data, list):
        return data
    return []


def unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        clean = item.strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


def discover_tokens(watchlist: list[str], max_tokens: int) -> list[str]:
    endpoints = [
        f"{DEX_API}/token-boosts/latest/v1",
        f"{DEX_API}/token-boosts/top/v1",
        f"{DEX_API}/token-profiles/latest/v1",
    ]
    tokens: list[str] = []
    for endpoint in endpoints:
        for row in rows_from_response(get_json(endpoint)):
            if row.get("chainId") == "solana" and row.get("tokenAddress"):
                tokens.append(str(row["tokenAddress"]))
    tokens.extend(watchlist)
    return unique(tokens)[:max_tokens]


def fetch_pairs(tokens: list[str]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for chunk in chunks(tokens, 30):
        csv = ",".join(chunk)
        pairs.extend(rows_from_response(get_json(f"{DEX_API}/tokens/v1/solana/{csv}")))
    return pairs


def best_pair_by_token(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        base = pair.get("baseToken") or {}
        token = base.get("address")
        if not token:
            continue
        liquidity = safe_float((pair.get("liquidity") or {}).get("usd"), 0.0) or 0.0
        current = best.get(token)
        current_liq = safe_float((current or {}).get("liquidity", {}).get("usd"), 0.0) if current else -1.0
        if current is None or liquidity > (current_liq or 0.0):
            best[token] = pair
    return list(best.values())


def score_pair(pair: dict[str, Any]) -> Candidate | None:
    base = pair.get("baseToken") or {}
    txns = pair.get("txns") or {}
    volume = pair.get("volume") or {}
    changes = pair.get("priceChange") or {}
    liquidity_data = pair.get("liquidity") or {}

    price = safe_float(pair.get("priceUsd"))
    liquidity = safe_float(liquidity_data.get("usd"), 0.0) or 0.0
    vol_24h = safe_float(volume.get("h24"), 0.0) or 0.0
    vol_1h = safe_float(volume.get("h1"), 0.0) or 0.0
    m5 = safe_float(changes.get("m5"))
    h1 = safe_float(changes.get("h1"))
    h6 = safe_float(changes.get("h6"))
    h24 = safe_float(changes.get("h24"))
    buys_1h = safe_int((txns.get("h1") or {}).get("buys"))
    sells_1h = safe_int((txns.get("h1") or {}).get("sells"))
    buy_sell_ratio = buys_1h / sells_1h if sells_1h > 0 else (9.99 if buys_1h > 0 else 0.0)

    if not price or price <= 0:
        return None

    score = 0
    if liquidity >= 150_000:
        score += 25
    elif liquidity >= 75_000:
        score += 22
    elif liquidity >= 40_000:
        score += 18
    elif liquidity >= 20_000:
        score += 12
    elif liquidity >= 12_000:
        score += 8
    else:
        score -= 15

    if vol_24h >= 2_000_000:
        score += 18
    elif vol_24h >= 750_000:
        score += 15
    elif vol_24h >= 250_000:
        score += 12
    elif vol_24h >= 75_000:
        score += 8
    elif vol_24h >= 25_000:
        score += 4
    else:
        score -= 6

    if vol_1h >= 75_000:
        score += 13
    elif vol_1h >= 25_000:
        score += 11
    elif vol_1h >= 7_500:
        score += 8
    elif vol_1h >= 2_000:
        score += 4
    else:
        score -= 5

    if h1 is not None:
        if 2 <= h1 <= 18:
            score += 18
        elif 0 <= h1 < 2:
            score += 8
        elif 18 < h1 <= 35:
            score += 5
        elif h1 > 35:
            score -= 8
        elif h1 < -5:
            score -= 14

    if m5 is not None:
        if -3 <= m5 <= 5:
            score += 12
        elif 5 < m5 <= 10:
            score += 3
        elif m5 < -7:
            score -= 12
        elif m5 > 14:
            score -= 8

    if buy_sell_ratio >= 1.25:
        score += 10
    elif buy_sell_ratio >= 0.95:
        score += 4
    else:
        score -= 8

    one_hour_tx = buys_1h + sells_1h
    if one_hour_tx >= 500:
        score += 12
    elif one_hour_tx >= 150:
        score += 10
    elif one_hour_tx >= 50:
        score += 6
    elif one_hour_tx < 15:
        score -= 8

    if h6 is not None:
        if h6 < -60:
            score -= 18
        elif h6 < -35:
            score -= 9

    created_at = "unknown"
    pair_created = safe_int(pair.get("pairCreatedAt"), 0)
    if pair_created:
        created_at = datetime.fromtimestamp(pair_created / 1000, tz=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M %Z"
        )

    return Candidate(
        score=score,
        symbol=str(base.get("symbol") or "UNKNOWN"),
        name=str(base.get("name") or ""),
        token=str(base.get("address") or ""),
        dex=str(pair.get("dexId") or "unknown"),
        price=price,
        m5=m5,
        h1=h1,
        h6=h6,
        h24=h24,
        buys_1h=buys_1h,
        sells_1h=sells_1h,
        buy_sell_ratio=round(buy_sell_ratio, 2),
        vol_1h=vol_1h,
        vol_24h=vol_24h,
        liquidity=liquidity,
        market_cap=safe_float(pair.get("marketCap")),
        pair_created_at=created_at,
        dexscreener_url=str(pair.get("url") or ""),
    )


def passes_filters(candidate: Candidate, args: argparse.Namespace) -> bool:
    if candidate.score < args.min_score:
        return False
    if candidate.liquidity < args.min_liq_usd:
        return False
    if candidate.vol_1h < args.min_vol_1h_usd:
        return False
    if candidate.vol_24h < args.min_vol_24h_usd:
        return False
    if candidate.buys_1h + candidate.sells_1h < args.min_tx_1h:
        return False
    if candidate.buy_sell_ratio < args.min_buy_sell_ratio:
        return False
    if candidate.h1 is not None and not (args.min_h1_pct <= candidate.h1 <= args.max_h1_pct):
        return False
    if candidate.h6 is not None and candidate.h6 < args.min_h6_pct:
        return False
    if candidate.m5 is not None and not (args.min_m5_pct <= candidate.m5 <= args.max_m5_pct):
        return False
    return True


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"calls": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"calls": {}}
    if not isinstance(data, dict):
        return {"calls": {}}
    data.setdefault("calls", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def is_on_cooldown(candidate: Candidate, state: dict[str, Any], cooldown_hours: float) -> bool:
    call = (state.get("calls") or {}).get(candidate.token)
    if not call or not call.get("sent_at"):
        return False
    try:
        sent_at = datetime.fromisoformat(call["sent_at"])
    except ValueError:
        return False
    return datetime.now(timezone.utc) - sent_at < timedelta(hours=cooldown_hours)


def mark_sent(candidate: Candidate, state: dict[str, Any]) -> None:
    state.setdefault("calls", {})[candidate.token] = {
        "symbol": candidate.symbol,
        "score": candidate.score,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }


def money(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def price(value: float) -> str:
    if value >= 1:
        return f"{value:.4f}"
    if value >= 0.001:
        return f"{value:.6f}"
    if value >= 0.000001:
        return f"{value:.8f}"
    return f"{value:.12f}".rstrip("0").rstrip(".")


def call_reason(candidate: Candidate) -> str:
    reasons: list[str] = []
    if candidate.liquidity >= 75_000:
        reasons.append("liquidity หนา")
    elif candidate.liquidity >= 30_000:
        reasons.append("liquidity พอรับไม้สั้น")
    if candidate.vol_1h >= 25_000:
        reasons.append("volume 1h เดิน")
    if candidate.h1 is not None and 2 <= candidate.h1 <= 18:
        reasons.append("momentum 1h ยังไม่ร้อนเกิน")
    if candidate.buy_sell_ratio >= 1.15:
        reasons.append("buy/sell 1h ฝั่งซื้อชนะ")
    if not reasons:
        reasons.append("ผ่านเกณฑ์รวมของระบบ")
    return ", ".join(reasons)


def build_message(candidate: Candidate, expiry_minutes: int) -> str:
    expires = (datetime.now().astimezone() + timedelta(minutes=expiry_minutes)).strftime("%Y-%m-%d %H:%M %Z")
    return "\n".join(
        [
            "MEME CALL - watchlist เท่านั้น",
            "ไม่ใช่คำแนะนำการลงทุน / ไม่มีการันตีกำไร",
            "",
            f"Token: {candidate.symbol} ({candidate.name})",
            f"CA: {candidate.token}",
            f"DEX: {candidate.dex}",
            "",
            f"Price: {price(candidate.price)}",
            f"TP1 +10%: {price(candidate.tp10)}",
            f"TP2 +15%: {price(candidate.tp15)}",
            f"SL -6%: {price(candidate.sl6)}",
            "",
            "Entry condition:",
            "- เข้าเฉพาะถ้าราคาไม่เกิน +2% จาก Price และแท่ง 5m ไม่ไหลแดงแรง",
            "- ถ้าแท่ง 5m เพิ่งพุ่งแรง ให้รอย่อ ไม่ไล่แท่งเขียว",
            f"- Call หมดอายุ: {expires}",
            "",
            f"Score: {candidate.score}",
            f"Liq: {money(candidate.liquidity)} | Vol 1h: {money(candidate.vol_1h)} | Vol 24h: {money(candidate.vol_24h)}",
            f"5m: {pct(candidate.m5)} | 1h: {pct(candidate.h1)} | 6h: {pct(candidate.h6)} | 24h: {pct(candidate.h24)}",
            f"Buy/Sell 1h: {candidate.buys_1h}/{candidate.sells_1h} ({candidate.buy_sell_ratio}x)",
            f"Reason: {call_reason(candidate)}",
            "",
            f"GMGN: {candidate.gmgn_url}",
            f"DexScreener: {candidate.dexscreener_url}",
        ]
    )


def parse_watchlist(raw: str) -> list[str]:
    return unique(part for part in raw.replace("\n", ",").split(","))


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def run_once(args: argparse.Namespace) -> int:
    watchlist = parse_watchlist(os.environ.get("WATCHLIST_TOKENS", ""))
    tokens = discover_tokens(watchlist, args.max_tokens)
    if not tokens:
        print("No Solana tokens discovered.", file=sys.stderr)
        return 1

    pairs = best_pair_by_token(fetch_pairs(tokens))
    scored = [candidate for pair in pairs if (candidate := score_pair(pair))]
    scored.sort(key=lambda item: item.score, reverse=True)

    state = load_state(args.state_file)
    candidates = [
        candidate
        for candidate in scored
        if passes_filters(candidate, args) and not is_on_cooldown(candidate, state, args.cooldown_hours)
    ]
    selected = candidates[: args.max_calls]

    if not selected:
        print("No fresh calls passed the filter.")
        print("Top observed candidates:")
        for candidate in scored[:8]:
            print(
                f"- {candidate.symbol}: score={candidate.score}, liq={money(candidate.liquidity)}, "
                f"vol1h={money(candidate.vol_1h)}, m5={pct(candidate.m5)}, h1={pct(candidate.h1)}"
            )
        return 0

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if args.send and (not bot_token or not chat_id):
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before using --send.")

    for index, candidate in enumerate(selected, start=1):
        message = build_message(candidate, args.expiry_minutes)
        print(f"\n--- CALL {index}/{len(selected)}: {candidate.symbol} score={candidate.score} ---")
        print(message)
        if args.send:
            post_telegram(bot_token, chat_id, message)
            mark_sent(candidate, state)
            save_state(args.state_file, state)
            print(f"Sent to Telegram: {chat_id}")
        else:
            print("Dry-run only. Add --send to publish to Telegram.")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Post conditional Solana meme-call alerts to Telegram.")
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--send", action="store_true", help="Publish to Telegram. Default is dry-run.")
    parser.add_argument("--loop-minutes", type=float, default=env_float("INTERVAL_MINUTES", 0))
    parser.add_argument("--max-calls", type=int, default=env_int("MAX_CALLS_PER_RUN", 3))
    parser.add_argument("--max-tokens", type=int, default=env_int("MAX_TOKENS", 90))
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--cooldown-hours", type=float, default=env_float("COOLDOWN_HOURS", 6))
    parser.add_argument("--expiry-minutes", type=int, default=env_int("CALL_EXPIRY_MINUTES", 30))
    parser.add_argument("--min-score", type=int, default=env_int("MIN_SCORE", 85))
    parser.add_argument("--min-liq-usd", type=float, default=env_float("MIN_LIQ_USD", 30_000))
    parser.add_argument("--min-vol-1h-usd", type=float, default=env_float("MIN_VOL_1H_USD", 7_500))
    parser.add_argument("--min-vol-24h-usd", type=float, default=env_float("MIN_VOL_24H_USD", 75_000))
    parser.add_argument("--min-tx-1h", type=int, default=env_int("MIN_TX_1H", 80))
    parser.add_argument("--min-buy-sell-ratio", type=float, default=env_float("MIN_BUY_SELL_RATIO", 0.95))
    parser.add_argument("--min-m5-pct", type=float, default=env_float("MIN_M5_PCT", -3))
    parser.add_argument("--max-m5-pct", type=float, default=env_float("MAX_M5_PCT", 10))
    parser.add_argument("--min-h1-pct", type=float, default=env_float("MIN_H1_PCT", 0))
    parser.add_argument("--max-h1-pct", type=float, default=env_float("MAX_H1_PCT", 25))
    parser.add_argument("--min-h6-pct", type=float, default=env_float("MIN_H6_PCT", -35))
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

    while True:
        try:
            status = run_once(args)
        except Exception as exc:  # Keep loop mode alive after temporary API errors.
            print(f"ERROR: {exc}", file=sys.stderr)
            status = 1
        if args.loop_minutes <= 0:
            return status
        sleep_seconds = max(60, int(args.loop_minutes * 60))
        print(f"Sleeping {sleep_seconds} seconds...")
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Receive TradingView webhook alerts and forward them to Telegram.

This service is intentionally small and dependency-free so it can run with the
same Python setup already used by the Telegram project in this folder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from telegram_meme_call_bot import ensure_utf8_stdio, load_env_file, post_telegram


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8787
DEFAULT_PATH = "/webhook/tradingview"


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value != "" else default


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
    ) -> None:
        super().__init__(server_address, request_handler_class)
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.webhook_path = webhook_path
        self.health_path = health_path
        self.webhook_secret = webhook_secret
        self.dry_run = dry_run


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

    server = WebhookServer(
        (args.host, args.port),
        WebhookHandler,
        bot_token=bot_token,
        chat_id=chat_id or "dry-run",
        webhook_path=args.webhook_path,
        health_path=args.health_path,
        webhook_secret=args.secret,
        dry_run=args.dry_run,
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
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

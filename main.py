from __future__ import annotations

import argparse
import json
from typing import Any

import telebot

from paper_bot import PaperBotService
from settings import get_settings

MAX_TG_MSG_LEN = 3900


def _build_bot() -> telebot.TeleBot:
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is empty.")
    return telebot.TeleBot(token)


def _send_long_message(bot: telebot.TeleBot, chat_id: int, text: str) -> int:
    remaining = (text or "").strip()
    if not remaining:
        return 0

    sent = 0
    while remaining:
        chunk = remaining[:MAX_TG_MSG_LEN]
        if len(remaining) > MAX_TG_MSG_LEN:
            split_at = chunk.rfind("\n")
            if split_at > 200:
                chunk = chunk[:split_at]
        bot.send_message(chat_id, chunk)
        sent += 1
        remaining = remaining[len(chunk) :].lstrip("\n")
    return sent


def _print_result(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _cmd_notify(args: argparse.Namespace) -> int:
    bot = _build_bot()
    chunks = _send_long_message(bot, args.chat_id, args.text)
    _print_result({"ok": True, "mode": "notify", "chat_id": args.chat_id, "chunks_sent": chunks})
    return 0


def _cmd_answer(args: argparse.Namespace) -> int:
    bot = _build_bot()
    service = PaperBotService()
    session_id = args.session_id or str(args.chat_id)

    trace_lines: list[str] = []

    def _on_trace(line: str, _: dict[str, Any]) -> None:
        trace_lines.append(line)

    result = service.answer_with_trace(
        query=args.query,
        session_id=session_id,
        pdf_path=args.pdf_path,
        on_trace=_on_trace,
        trace_on=bool(args.trace),
    )

    answer = str(result.get("answer", "")).strip() or "답변을 생성하지 못했습니다."
    answer_chunks = _send_long_message(bot, args.chat_id, f"[ANSWER]\n{answer}")

    trace_chunks = 0
    if args.send_trace and trace_lines:
        trace_chunks = _send_long_message(bot, args.chat_id, "\n".join(trace_lines))

    _print_result(
        {
            "ok": True,
            "mode": "answer",
            "chat_id": args.chat_id,
            "session_id": session_id,
            "route": result.get("route"),
            "answer_chunks_sent": answer_chunks,
            "trace_chunks_sent": trace_chunks,
        }
    )
    return 0


def _cmd_daily_briefing(args: argparse.Namespace) -> int:
    bot = _build_bot()
    service = PaperBotService()
    session_id = args.session_id or str(args.chat_id)

    result = service.generate_daily_briefing(session_id=session_id, max_papers=args.max_papers)
    message = str(result.get("message", "")).strip()
    chunks = _send_long_message(bot, args.chat_id, message)

    _print_result(
        {
            "ok": True,
            "mode": "daily-briefing",
            "chat_id": args.chat_id,
            "session_id": session_id,
            "papers": result.get("count", 0),
            "chunks_sent": chunks,
            "cache_hits": result.get("cache_hits", 0),
            "pages_used": result.get("pages_used", 0),
        }
    )
    return 0


def _resolve_session_id(session_id: str, chat_id: int | None) -> str:
    sid = (session_id or "").strip()
    if sid:
        return sid
    if chat_id is not None:
        return str(chat_id)
    raise ValueError("Either --session-id or --chat-id is required.")


def _cmd_reset(args: argparse.Namespace) -> int:
    service = PaperBotService()
    session_id = _resolve_session_id(args.session_id, args.chat_id)
    service.reset_session(session_id=session_id)
    _print_result({"ok": True, "mode": "reset", "session_id": session_id})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="n8n-oriented Telegram notifier entrypoint")
    sub = parser.add_subparsers(dest="command", required=True)

    p_notify = sub.add_parser("notify", help="send plain text to Telegram")
    p_notify.add_argument("--chat-id", type=int, required=True)
    p_notify.add_argument("--text", required=True)
    p_notify.set_defaults(func=_cmd_notify)

    p_answer = sub.add_parser("answer", help="run QA then send answer to Telegram")
    p_answer.add_argument("--chat-id", type=int, required=True)
    p_answer.add_argument("--query", required=True)
    p_answer.add_argument("--session-id", default="")
    p_answer.add_argument("--pdf-path", default=None)
    p_answer.add_argument("--trace", action="store_true", help="enable trace generation")
    p_answer.add_argument("--send-trace", action="store_true", help="send trace lines to Telegram")
    p_answer.set_defaults(func=_cmd_answer)

    p_daily = sub.add_parser("daily-briefing", help="generate daily briefing and send to Telegram")
    p_daily.add_argument("--chat-id", type=int, required=True)
    p_daily.add_argument("--session-id", default="")
    p_daily.add_argument("--max-papers", type=int, default=None)
    p_daily.set_defaults(func=_cmd_daily_briefing)

    p_reset = sub.add_parser("reset", help="reset session memory and persist cleared state")
    p_reset.add_argument("--session-id", default="")
    p_reset.add_argument("--chat-id", type=int, default=None)
    p_reset.set_defaults(func=_cmd_reset)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

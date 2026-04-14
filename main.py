from __future__ import annotations

import json
import os
from pathlib import Path

import telebot
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from paper_bot.bot_main import PaperBotService
from settings import get_settings

SETTINGS = get_settings()
TELEGRAM_BOT_TOKEN = SETTINGS.telegram_bot_token

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN 이 비어 있습니다.")

bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
paper_bot = PaperBotService()

MAX_TG_MSG_LEN = 3900
DAILY_SUMMARY_HOUR = int(os.getenv("DAILY_SUMMARY_HOUR", "7"))
DAILY_SUMMARY_MINUTE = int(os.getenv("DAILY_SUMMARY_MINUTE", "30"))
DAILY_SUMMARY_TIMEZONE = os.getenv("DAILY_SUMMARY_TIMEZONE", "Asia/Seoul")
SUBSCRIBERS_FILE = Path(os.getenv("DAILY_SUBSCRIBERS_FILE", "sessions/subscribers.json"))
SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_subscribers() -> set[int]:
    if not SUBSCRIBERS_FILE.exists():
        return set()
    try:
        data = json.loads(SUBSCRIBERS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()

    if not isinstance(data, list):
        return set()

    out: set[int] = set()
    for x in data:
        try:
            out.add(int(x))
        except Exception:
            continue
    return out


def _save_subscribers() -> None:
    SUBSCRIBERS_FILE.write_text(json.dumps(sorted(SUBSCRIBERS), ensure_ascii=False, indent=2), encoding="utf-8")


def _add_subscriber(chat_id: int) -> bool:
    if chat_id in SUBSCRIBERS:
        return False
    SUBSCRIBERS.add(chat_id)
    _save_subscribers()
    return True


def _remove_subscriber(chat_id: int) -> bool:
    if chat_id not in SUBSCRIBERS:
        return False
    SUBSCRIBERS.remove(chat_id)
    _save_subscribers()
    return True


def _send_long_message(chat_id: int, text: str) -> None:
    """Telegram message size guard (hard limit ~4096 chars)."""
    remaining = text.strip()
    if not remaining:
        return

    while remaining:
        chunk = remaining[:MAX_TG_MSG_LEN]
        if len(remaining) > MAX_TG_MSG_LEN:
            split_at = chunk.rfind("\n")
            if split_at > 200:
                chunk = chunk[:split_at]
        bot.send_message(chat_id, chunk)
        remaining = remaining[len(chunk):].lstrip("\n")


def _run_daily_briefing_job() -> None:
    if not SUBSCRIBERS:
        return

    for chat_id in list(SUBSCRIBERS):
        try:
            result = paper_bot.generate_daily_briefing(session_id=str(chat_id))
            message = str(result.get("message", "")).strip()
            if message:
                _send_long_message(chat_id, message)
        except Exception as e:
            _send_long_message(chat_id, f"[Daily 07:30 Briefing] 실패: {type(e).__name__}: {e}")


def _start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=DAILY_SUMMARY_TIMEZONE)
    trigger = CronTrigger(
        hour=DAILY_SUMMARY_HOUR,
        minute=DAILY_SUMMARY_MINUTE,
        timezone=DAILY_SUMMARY_TIMEZONE,
    )
    scheduler.add_job(
        _run_daily_briefing_job,
        trigger=trigger,
        id="daily_0730_briefing",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    scheduler.start()
    return scheduler


@bot.message_handler(commands=["start"])
def start_handler(message):
    chat_id = message.chat.id
    _add_subscriber(chat_id)
    trace_enabled = paper_bot.get_trace_enabled(str(chat_id))
    trace_text = "ON" if trace_enabled else "OFF"
    bot.reply_to(
        message,
        "안녕하세요. Paper Bot 입니다.\n"
        f"매일 {DAILY_SUMMARY_HOUR:02d}:{DAILY_SUMMARY_MINUTE:02d} ({DAILY_SUMMARY_TIMEZONE})에 "
        "Papers 폴더 논문 요약을 자동으로 보냅니다.\n"
        "질문하면 같은 세션 맥락으로 질의응답합니다.\n"
        f"trace 상태: {trace_text}\n"
        "명령어: /subscribe /unsubscribe /daily_now /trace /trace_on /trace_off /session_reset",
    )


@bot.message_handler(commands=["subscribe"])
def subscribe_handler(message):
    added = _add_subscriber(message.chat.id)
    if added:
        bot.reply_to(message, "매일 07:30 요약 알림을 구독했습니다.")
    else:
        bot.reply_to(message, "이미 구독 중입니다.")


@bot.message_handler(commands=["unsubscribe"])
def unsubscribe_handler(message):
    removed = _remove_subscriber(message.chat.id)
    if removed:
        bot.reply_to(message, "매일 07:30 요약 알림을 해지했습니다.")
    else:
        bot.reply_to(message, "현재 구독 상태가 아닙니다.")


@bot.message_handler(commands=["daily_now"])
def daily_now_handler(message):
    chat_id = message.chat.id
    bot.send_chat_action(chat_id, "typing")
    try:
        result = paper_bot.generate_daily_briefing(session_id=str(chat_id))
        msg = str(result.get("message", "")).strip() or "브리핑 결과가 비어 있습니다."
        _send_long_message(chat_id, msg)
    except Exception as e:
        bot.reply_to(message, f"오류가 발생했습니다: {type(e).__name__}: {e}")


@bot.message_handler(commands=["trace"])
def trace_status_handler(message):
    enabled = paper_bot.get_trace_enabled(str(message.chat.id))
    state = "ON" if enabled else "OFF"
    bot.reply_to(message, f"현재 trace 상태: {state}")


@bot.message_handler(commands=["trace_on"])
def trace_on_handler(message):
    paper_bot.set_trace_enabled(str(message.chat.id), True)
    bot.reply_to(message, "trace를 켰습니다. 다음 질문부터 중간 로그를 출력합니다.")


@bot.message_handler(commands=["trace_off"])
def trace_off_handler(message):
    paper_bot.set_trace_enabled(str(message.chat.id), False)
    bot.reply_to(message, "trace를 껐습니다. 다음 질문부터 중간 로그를 숨깁니다.")


@bot.message_handler(commands=["session_reset"])
def session_reset_handler(message):
    paper_bot.reset_session(str(message.chat.id))
    bot.reply_to(message, "세션 메모리를 수동 초기화했습니다.")


@bot.message_handler(func=lambda message: message.text is not None)
def text_handler(message):
    user_text = message.text.strip()
    if not user_text:
        bot.reply_to(message, "텍스트를 보내주세요.")
        return

    chat_id = message.chat.id
    _add_subscriber(chat_id)
    bot.send_chat_action(chat_id, "typing")

    def _stream_trace(line: str, _: dict):
        _send_long_message(chat_id, line)

    try:
        result = paper_bot.answer_with_trace(
            query=user_text,
            session_id=str(chat_id),
            on_trace=_stream_trace,
            trace_on=paper_bot.get_trace_enabled(str(chat_id)),
        )
        answer = result.get("answer", "")
        if not answer:
            answer = "답변을 생성하지 못했습니다."
        _send_long_message(chat_id, f"[ANSWER]\n{answer}")
    except Exception as e:
        bot.reply_to(message, f"오류가 발생했습니다: {type(e).__name__}: {e}")


SUBSCRIBERS = _load_subscribers()
SCHEDULER: BackgroundScheduler | None = None


if __name__ == "__main__":
    SCHEDULER = _start_scheduler()
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)

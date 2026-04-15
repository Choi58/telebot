from __future__ import annotations

import os
from typing import Any

import telebot
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from paper_bot import PaperBotService
from settings import get_settings

MAX_TG_MSG_LEN = 3900

app = FastAPI(title="telebot-api", version="1.0.0")


def _require_api_key(x_api_key: str | None) -> None:
    expected = (os.getenv("TELEBOT_API_KEY", "") or "").strip()
    if not expected:
        return
    if (x_api_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="invalid api key")


def _build_bot() -> telebot.TeleBot:
    settings = get_settings()
    token = settings.telegram_bot_token
    if not token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN is empty")
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


class DailyBriefingRequest(BaseModel):
    chat_id: int
    session_id: str | None = None
    max_papers: int | None = None


class AnswerRequest(BaseModel):
    chat_id: int
    query: str
    session_id: str | None = None
    pdf_path: str | None = None
    trace: bool = False
    send_trace: bool = False


class ResetRequest(BaseModel):
    session_id: str | None = None
    chat_id: int | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/daily-briefing")
def daily_briefing(payload: DailyBriefingRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_key(x_api_key)
    bot = _build_bot()
    service = PaperBotService()
    session_id = (payload.session_id or "").strip() or str(payload.chat_id)

    result = service.generate_daily_briefing(session_id=session_id, max_papers=payload.max_papers)
    message = str(result.get("message", "")).strip()
    chunks = _send_long_message(bot, payload.chat_id, message)

    return {
        "ok": True,
        "mode": "daily-briefing",
        "chat_id": payload.chat_id,
        "session_id": session_id,
        "message": message,
        "papers": result.get("count", 0),
        "chunks_sent": chunks,
        "cache_hits": result.get("cache_hits", 0),
        "pages_used": result.get("pages_used", 0),
    }


@app.post("/answer")
def answer(payload: AnswerRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_key(x_api_key)
    bot = _build_bot()
    service = PaperBotService()
    session_id = (payload.session_id or "").strip() or str(payload.chat_id)

    trace_lines: list[str] = []

    def _on_trace(line: str, _: dict[str, Any]) -> None:
        trace_lines.append(line)

    result = service.answer_with_trace(
        query=payload.query,
        session_id=session_id,
        pdf_path=payload.pdf_path,
        on_trace=_on_trace,
        trace_on=bool(payload.trace),
    )

    answer_text = str(result.get("answer", "")).strip() or "답변을 생성하지 못했습니다."
    answer_chunks = _send_long_message(bot, payload.chat_id, f"[ANSWER]\n{answer_text}")

    trace_chunks = 0
    if payload.send_trace and trace_lines:
        trace_chunks = _send_long_message(bot, payload.chat_id, "\n".join(trace_lines))

    return {
        "ok": True,
        "mode": "answer",
        "chat_id": payload.chat_id,
        "session_id": session_id,
        "route": result.get("route"),
        "answer_chunks_sent": answer_chunks,
        "trace_chunks_sent": trace_chunks,
    }


@app.post("/reset")
def reset(payload: ResetRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _require_api_key(x_api_key)
    service = PaperBotService()
    session_id = (payload.session_id or "").strip() or (str(payload.chat_id) if payload.chat_id is not None else "")
    if not session_id:
        raise HTTPException(status_code=400, detail="Either session_id or chat_id is required")

    service.reset_session(session_id=session_id)
    return {"ok": True, "mode": "reset", "session_id": session_id}

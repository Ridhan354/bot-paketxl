"""Shared Telegram helper routines."""
from __future__ import annotations

from typing import Iterable, List, Optional

from telegram import InlineKeyboardMarkup
from telegram.constants import ParseMode


def chunk_text(text: str, limit: int = 3800) -> List[str]:
    if not text:
        return []
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1 or split_at < limit * 0.6:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def reply_in_chunks(message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, limit: int = 3800):
    chunks = chunk_text(text, limit)
    if not chunks:
        return
    for idx, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup if idx == 0 else None,
        )


async def edit_or_reply_in_chunks(query, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, limit: int = 3800):
    chunks = chunk_text(text, limit)
    if not chunks:
        await query.edit_message_text("", parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        return
    await query.edit_message_text(chunks[0], parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    message = query.message
    if message is None:
        return
    for chunk in chunks[1:]:
        await message.reply_text(chunk, parse_mode=ParseMode.HTML)


async def bot_send_in_chunks(bot, chat_id: int, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None, limit: int = 3800):
    chunks = chunk_text(text, limit)
    if not chunks:
        return
    for idx, chunk in enumerate(chunks):
        await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup if idx == 0 else None,
        )


__all__ = [
    "chunk_text",
    "reply_in_chunks",
    "edit_or_reply_in_chunks",
    "bot_send_in_chunks",
]

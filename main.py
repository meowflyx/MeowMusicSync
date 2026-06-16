"""Telegram bot to manage and monitor Spotify and Yandex Music synchronization.

Provides commands for running sync, reviewing pending approvals, clearing caches,
and viewing statistics, all backed by an SQLite database.
"""

import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from logging.handlers import TimedRotatingFileHandler
from config import TG_BOT_TOKEN, TG_ADMIN_ID
from sync_logic import (
    sync_ym_to_sp, sync_sp_to_ym, full_two_way_sync,
    approve_pending, reject_pending, get_status_stats,
    get_pending_tracks, clear_failed_tracks, get_failed_tracks
)

log_handler = TimedRotatingFileHandler('sync.log', when='midnight', interval=1, backupCount=1)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler()])

if not TG_BOT_TOKEN:
    raise ValueError("TG_BOT_TOKEN is not set in .env")

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()


def get_pending_cb(loop):
    """Return a callback function to send Telegram notifications for pending approvals."""
    def cb(pend_key, source, found, score, direction):
        if not TG_ADMIN_ID:
            return
        arrow = "🟡 YM → SP" if direction == "ym_to_sp" else "🔵 SP → YM"
        text = (
            f"⏳ Требуется одобрение ({score}%)\n"
            f"{arrow}\n\n"
            f"🔍 Искали: {source}\n"
            f"📀 Нашли: {found}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data=f"approve:{pend_key}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{pend_key}"),
            ]
        ])
        asyncio.run_coroutine_threadsafe(bot.send_message(TG_ADMIN_ID, text, reply_markup=kb), loop)
    return cb


async def periodic_sync():
    """Background task to run two-way sync periodically."""
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, full_two_way_sync, None, get_pending_cb(loop))
        logging.info(f"Фоновая синхронизация завершена:\n{res}")
    except Exception as e:
        logging.error(f"Ошибка фоновой синхронизации: {e}")


@dp.message(Command("start"))
async def start_handler(message: Message):
    """Handle /start command, showing list of available bot commands to the admin."""
    if message.from_user.id != TG_ADMIN_ID:
        return await message.answer("Ты кто такой? Я тебя не звал.")
    await message.answer(
        "🎵 Бот синхронизации музыки\n\n"
        "Команды:\n"
        "/sync_all - Полная синхронизация (в обе стороны)\n"
        "/sync_ym_sp - Яндекс → Spotify\n"
        "/sync_sp_ym - Spotify → Яндекс\n"
        "/status - Статистика\n"
        "/pending - Показать ожидающие одобрения\n"
        "/retry_failed - Очистить кэш ненайденных\n"
        "/list_failed - Список ненайденных"
    )


@dp.message(Command("sync_all"))
async def sync_all_handler(message: Message):
    """Trigger manual full two-way synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Начинаю полную синхронизацию...")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, full_two_way_sync, None, get_pending_cb(loop))
    await message.answer(f"✅ Готово:\n{res}")


@dp.message(Command("sync_ym_sp"))
async def sync_ym_sp_handler(message: Message):
    """Trigger manual Yandex Music to Spotify synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Начинаю синхронизацию Яндекс → Spotify...")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, sync_ym_to_sp, None, get_pending_cb(loop))
    await message.answer(f"✅ Готово:\n{res}")


@dp.message(Command("sync_sp_ym"))
async def sync_sp_ym_handler(message: Message):
    """Trigger manual Spotify to Yandex Music synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Начинаю синхронизацию Spotify → Яндекс...")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, sync_sp_to_ym, None, get_pending_cb(loop))
    await message.answer(f"✅ Готово:\n{res}")


@dp.message(Command("status"))
async def status_handler(message: Message):
    """Display synchronization database statistics."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    stats = get_status_stats()
    await message.answer(
        f"📊 Статистика:\n"
        f"Яндекс в кэше: {stats['yandex']}\n"
        f"Spotify в кэше: {stats['spotify']}\n"
        f"🔗 Сопоставлено треков: {stats['mappings']}\n"
        f"⏳ Ожидают одобрения: {stats['pending']}\n"
        f"❌ Не найдено: {stats['failed']}"
    )


@dp.message(Command("pending"))
async def pending_handler(message: Message):
    """List all tracks currently waiting for user manual match approval."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    pending = get_pending_tracks()
    if not pending:
        return await message.answer("Нет треков на одобрении.")
    
    for key, entry in pending.items():
        arrow = "🟡 YM → SP" if entry["direction"] == "ym_to_sp" else "🔵 SP → YM"
        text = (
            f"⏳ Одобрение ({entry['score']}%)\n"
            f"{arrow}\n\n"
            f"🔍 Искали: {entry['source']}\n"
            f"📀 Нашли: {entry['found']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Добавить", callback_data=f"approve:{key}"),
                InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{key}"),
            ]
        ])
        await message.answer(text, reply_markup=kb)


@dp.message(Command("retry_failed"))
async def retry_failed_handler(message: Message):
    """Clear all records from failed cache so that the next sync will re-attempt them."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    count = clear_failed_tracks()
    await message.answer(f"🗑 Очищено {count} записей. Следующая синхронизация попробует снова.")


@dp.message(Command("list_failed"))
async def list_failed_handler(message: Message):
    """List all tracks that could not be matched during previous synchronizations."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    failed = get_failed_tracks()
    if not failed:
        return await message.answer("Кэш ненайденных пуст.")
    
    lines = []
    for key, query in failed.items():
        direction = "🟡 YM→SP" if key.startswith("ym_to_sp") else "🔵 SP→YM"
        lines.append(f"{direction}: {query}")
    
    text = "\n".join(lines)
    if len(text) > 4000:
        chunks = []
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > 4000:
                chunks.append(current)
                current = line
            else:
                current += "\n" + line if current else line
        if current:
            chunks.append(current)
        for chunk in chunks:
            await message.answer(f"📋 Ненайденные:\n{chunk}")
    else:
        await message.answer(f"📋 Ненайденные:\n{text}")


@dp.callback_query(F.data.startswith("approve:"))
async def approve_callback(callback: CallbackQuery):
    """Handle the inline callback query to approve a pending track match."""
    if callback.from_user.id != TG_ADMIN_ID:
        return await callback.answer("Нет доступа")
    
    pend_key = callback.data.split(":", 1)[1]
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, approve_pending, pend_key)
    
    if ok:
        await callback.message.edit_text(f"✅ {msg}")
    else:
        await callback.message.edit_text(f"⚠️ {msg}")
    await callback.answer()


@dp.callback_query(F.data.startswith("reject:"))
async def reject_callback(callback: CallbackQuery):
    """Handle the inline callback query to reject a pending track match."""
    if callback.from_user.id != TG_ADMIN_ID:
        return await callback.answer("Нет доступа")
    
    pend_key = callback.data.split(":", 1)[1]
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, reject_pending, pend_key)
    
    if ok:
        await callback.message.edit_text(f"❌ {msg}")
    else:
        await callback.message.edit_text(f"⚠️ {msg}")
    await callback.answer()


async def main():
    """Start the scheduler for periodic sync and start Telegram bot polling."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(periodic_sync, 'interval', hours=3)
    scheduler.start()
        
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

"""Telegram bot to manage and monitor Spotify and Yandex Music synchronization.

Provides commands for running sync, reviewing pending approvals, clearing caches,
and viewing statistics, all backed by an SQLite database.
"""

import asyncio
import logging
import signal
import sys
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from logging.handlers import TimedRotatingFileHandler
from config import TG_BOT_TOKEN, TG_ADMIN_ID
from sync_logic import (
    sync_ym_to_sp, sync_sp_to_ym, full_two_way_sync,
    approve_pending, reject_pending, get_status_stats,
    get_pending_tracks, clear_failed_tracks, get_failed_tracks,
    get_blacklist, clear_blacklist, remove_spotify_duplicates,
    remove_yandex_duplicates, get_last_sync_info, is_sync_running,
    get_recent_logs, check_api_health, add_manual_mapping,
    remove_from_blacklist, clear_stale_sync_lock
)

log_handler = TimedRotatingFileHandler('sync.log', when='midnight', interval=1, backupCount=7)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler()])

if not TG_BOT_TOKEN:
    logging.error("TG_BOT_TOKEN не задан. Создайте .env файл на основе .env.example.")
    sys.exit(1)

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()
_shutdown_event = asyncio.Event()


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


def get_progress_cb(loop, chat_id):
    """Return a callback that sends periodic progress updates to the admin chat."""
    last_sent = {"text": ""}
    def cb(current, total, direction):
        if not TG_ADMIN_ID:
            return
        arrow = "🟡 YM → SP" if direction == "ym_to_sp" else "🔵 SP → YM"
        text = f"⏳ Прогресс {arrow}: {current}/{total}"
        if text == last_sent["text"]:
            return
        last_sent["text"] = text
        asyncio.run_coroutine_threadsafe(bot.send_message(chat_id, text), loop)
    return cb


async def send_long_message(message: Message, header: str, lines: list, chunk_limit: int = 4000):
    """Send a list of lines as one or more Telegram messages, splitting at chunk_limit."""
    if not lines:
        await message.answer(f"{header}\n(пусто)")
        return
    
    current = header
    for line in lines:
        candidate = f"{current}\n{line}" if current and current != header else (
            f"{header}\n{line}" if current == header else line
        )
        if len(candidate) > chunk_limit:
            await message.answer(current)
            current = f"{line}"
        else:
            current = candidate
    if current:
        await message.answer(current)


async def periodic_sync():
    """Background task to run two-way sync periodically."""
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, full_two_way_sync, None, get_pending_cb(loop))
        logging.info(f"Фоновая синхронизация завершена:\n{res}")
        if TG_ADMIN_ID:
            await bot.send_message(TG_ADMIN_ID, f"🔄 Фоновая синхронизация завершена:\n{res}")
    except Exception as e:
        logging.error(f"Ошибка фоновой синхронизации: {e}")
        if TG_ADMIN_ID:
            await bot.send_message(TG_ADMIN_ID, f"❌ Ошибка фоновой синхронизации: {e}")


@dp.message(Command("start"))
async def start_handler(message: Message):
    """Handle /start command, showing list of available bot commands to the admin."""
    if message.from_user.id != TG_ADMIN_ID:
        return await message.answer("Ты кто такой? Я тебя не звал.")
    await help_handler(message)


@dp.message(Command("help"))
async def help_handler(message: Message):
    """Show the full list of available bot commands."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer(
        "🎵 Бот синхронизации музыки\n\n"
        "Команды:\n"
        "/sync_all - Полная синхронизация (в обе стороны)\n"
        "/sync_ym_sp - Яндекс → Spotify\n"
        "/sync_sp_ym - Spotify → Яндекс\n"
        "/status - Статистика\n"
        "/pending - Показать ожидающие одобрения\n"
        "/retry_failed - Очистить кэш ненайденных\n"
        "/list_failed - Список ненайденных\n"
        "/blacklist - Показать блеклист\n"
        "/clear_blacklist - Очистить блеклист\n"
        "/unblacklist <ym_id|sp_id> - Удалить один трек из блеклиста\n"
        "/clean_sp_dupes - Удалить дубликаты из Spotify\n"
        "/clean_ym_dupes - Удалить дубликаты из Яндекс Музыки\n"
        "/last_sync - Информация о последней синхронизации\n"
        "/logs [n] - Последние n строк лога (по умолчанию 20)\n"
        "/health - Проверка доступности API\n"
        "/add_mapping <ym_id> <sp_id> - Ручное сопоставление треков"
    )


@dp.message(Command("sync_all"))
async def sync_all_handler(message: Message):
    """Trigger manual full two-way synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    if is_sync_running():
        return await message.answer("⏳ Синхронизация уже выполняется. Подождите.")
    await message.answer("🔄 Начинаю полную синхронизацию...")
    try:
        loop = asyncio.get_running_loop()
        progress_cb = get_progress_cb(loop, message.chat.id)
        res = await loop.run_in_executor(None, full_two_way_sync, progress_cb, get_pending_cb(loop))
        await message.answer(f"✅ Готово:\n{res}")
    except Exception as e:
        logging.error(f"Ошибка полной синхронизации: {e}")
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("sync_ym_sp"))
async def sync_ym_sp_handler(message: Message):
    """Trigger manual Yandex Music to Spotify synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    if is_sync_running():
        return await message.answer("⏳ Синхронизация уже выполняется. Подождите.")
    await message.answer("🔄 Начинаю синхронизацию Яндекс → Spotify...")
    try:
        loop = asyncio.get_running_loop()
        progress_cb = get_progress_cb(loop, message.chat.id)
        res = await loop.run_in_executor(None, sync_ym_to_sp, progress_cb, get_pending_cb(loop))
        await message.answer(f"✅ Готово:\n{res}")
    except Exception as e:
        logging.error(f"Ошибка синхронизации YM→SP: {e}")
        await message.answer(f"❌ Ошибка: {e}")


@dp.message(Command("sync_sp_ym"))
async def sync_sp_ym_handler(message: Message):
    """Trigger manual Spotify to Yandex Music synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    if is_sync_running():
        return await message.answer("⏳ Синхронизация уже выполняется. Подождите.")
    await message.answer("🔄 Начинаю синхронизацию Spotify → Яндекс...")
    try:
        loop = asyncio.get_running_loop()
        progress_cb = get_progress_cb(loop, message.chat.id)
        res = await loop.run_in_executor(None, sync_sp_to_ym, progress_cb, get_pending_cb(loop))
        await message.answer(f"✅ Готово:\n{res}")
    except Exception as e:
        logging.error(f"Ошибка синхронизации SP→YM: {e}")
        await message.answer(f"❌ Ошибка: {e}")


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
    
    await send_long_message(message, "📋 Ненайденные:", lines)


@dp.message(Command("blacklist"))
async def blacklist_handler(message: Message):
    """List all tracks currently blacklisted."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    bl = get_blacklist()
    if not bl:
        return await message.answer("Блеклист пуст.")
    
    lines = [f"⚫ {item['artists']} - {item['title']}" for item in bl]
    await send_long_message(message, "📋 Блеклист:", lines)


@dp.message(Command("clear_blacklist"))
async def clear_blacklist_handler(message: Message):
    """Clear all tracks from the blacklist."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    count = clear_blacklist()
    await message.answer(f"🗑 Блеклист очищен. Удалено {count} записей.")


@dp.message(Command("unblacklist"))
async def unblacklist_handler(message: Message):
    """Remove a single track from the blacklist by ym_id or sp_id."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    args = message.text.split()[1:]
    if not args:
        return await message.answer("Использование: /unblacklist <ym_id|sp_id>")
    track_id = args[0]
    count = remove_from_blacklist(ym_id=track_id) or remove_from_blacklist(sp_id=track_id)
    if count:
        await message.answer(f"🗑 Удалено из блеклиста: {count} записей.")
    else:
        await message.answer("Трек не найден в блеклисте.")


@dp.message(Command("clean_sp_dupes"))
async def clean_sp_dupes_handler(message: Message):
    """Remove duplicate tracks from Spotify saved tracks."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Сканирую библиотеку Spotify на наличие дубликатов...")
    loop = asyncio.get_running_loop()
    success, res = await loop.run_in_executor(None, remove_spotify_duplicates)
    if not success:
        return await message.answer(f"❌ Ошибка: {res}")
        
    if isinstance(res, str):
        await message.answer(res)
    else:
        await message.answer(f"✅ Удалено дубликатов из Spotify: {len(res)}")
        await send_long_message(message, "📋 Удалённые:", res)


@dp.message(Command("clean_ym_dupes"))
async def clean_ym_dupes_handler(message: Message):
    """Remove duplicate tracks from Yandex Music liked tracks."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Сканирую библиотеку Яндекс Музыки на наличие дубликатов...")
    loop = asyncio.get_running_loop()
    success, res = await loop.run_in_executor(None, remove_yandex_duplicates)
    if not success:
        return await message.answer(f"❌ Ошибка: {res}")
        
    if isinstance(res, str):
        await message.answer(res)
    else:
        await message.answer(f"✅ Удалено дубликатов из Яндекс Музыки: {len(res)}")
        await send_long_message(message, "📋 Удалённые:", res)


@dp.message(Command("last_sync"))
async def last_sync_handler(message: Message):
    """Show information about the last completed synchronization."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    info = get_last_sync_info()
    if not info["timestamp"]:
        return await message.answer("Синхронизация ещё не выполнялась.")
    await message.answer(
        f"🕒 Последняя синхронизация:\n"
        f"Время: {info['timestamp']}\n"
        f"Результат:\n{info['result']}"
    )


@dp.message(Command("logs"))
async def logs_handler(message: Message):
    """Show the last n lines from the sync log."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    args = message.text.split()[1:]
    n = 20
    if args and args[0].isdigit():
        n = min(int(args[0]), 100)
    lines = get_recent_logs(n)
    if not lines:
        return await message.answer("Лог пуст или файл недоступен.")
    await send_long_message(message, f"📋 Последние {n} строк лога:", lines)


@dp.message(Command("health"))
async def health_handler(message: Message):
    """Check connectivity to Yandex Music and Spotify APIs."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Проверяю доступность API...")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, check_api_health)
    
    ym_status = "✅ OK" if result["yandex"] else f"❌ {result['yandex_error']}"
    sp_status = "✅ OK" if result["spotify"] else f"❌ {result['spotify_error']}"
    await message.answer(
        f"🩺 Проверка API:\n\n"
        f"Яндекс Музыка: {ym_status}\n"
        f"Spotify: {sp_status}"
    )


@dp.message(Command("add_mapping"))
async def add_mapping_handler(message: Message):
    """Manually link a Yandex Music track ID to a Spotify track ID."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    args = message.text.split()[1:]
    if len(args) != 2:
        return await message.answer("Использование: /add_mapping <ym_id> <sp_id>")
    ym_id, sp_id = args
    add_manual_mapping(ym_id, sp_id)
    await message.answer(f"🔗 Добавлен маппинг: YM {ym_id} ↔ SP {sp_id}")


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


def _handle_shutdown(signum, frame):
    """Signal handler that triggers graceful shutdown."""
    logging.info(f"Получен сигнал {signum}. Начинаю graceful shutdown...")
    _shutdown_event.set()


async def main():
    """Start the scheduler for periodic sync and start Telegram bot polling."""
    clear_stale_sync_lock()
    logging.info("Проверка устаревших блокировок синхронизации выполнена.")
    
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(periodic_sync, 'interval', hours=3)
    scheduler.start()
    logging.info("Планировщик запущен. Интервал синхронизации: 3 часа.")
        
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

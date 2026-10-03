"""Telegram bot to manage and monitor Spotify and Yandex Music synchronization.

Provides commands for running sync, reviewing pending approvals, clearing caches,
and viewing statistics, all backed by an SQLite database.
"""

import asyncio
import logging
import sys
from error_messages import explain_error
from aiogram import Bot, Dispatcher, F
from aiogram.utils.token import TokenValidationError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand, ErrorEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from logging.handlers import TimedRotatingFileHandler
from config import TG_BOT_TOKEN, TG_ADMIN_ID
from sync_logic import (
    sync_ym_to_sp, sync_sp_to_ym, full_two_way_sync,
    approve_pending, reject_pending, select_pending_candidate, get_status_stats,
    get_pending_tracks, clear_failed_tracks, get_failed_tracks,
    remove_spotify_duplicates,
    remove_yandex_duplicates, get_last_sync_info, is_sync_running,
    get_recent_logs, check_api_health, add_manual_mapping,
    like_playlist_tracks,
    configure_jev, set_jev_enabled, get_jev_status,
    set_matching_mode, revalidate_mappings
)

log_handler = TimedRotatingFileHandler('sync.log', when='midnight', interval=1, backupCount=7)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler()])

if not TG_BOT_TOKEN or not TG_ADMIN_ID:
    logging.error("Задайте TG_BOT_TOKEN и числовой TG_ADMIN_ID в .env.")
    sys.exit(1)

try:
    bot = Bot(token=TG_BOT_TOKEN)
except TokenValidationError:
    sys.exit("Неверный формат TG_BOT_TOKEN. Скопируйте токен бота из @BotFather в .env.")
dp = Dispatcher()
_last_background_error = None


async def send_long_message(message: Message, header: str, lines: list, chunk_limit: int = 4000):
    """Send a list of lines as one or more Telegram messages, splitting at chunk_limit."""
    if not lines:
        await message.answer(f"{header}\n(пусто)")
        return
    
    text = "\n".join([header, *lines])
    # Telegram counts UTF-16 units: astral characters take two units.
    current = ""
    units = 0
    for char in text:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > chunk_limit:
            await message.answer(current)
            current, units = "", 0
        current += char
        units += size
    if current:
        await message.answer(current)


async def periodic_sync():
    """Background task to run two-way sync periodically."""
    global _last_background_error
    try:
        if is_sync_running():
            return
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, full_two_way_sync)
        _last_background_error = None
        logging.info(f"Фоновая синхронизация завершена:\n{res}")
        if TG_ADMIN_ID:
            await bot.send_message(TG_ADMIN_ID, f"🔄 Фоновая синхронизация завершена:\n{res}\nОдобрения: /pending", disable_notification=True)
    except Exception as e:
        logging.error(f"Ошибка фоновой синхронизации: {e}")
        error = explain_error(e)
        if TG_ADMIN_ID and error != _last_background_error:
            await bot.send_message(TG_ADMIN_ID, f"❌ Ошибка фоновой синхронизации: {error}")
            _last_background_error = error


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
        "/sync или /sync_all - Полная синхронизация (в обе стороны)\n"
        "/sync_ym_sp - Яндекс → Spotify\n"
        "/sync_sp_ym - Spotify → Яндекс\n"
        "/status - Статистика\n"
        "/pending [страница] - Одобрения, по 5 треков\n"
        "/retry_failed - Повторить ненайденные треки\n"
        "/list_failed - Список ненайденных\n"
        "/clean_sp_dupes - Удалить дубликаты из Spotify (с подтверждением)\n"
        "/clean_ym_dupes - Удалить дубликаты из Яндекс Музыки (с подтверждением)\n"
        "/last_sync - Информация о последней синхронизации\n"
        "/logs [n] - Последние n строк лога (по умолчанию 20)\n"
        "/health - Проверка доступности API\n"
        "/add_mapping <ym_id> <sp_id> - Ручное сопоставление треков"
        "\n/like_playlist <ссылка> - Лайкнуть все треки из плейлиста Яндекс Музыки"
        "\n/jev - Настроить проверку совпадений Jev"
        "\n/matching [hybrid|jev_only] - Режим сопоставления"
        "\n/revalidate - Переоценить сохранённые сопоставления"
    )


@dp.message(Command("jev"))
async def jev_handler(message: Message):
    """Configure optional Jev verification without exposing the key in replies."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    parts = (message.text or "").split(maxsplit=2)
    action = parts[1].lower() if len(parts) > 1 else "status"
    if action in ("typesafe", "openrouter", "vercel"):
        if len(parts) != 3:
            return await message.answer("Использование: /jev typesafe|openrouter|vercel <API-ключ>")
        try:
            await message.delete()
            deleted = True
        except Exception:
            deleted = False
        if message.chat.type != "private":
            return await message.answer("Настройте Jev в личном чате с ботом." + (" Удалите сообщение с ключом вручную." if not deleted else ""))
        try:
            await asyncio.get_running_loop().run_in_executor(None, configure_jev, action, parts[2])
        except (ValueError, RuntimeError) as exc:
            return await message.answer(f"❌ {exc}" + (" Удалите сообщение с ключом вручную." if not deleted else ""))
        return await message.answer(f"✅ Jev включён через {action}." + (" Удалите сообщение с ключом вручную." if not deleted else ""))
    if action in ("on", "off"):
        try:
            await asyncio.get_running_loop().run_in_executor(None, set_jev_enabled, action == "on")
        except (ValueError, RuntimeError) as exc:
            return await message.answer(f"❌ {exc}")
    elif action != "status":
        return await message.answer("Использование: /jev [status|on|off|typesafe <ключ>|openrouter <ключ>|vercel <ключ>]")
    status = get_jev_status()
    await message.answer(
        f"Jev: {'включён' if status['enabled'] else 'выключен'}; "
        f"провайдер: {status['provider'] or 'не выбран'}; "
        f"ключ: {'сохранён' if status['configured'] else 'не задан'}; "
        f"режим: {status['mode']}.\n"
        "Настройка: /jev typesafe|openrouter|vercel <ключ>; управление: /jev on, /jev off."
    )


@dp.message(Command("matching"))
async def matching_handler(message: Message):
    if message.from_user.id != TG_ADMIN_ID:
        return
    args = (message.text or "").split()
    if len(args) == 1:
        return await message.answer(f"Режим: {get_jev_status()['mode']}. /matching hybrid|jev_only")
    if len(args) != 2:
        return await message.answer("Использование: /matching hybrid|jev_only")
    try:
        await asyncio.get_running_loop().run_in_executor(None, set_matching_mode, args[1])
    except (ValueError, RuntimeError) as error:
        return await message.answer(f"❌ {error}")
    await message.answer(f"Режим сопоставления: {args[1]}. Сохранённые пары будут переоценены при синхронизации.")


@dp.message(Command("revalidate"))
async def revalidate_handler(message: Message):
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Переоцениваю сохранённые пары...")
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, revalidate_mappings)
    except Exception as error:
        return await message.answer(f"❌ {explain_error(error)}")
    await message.answer(f"✅ {result}\nПроверьте /pending")


@dp.message(Command("sync_all", "sync", "sync_ym_sp", "sync_sp_ym"))
async def sync_handler(message: Message, command: CommandObject):
    """Run the requested synchronization direction."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    if is_sync_running():
        return await message.answer("⏳ Синхронизация уже выполняется. Подождите.")
    operation, direction = {
        "sync": (full_two_way_sync, "в обе стороны"),
        "sync_all": (full_two_way_sync, "в обе стороны"),
        "sync_ym_sp": (sync_ym_to_sp, "Яндекс → Spotify"),
        "sync_sp_ym": (sync_sp_to_ym, "Spotify → Яндекс"),
    }[command.command]
    await message.answer(f"🔄 Начинаю синхронизацию {direction}...")
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, operation)
        await message.answer(f"✅ Готово:\n{res}\nОдобрения: /pending")
    except Exception as e:
        logging.error(f"Ошибка синхронизации {direction}: {e}")
        await message.answer(f"❌ Ошибка: {explain_error(e)}")


@dp.message(Command("status"))
async def status_handler(message: Message):
    """Display synchronization database statistics."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    stats = get_status_stats()
    await message.answer(
        f"📊 Статистика:\n"
        f"🔗 Сопоставлено треков: {stats['mappings']}\n"
        f"⏳ Ожидают одобрения: {stats['pending']}\n"
        f"❌ Не найдено: {stats['failed']}"
    )


def pending_card(key: str, entry: dict) -> tuple[str, InlineKeyboardMarkup]:
    arrow = "🟡 YM → SP" if entry["direction"] == "ym_to_sp" else "🔵 SP → YM"
    score_label = (f"Jev: {entry['jev_probability']:.1%}; " if entry.get('jev_probability') is not None else "")
    score_label += (f"Ранг метаданных: {entry['metadata_rank']:.1f}/100" if entry.get('metadata_rank') is not None
                    else "Старая оценка: источник не сохранён")
    reason_labels = {
        "version_markers_differ": "разные версии записи",
        "artists_differ": "разный состав артистов",
        "isrc_conflict": "разные ISRC",
        "isrc_equal": "одинаковый ISRC",
        "duration_conflict": "большая разница длительности",
        "duration_differs": "разница длительности",
        "title_equal": "названия совпали",
        "jev_rejected": "Jev отверг пару",
        "jev_close_candidates": "несколько близких кандидатов",
        "mapping_conflict": "ID уже связан с другой парой",
        "censorship_conflict": "несовместимая цензура или clean/radio версия",
        "uncensored_preferred": "выбрана версия без цензуры",
        "censored_candidates_excluded": "clean/radio кандидаты исключены",
        "no_eligible_candidate": "нет подходящей версии",
    }
    details = ", ".join(reason_labels.get(reason, reason) for reason in
                        (entry.get('reasons') or ())) or "нет подробностей"
    choices = (entry.get('diagnostics') or [])[:5]
    candidates = "\n".join(
        f"{'✅' if item['id'] == entry.get('found_id') else '•'} {index}. "
        f"{item.get('label', item['id'])[:180]}: ранг {item['rank']:.0f}"
        + (f", Jev {item['jev']:.1%}" if item.get('jev') is not None else "")
        + (", ⛔ несовместимая цензура/версия" if "censorship_conflict" in item.get('reasons', []) else "")
        for index, item in enumerate(choices, 1)
    )
    text = (
        f"⏳ {'Проверка старой пары' if entry.get('purpose') == 'revalidate' else 'Одобрение'}\n"
        f"Режим: {entry.get('mode') or 'legacy'}; {score_label}\nПричины: {details}\n"
        f"{arrow}\n\n"
        f"🔍 Искали: {entry['source'][:700]}\n"
        f"📀 Выбран: {entry['found'][:700]}"
        + (f"\nКандидаты:\n{candidates}" if candidates else "")
    )
    candidate_buttons = [
        [InlineKeyboardButton(
            text=f"{'✅' if item['id'] == entry.get('found_id') else 'Выбрать'} {index}. "
                 f"{item.get('label', item['id'])[:70]}",
            callback_data=f"select:{key}:{item['id']}")]
        for index, item in enumerate(choices, 1) if len(choices) > 1
    ]
    approve_data = f"approve:{key}"
    if entry.get('found_id'):
        approve_data += f":{entry['found_id']}"
    kb = InlineKeyboardMarkup(inline_keyboard=candidate_buttons + [
        [
            InlineKeyboardButton(
                text="✅ Подтвердить" if entry.get('purpose') == 'revalidate' else "✅ Добавить",
                callback_data=approve_data),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{key}"),
        ]
    ])
    return text, kb


@dp.message(Command("pending"))
async def pending_handler(message: Message):
    """List all tracks currently waiting for user manual match approval."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    pending = get_pending_tracks()
    if not pending:
        return await message.answer("Нет треков на одобрении.")
    
    args = message.text.split()[1:]
    if args and (not args[0].isascii() or not args[0].isdigit() or len(args[0]) > 6 or int(args[0]) < 1):
        return await message.answer("Использование: /pending [номер страницы от 1]")
    page = int(args[0]) if args else 1
    pages = (len(pending) + 4) // 5
    if page > pages:
        return await message.answer(f"Всего страниц: {pages}. Начать: /pending")
    for key, entry in list(pending.items())[(page - 1) * 5:page * 5]:
        text, kb = pending_card(key, entry)
        await message.answer(text, reply_markup=kb)
    await message.answer(f"Страница {page}/{pages}. Всего: {len(pending)}.\n"
                         + (f"Далее: /pending {page + 1}\n" if page < pages else "")
                         + "После решений очередь сдвигается — обновить: /pending")


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


@dp.message(Command("clean_sp_dupes"))
async def clean_sp_dupes_handler(message: Message):
    """Remove duplicate tracks from Spotify saved tracks."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    if message.text.split()[1:] != ["confirm"]:
        return await message.answer("⚠️ Удаление лайков по похожим названиям необратимо и может ошибаться.\nПодтвердить: /clean_sp_dupes confirm")
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
    if message.text.split()[1:] != ["confirm"]:
        return await message.answer("⚠️ Удаление лайков по похожим названиям необратимо и может ошибаться.\nПодтвердить: /clean_ym_dupes confirm")
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
    if args:
        if not args[0].isascii() or not args[0].isdigit() or len(args[0]) > 3 or not 1 <= int(args[0]) <= 100:
            return await message.answer("Использование: /logs [число от 1 до 100]")
        n = int(args[0])
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
    try:
        add_manual_mapping(ym_id, sp_id)
    except ValueError as error:
        return await message.answer(f"❌ {error}")
    await message.answer(f"🔗 Добавлен маппинг: YM {ym_id} ↔ SP {sp_id}")


@dp.message(Command("like_playlist"))
async def like_playlist_handler(message: Message):
    """Like all tracks in the supplied Yandex Music playlist."""
    if message.from_user.id != TG_ADMIN_ID:
        return
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        return await message.answer("Использование: /like_playlist <ссылка на плейлист Яндекс Музыки>")
    await message.answer("🔄 Добавляю треки в «Мне нравится»...")
    try:
        result = await asyncio.get_running_loop().run_in_executor(None, like_playlist_tracks, args[1])
        await message.answer(f"✅ {result}")
    except Exception as error:
        await message.answer(f"❌ {explain_error(error)}")


@dp.callback_query(F.data.startswith("select:"))
async def select_candidate_callback(callback: CallbackQuery):
    if callback.from_user.id != TG_ADMIN_ID:
        return await callback.answer("Нет доступа")
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        return await callback.answer("Некорректный кандидат", show_alert=True)
    pend_key = ":".join(parts[1:3])
    await callback.answer("Выбираю…")
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, select_pending_candidate, pend_key, parts[3])
    if not ok:
        return await callback.message.answer(f"⚠️ {msg}")
    entry = get_pending_tracks().get(pend_key)
    if not entry:
        return await callback.message.answer("Трек уже обработан. Обновите /pending")
    text, keyboard = pending_card(pend_key, entry)
    if text != callback.message.text or keyboard != callback.message.reply_markup:
        await callback.message.edit_text(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("approve:"))
async def approve_callback(callback: CallbackQuery):
    """Handle the inline callback query to approve a pending track match."""
    if callback.from_user.id != TG_ADMIN_ID:
        return await callback.answer("Нет доступа")
    
    parts = callback.data.split(":", 3)
    if len(parts) != 4:
        return await callback.answer("Обновите /pending, чтобы одобрить выбранного кандидата", show_alert=True)
    pend_key = ":".join(parts[1:3])
    expected_id = parts[3]
    await callback.answer("Обрабатываю…")
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, approve_pending, pend_key, expected_id)
    
    if ok:
        await callback.message.edit_text(f"✅ {msg}")
    else:
        await callback.message.answer(f"⚠️ {msg}")


@dp.callback_query(F.data.startswith("reject:"))
async def reject_callback(callback: CallbackQuery):
    """Handle the inline callback query to reject a pending track match."""
    if callback.from_user.id != TG_ADMIN_ID:
        return await callback.answer("Нет доступа")
    
    pend_key = callback.data.split(":", 1)[1]
    await callback.answer("Обрабатываю…")
    loop = asyncio.get_running_loop()
    ok, msg = await loop.run_in_executor(None, reject_pending, pend_key)
    
    if ok:
        await callback.message.edit_text(f"❌ {msg}")
    else:
        await callback.message.answer(f"⚠️ {msg}")


@dp.errors()
async def handle_error(event: ErrorEvent):
    logging.error("Ошибка команды", exc_info=event.exception)
    message = event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
    user = event.update.message.from_user if event.update.message else (event.update.callback_query.from_user if event.update.callback_query else None)
    if message and user and user.id == TG_ADMIN_ID:
        await message.answer(f"⚠️ {explain_error(event.exception)}\n/help")
    return True


@dp.message(F.text.startswith("/"))
async def unknown_command(message: Message):
    if message.from_user.id == TG_ADMIN_ID:
        await message.answer("Неизвестная команда. Синхронизация: /sync. Все команды: /help")


async def main():
    """Start the scheduler for periodic sync and start Telegram bot polling."""
    await bot.set_my_commands([BotCommand(command=command, description=description) for command, description in [
        ("sync", "Синхронизировать в обе стороны"), ("pending", "Одобрить совпадения"),
        ("like_playlist", "Лайкнуть треки из плейлиста"), ("status", "Статистика"),
        ("last_sync", "Последняя синхронизация"), ("jev", "Проверка совпадений Jev"),
        ("matching", "Режим сопоставления"), ("revalidate", "Переоценить пары"),
        ("help", "Все команды")]])
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(periodic_sync, 'interval', hours=3)
    scheduler.start()
    logging.info("Планировщик запущен. Интервал синхронизации: 3 часа.")
        
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as error:
        sys.exit(explain_error(error))

import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from logging.handlers import TimedRotatingFileHandler

from config import TG_BOT_TOKEN, TG_ADMIN_ID
from sync_logic import sync_ym_to_sp, sync_sp_to_ym, full_two_way_sync

# Настраиваем логгер, который будет ротироваться каждые 24 часа (в полночь) и хранить 1 бэкап
log_handler = TimedRotatingFileHandler('sync.log', when='midnight', interval=1, backupCount=1)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler()])

if not TG_BOT_TOKEN:
    raise ValueError("TG_BOT_TOKEN is not set in .env")

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher()

def get_log_cb(loop):
    def cb(msg):
        if TG_ADMIN_ID:
            asyncio.run_coroutine_threadsafe(bot.send_message(TG_ADMIN_ID, f"⚠️ Ошибка поиска: {msg}"), loop)
    return cb

async def periodic_sync():
    try:
        loop = asyncio.get_running_loop()
        res = await loop.run_in_executor(None, full_two_way_sync, get_log_cb(loop))
        logging.info(f"Фоновая синхронизация завершена:\n{res}")
    except Exception as e:
        logging.error(f"Ошибка фоновой синхронизации: {e}")

@dp.message(Command("start"))
async def start_handler(message: Message):
    if message.from_user.id != TG_ADMIN_ID:
        return await message.answer("Ты кто такой? Я тебя не звал.")
    await message.answer("Бот синхронизации запущен. Команды:\n/sync_all - Полная синхронизация (в обе стороны)\n/sync_ym_sp - Яндекс -> Spotify\n/sync_sp_ym - Spotify -> Яндекс")

@dp.message(Command("sync_all"))
async def sync_all_handler(message: Message):
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Начинаю полную синхронизацию...")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, full_two_way_sync, get_log_cb(loop))
    await message.answer(f"✅ Готово:\n{res}")

@dp.message(Command("sync_ym_sp"))
async def sync_ym_sp_handler(message: Message):
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Начинаю синхронизацию Яндекс -> Spotify...")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, sync_ym_to_sp, get_log_cb(loop))
    await message.answer(f"✅ Готово:\n{res}")

@dp.message(Command("sync_sp_ym"))
async def sync_sp_ym_handler(message: Message):
    if message.from_user.id != TG_ADMIN_ID:
        return
    await message.answer("🔄 Начинаю синхронизацию Spotify -> Яндекс...")
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(None, sync_sp_to_ym, get_log_cb(loop))
    await message.answer(f"✅ Готово:\n{res}")

async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(periodic_sync, 'interval', minutes=10)
    scheduler.start()
        
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

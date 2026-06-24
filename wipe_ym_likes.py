"""Скрипт для полного удаления лайкнутых треков из Яндекс Музыки и очистки БД."""

import sys
import logging
import sqlite3
from yandex_music import Client
from config import YANDEX_MUSIC_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)


def wipe_yandex_music_likes():
    """Получает все лайкнутые треки из Яндекс Музыки, удаляет их и чистит таблицы БД."""
    if not YANDEX_MUSIC_TOKEN:
        logging.error("YANDEX_MUSIC_TOKEN не задан в .env")
        sys.exit(1)

    logging.info("Подключение к API Яндекс Музыки...")
    ym_client = Client(YANDEX_MUSIC_TOKEN).init()

    logging.info("Получение списка лайкнутых треков...")
    likes = ym_client.users_likes_tracks()
    if not likes or not likes.tracks:
        logging.info("Лайкнутые треки в Яндекс Музыке не найдены.")
        track_ids = []
    else:
        track_ids = [t.id for t in likes.tracks]
        logging.info(f"Найдено {len(track_ids)} лайкнутых треков.")

    if track_ids:
        logging.info("Удаление лайкнутых треков из Яндекс Музыки...")
        chunk_size = 100
        for i in range(0, len(track_ids), chunk_size):
            chunk = track_ids[i:i + chunk_size]
            batch_num = i // chunk_size + 1
            logging.info(f"Удаление пакета {batch_num} ({len(chunk)} треков)...")
            ym_client.users_likes_tracks_remove(chunk)
        logging.info("Все лайкнутые треки удалены из Яндекс Музыки.")

    logging.info("Очистка таблиц базы данных SQLite...")
    conn = sqlite3.connect("sync_data.db")
    cursor = conn.cursor()

    tables = ["yandex_cache", "mappings", "failed_syncs", "pending_syncs"]
    for table in tables:
        cursor.execute(f"DELETE FROM {table}")
        logging.info(f"Очищена таблица '{table}'.")

    conn.commit()
    conn.close()
    logging.info("Таблицы базы данных очищены.")


if __name__ == "__main__":
    wipe_yandex_music_likes()

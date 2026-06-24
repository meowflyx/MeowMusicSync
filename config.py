import os
from dotenv import load_dotenv

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

_raw_admin_id = os.getenv("TG_ADMIN_ID", "").strip()
TG_ADMIN_ID = int(_raw_admin_id) if _raw_admin_id.isdigit() else 0

SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")

YANDEX_MUSIC_TOKEN = os.getenv("YANDEX_MUSIC_TOKEN")

import os
from urllib.parse import urlsplit
from dotenv import load_dotenv

load_dotenv()

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN")

_raw_admin_id = os.getenv("TG_ADMIN_ID", "").strip()
TG_ADMIN_ID = int(_raw_admin_id) if _raw_admin_id.isdigit() else 0

SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID")
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET")
SPOTIPY_REDIRECT_URI = os.getenv("SPOTIPY_REDIRECT_URI")

YANDEX_MUSIC_TOKEN = os.getenv("YANDEX_MUSIC_TOKEN")


def validate_spotify_config(client_id, client_secret, redirect_uri):
    values = dict(SPOTIPY_CLIENT_ID=client_id, SPOTIPY_CLIENT_SECRET=client_secret,
                  SPOTIPY_REDIRECT_URI=redirect_uri)
    missing = [key for key, value in values.items()
               if not value or not value.strip() or value.startswith("your_")]
    if missing:
        raise ValueError("Заполните в .env: " + ", ".join(missing))
    if any(value != value.strip() for value in values.values()):
        raise ValueError("Удалите пробелы по краям значений SPOTIPY_* в .env.")
    uri = urlsplit(redirect_uri)
    if not uri.hostname or uri.fragment or uri.query or not (
        uri.scheme == "https" or uri.scheme == "http" and uri.hostname in ("127.0.0.1", "::1")
    ):
        raise ValueError("SPOTIPY_REDIRECT_URI: используйте http://127.0.0.1:8888/callback и такой же адрес в настройках приложения Spotify.")

"""Module for music synchronization logic between Yandex Music and Spotify.

Uses an SQLite database for reliable, transaction-safe storage of track caches,
failed attempts, pending approvals, and cross-platform track ID mappings.
Optimized to run check operations using fast set lookups and instant mapping shortcuts.
"""

import logging
import json
import os
import re
import unicodedata
import time
import sqlite3
from yandex_music import Client
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from fuzzywuzzy import fuzz
from config import (
    SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI,
    YANDEX_MUSIC_TOKEN
)

DB_FILE = "sync_data.db"

TRANSLIT_MAP = {
    "korol i shut": "Король и Шут",
    "sektor gaza": "Сектор Газа",
    "kino": "Кино",
    "splean": "Сплин",
    "gradusy": "Градусы",
    "alyans": "Альянс",
    "molchat doma": "Молчат Дома",
    "valentin strykalo": "Валентин Стрыкало",
    "agatha christie": "Агата Кристи",
    "katya lel": "Катя Лель",
    "zveri": "Звери",
    "5'nizza": "5'nizza",
    "maksim leonidov": "Максим Леонидов",
    "mark bernes": "Марк Бернес",
    "andrey gubin": "Андрей Губин",
    "mukka": "МУККА",
    "eurythmics": "Eurythmics",
    "bi-2": "Би-2",
    "nogu svelo": "Ногу Свело",
    "leningrad": "Ленинград",
    "basta": "Баста",
    "oxxxymiron": "Oxxxymiron",
    "ssshhhiiittt!!!": "Ssshhhiiittt!!!",
    "monetochka": "Монеточка",
    "tatu": "Тату",
    "zemfira": "Земфира",
    "sekret": "Секрет",
    "mashina vremeni": "Машина Времени",
    "nautilus pompilius": "Наутилус Помпилиус",
    "ddt": "ДДТ",
    "aquarium": "Аквариум",
    "mumi troll": "Мумий Тролль",
    "seb lowe": "Seb Lowe",
    "lumen": "Lumen",
    "slot": "Слот",
    "aria": "Ария",
    "piknik": "Пикник",
    "chizh": "Чиж",
    "chizh & co": "Чиж & Co",
    "lyapis trubetskoy": "Ляпис Трубецкой",
    "grazhdanskaya oborona": "Гражданская Оборона",
    "sansara": "Сансара",
    "nervy": "Нервы",
    "noize mc": "Noize MC",
    "pornofilmy": "Порнофильмы",
    "lsp": "ЛСП",
    "morgenshtern": "MORGENSHTERN",
    "mayot": "MAYOT",
    "obladaet": "OBLADAET",
    "pharaoh": "PHARAOH",
    "face": "Face",
    "scriptonite": "Скриптонит",
    "miyagi": "Miyagi",
    "jony": "JONY",
    "hammali & navai": "HammAli & Navai",
    "egor kreed": "Егор Крид",
    "max korzh": "Макс Корж",
    "tsoi": "Цой",
    "viktor tsoi": "Виктор Цой",
}

LATIN_TO_CYRILLIC = [
    ("shch", "щ"), ("sch", "щ"),
    ("zh", "ж"), ("ch", "ч"), ("sh", "ш"),
    ("ya", "я"), ("yu", "ю"), ("yo", "ё"), ("ye", "е"),
    ("ts", "ц"), ("kh", "х"),
    ("a", "а"), ("b", "б"), ("v", "в"), ("g", "г"), ("d", "д"),
    ("e", "е"), ("z", "з"), ("i", "и"), ("j", "й"),
    ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"),
    ("p", "п"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"),
    ("f", "ф"), ("h", "х"), ("c", "ц"), ("w", "в"),
    ("y", "ы"), ("x", "кс"),
    ("'", "ь"), ("\'", "ь"),
]

REMASTER_PATTERN = re.compile(
    r'\s*[-–—]\s*'
    r'(?:\d{4}\s*[-–—]?\s*)?'
    r'(?:remaster(?:ed)?|remastered version|deluxe|bonus track|'
    r'anniversary|edition|version|mix|mono|stereo|original|single|ep)'
    r'(?:\s*\d{4})?'
    r'\s*$',
    re.IGNORECASE
)

FEAT_PATTERN = re.compile(
    r'\s*[\(\[]\s*(?:feat\.?|ft\.?|with|prod\.?|prod\s+by)\s+[^\)\]]+[\)\]]',
    re.IGNORECASE
)

SUFFIX_PATTERN = re.compile(
    r'\s*[-–—]\s*'
    r'(?:[A-Za-z0-9\s]+(?:version|edit|mix|remix))'
    r'\s*$',
    re.IGNORECASE
)

SOUNDTRACK_PATTERN = re.compile(r'\s*[-–—]\s*(?:from\s+.*)?soundtrack.*', re.IGNORECASE)
SOUNDTRACK_PAREN_PATTERN = re.compile(r'\s*[\(\[]\s*(?:from\s+.*)?soundtrack.*[\)\]]', re.IGNORECASE)

SCORE_AUTO_ACCEPT = 93
SCORE_PENDING = 70


def init_db():
    """Initialize SQLite database tables and automatically migrate data from manifest.json if present."""
    conn = sqlite3.connect(DB_FILE)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS yandex_cache (
            id TEXT PRIMARY KEY,
            artists TEXT,
            title TEXT,
            query TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS spotify_cache (
            id TEXT PRIMARY KEY,
            artists TEXT,
            title TEXT,
            query TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS failed_syncs (
            key TEXT PRIMARY KEY,
            query TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_syncs (
            key TEXT PRIMARY KEY,
            direction TEXT,
            source TEXT,
            found TEXT,
            found_id TEXT,
            score INTEGER
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS mappings (
            ym_id TEXT UNIQUE,
            sp_id TEXT UNIQUE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            ym_id TEXT,
            sp_id TEXT,
            artists TEXT,
            title TEXT,
            PRIMARY KEY (ym_id, sp_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    
    if os.path.exists("manifest.json"):
        logging.info("Обнаружен старый файл manifest.json. Начинаю миграцию в SQLite...")
        try:
            with open("manifest.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                
            yandex = data.get("yandex", {})
            for y_id, info in yandex.items():
                if isinstance(info, str):
                    artists, title = parse_query(info)
                    query = info
                else:
                    artists = info.get("artists", "")
                    title = info.get("title", "")
                    query = info.get("query", "")
                cursor.execute(
                    "INSERT OR IGNORE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)",
                    (str(y_id), artists, title, query)
                )
                
            spotify = data.get("spotify", {})
            for s_id, info in spotify.items():
                if isinstance(info, str):
                    artists, title = parse_query(info)
                    query = info
                else:
                    artists = info.get("artists", "")
                    title = info.get("title", "")
                    query = info.get("query", "")
                cursor.execute(
                    "INSERT OR IGNORE INTO spotify_cache (id, artists, title, query) VALUES (?, ?, ?, ?)",
                    (str(s_id), artists, title, query)
                )
                
            failed = data.get("failed", {})
            for key, query in failed.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO failed_syncs (key, query) VALUES (?, ?)",
                    (str(key), query)
                )
                
            pending = data.get("pending", {})
            for key, entry in pending.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO pending_syncs (key, direction, source, found, found_id, score) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(key), entry.get("direction", ""), entry.get("source", ""), entry.get("found", ""), entry.get("found_id", ""), entry.get("score", 0))
                )
                
            mappings = data.get("mappings", {})
            ym_to_sp = mappings.get("ym_to_sp", {})
            for ym_id, sp_id in ym_to_sp.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)",
                    (str(ym_id), str(sp_id))
                )
            
            conn.commit()
            logging.info("Миграция в SQLite успешно завершена.")
            os.rename("manifest.json", "manifest.json.bak")
            logging.info("Файл manifest.json переименован в manifest.json.bak")
        except Exception as e:
            logging.error(f"Ошибка при миграции манифеста в SQLite: {e}")
            
    conn.close()


def get_status_stats():
    """Retrieve statistics from the SQLite database."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM yandex_cache")
    yandex_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM spotify_cache")
    spotify_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM mappings")
    mappings_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM pending_syncs")
    pending_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM failed_syncs")
    failed_count = cursor.fetchone()[0]
    
    conn.close()
    return {
        "yandex": yandex_count,
        "spotify": spotify_count,
        "mappings": mappings_count,
        "pending": pending_count,
        "failed": failed_count
    }


def get_pending_tracks():
    """Retrieve all pending review items from the database."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT key, direction, source, found, found_id, score FROM pending_syncs")
    rows = cursor.fetchall()
    conn.close()
    return {
        r[0]: {
            "direction": r[1],
            "source": r[2],
            "found": r[3],
            "found_id": r[4],
            "score": r[5]
        } for r in rows
    }


def clear_failed_tracks():
    """Delete all records from failed_syncs table and return the count of deleted items."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM failed_syncs")
    count = cursor.fetchone()[0]
    cursor.execute("DELETE FROM failed_syncs")
    conn.commit()
    conn.close()
    return count


def get_failed_tracks():
    """Retrieve all failed tracks from the database."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT key, query FROM failed_syncs")
    rows = cursor.fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


def is_blacklisted(ym_id=None, sp_id=None):
    """Check if a track is in the blacklist by exact (ym_id, sp_id) pair or by single ID."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if ym_id and sp_id:
        cursor.execute(
            "SELECT 1 FROM blacklist WHERE ym_id = ? AND sp_id = ?",
            (str(ym_id), str(sp_id))
        )
    elif ym_id:
        cursor.execute("SELECT 1 FROM blacklist WHERE ym_id = ?", (str(ym_id),))
    elif sp_id:
        cursor.execute("SELECT 1 FROM blacklist WHERE sp_id = ?", (str(sp_id),))
    else:
        conn.close()
        return False
    row = cursor.fetchone()
    conn.close()
    return row is not None


def get_setting(key, default=None):
    """Retrieve a value from the settings table."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key, value):
    """Insert or update a value in the settings table."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value))
    )
    conn.commit()
    conn.close()


def get_last_sync_info():
    """Return dict with last sync timestamp and result string."""
    return {
        "timestamp": get_setting("last_sync_timestamp"),
        "result": get_setting("last_sync_result"),
    }


def is_sync_running():
    """Check if a sync is currently in progress."""
    try:
        return get_setting("sync_in_progress") == "1"
    except Exception:
        return False


def remove_from_blacklist(ym_id=None, sp_id=None):
    """Remove tracks from blacklist by ym_id and/or sp_id. Returns count of removed rows."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    if ym_id and sp_id:
        cursor.execute(
            "DELETE FROM blacklist WHERE ym_id = ? AND sp_id = ?",
            (str(ym_id), str(sp_id))
        )
    elif ym_id:
        cursor.execute("DELETE FROM blacklist WHERE ym_id = ?", (str(ym_id),))
    elif sp_id:
        cursor.execute("DELETE FROM blacklist WHERE sp_id = ?", (str(sp_id),))
    else:
        conn.close()
        return 0
    count = cursor.rowcount
    conn.commit()
    conn.close()
    return count


def add_manual_mapping(ym_id, sp_id):
    """Manually link a Yandex Music track ID to a Spotify track ID."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO mappings (ym_id, sp_id) VALUES (?, ?)",
        (str(ym_id), str(sp_id))
    )
    conn.commit()
    conn.close()


def get_recent_logs(n=20):
    """Read the last n lines from the sync log file."""
    log_path = "sync.log"
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-n:]]
    except Exception:
        return []


def check_api_health():
    """Test connectivity to Yandex Music and Spotify APIs. Returns dict of statuses."""
    result = {"yandex": False, "spotify": False, "yandex_error": None, "spotify_error": None}
    
    ym_client = get_ym_client()
    if ym_client:
        try:
            ym_client.users_likes_tracks()
            result["yandex"] = True
        except Exception as e:
            result["yandex_error"] = str(e)
    else:
        result["yandex_error"] = "Токен не настроен"
    
    sp_client = get_sp_client()
    if sp_client:
        try:
            sp_client.current_user()
            result["spotify"] = True
        except Exception as e:
            result["spotify_error"] = str(e)
    else:
        result["spotify_error"] = "Учётные данные не настроены"
    
    return result


def add_to_blacklist(ym_id, sp_id, artists, title):
    """Add a track to the blacklist table."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO blacklist (ym_id, sp_id, artists, title) VALUES (?, ?, ?, ?)",
        (str(ym_id), str(sp_id), artists, title)
    )
    conn.commit()
    conn.close()


def get_blacklist():
    """Retrieve all tracks from the blacklist."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT ym_id, sp_id, artists, title FROM blacklist")
    rows = cursor.fetchall()
    conn.close()
    return [{"ym_id": r[0], "sp_id": r[1], "artists": r[2], "title": r[3]} for r in rows]


def clear_blacklist():
    """Clear all tracks from the blacklist and return the count."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM blacklist")
    count = cursor.fetchone()[0]
    cursor.execute("DELETE FROM blacklist")
    conn.commit()
    conn.close()
    return count


def translit_to_cyrillic(text):
    """Perform character-by-character Latin to Cyrillic transliteration fallback."""
    if any(ord(c) > 127 for c in text):
        return text
    result = text.lower()
    for lat, cyr in LATIN_TO_CYRILLIC:
        result = result.replace(lat, cyr)
    return result


def has_latin(text):
    """Check if the text contains any latin characters."""
    return any('a' <= c.lower() <= 'z' for c in text)


def get_ym_client():
    """Initialize and return the Yandex Music API client."""
    if not YANDEX_MUSIC_TOKEN:
        return None
    return Client(YANDEX_MUSIC_TOKEN).init()


def get_sp_client():
    """Initialize and return the Spotify API client using OAuth."""
    if not SPOTIPY_CLIENT_ID or not SPOTIPY_CLIENT_SECRET or not SPOTIPY_REDIRECT_URI:
        return None
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope="user-library-read user-library-modify",
        open_browser=False
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def api_call_with_retry(func, *args, max_retries=3, base_delay=2, **kwargs):
    """Call an API function with exponential backoff retry on transient failures."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs), None
        except Exception as e:
            if attempt == max_retries - 1:
                return None, e
            delay = base_delay * (2 ** attempt)
            logging.warning(f"API вызов не удался (попытка {attempt + 1}/{max_retries}): {e}. Повтор через {delay}с")
            time.sleep(delay)
    logging.error(f"Исчерпаны все попытки ({max_retries}) для {func.__name__}")
    return None, RuntimeError(f"Не удалось выполнить {func.__name__} после {max_retries} попыток")


def clean_title(title):
    """Remove remaster, features, and other auxiliary suffixes from track titles."""
    title = REMASTER_PATTERN.sub('', title)
    title = FEAT_PATTERN.sub('', title)
    title = SUFFIX_PATTERN.sub('', title)
    title = title.strip(' -–—')
    return title


def normalize(text):
    """Normalize string case, remove accents/diacritics, and strip punctuation."""
    text = text.lower().strip()
    text = unicodedata.normalize('NFKD', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def translate_artist(artist_name):
    """Translate latinized Russian artist name to Cyrillic using maps or transliteration."""
    key = artist_name.lower().strip()
    if key in TRANSLIT_MAP:
        return TRANSLIT_MAP[key]
    if has_latin(artist_name):
        return translit_to_cyrillic(artist_name)
    return artist_name


def build_search_queries(artists_str, title):
    """Build a prioritized list of fallback search queries based on variations of artist translation."""
    clean = clean_title(title)
    artists_list = [a.strip() for a in artists_str.split(',')]
    
    map_translated = [TRANSLIT_MAP.get(a.lower().strip(), a) for a in artists_list]
    map_str = ", ".join(map_translated)
    
    char_translated = [translit_to_cyrillic(a) if has_latin(a) else a for a in artists_list]
    char_str = ", ".join(char_translated)
    
    queries = []
    
    queries.append(f"{map_str} {clean}")
    queries.append(f"{map_translated[0]} {clean}")
    
    if char_str.lower() != map_str.lower():
        queries.append(f"{char_str} {clean}")
        queries.append(f"{char_translated[0]} {clean}")
    
    if artists_str.lower() != map_str.lower():
        queries.append(f"{artists_str} {clean}")
    
    if len(clean) > 3:
        queries.append(clean)
    
    seen = set()
    unique = []
    for q in queries:
        norm = normalize(q)
        if norm not in seen:
            seen.add(norm)
            unique.append(q)
    
    return unique


def score_match(query_artists, query_title, result_artists, result_title):
    """Calculate similarity score between search query details and search result metadata."""
    q_title_clean = normalize(clean_title(query_title))
    r_title_clean = normalize(clean_title(result_title))
    
    title_ratio = fuzz.ratio(q_title_clean, r_title_clean)
    title_sort = fuzz.token_sort_ratio(q_title_clean, r_title_clean)
    title_score = max(title_ratio, title_sort)
    
    q_artists_raw = [a.strip() for a in query_artists.split(',')]
    r_artists_raw = [a.strip() for a in result_artists.split(',')]
    
    q_forms = []
    for a in q_artists_raw:
        key = a.lower().strip()
        q_forms.append(normalize(a))
        mapped = TRANSLIT_MAP.get(key)
        if mapped:
            q_forms.append(normalize(mapped))
        if has_latin(a):
            q_forms.append(normalize(translit_to_cyrillic(a)))
    
    r_forms = []
    for a in r_artists_raw:
        key = a.lower().strip()
        r_forms.append(normalize(a))
        mapped = TRANSLIT_MAP.get(key)
        if mapped:
            r_forms.append(normalize(mapped))
        if has_latin(a):
            r_forms.append(normalize(translit_to_cyrillic(a)))
    
    artist_score = 0
    for qf in q_forms:
        for rf in r_forms:
            s = fuzz.ratio(qf, rf)
            if s > artist_score:
                artist_score = s

    penalty = 0
    mismatch_words = ["remix", "cover", "live", "acoustic", "instrumental", "karaoke", "hardtekk", "slowed", "sped up", "nightcore"]
    q_full = normalize(f"{query_artists} {query_title}")
    r_full = normalize(f"{result_artists} {result_title}")
    
    for word in mismatch_words:
        if (word in q_full) != (word in r_full):
            penalty += 25

    if artist_score < 50 and title_score > 80:
        penalty += 20

    if title_score < 85 and artist_score >= 80:
        penalty += 15
    
    final = (title_score * 0.6) + (artist_score * 0.4) - penalty
    return max(0, final)


def match_track_spotify(query_artists, query_title, sp_client):
    """Search for the track on Spotify and return the best match track ID, clean name, and score."""
    queries = build_search_queries(query_artists, query_title)
    
    best_match = None
    best_score = 0
    best_name = ""
    
    for q in queries:
        results, err = api_call_with_retry(
            sp_client.search, q=q, limit=5, type='track'
        )
        if err:
            logging.warning(f"Поиск в Spotify не удался для '{q}': {err}")
            continue
        if not results['tracks']['items']:
            continue
        
        for t in results['tracks']['items']:
            artists = ", ".join([a['name'] for a in t['artists']])
            title = t['name']
            score = score_match(query_artists, query_title, artists, title)
            if score > best_score:
                best_score = score
                best_match = t['id']
                best_name = f"{artists} {title}"
        
        if best_score >= SCORE_AUTO_ACCEPT:
            break
        time.sleep(1.5)
    
    if best_score >= SCORE_PENDING:
        return best_match, best_name, best_score
    return None, None, best_score


def match_track_yandex(query_artists, query_title, ym_client):
    """Search for the track on Yandex Music and return the best match track ID, clean name, and score."""
    queries = build_search_queries(query_artists, query_title)
    
    best_match = None
    best_score = 0
    best_name = ""
    
    for q in queries:
        results, err = api_call_with_retry(ym_client.search, q, type_='track')
        if err:
            logging.warning(f"Поиск в Яндекс Музыке не удался для '{q}': {err}")
            continue
        if not results.tracks or not results.tracks.results:
            continue
            
        for t in results.tracks.results:
            artists = ", ".join([a.name for a in t.artists]) if t.artists else "Unknown"
            title = t.title or ""
            score = score_match(query_artists, query_title, artists, title)
            if score > best_score:
                best_score = score
                best_match = t.id
                best_name = f"{artists} {title}"
        
        if best_score >= SCORE_AUTO_ACCEPT:
            break
        time.sleep(1.5)
    
    if best_score >= SCORE_PENDING:
        return best_match, best_name, best_score
    return None, None, best_score


def parse_query(search_query):
    """Parse a search query string into simple (artist, title) fallback parts."""
    parts = search_query.split(' ', 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return search_query, ""


def get_ym_likes(ym_client):
    """Retrieve all liked tracks from Yandex Music, utilizing and updating the database cache."""
    tracks_short = ym_client.users_likes_tracks().tracks
    if not tracks_short:
        return []
    
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    res = []
    track_ids_to_fetch = []
    
    for t in tracks_short:
        tid = str(t.id)
        cursor.execute("SELECT artists, title, query FROM yandex_cache WHERE id = ?", (tid,))
        row = cursor.fetchone()
        if row:
            res.append({"id": tid, "artists": row[0], "title": row[1], "search_query": row[2]})
        else:
            fetch_id = f"{t.id}:{t.album_id}" if t.album_id else str(t.id)
            track_ids_to_fetch.append(fetch_id)
            
    if track_ids_to_fetch:
        full_tracks = []
        for i in range(0, len(track_ids_to_fetch), 50):
            chunk = track_ids_to_fetch[i:i+50]
            try:
                full_tracks.extend(ym_client.tracks(chunk))
            except Exception as e:
                logging.warning(f"YM чанк {i} не удался, пробую по одному: {e}")
                for single_id in chunk:
                    try:
                        single = ym_client.tracks([single_id])
                        if single:
                            full_tracks.extend(single)
                    except Exception:
                        logging.error(f"Пропуск проблемного трека YM: {single_id}")
        
        for t in full_tracks:
            if not t:
                continue
            artists = ", ".join([a.name for a in t.artists]) if t.artists else "Unknown"
            title = t.title or ""
            query = f"{artists} {title}"
            tid = str(t.id)
            cursor.execute("INSERT OR REPLACE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (tid, artists, title, query))
            res.append({"id": tid, "artists": artists, "title": title, "search_query": query})
            
        conn.commit()
    conn.close()
    return res


def get_sp_likes(sp_client):
    """Retrieve all saved tracks from Spotify, utilizing and updating the database cache."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    res = []
    results = sp_client.current_user_saved_tracks(limit=50)
    
    while results:
        new_tracks = []
        for item in results['items']:
            track = item['track']
            tid = track['id']
            cursor.execute("SELECT artists, title, query FROM spotify_cache WHERE id = ?", (tid,))
            row = cursor.fetchone()
            if row:
                res.append({"id": tid, "artists": row[0], "title": row[1], "search_query": row[2]})
            else:
                artists = ", ".join([a['name'] for a in track['artists']])
                title = track['name']
                query = f"{artists} {title}"
                new_tracks.append((tid, artists, title, query))
                res.append({"id": tid, "artists": artists, "title": title, "search_query": query})
                
        if new_tracks:
            cursor.executemany("INSERT OR REPLACE INTO spotify_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", new_tracks)
            conn.commit()
            
        if results['next']:
            results = sp_client.next(results)
        else:
            break
            
    conn.close()
    return res


def find_already_present_id(track_artists, track_title, existing_tracks):
    """Find and return the ID of a matching track in the existing tracks list, or None."""
    q_title = normalize(clean_title(track_title))
    q_artists = [normalize(translate_artist(a.strip())) for a in track_artists.split(',')]
    q_main = q_artists[0] if q_artists else ""
    
    for ex in existing_tracks:
        ex_title = normalize(clean_title(ex.get('title', '')))
        ex_artists = [normalize(translate_artist(a.strip())) for a in ex.get('artists', '').split(',')]
        ex_main = ex_artists[0] if ex_artists else ""
        
        title_match = fuzz.ratio(q_title, ex_title) > 85 or fuzz.token_sort_ratio(q_title, ex_title) > 85
        artist_match = fuzz.ratio(q_main, ex_main) > 75
        
        if title_match and artist_match:
            return ex.get('id')
    return None


def sync_ym_to_sp(progress_callback=None, pending_callback=None):
    """Synchronize liked tracks from Yandex Music to Spotify, preventing duplicates."""
    ym_client = get_ym_client()
    sp_client = get_sp_client()
    if not ym_client or not sp_client:
        return "Клиенты не настроены"

    ym_likes = get_ym_likes(ym_client)
    sp_likes = get_sp_likes(sp_client)
    
    sp_liked_ids = {str(ex['id']) for ex in sp_likes}
    
    added = 0
    failed_count = 0
    pending_count = 0
    skipped = 0
    total = len(ym_likes)
    
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT ym_id, sp_id FROM mappings")
    ym_to_sp_map = {r[0]: r[1] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM failed_syncs WHERE key LIKE 'ym_to_sp:%'")
    failed_keys = {r[0] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM pending_syncs WHERE key LIKE 'ym_to_sp:%'")
    pending_keys = {r[0] for r in cursor.fetchall()}
    
    for idx, ym_track in enumerate(ym_likes, 1):
        ym_id = str(ym_track['id'])
        artists = ym_track.get('artists', '')
        title = ym_track.get('title', '')
        query = ym_track.get('search_query', '')
        
        fail_key = f"ym_to_sp:{ym_id}"
        pend_key = fail_key
        
        if is_blacklisted(ym_id=ym_id):
            skipped += 1
            continue
            
        if fail_key in failed_keys or pend_key in pending_keys:
            skipped += 1
            continue
            
        mapped_sp_id = ym_to_sp_map.get(ym_id)
        if mapped_sp_id:
            if mapped_sp_id in sp_liked_ids:
                continue
            add_to_blacklist(ym_id, mapped_sp_id, artists, title)
            cursor.execute("DELETE FROM mappings WHERE ym_id = ?", (ym_id,))
            conn.commit()
            del ym_to_sp_map[ym_id]
            logging.info(f"⚫ [Блеклист] '{artists} - {title}' удалён из Spotify пользователем. Добавлен в блеклист.")
            continue
            
        matched_sp_id = find_already_present_id(artists, title, sp_likes)
        if matched_sp_id:
            cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, matched_sp_id))
            conn.commit()
            ym_to_sp_map[ym_id] = matched_sp_id
            logging.info(f"🔗 [Сопоставление] Трек уже есть в Spotify по нечеткому совпадению: '{query}' -> ID '{matched_sp_id}'")
            continue
            
        sp_id, best_name, score = match_track_spotify(artists, title, sp_client)
        if sp_id:
            if sp_id in sp_liked_ids:
                cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, sp_id))
                conn.commit()
                ym_to_sp_map[ym_id] = sp_id
                logging.info(f"ℹ️ [{score:.0f}%] Трек уже есть в Spotify по ID: '{query}' -> '{best_name}'")
                continue
                
            if score >= SCORE_AUTO_ACCEPT:
                _, err = api_call_with_retry(
                    sp_client.current_user_saved_tracks_add, tracks=[sp_id]
                )
                if err:
                    logging.error(f"Ошибка при добавлении трека в Spotify: {err}")
                    failed_count += 1
                else:
                    cursor.execute("INSERT OR REPLACE INTO spotify_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (sp_id, artists, title, best_name))
                    sp_likes.append({"id": sp_id, "artists": artists, "title": title, "search_query": best_name})
                    sp_liked_ids.add(sp_id)
                    
                    cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, sp_id))
                    conn.commit()
                    ym_to_sp_map[ym_id] = sp_id
                    
                    logging.info(f"✅ [{score:.0f}%] Добавлен в Spotify: '{query}' -> как '{best_name}'")
                    added += 1
            elif score >= SCORE_PENDING:
                cursor.execute(
                    "INSERT OR REPLACE INTO pending_syncs (key, direction, source, found, found_id, score) VALUES (?, ?, ?, ?, ?, ?)",
                    (pend_key, "ym_to_sp", query, best_name, sp_id, round(score))
                )
                conn.commit()
                pending_keys.add(pend_key)
                logging.info(f"⏳ [{score:.0f}%] Ожидает одобрения: '{query}' -> '{best_name}'")
                pending_count += 1
                if pending_callback:
                    pending_callback(pend_key, query, best_name, round(score), "ym_to_sp")
            else:
                logging.warning(f"❌ [{score:.0f}%] Не найден в Spotify: '{query}'")
                cursor.execute("INSERT OR REPLACE INTO failed_syncs (key, query) VALUES (?, ?)", (fail_key, query))
                conn.commit()
                failed_keys.add(fail_key)
                failed_count += 1
        else:
            logging.warning(f"❌ [{score:.0f}%] Не найден в Spotify: '{query}'")
            cursor.execute("INSERT OR REPLACE INTO failed_syncs (key, query) VALUES (?, ?)", (fail_key, query))
            conn.commit()
            failed_keys.add(fail_key)
            failed_count += 1
        
        if progress_callback and idx % 25 == 0:
            progress_callback(idx, total, "ym_to_sp")
            
    conn.close()
    msg = f"Яндекс → Spotify: добавлено {added}, на одобрении {pending_count}, не найдено {failed_count}."
    if skipped:
        msg += f" Пропущено: {skipped}."
    return msg


def sync_sp_to_ym(progress_callback=None, pending_callback=None):
    """Synchronize liked tracks from Spotify to Yandex Music, preventing duplicates."""
    ym_client = get_ym_client()
    sp_client = get_sp_client()
    if not ym_client or not sp_client:
        return "Клиенты не настроены"

    sp_likes = get_sp_likes(sp_client)
    ym_likes = get_ym_likes(ym_client)
    
    ym_liked_ids = {str(ex['id']) for ex in ym_likes}
    
    added = 0
    failed_count = 0
    pending_count = 0
    skipped = 0
    total = len(sp_likes)
    
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT sp_id, ym_id FROM mappings")
    sp_to_ym_map = {r[0]: r[1] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM failed_syncs WHERE key LIKE 'sp_to_ym:%'")
    failed_keys = {r[0] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM pending_syncs WHERE key LIKE 'sp_to_ym:%'")
    pending_keys = {r[0] for r in cursor.fetchall()}
    
    for idx, sp_track in enumerate(sp_likes, 1):
        sp_id = str(sp_track['id'])
        artists = sp_track.get('artists', '')
        title = sp_track.get('title', '')
        query = sp_track.get('search_query', '')
        
        fail_key = f"sp_to_ym:{sp_id}"
        pend_key = fail_key
        
        if is_blacklisted(sp_id=sp_id):
            skipped += 1
            continue
            
        if fail_key in failed_keys or pend_key in pending_keys:
            skipped += 1
            continue
            
        mapped_ym_id = sp_to_ym_map.get(sp_id)
        if mapped_ym_id:
            mapped_ym_id = str(mapped_ym_id)
            if mapped_ym_id in ym_liked_ids:
                continue
            add_to_blacklist(mapped_ym_id, sp_id, artists, title)
            cursor.execute("DELETE FROM mappings WHERE sp_id = ?", (sp_id,))
            conn.commit()
            del sp_to_ym_map[sp_id]
            logging.info(f"⚫ [Блеклист] '{artists} - {title}' удалён из Яндекс Музыки пользователем. Добавлен в блеклист.")
            continue
            
        matched_ym_id = find_already_present_id(artists, title, ym_likes)
        if matched_ym_id:
            cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (matched_ym_id, sp_id))
            conn.commit()
            sp_to_ym_map[sp_id] = matched_ym_id
            logging.info(f"🔗 [Сопоставление] Трек уже есть в Яндекс по нечеткому совпадению: '{query}' -> ID '{matched_ym_id}'")
            continue
            
        ym_id, best_name, score = match_track_yandex(artists, title, ym_client)
        if ym_id:
            ym_id = str(ym_id)
            if ym_id in ym_liked_ids:
                cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, sp_id))
                conn.commit()
                sp_to_ym_map[sp_id] = ym_id
                logging.info(f"ℹ️ [{score:.0f}%] Трек уже есть в Яндекс по ID: '{query}' -> '{best_name}'")
                continue
                
            if score >= SCORE_AUTO_ACCEPT:
                _, err = api_call_with_retry(
                    ym_client.users_likes_tracks_add, track_ids=[ym_id]
                )
                if err:
                    logging.error(f"Ошибка при добавлении трека в Яндекс Музыку: {err}")
                    failed_count += 1
                else:
                    cursor.execute("INSERT OR REPLACE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (ym_id, artists, title, best_name))
                    ym_likes.append({"id": ym_id, "artists": artists, "title": title, "search_query": best_name})
                    ym_liked_ids.add(ym_id)
                    
                    cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, sp_id))
                    conn.commit()
                    sp_to_ym_map[sp_id] = ym_id
                    
                    logging.info(f"✅ [{score:.0f}%] Добавлен в Яндекс: '{query}' -> как '{best_name}'")
                    added += 1
            elif score >= SCORE_PENDING:
                cursor.execute(
                    "INSERT OR REPLACE INTO pending_syncs (key, direction, source, found, found_id, score) VALUES (?, ?, ?, ?, ?, ?)",
                    (pend_key, "sp_to_ym", query, best_name, ym_id, round(score))
                )
                conn.commit()
                pending_keys.add(pend_key)
                logging.info(f"⏳ [{score:.0f}%] Ожидает одобрения: '{query}' -> '{best_name}'")
                pending_count += 1
                if pending_callback:
                    pending_callback(pend_key, query, best_name, round(score), "sp_to_ym")
            else:
                logging.warning(f"❌ [{score:.0f}%] Не найден в Яндексе: '{query}'")
                cursor.execute("INSERT OR REPLACE INTO failed_syncs (key, query) VALUES (?, ?)", (fail_key, query))
                conn.commit()
                failed_keys.add(fail_key)
                failed_count += 1
        else:
            logging.warning(f"❌ [{score:.0f}%] Не найден в Яндексе: '{query}'")
            cursor.execute("INSERT OR REPLACE INTO failed_syncs (key, query) VALUES (?, ?)", (fail_key, query))
            conn.commit()
            failed_keys.add(fail_key)
            failed_count += 1
        
        if progress_callback and idx % 25 == 0:
            progress_callback(idx, total, "sp_to_ym")
            
    conn.close()
    msg = f"Spotify → Яндекс: добавлено {added}, на одобрении {pending_count}, не найдено {failed_count}."
    if skipped:
        msg += f" Пропущено: {skipped}."
    return msg


def approve_pending(pend_key):
    """Approve a pending track match, add it to likes, add mapping, and delete from pending."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT direction, found_id, found, source FROM pending_syncs WHERE key = ?", (pend_key,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "Трек не найден в ожидающих"
        
    direction, found_id, found_name, source_name = row
    
    parts = pend_key.split(":", 1)
    source_id = parts[1] if len(parts) == 2 else None
    
    if direction == "sp_to_ym":
        ym_client = get_ym_client()
        if not ym_client:
            conn.close()
            return False, "Яндекс клиент не настроен"
        _, err = api_call_with_retry(ym_client.users_likes_tracks_add, track_ids=[found_id])
        if err:
            conn.close()
            return False, f"Ошибка при добавлении в Яндекс: {err}"
        cursor.execute("INSERT OR REPLACE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (found_id, "", "", found_name))
        if source_id:
            cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (found_id, source_id))
        logging.info(f"✅ Одобрен: '{source_name}' -> '{found_name}'")
    elif direction == "ym_to_sp":
        sp_client = get_sp_client()
        if not sp_client:
            conn.close()
            return False, "Spotify клиент не настроен"
        _, err = api_call_with_retry(sp_client.current_user_saved_tracks_add, tracks=[found_id])
        if err:
            conn.close()
            return False, f"Ошибка при добавлении в Spotify: {err}"
        cursor.execute("INSERT OR REPLACE INTO spotify_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (found_id, "", "", found_name))
        if source_id:
            cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (source_id, found_id))
        logging.info(f"✅ Одобрен: '{source_name}' -> '{found_name}'")
    else:
        conn.close()
        return False, f"Неизвестное направление: {direction}"
            
    cursor.execute("DELETE FROM pending_syncs WHERE key = ?", (pend_key,))
    conn.commit()
    conn.close()
    return True, f"Добавлен: {found_name}"


def reject_pending(pend_key):
    """Reject a pending track match and move it to failed_syncs."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT source FROM pending_syncs WHERE key = ?", (pend_key,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, "Трек не найден в ожидающих"
        
    source_name = row[0]
    cursor.execute("INSERT OR REPLACE INTO failed_syncs (key, query) VALUES (?, ?)", (pend_key, source_name))
    cursor.execute("DELETE FROM pending_syncs WHERE key = ?", (pend_key,))
    conn.commit()
    conn.close()
    logging.info(f"❌ Отклонён: '{source_name}'")
    return True, f"Отклонён: {source_name}"


def clean_duplicate_title(title):
    """Clean track title specifically for duplicate detection, removing soundtrack tags."""
    t = clean_title(title)
    t = SOUNDTRACK_PATTERN.sub('', t)
    t = SOUNDTRACK_PAREN_PATTERN.sub('', t)
    return t.strip()


def check_duplicate(track1, track2):
    """Check if track1 is a fuzzy duplicate of track2, considering artists and version tags."""
    a1 = normalize(track1.get('artists', '').split(',')[0])
    a2 = normalize(track2.get('artists', '').split(',')[0])
    if fuzz.ratio(a1, a2) < 80:
        return False
        
    rt1 = normalize(track1.get('title', ''))
    rt2 = normalize(track2.get('title', ''))
    if fuzz.ratio(rt1, rt2) >= 95:
        return True
        
    ct1 = normalize(clean_duplicate_title(track1.get('title', '')))
    ct2 = normalize(clean_duplicate_title(track2.get('title', '')))
    
    if fuzz.ratio(ct1, ct2) >= 95:
        mismatch_words = ["remix", "cover", "live", "acoustic", "instrumental", "karaoke", "slowed", "sped up", "nightcore"]
        for word in mismatch_words:
            if (word in rt1) != (word in rt2):
                return False
        return True
        
    return False


def remove_spotify_duplicates():
    """Find and delete duplicate tracks in Spotify saved tracks, keeping the newest."""
    sp_client = get_sp_client()
    if not sp_client:
        return False, "Клиент Spotify не настроен"
        
    logging.info("Получение всех сохраненных треков из Spotify...")
    tracks = []
    try:
        results = sp_client.current_user_saved_tracks(limit=50)
        while results:
            for item in results['items']:
                track = item['track']
                added_at = item.get('added_at', '')
                artists = ", ".join([a['name'] for a in track['artists']])
                title = track['name']
                tracks.append({
                    "id": track['id'],
                    "artists": artists,
                    "title": title,
                    "added_at": added_at
                })
            if results['next']:
                results = sp_client.next(results)
            else:
                break
    except Exception as e:
        logging.error(f"Ошибка при получении треков Spotify: {e}")
        return False, f"Ошибка API Spotify: {e}"
        
    if not tracks:
        return True, "Библиотека Spotify пуста."
        
    tracks.sort(key=lambda x: x.get('added_at', ''), reverse=True)
    
    keep = []
    delete = []
    
    for t in tracks:
        is_dup = False
        for k in keep:
            if check_duplicate(t, k):
                is_dup = True
                break
        if is_dup:
            delete.append(t)
        else:
            keep.append(t)
            
    if not delete:
        return True, "Дубликаты в библиотеке Spotify не найдены."
        
    logging.info(f"Найдено {len(delete)} дубликатов в Spotify. Начинаю удаление...")

    delete_ids = [t['id'] for t in delete]
    deleted_actual = []
    failed_actual = []

    for i in range(0, len(delete_ids), 50):
        chunk = delete_ids[i:i+50]
        _, err = api_call_with_retry(
            sp_client.current_user_saved_tracks_delete, tracks=chunk
        )
        if err:
            logging.error(f"Ошибка при удалении чанка {i//50 + 1}: {err}")
            failed_actual.extend(chunk)
        else:
            deleted_actual.extend(chunk)
            logging.info(f"Удалено {len(chunk)} дубликатов из Spotify (чанк {i//50 + 1})")
        time.sleep(0.5)

    if failed_actual:
        logging.warning(f"Не удалось удалить {len(failed_actual)} дубликатов из Spotify")

    deleted_tracks = [t for t in delete if t['id'] in deleted_actual]

    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    for tid in deleted_actual:
        cursor.execute("DELETE FROM spotify_cache WHERE id = ?", (tid,))
        cursor.execute("DELETE FROM mappings WHERE sp_id = ?", (tid,))
    conn.commit()
    conn.close()

    deleted_list = [f"⚫ {t['artists']} - {t['title']}" for t in deleted_tracks]
    return True, deleted_list


def remove_yandex_duplicates():
    """Find and delete duplicate tracks in Yandex Music liked tracks, keeping the newest."""
    ym_client = get_ym_client()
    if not ym_client:
        return False, "Клиент Яндекс Музыки не настроен"
        
    logging.info("Получение лайкнутых треков из Яндекс Музыки...")
    try:
        likes = ym_client.users_likes_tracks()
    except Exception as e:
        logging.error(f"Ошибка при получении лайков YM: {e}")
        return False, f"Ошибка API Яндекс Музыки: {e}"
        
    if not likes or not likes.tracks:
        return True, "Библиотека Яндекс Музыки пуста."
        
    short_tracks = likes.tracks
    short_tracks.sort(key=lambda x: x.timestamp or '', reverse=True)
    
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    tracks_to_check = []
    track_ids_to_fetch = []
    
    for t in short_tracks:
        tid = str(t.id)
        cursor.execute("SELECT artists, title FROM yandex_cache WHERE id = ?", (tid,))
        row = cursor.fetchone()
        if row:
            tracks_to_check.append({
                "id": tid,
                "artists": row[0],
                "title": row[1],
                "timestamp": t.timestamp if t.timestamp else ''
            })
        else:
            fetch_id = f"{t.id}:{t.album_id}" if t.album_id else str(t.id)
            track_ids_to_fetch.append((tid, fetch_id, t.timestamp if t.timestamp else ''))
            
    if track_ids_to_fetch:
        full_tracks_map = {}
        ids_only = [item[1] for item in track_ids_to_fetch]
        for i in range(0, len(ids_only), 50):
            chunk = ids_only[i:i+50]
            try:
                fetched = ym_client.tracks(chunk)
                for ft in fetched:
                    if ft:
                        full_tracks_map[str(ft.id)] = ft
            except Exception as e:
                logging.error(f"Ошибка при получении полной информации о треках YM: {e}")
                continue
                
        new_cache_rows = []
        for tid, _, ts in track_ids_to_fetch:
            ft = full_tracks_map.get(tid)
            if ft:
                artists = ", ".join([a.name for a in ft.artists]) if ft.artists else "Unknown"
                title = ft.title or ""
                query = f"{artists} {title}"
                new_cache_rows.append((tid, artists, title, query))
                tracks_to_check.append({
                    "id": tid,
                    "artists": artists,
                    "title": title,
                    "timestamp": ts
                })
        if new_cache_rows:
            cursor.executemany("INSERT OR REPLACE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", new_cache_rows)
            conn.commit()
            
    tracks_to_check.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
    
    keep = []
    delete = []
    
    for t in tracks_to_check:
        is_dup = False
        for k in keep:
            if check_duplicate(t, k):
                is_dup = True
                break
        if is_dup:
            delete.append(t)
        else:
            keep.append(t)
            
    if not delete:
        conn.close()
        return True, "Дубликаты в библиотеке Яндекс Музыки не найдены."
        
    logging.info(f"Найдено {len(delete)} дубликатов в Яндекс Музыке. Начинаю удаление...")
    
    delete_ids = [t['id'] for t in delete]
    deleted_actual = []
    failed_actual = []

    for i in range(0, len(delete_ids), 100):
        chunk = delete_ids[i:i+100]
        _, err = api_call_with_retry(ym_client.users_likes_tracks_remove, chunk)
        if err:
            logging.error(f"Ошибка при удалении чанка {i//100 + 1}: {err}")
            failed_actual.extend(chunk)
        else:
            deleted_actual.extend(chunk)
            logging.info(f"Удалено {len(chunk)} дубликатов из Яндекс Музыки (чанк {i//100 + 1})")

    if failed_actual:
        logging.warning(f"Не удалось удалить {len(failed_actual)} дубликатов из Яндекс Музыки")

    for tid in deleted_actual:
        cursor.execute("DELETE FROM yandex_cache WHERE id = ?", (tid,))
        cursor.execute("DELETE FROM mappings WHERE ym_id = ?", (tid,))
    conn.commit()
    conn.close()

    deleted_tracks = [t for t in delete if t['id'] in deleted_actual]
    deleted_list = [f"⚫ {t['artists']} - {t['title']}" for t in deleted_tracks]
    return True, deleted_list


def clear_stale_sync_lock():
    """Clear the sync_in_progress flag if the lock is stale (over 1 hour old)."""
    try:
        lock_time = get_setting("sync_started_at")
        if not lock_time:
            set_setting("sync_in_progress", "0")
            return
        from datetime import datetime
        lock_dt = datetime.strptime(lock_time, "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - lock_dt).total_seconds() > 3600:
            logging.warning("Обнаружена устаревшая блокировка синхронизации. Сброс.")
            set_setting("sync_in_progress", "0")
            set_setting("sync_started_at", "")
    except Exception:
        set_setting("sync_in_progress", "0")


def full_two_way_sync(progress_callback=None, pending_callback=None):
    """Execute complete two-way synchronization between Yandex Music and Spotify."""
    if is_sync_running():
        clear_stale_sync_lock()
        if is_sync_running():
            return "Синхронизация уже выполняется. Подождите."

    try:
        set_setting("sync_in_progress", "1")
        from datetime import datetime
        set_setting("sync_started_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    except Exception:
        pass

    try:
        res1 = sync_ym_to_sp(progress_callback, pending_callback)
        res2 = sync_sp_to_ym(progress_callback, pending_callback)
        result = f"{res1}\n{res2}"
    except Exception as e:
        logging.error(f"Ошибка при синхронизации: {e}")
        result = f"Ошибка синхронизации: {e}"
    finally:
        try:
            set_setting("sync_in_progress", "0")
            set_setting("sync_started_at", "")
        except Exception:
            pass

    try:
        from datetime import datetime
        set_setting("last_sync_timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        set_setting("last_sync_result", result)
    except Exception:
        pass

    return result

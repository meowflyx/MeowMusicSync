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
    r'anniversary|edition|version|mix|mono|stereo|original)'
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

SCORE_AUTO_ACCEPT = 93
SCORE_PENDING = 70


def init_db():
    """Initialize SQLite database tables and automatically migrate data from manifest.json if present."""
    conn = sqlite3.connect(DB_FILE)
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
        q_forms.append(normalize(a))
        mapped = TRANSLIT_MAP.get(a.lower().strip())
        if mapped:
            q_forms.append(normalize(mapped))
        if has_latin(a):
            q_forms.append(normalize(translit_to_cyrillic(a)))
    
    r_forms = []
    for a in r_artists_raw:
        r_forms.append(normalize(a))
        mapped = TRANSLIT_MAP.get(a.lower().strip())
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
        try:
            results = sp_client.search(q=q, limit=5, type='track')
        except Exception:
            time.sleep(5)
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
        try:
            results = ym_client.search(q, type_='track')
        except Exception:
            time.sleep(5)
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
                logging.error(f"Skipping bad YM chunk {i}: {e}")
                continue
        
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
        ex_artists = [normalize(a.strip()) for a in ex.get('artists', '').split(',')]
        ex_main = ex_artists[0] if ex_artists else ""
        
        title_match = fuzz.ratio(q_title, ex_title) > 85 or fuzz.token_sort_ratio(q_title, ex_title) > 85
        artist_match = fuzz.ratio(q_main, ex_main) > 75
        
        if title_match and artist_match:
            return ex.get('id')
    return None


def sync_ym_to_sp(log_callback=None, pending_callback=None):
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
    
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT ym_id, sp_id FROM mappings")
    ym_to_sp_map = {r[0]: r[1] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM failed_syncs WHERE key LIKE 'ym_to_sp:%'")
    failed_keys = {r[0] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM pending_syncs WHERE key LIKE 'ym_to_sp:%'")
    pending_keys = {r[0] for r in cursor.fetchall()}
    
    for ym_track in ym_likes:
        ym_id = str(ym_track['id'])
        artists = ym_track.get('artists', '')
        title = ym_track.get('title', '')
        query = ym_track.get('search_query', '')
        
        fail_key = f"ym_to_sp:{ym_id}"
        pend_key = fail_key
        
        if fail_key in failed_keys or pend_key in pending_keys:
            skipped += 1
            continue
            
        mapped_sp_id = ym_to_sp_map.get(ym_id)
        if mapped_sp_id:
            if mapped_sp_id in sp_liked_ids:
                continue
            try:
                sp_client.current_user_saved_tracks_add(tracks=[mapped_sp_id])
                sp_likes.append({"id": mapped_sp_id, "artists": artists, "title": title, "search_query": query})
                sp_liked_ids.add(mapped_sp_id)
                added += 1
                logging.info(f"✅ [Маппинг] Добавлен в Spotify: '{query}' -> ID '{mapped_sp_id}'")
            except Exception as e:
                logging.error(f"Ошибка при добавлении мапированного трека в Spotify: {e}")
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
                try:
                    sp_client.current_user_saved_tracks_add(tracks=[sp_id])
                    cursor.execute("INSERT OR REPLACE INTO spotify_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (sp_id, artists, title, best_name))
                    sp_likes.append({"id": sp_id, "artists": artists, "title": title, "search_query": best_name})
                    sp_liked_ids.add(sp_id)
                    
                    cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, sp_id))
                    conn.commit()
                    ym_to_sp_map[ym_id] = sp_id
                    
                    logging.info(f"✅ [{score:.0f}%] Добавлен в Spotify: '{query}' -> как '{best_name}'")
                    added += 1
                except Exception as e:
                    logging.error(f"Ошибка при добавлении трека в Spotify: {e}")
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
            
    conn.close()
    msg = f"Яндекс → Spotify: добавлено {added}, на одобрении {pending_count}, не найдено {failed_count}."
    if skipped:
        msg += f" Пропущено: {skipped}."
    return msg


def sync_sp_to_ym(log_callback=None, pending_callback=None):
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
    
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT sp_id, ym_id FROM mappings")
    sp_to_ym_map = {r[0]: r[1] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM failed_syncs WHERE key LIKE 'sp_to_ym:%'")
    failed_keys = {r[0] for r in cursor.fetchall()}
    
    cursor.execute("SELECT key FROM pending_syncs WHERE key LIKE 'sp_to_ym:%'")
    pending_keys = {r[0] for r in cursor.fetchall()}
    
    for sp_track in sp_likes:
        sp_id = str(sp_track['id'])
        artists = sp_track.get('artists', '')
        title = sp_track.get('title', '')
        query = sp_track.get('search_query', '')
        
        fail_key = f"sp_to_ym:{sp_id}"
        pend_key = fail_key
        
        if fail_key in failed_keys or pend_key in pending_keys:
            skipped += 1
            continue
            
        mapped_ym_id = sp_to_ym_map.get(sp_id)
        if mapped_ym_id:
            mapped_ym_id = str(mapped_ym_id)
            if mapped_ym_id in ym_liked_ids:
                continue
            try:
                ym_client.users_likes_tracks_add(track_ids=[mapped_ym_id])
                ym_likes.append({"id": mapped_ym_id, "artists": artists, "title": title, "search_query": query})
                ym_liked_ids.add(mapped_ym_id)
                added += 1
                logging.info(f"✅ [Маппинг] Добавлен в Яндекс: '{query}' -> ID '{mapped_ym_id}'")
            except Exception as e:
                logging.error(f"Ошибка при добавлении мапированного трека в Яндекс Музыку: {e}")
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
                try:
                    ym_client.users_likes_tracks_add(track_ids=[ym_id])
                    cursor.execute("INSERT OR REPLACE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (ym_id, artists, title, best_name))
                    ym_likes.append({"id": ym_id, "artists": artists, "title": title, "search_query": best_name})
                    ym_liked_ids.add(ym_id)
                    
                    cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (ym_id, sp_id))
                    conn.commit()
                    sp_to_ym_map[sp_id] = ym_id
                    
                    logging.info(f"✅ [{score:.0f}%] Добавлен в Яндекс: '{query}' -> как '{best_name}'")
                    added += 1
                except Exception as e:
                    logging.error(f"Ошибка при добавлении трека в Яндекс Музыку: {e}")
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
        try:
            ym_client.users_likes_tracks_add(track_ids=[found_id])
            cursor.execute("INSERT OR REPLACE INTO yandex_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (found_id, "", "", found_name))
            if source_id:
                cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (found_id, source_id))
            logging.info(f"✅ Одобрен: '{source_name}' -> '{found_name}'")
        except Exception as e:
            conn.close()
            return False, f"Ошибка при добавлении в Яндекс: {e}"
    elif direction == "ym_to_sp":
        sp_client = get_sp_client()
        if not sp_client:
            conn.close()
            return False, "Spotify клиент не настроен"
        try:
            sp_client.current_user_saved_tracks_add(tracks=[found_id])
            cursor.execute("INSERT OR REPLACE INTO spotify_cache (id, artists, title, query) VALUES (?, ?, ?, ?)", (found_id, "", "", found_name))
            if source_id:
                cursor.execute("INSERT OR IGNORE INTO mappings (ym_id, sp_id) VALUES (?, ?)", (source_id, found_id))
            logging.info(f"✅ Одобрен: '{source_name}' -> '{found_name}'")
        except Exception as e:
            conn.close()
            return False, f"Ошибка при добавлении в Spotify: {e}"
            
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


def full_two_way_sync(log_callback=None, pending_callback=None):
    """Execute complete two-way synchronization between Yandex Music and Spotify."""
    res1 = sync_ym_to_sp(log_callback, pending_callback)
    res2 = sync_sp_to_ym(log_callback, pending_callback)
    return f"{res1}\n{res2}"

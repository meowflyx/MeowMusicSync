"""Module for music synchronization logic between Yandex Music and Spotify.

Uses an SQLite database for failed attempts, pending approvals, and
cross-platform track ID mappings.
Optimized to run check operations using fast set lookups and instant mapping shortcuts.
"""

import logging
import json
import os
import re
from urllib.parse import urlsplit
import time
import sqlite3
from error_messages import explain_error
import fcntl
import threading
from contextlib import contextmanager, closing
from functools import wraps
from collections import deque
from dataclasses import replace
from yandex_music import Client
import spotipy
from spotipy.exceptions import SpotifyException, SpotifyOauthError
from yandex_music.exceptions import UnauthorizedError, BadRequestError, NotFoundError
from spotipy.oauth2 import SpotifyOAuth
import jev
from candidate_search import discover_spotify, discover_yandex, spotify_track, yandex_track
from matching import (ALGORITHM_VERSION, DecisionKind, MatchingEngine,
                      MatchingMode, Track, metadata_features, normalize)
from matching_store import (MappingResult, migrate_matching, save_failed,
                            save_mapping, save_pending, SCHEMA_VERSION)
from config import (
    SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI,
    YANDEX_MUSIC_TOKEN, validate_spotify_config
)

DB_FILE = "sync_data.db"
CATALOG_SEARCH_INTERVAL_SECONDS = 10

# ponytail: one account on one Linux host; distributed workers need a shared lock.
_operation_lock = threading.RLock()
_operation_state = threading.local()


@contextmanager
def music_operation():
    """Exclude concurrent threads/processes; nested two-way sync is reentrant."""
    if not _operation_lock.acquire(blocking=False):
        raise RuntimeError("Операция уже выполняется. Подождите и повторите команду.")
    try:
        if getattr(_operation_state, "active", False):
            yield
            return
        with open(DB_FILE + ".lock", "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Операция уже выполняется. Подождите и повторите команду.") from None
            _operation_state.active = True
            try:
                yield
            finally:
                _operation_state.active = False
                fcntl.flock(lock, fcntl.LOCK_UN)
    finally:
        _operation_lock.release()


def serialized(func):
    @wraps(func)
    def wrapped(*args, **kwargs):
        with music_operation():
            return func(*args, **kwargs)
    return wrapped

def init_db() -> None:
    """Create current state tables and migrate older rows in place."""
    with closing(sqlite3.connect(DB_FILE)) as conn:
        if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
            return
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        with conn:
            conn.execute("CREATE TABLE IF NOT EXISTS failed_syncs (key TEXT PRIMARY KEY, query TEXT)")
            conn.execute("CREATE TABLE IF NOT EXISTS pending_syncs (key TEXT PRIMARY KEY, "
                         "direction TEXT, source TEXT, found TEXT, found_id TEXT, score INTEGER)")
            conn.execute("CREATE TABLE IF NOT EXISTS mappings (ym_id TEXT UNIQUE, sp_id TEXT UNIQUE)")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        migrate_matching(conn)


def get_status_stats():
    """Retrieve statistics from the SQLite database."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute("SELECT COUNT(*) FROM mappings")
    mappings_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM pending_syncs")
    pending_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM failed_syncs")
    failed_count = cursor.fetchone()[0]
    
    conn.close()
    return {
        "mappings": mappings_count,
        "pending": pending_count,
        "failed": failed_count
    }


def get_pending_tracks():
    """Retrieve all pending review items from the database."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT key, direction, source, found, found_id, score, score_type, "
                   "purpose, mode, metadata_rank, jev_probability, reasons, diagnostics "
                   "FROM pending_syncs")
    rows = cursor.fetchall()
    conn.close()
    return {
        r[0]: {
            "direction": r[1],
            "source": r[2],
            "found": r[3],
            "found_id": r[4],
            "score": r[5],
            "score_type": r[6], "purpose": r[7], "mode": r[8],
            "metadata_rank": r[9], "jev_probability": r[10],
            "reasons": json.loads(r[11] or "[]"),
            "diagnostics": json.loads(r[12] or "[]")
        } for r in rows
    }


@serialized
def clear_failed_tracks():
    """Delete all records from failed_syncs table and return the count of deleted items."""
    init_db()
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM failed_syncs")
    count = cursor.rowcount
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


def get_jev_status():
    provider = get_setting("jev_provider")
    return {"enabled": get_setting("jev_enabled") == "1", "provider": provider,
            "configured": provider in jev.PROVIDERS and bool(get_setting("jev_api_key")),
            "mode": get_setting("matching_mode", MatchingMode.HYBRID.value)}


@serialized
def configure_jev(provider, api_key):
    if provider not in jev.PROVIDERS:
        raise ValueError("Провайдер Jev: typesafe, openrouter или vercel.")
    if not api_key or any(char.isspace() for char in api_key):
        raise ValueError("Укажите API-ключ без пробелов.")
    ciphertext = jev.encrypt_key(api_key)
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (("jev_api_key", ciphertext), ("jev_provider", provider), ("jev_enabled", "1"))
        )


@serialized
def set_jev_enabled(enabled):
    if not enabled and get_setting("matching_mode") == MatchingMode.JEV_ONLY.value:
        raise ValueError("Сначала переключите режим на hybrid: /matching hybrid")
    if enabled:
        status = get_jev_status()
        if not status["configured"]:
            raise ValueError("Сначала задайте провайдера и ключ: /jev typesafe|openrouter|vercel <ключ>.")
        jev.decrypt_key(get_setting("jev_api_key"))
    set_setting("jev_enabled", "1" if enabled else "0")


@serialized
def set_matching_mode(mode: str) -> None:
    selected = MatchingMode(mode)
    if selected is MatchingMode.JEV_ONLY:
        status = get_jev_status()
        if not status["enabled"] or not status["configured"]:
            raise ValueError("Для jev_only сначала настройте и включите Jev командой /jev")
        jev.decrypt_key(get_setting("jev_api_key"))
    if get_setting("matching_mode", MatchingMode.HYBRID.value) != selected.value:
        set_setting("matching_mode", selected.value)
        set_setting("matching_revalidate", "1")


def get_last_sync_info():
    """Return dict with last sync timestamp and result string."""
    return {
        "timestamp": get_setting("last_sync_timestamp"),
        "result": get_setting("last_sync_result"),
    }


def is_sync_running():
    """Check if a sync is currently in progress."""
    try:
        with music_operation():
            return False
    except RuntimeError:
        return True


@serialized
def add_manual_mapping(ym_id, sp_id):
    """Manually link a Yandex Music track ID to a Spotify track ID."""
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn:
        if save_mapping(conn, str(ym_id), str(sp_id),
                        get_setting("matching_mode", MatchingMode.HYBRID.value),
                        "manual") is MappingResult.CONFLICT:
            raise ValueError("Один из ID уже связан с другим треком; удалите старую пару вручную")


def get_recent_logs(n=20):
    """Read the last n lines from the sync log file."""
    log_path = "sync.log"
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = deque(f, maxlen=max(0, min(n, 100)))
        return [line.rstrip("\n") for line in lines]
    except Exception:
        return []


def check_api_health():
    """Test connectivity to Yandex Music and Spotify APIs. Returns dict of statuses."""
    result = {"yandex": False, "spotify": False, "yandex_error": None, "spotify_error": None}
    
    try:
        ym_client = get_ym_client()
        if ym_client:
            ym_client.users_likes_tracks()
            result["yandex"] = True
        else:
            result["yandex_error"] = "Токен не настроен"
    except Exception as e:
        result["yandex_error"] = explain_error(e)
    
    try:
        sp_client = get_sp_client()
        if sp_client:
            sp_client.current_user()
            result["spotify"] = True
        else:
            result["spotify_error"] = "Учётные данные не настроены"
    except Exception as e:
        result["spotify_error"] = explain_error(e)
    
    return result


def get_ym_client():
    """Initialize and return the Yandex Music API client."""
    if not YANDEX_MUSIC_TOKEN:
        return None
    return Client(YANDEX_MUSIC_TOKEN).init()


@serialized
def like_playlist_tracks(playlist_url):
    """Add every available track from an own Yandex Music playlist to likes."""
    parsed = urlsplit(playlist_url)
    match = re.fullmatch(r"/users/([^/]+)/playlists/(\d+)/?", parsed.path)
    uuid_match = re.fullmatch(r"/playlists/([0-9a-f-]{36})/?", parsed.path, re.IGNORECASE)
    if parsed.scheme != "https" or parsed.hostname != "music.yandex.ru" or not (match or uuid_match):
        raise ValueError("Пришлите ссылку Яндекс Музыки вида https://music.yandex.ru/playlists/... или https://music.yandex.ru/users/.../playlists/...")
    client = get_ym_client()
    if not client:
        raise RuntimeError("YANDEX_MUSIC_TOKEN не настроен. Проверьте .env и выполните /health.")
    playlist = client.playlist(uuid_match.group(1)) if uuid_match else client.users_playlists(match.group(2), match.group(1))
    track_ids = list(dict.fromkeys(str(track.id) for track in playlist.fetch_tracks() if track and track.id))
    if not track_ids:
        return f"В плейлисте «{playlist.title or 'без названия'}» нет доступных треков."
    for index in range(0, len(track_ids), 100):
        _, error = api_call_with_retry(client.users_likes_tracks_add, track_ids[index:index + 100])
        if error:
            raise error
    return f"Лайкнуто {len(track_ids)} трека из плейлиста «{playlist.title or 'без названия'}»."


def get_sp_client():
    """Initialize and return the Spotify API client using OAuth."""
    validate_spotify_config(SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI)
    auth_manager = SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope="user-library-read user-library-modify",
        open_browser=False
    )
    if not auth_manager.validate_token(auth_manager.cache_handler.get_cached_token()):
        raise RuntimeError("Spotify не авторизован. Выполните .venv/bin/python auth_spotify.py на сервере.")
    return spotipy.Spotify(auth_manager=auth_manager, retries=0, status_retries=0,
                           status_forcelist=(500, 502, 503, 504))


def api_call_with_retry(func, *args, max_retries=3, base_delay=2, **kwargs):
    """Call an API function with exponential backoff retry on transient failures."""
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs), None
        except Exception as e:
            if isinstance(e, SpotifyException) and e.http_status == 429:
                return None, e
            if isinstance(e, (SpotifyOauthError, UnauthorizedError, BadRequestError, NotFoundError)) or (
                isinstance(e, SpotifyException) and 400 <= e.http_status < 500 and e.http_status != 429
            ):
                return None, RuntimeError(explain_error(e))
            if attempt == max_retries - 1:
                return None, e
            delay = base_delay * (2 ** attempt)
            logging.warning(f"API вызов не удался (попытка {attempt + 1}/{max_retries}): {e}. Повтор через {delay}с")
            time.sleep(delay)
    logging.error(f"Исчерпаны все попытки ({max_retries}) для {func.__name__}")
    return None, RuntimeError(f"Не удалось выполнить {func.__name__} после {max_retries} попыток")


def get_ym_likes(ym_client) -> list[dict]:
    """Fetch current liked-record metadata; no saved metadata is trusted."""
    short = ym_client.users_likes_tracks().tracks or []
    ids = [f"{item.id}:{item.album_id}" if item.album_id else str(item.id) for item in short]
    fetched = {}
    for offset in range(0, len(ids), 50):
        chunk = ids[offset:offset + 50]
        try:
            records = ym_client.tracks(chunk)
        except (OSError, TimeoutError, ValueError) as error:
            logging.warning("YM metadata batch failed at %s: %s", offset, error)
            records = []
            for track_id in chunk:
                try:
                    records.extend(ym_client.tracks([track_id]) or [])
                except (OSError, TimeoutError, ValueError):
                    logging.warning("YM metadata unavailable for %s", track_id)
        for record in records or []:
            if record:
                fetched[str(record.id)] = yandex_track(record, original=True)
    if {str(item.id) for item in short} != set(fetched):
        raise RuntimeError("Яндекс вернул неполную библиотеку. Синхронизация остановлена; повторите позже.")
    tracks = []
    for item in short:
        record = fetched[str(item.id)]
        tracks.append({"id": record.id, "artists": ", ".join(record.artists),
                       "title": record.title, "search_query": record.label,
                       "album": record.album, "duration_ms": record.duration_ms,
                       "isrc": record.isrc, "explicit": record.explicit,
                       "version": record.version,
                       "timestamp": getattr(item, "timestamp", None) or ""})
    return tracks


def get_sp_likes(sp_client) -> list[dict]:
    """Fetch current saved-record metadata directly from Spotify."""
    result = sp_client.current_user_saved_tracks(limit=50)
    tracks = []
    while result:
        for item in result["items"]:
            data = item.get("track")
            if not data or not data.get("id") or data.get("is_local"):
                continue
            record = spotify_track(data)
            tracks.append({"id": record.id, "artists": ", ".join(record.artists),
                           "title": record.title, "search_query": record.label,
                           "album": record.album, "duration_ms": record.duration_ms,
                           "isrc": record.isrc, "explicit": record.explicit,
                           "added_at": item.get("added_at") or ""})
        result = sp_client.next(result) if result.get("next") else None
    return tracks


def _as_track(platform: str, row: dict) -> Track:
    return Track(platform, str(row["id"]), row.get("title") or "",
                 tuple(part.strip() for part in (row.get("artists") or "").split(",") if part.strip()),
                 row.get("album"), row.get("duration_ms"), row.get("isrc"),
                 row.get("explicit"), row.get("version"))


def _matching_engine() -> MatchingEngine:
    mode = MatchingMode(get_setting("matching_mode", MatchingMode.HYBRID.value))
    status = get_jev_status()
    if mode is MatchingMode.JEV_ONLY and not status["enabled"]:
        raise ValueError("Для jev_only включите Jev командой /jev on")
    if status["enabled"]:
        if not status["configured"]:
            raise RuntimeError("Jev включён без провайдера или ключа")
        provider = status["provider"]
        key = jev.decrypt_key(get_setting("jev_api_key"))
        def verify(source: Track, target: Track) -> float:
            yandex, spotify = (source, target) if source.platform == "yandex" else (target, source)
            return jev.match_probability(provider, key, yandex, spotify)
        return MatchingEngine(mode, verify)
    return MatchingEngine(mode)


def _sync_direction(direction: str) -> str:
    ym_client, sp_client = get_ym_client(), get_sp_client()
    if not ym_client or not sp_client:
        raise RuntimeError("Клиенты не настроены. Проверьте токены в .env и выполните /health.")
    engine = _matching_engine()
    ym_likes, sp_likes = get_ym_likes(ym_client), get_sp_likes(sp_client)
    source_is_yandex = direction == "ym_to_sp"
    source_rows, target_rows = (ym_likes, sp_likes) if source_is_yandex else (sp_likes, ym_likes)
    source_platform, target_platform = (("yandex", "spotify") if source_is_yandex
                                        else ("spotify", "yandex"))
    source_tracks = [_as_track(source_platform, row) for row in source_rows]
    target_tracks = [_as_track(target_platform, row) for row in target_rows]
    target_by_id = {track.id: track for track in target_tracks}
    target_ids = set(target_by_id)
    target_client = sp_client if source_is_yandex else ym_client
    discover = discover_spotify if source_is_yandex else discover_yandex
    add = (lambda track_id: api_call_with_retry(sp_client.current_user_saved_tracks_add,
                                                tracks=[track_id])) if source_is_yandex else (
          lambda track_id: api_call_with_retry(ym_client.users_likes_tracks_add,
                                               track_ids=[track_id]))
    init_db()
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    added = pending_count = failed_count = skipped = 0
    last_catalog_search = None
    revalidate = get_setting("matching_revalidate") == "1"
    try:
        mappings = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT ym_id, sp_id, algorithm_version FROM mappings" if source_is_yandex
            else "SELECT sp_id, ym_id, algorithm_version FROM mappings")}
        claimed_target_ids = {target_id for target_id, _ in mappings.values()}
        failed = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT key, algorithm_version, mode FROM failed_syncs WHERE key LIKE ?",
            (direction + ":%",))}
        pending = {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT key, algorithm_version, mode FROM pending_syncs WHERE key LIKE ?",
            (direction + ":%",))}
        for source in source_tracks:
            key = f"{direction}:{source.id}"
            if ((key in pending and pending[key] == (ALGORITHM_VERSION, engine.mode.value))
                    or (key in failed and failed[key] == (ALGORITHM_VERSION, engine.mode.value))):
                skipped += 1
                continue
            mapped = mappings.get(source.id)
            if mapped:
                mapped_id, mapped_version = mapped
                if mapped_id not in target_ids:
                    ym_id, sp_id = ((source.id, mapped_id) if source_is_yandex
                                    else (mapped_id, source.id))
                    with conn:
                        conn.execute("DELETE FROM mappings WHERE ym_id = ? AND sp_id = ?", (ym_id, sp_id))
                    mappings.pop(source.id, None)
                    claimed_target_ids.discard(mapped_id)
                else:
                    if not revalidate and mapped_version >= ALGORITHM_VERSION:
                        continue
                    decision = engine.decide(source, [target_by_id[mapped_id]])
                    if decision.kind is DecisionKind.MATCH:
                        ym_id, sp_id = ((source.id, mapped_id) if source_is_yandex
                                        else (mapped_id, source.id))
                        result = save_mapping(conn, ym_id, sp_id, engine.mode.value, decision.source)
                        if result is MappingResult.CONFLICT:
                            raise RuntimeError(f"Конфликт существующего mapping: {ym_id} ↔ {sp_id}")
                    else:
                        with conn:
                            conn.execute("UPDATE mappings SET status = 'needs_review' WHERE "
                                         + ("ym_id = ?" if source_is_yandex else "sp_id = ?"), (source.id,))
                        save_pending(conn, key, direction, source.label, decision, "revalidate")
                        pending_count += 1
                    continue
            known = [target for target in target_tracks
                     if target.id not in claimed_target_ids
                     and metadata_features(source, target).safe_exact]
            decision = engine.decide(source, known) if known else None
            if decision is None or decision.kind is not DecisionKind.MATCH:
                if last_catalog_search is not None:
                    wait = CATALOG_SEARCH_INTERVAL_SECONDS - (time.monotonic() - last_catalog_search)
                    if wait > 0:
                        time.sleep(wait)
                last_catalog_search = time.monotonic()
                candidates = discover(source, target_client, target_tracks, api_call_with_retry)
                decision = engine.decide(source, candidates)
            logging.info("Match %s %s: %s candidate=%s rank=%s jev=%s reasons=%s",
                         direction, source.id, decision.kind.value,
                         decision.candidate.id if decision.candidate else None,
                         decision.metadata_rank, decision.jev_probability, decision.reasons)
            logging.debug("Match candidates %s: %s", key, [
                (item.candidate.id, round(item.features.rank, 1), item.features.reasons,
                 item.jev_probability) for item in decision.considered[:10]])
            if decision.kind is DecisionKind.MATCH:
                target = decision.candidate
                ym_id, sp_id = ((source.id, target.id) if source_is_yandex
                                else (target.id, source.id))
                conflicts = conn.execute("SELECT 1 FROM mappings WHERE "
                                         "(ym_id = ? AND sp_id <> ?) OR (sp_id = ? AND ym_id <> ?)",
                                         (ym_id, sp_id, sp_id, ym_id)).fetchone()
                if conflicts:
                    decision = replace(decision, kind=DecisionKind.REVIEW,
                                       reasons=(*decision.reasons, "mapping_conflict"))
                else:
                    if target.id not in target_ids:
                        _, error = add(target.id)
                        if error:
                            logging.error("Не удалось добавить %s: %s", target.label, error)
                            failed_count += 1
                            continue
                        target_ids.add(target.id)
                        target_by_id[target.id] = target
                        target_tracks.append(target)
                        added += 1
                    result = save_mapping(conn, ym_id, sp_id, engine.mode.value, decision.source)
                    if result is MappingResult.CONFLICT:
                        decision = replace(decision, kind=DecisionKind.REVIEW,
                                           reasons=(*decision.reasons, "mapping_conflict"))
                    else:
                        mappings[source.id] = (target.id, ALGORITHM_VERSION)
                        claimed_target_ids.add(target.id)
                        with conn:
                            conn.execute("DELETE FROM pending_syncs WHERE key = ?", (key,))
                            conn.execute("DELETE FROM failed_syncs WHERE key = ?", (key,))
                        continue
            if decision.kind is DecisionKind.REVIEW:
                save_pending(conn, key, direction, source.label, decision)
                with conn:
                    conn.execute("DELETE FROM failed_syncs WHERE key = ?", (key,))
                pending_count += 1
            else:
                save_failed(conn, key, source.label, engine.mode.value,
                            ",".join(decision.reasons) or decision.kind.value)
                with conn:
                    conn.execute("DELETE FROM pending_syncs WHERE key = ?", (key,))
                failed_count += 1
    finally:
        conn.close()
    label = "Яндекс → Spotify" if source_is_yandex else "Spotify → Яндекс"
    result = f"{label}: добавлено {added}, на одобрении {pending_count}, не найдено {failed_count}."
    if skipped:
        result += f" Пропущено: {skipped}."
    return result


@serialized
def sync_ym_to_sp() -> str:
    return _sync_direction("ym_to_sp")


@serialized
def sync_sp_to_ym() -> str:
    return _sync_direction("sp_to_ym")


@serialized
def select_pending_candidate(pend_key: str, candidate_id: str) -> tuple[bool, str]:
    """Select only a candidate recorded in this pending decision; do not call platform APIs."""
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn:
        row = conn.execute("SELECT diagnostics, purpose, found_id FROM pending_syncs WHERE key = ?",
                           (pend_key,)).fetchone()
        if not row:
            return False, "Трек уже обработан. Обновите /pending"
        diagnostics, purpose, found_id = row
        candidate = next((item for item in json.loads(diagnostics or "[]")
                          if item.get("id") == candidate_id), None)
        if not candidate:
            return False, "Кандидат больше не доступен. Обновите /pending"
        if purpose == "revalidate" and candidate_id != found_id:
            return False, "При проверке старой пары можно подтвердить только её текущий трек"
        probability = candidate.get("jev")
        rank = candidate.get("rank")
        score_type = "jev" if probability is not None else "metadata"
        score = round(probability * 100 if probability is not None else (rank or 0))
        with conn:
            conn.execute("UPDATE pending_syncs SET found_id = ?, found = ?, score = ?, "
                         "score_type = ?, metadata_rank = ?, jev_probability = ?, reasons = ? "
                         "WHERE key = ?", (candidate_id, candidate["label"], score, score_type,
                                         rank, probability,
                                         json.dumps(candidate.get("reasons", []), ensure_ascii=False), pend_key))
        return True, "Кандидат выбран"


@serialized
def approve_pending(pend_key: str, expected_candidate_id: str | None = None) -> tuple[bool, str]:
    """Apply an explicit human decision, including revalidation without re-liking."""
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn:
        row = conn.execute("SELECT direction, found_id, found, source, purpose, mode "
                           "FROM pending_syncs WHERE key = ?", (pend_key,)).fetchone()
        if not row:
            return False, "Трек не найден в ожидающих"
        direction, found_id, found_name, source_name, purpose, mode = row
        if expected_candidate_id is not None and found_id != expected_candidate_id:
            return False, "Выбранный кандидат изменился. Обновите /pending перед одобрением"
        source_id = pend_key.split(":", 1)[1]
        if direction not in ("ym_to_sp", "sp_to_ym"):
            return False, f"Неизвестное направление: {direction}"
        ym_id, sp_id = ((source_id, found_id) if direction == "ym_to_sp"
                        else (found_id, source_id))
        conflict = conn.execute("SELECT 1 FROM mappings WHERE "
                                "(ym_id = ? AND sp_id <> ?) OR (sp_id = ? AND ym_id <> ?)",
                                (ym_id, sp_id, sp_id, ym_id)).fetchone()
        if conflict:
            return False, "Конфликт mapping: один из ID уже связан с другим треком"
        if purpose != "revalidate":
            client = get_sp_client() if direction == "ym_to_sp" else get_ym_client()
            if not client:
                return False, "Клиент целевой платформы не настроен"
            operation = (client.current_user_saved_tracks_add if direction == "ym_to_sp"
                         else client.users_likes_tracks_add)
            kwargs = {"tracks": [found_id]} if direction == "ym_to_sp" else {"track_ids": [found_id]}
            _, error = api_call_with_retry(operation, **kwargs)
            if error:
                return False, explain_error(error)
        result = save_mapping(conn, ym_id, sp_id, mode or MatchingMode.HYBRID.value,
                              "manual_review")
        if result is MappingResult.CONFLICT:
            return False, "Конфликт mapping: один из ID уже связан с другим треком"
        with conn:
            conn.execute("DELETE FROM pending_syncs WHERE key = ?", (pend_key,))
        logging.info("Одобрен %s: %s -> %s", purpose, source_name, found_name)
        return True, ("Подтверждено: " if purpose == "revalidate" else "Добавлен: ") + found_name


@serialized
def reject_pending(pend_key):
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn:
        row = conn.execute("SELECT source, direction, found_id, purpose, mode "
                           "FROM pending_syncs WHERE key = ?", (pend_key,)).fetchone()
        if not row:
            return False, "Трек не найден в ожидающих"
        source_name, direction, found_id, purpose, mode = row
        source_id = pend_key.split(":", 1)[1]
        with conn:
            if purpose == "revalidate":
                ym_id, sp_id = ((source_id, found_id) if direction == "ym_to_sp"
                                else (found_id, source_id))
                conn.execute("DELETE FROM mappings WHERE ym_id = ? AND sp_id = ?", (ym_id, sp_id))
            conn.execute("DELETE FROM pending_syncs WHERE key = ?", (pend_key,))
        save_failed(conn, pend_key, source_name, mode or MatchingMode.HYBRID.value,
                    "manual_rejection")
        logging.info("Отклонён %s: %s", purpose, source_name)
        return True, f"Отклонён: {source_name}"


def check_duplicate(track1: dict, track2: dict) -> bool:
    """Conservative duplicate check using the same recording features as sync."""
    left = _as_track("local", {"id": track1.get("id", "1"), **track1})
    right = _as_track("local", {"id": track2.get("id", "2"), **track2})
    return metadata_features(left, right).safe_exact


def _duplicate_groups(tracks, timestamp_field):
    keep, duplicate = [], []
    for track in sorted(tracks, key=lambda row: row.get(timestamp_field, ""), reverse=True):
        (duplicate if any(check_duplicate(track, previous) for previous in keep) else keep).append(track)
    return duplicate


@serialized
def remove_spotify_duplicates():
    client = get_sp_client()
    if not client:
        return False, "Клиент Spotify не настроен"
    try:
        tracks = get_sp_likes(client)
    except (SpotifyException, SpotifyOauthError, OSError, TimeoutError, ValueError) as error:
        return False, explain_error(error)
    if not tracks:
        return True, "Библиотека Spotify пуста."
    duplicates = _duplicate_groups(tracks, "added_at")
    if not duplicates:
        return True, "Дубликаты в библиотеке Spotify не найдены."
    deleted, failed = [], []
    for index in range(0, len(duplicates), 50):
        ids = [track["id"] for track in duplicates[index:index + 50]]
        _, error = api_call_with_retry(client.current_user_saved_tracks_delete, tracks=ids)
        (failed if error else deleted).extend(ids)
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.executemany("DELETE FROM mappings WHERE sp_id = ?", ((id,) for id in deleted))
    if failed:
        return False, f"Удалено: {len(deleted)}. Не удалось удалить: {len(failed)}. Повторите позже."
    return True, [f"⚫ {track['artists']} - {track['title']}" for track in duplicates
                  if track["id"] in deleted]


@serialized
def remove_yandex_duplicates():
    client = get_ym_client()
    if not client:
        return False, "Клиент Яндекс Музыки не настроен"
    try:
        tracks = get_ym_likes(client)
    except (UnauthorizedError, BadRequestError, NotFoundError,
            OSError, TimeoutError, ValueError) as error:
        return False, explain_error(error)
    if not tracks:
        return True, "Библиотека Яндекс Музыки пуста."
    duplicates = _duplicate_groups(tracks, "timestamp")
    if not duplicates:
        return True, "Дубликаты в библиотеке Яндекс Музыки не найдены."
    deleted, failed = [], []
    for index in range(0, len(duplicates), 100):
        ids = [track["id"] for track in duplicates[index:index + 100]]
        _, error = api_call_with_retry(client.users_likes_tracks_remove, ids)
        (failed if error else deleted).extend(ids)
    init_db()
    with closing(sqlite3.connect(DB_FILE)) as conn, conn:
        conn.executemany("DELETE FROM mappings WHERE ym_id = ?", ((id,) for id in deleted))
    if failed:
        return False, f"Удалено: {len(deleted)}. Не удалось удалить: {len(failed)}. Повторите позже."
    return True, [f"⚫ {track['artists']} - {track['title']}" for track in duplicates
                  if track["id"] in deleted]


@serialized
def full_two_way_sync():
    """Execute complete two-way synchronization between Yandex Music and Spotify."""
    try:
        res1 = sync_ym_to_sp()
        res2 = sync_sp_to_ym()
        result = f"{res1}\n{res2}"
        set_setting("matching_revalidate", "0")
    except Exception as e:
        logging.error(f"Ошибка при синхронизации: {e}")
        set_setting("last_sync_result", explain_error(e))
        raise

    try:
        from datetime import datetime
        set_setting("last_sync_timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        set_setting("last_sync_result", result)
    except Exception:
        pass

    return result


@serialized
def revalidate_mappings():
    set_setting("matching_revalidate", "1")
    return full_two_way_sync()

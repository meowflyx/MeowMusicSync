import logging
import json
import os
import re
import unicodedata
import time
from yandex_music import Client
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from fuzzywuzzy import fuzz
from config import (
    SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI,
    YANDEX_MUSIC_TOKEN
)

MANIFEST_FILE = "manifest.json"

# Spotify хранит русских артистов латиницей, а Яндекс кириллицей.
# Без этой таблицы мы НИКОГДА не найдем Короля и Шута по запросу "Korol i Shut"
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
    "korol i shut": "Король и Шут",
    "piknik": "Пикник",
    "chizh": "Чиж",
    "chizh & co": "Чиж & Co",
    "lyapis trubetskoy": "Ляпис Трубецкой",
    "grazhdanskaya oborona": "Гражданская Оборона",
    "korol i shut": "Король и Шут",
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

# Посимвольная транслитерация латиница -> кириллица (фолбэк)
# Порядок важен: многобуквенные сочетания первыми
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

def translit_to_cyrillic(text):
    if any(ord(c) > 127 for c in text):
        return text
    result = text.lower()
    for lat, cyr in LATIN_TO_CYRILLIC:
        result = result.replace(lat, cyr)
    return result

def has_latin(text):
    return any('a' <= c.lower() <= 'z' for c in text)

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

def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            for key in ["failed", "pending"]:
                if key not in data:
                    data[key] = {}
            return data
    return {"yandex": {}, "spotify": {}, "failed": {}, "pending": {}}

def save_manifest(manifest):
    with open(MANIFEST_FILE, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

def get_ym_client():
    if not YANDEX_MUSIC_TOKEN:
        return None
    return Client(YANDEX_MUSIC_TOKEN).init()

def get_sp_client():
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
    title = REMASTER_PATTERN.sub('', title)
    title = FEAT_PATTERN.sub('', title)
    title = SUFFIX_PATTERN.sub('', title)
    title = title.strip(' -–—')
    return title

def normalize(text):
    text = text.lower().strip()
    text = unicodedata.normalize('NFKD', text)
    text = re.sub(r'[^\w\s]', ' ', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def translate_artist(artist_name):
    key = artist_name.lower().strip()
    if key in TRANSLIT_MAP:
        return TRANSLIT_MAP[key]
    if has_latin(artist_name):
        return translit_to_cyrillic(artist_name)
    return artist_name

def build_search_queries(artists_str, title):
    clean = clean_title(title)
    
    artists_list = [a.strip() for a in artists_str.split(',')]
    
    # Сначала пробуем таблицу известных артистов
    map_translated = [TRANSLIT_MAP.get(a.lower().strip(), a) for a in artists_list]
    map_str = ", ".join(map_translated)
    
    # Потом посимвольную транслитерацию
    char_translated = [translit_to_cyrillic(a) if has_latin(a) else a for a in artists_list]
    char_str = ", ".join(char_translated)
    
    queries = []
    
    # Попытка 1: артисты из таблицы + чистое название
    queries.append(f"{map_str} {clean}")
    
    # Попытка 2: только первый артист (таблица) + название
    queries.append(f"{map_translated[0]} {clean}")
    
    # Попытка 3: посимвольная транслитерация + название
    if char_str.lower() != map_str.lower():
        queries.append(f"{char_str} {clean}")
        queries.append(f"{char_translated[0]} {clean}")
    
    # Попытка 4: оригинальные артисты (латиница) + чистое название
    if artists_str.lower() != map_str.lower():
        queries.append(f"{artists_str} {clean}")
    
    # Попытка 5: только название
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
    q_title_clean = normalize(clean_title(query_title))
    r_title_clean = normalize(clean_title(result_title))
    
    title_ratio = fuzz.ratio(q_title_clean, r_title_clean)
    title_sort = fuzz.token_sort_ratio(q_title_clean, r_title_clean)
    title_score = max(title_ratio, title_sort)
    
    # Сравниваем артистов в нескольких формах и берём лучший результат
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

    # Тот же артист но title не идеально = скорее всего другой трек
    if title_score < 85 and artist_score >= 80:
        penalty += 15
    
    final = (title_score * 0.6) + (artist_score * 0.4) - penalty
    return max(0, final)

def match_track_spotify(query_artists, query_title, sp_client):
    queries = build_search_queries(query_artists, query_title)
    
    best_match = None
    best_score = 0
    best_name = ""
    
    for q in queries:
        try:
            results = sp_client.search(q=q, limit=5, type='track')
        except Exception:
            time.sleep(1)
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
        
        # Если нашли отличное совпадение, не делаем лишних запросов
        if best_score >= SCORE_AUTO_ACCEPT:
            break
        time.sleep(0.15)
    
    if best_score >= SCORE_PENDING:
        return best_match, best_name, best_score
    return None, None, best_score

def match_track_yandex(query_artists, query_title, ym_client):
    queries = build_search_queries(query_artists, query_title)
    
    best_match = None
    best_score = 0
    best_name = ""
    
    for q in queries:
        try:
            results = ym_client.search(q, type_='track')
        except Exception:
            time.sleep(1)
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
        time.sleep(0.15)
    
    if best_score >= SCORE_PENDING:
        return best_match, best_name, best_score
    return None, None, best_score

def parse_query(search_query):
    parts = search_query.split(' ', 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return search_query, ""

def get_ym_likes(ym_client, manifest):
    tracks_short = ym_client.users_likes_tracks().tracks
    if not tracks_short:
        return []
    
    res = []
    track_ids_to_fetch = []
    track_id_map = {}
    
    for t in tracks_short:
        tid = str(t.id)
        if tid in manifest["yandex"]:
            entry = manifest["yandex"][tid]
            if isinstance(entry, str):
                # Старый формат, мигрируем
                artists, title = parse_query(entry)
                manifest["yandex"][tid] = {"artists": artists, "title": title, "query": entry}
                entry = manifest["yandex"][tid]
            res.append({"id": tid, "artists": entry.get("artists", ""), "title": entry.get("title", ""), "search_query": entry.get("query", "")})
        else:
            fetch_id = f"{t.id}:{t.album_id}" if t.album_id else str(t.id)
            track_ids_to_fetch.append(fetch_id)
            track_id_map[fetch_id] = tid
    
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
            if not t: continue
            artists = ", ".join([a.name for a in t.artists]) if t.artists else "Unknown"
            title = t.title or ""
            query = f"{artists} {title}"
            tid = str(t.id)
            manifest["yandex"][tid] = {"artists": artists, "title": title, "query": query}
            res.append({"id": tid, "artists": artists, "title": title, "search_query": query})
            
        save_manifest(manifest)
    return res

def get_sp_likes(sp_client, manifest):
    res = []
    results = sp_client.current_user_saved_tracks(limit=50)
    new_tracks_found = False
    
    while results:
        for item in results['items']:
            track = item['track']
            tid = track['id']
            if tid in manifest["spotify"]:
                entry = manifest["spotify"][tid]
                if isinstance(entry, str):
                    artists_list = [a['name'] for a in track['artists']]
                    artists = ", ".join(artists_list)
                    title = track['name']
                    manifest["spotify"][tid] = {"artists": artists, "title": title, "query": entry}
                    entry = manifest["spotify"][tid]
                    new_tracks_found = True
                res.append({"id": tid, "artists": entry.get("artists", ""), "title": entry.get("title", ""), "search_query": entry.get("query", "")})
            else:
                artists = ", ".join([a['name'] for a in track['artists']])
                title = track['name']
                query = f"{artists} {title}"
                manifest["spotify"][tid] = {"artists": artists, "title": title, "query": query}
                res.append({"id": tid, "artists": artists, "title": title, "search_query": query})
                new_tracks_found = True
                
        if results['next']:
            results = sp_client.next(results)
        else:
            break
            
    if new_tracks_found:
        save_manifest(manifest)
    return res

def is_already_present(track_artists, track_title, existing_tracks):
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
            return True
    return False

def sync_ym_to_sp(log_callback=None, pending_callback=None):
    ym_client = get_ym_client()
    sp_client = get_sp_client()
    if not ym_client or not sp_client:
        return "Клиенты не настроены"

    manifest = load_manifest()
    ym_likes = get_ym_likes(ym_client, manifest)
    sp_likes = get_sp_likes(sp_client, manifest)
    
    added = 0
    failed = []
    pending_count = 0
    skipped = 0
    
    for ym_track in ym_likes:
        artists = ym_track.get('artists', '')
        title = ym_track.get('title', '')
        query = ym_track.get('search_query', '')
        
        fail_key = f"ym_to_sp:{ym_track['id']}"
        pend_key = fail_key
        
        if fail_key in manifest["failed"] or pend_key in manifest["pending"]:
            skipped += 1
            continue
        
        if is_already_present(artists, title, sp_likes):
            continue
            
        sp_id, best_name, score = match_track_spotify(artists, title, sp_client)
        if sp_id and score >= SCORE_AUTO_ACCEPT:
            sp_client.current_user_saved_tracks_add(tracks=[sp_id])
            manifest["spotify"][sp_id] = {"artists": artists, "title": title, "query": best_name}
            sp_likes.append({"id": sp_id, "artists": artists, "title": title, "search_query": best_name})
            logging.info(f"✅ [{score:.0f}%] Добавлен в Spotify: '{query}' -> как '{best_name}'")
            added += 1
        elif sp_id and score >= SCORE_PENDING:
            manifest["pending"][pend_key] = {
                "direction": "ym_to_sp",
                "source": query,
                "found": best_name,
                "found_id": sp_id,
                "score": round(score),
            }
            logging.info(f"⏳ [{score:.0f}%] Ожидает одобрения: '{query}' -> '{best_name}'")
            pending_count += 1
            if pending_callback:
                pending_callback(pend_key, query, best_name, round(score), "ym_to_sp")
        else:
            logging.warning(f"❌ [{score:.0f}%] Не найден в Spotify: '{query}'")
            manifest["failed"][fail_key] = query
            failed.append(query)
                
    save_manifest(manifest)
    msg = f"Яндекс → Spotify: добавлено {added}, на одобрении {pending_count}, не найдено {len(failed)}."
    if skipped:
        msg += f" Пропущено: {skipped}."
    return msg

def sync_sp_to_ym(log_callback=None, pending_callback=None):
    ym_client = get_ym_client()
    sp_client = get_sp_client()
    if not ym_client or not sp_client:
        return "Клиенты не настроены"

    manifest = load_manifest()
    sp_likes = get_sp_likes(sp_client, manifest)
    ym_likes = get_ym_likes(ym_client, manifest)
    
    added = 0
    failed = []
    pending_count = 0
    skipped = 0
    
    for sp_track in sp_likes:
        artists = sp_track.get('artists', '')
        title = sp_track.get('title', '')
        query = sp_track.get('search_query', '')
        
        fail_key = f"sp_to_ym:{sp_track['id']}"
        pend_key = fail_key
        
        if fail_key in manifest["failed"] or pend_key in manifest["pending"]:
            skipped += 1
            continue
        
        if is_already_present(artists, title, ym_likes):
            continue
            
        ym_id, best_name, score = match_track_yandex(artists, title, ym_client)
        if ym_id and score >= SCORE_AUTO_ACCEPT:
            ym_client.users_likes_tracks_add(track_ids=[ym_id])
            manifest["yandex"][str(ym_id)] = {"artists": artists, "title": title, "query": best_name}
            ym_likes.append({"id": str(ym_id), "artists": artists, "title": title, "search_query": best_name})
            logging.info(f"✅ [{score:.0f}%] Добавлен в Яндекс: '{query}' -> как '{best_name}'")
            added += 1
        elif ym_id and score >= SCORE_PENDING:
            manifest["pending"][pend_key] = {
                "direction": "sp_to_ym",
                "source": query,
                "found": best_name,
                "found_id": str(ym_id),
                "score": round(score),
            }
            logging.info(f"⏳ [{score:.0f}%] Ожидает одобрения: '{query}' -> '{best_name}'")
            pending_count += 1
            if pending_callback:
                pending_callback(pend_key, query, best_name, round(score), "sp_to_ym")
        else:
            logging.warning(f"❌ [{score:.0f}%] Не найден в Яндексе: '{query}'")
            manifest["failed"][fail_key] = query
            failed.append(query)

    save_manifest(manifest)
    msg = f"Spotify → Яндекс: добавлено {added}, на одобрении {pending_count}, не найдено {len(failed)}."
    if skipped:
        msg += f" Пропущено: {skipped}."
    return msg

def approve_pending(pend_key):
    manifest = load_manifest()
    if pend_key not in manifest["pending"]:
        return False, "Трек не найден в ожидающих"
    
    entry = manifest["pending"][pend_key]
    direction = entry["direction"]
    found_id = entry["found_id"]
    found_name = entry["found"]
    source_name = entry["source"]
    
    if direction == "sp_to_ym":
        ym_client = get_ym_client()
        if not ym_client:
            return False, "Яндекс клиент не настроен"
        ym_client.users_likes_tracks_add(track_ids=[found_id])
        manifest["yandex"][found_id] = {"artists": "", "title": "", "query": found_name}
        logging.info(f"✅ Одобрен: '{source_name}' -> '{found_name}'")
    elif direction == "ym_to_sp":
        sp_client = get_sp_client()
        if not sp_client:
            return False, "Spotify клиент не настроен"
        sp_client.current_user_saved_tracks_add(tracks=[found_id])
        manifest["spotify"][found_id] = {"artists": "", "title": "", "query": found_name}
        logging.info(f"✅ Одобрен: '{source_name}' -> '{found_name}'")
    
    del manifest["pending"][pend_key]
    save_manifest(manifest)
    return True, f"Добавлен: {found_name}"

def reject_pending(pend_key):
    manifest = load_manifest()
    if pend_key not in manifest["pending"]:
        return False, "Трек не найден в ожидающих"
    
    entry = manifest["pending"][pend_key]
    manifest["failed"][pend_key] = entry["source"]
    del manifest["pending"][pend_key]
    save_manifest(manifest)
    logging.info(f"❌ Отклонён: '{entry['source']}'")
    return True, f"Отклонён: {entry['source']}"

def full_two_way_sync(log_callback=None, pending_callback=None):
    res1 = sync_ym_to_sp(log_callback, pending_callback)
    res2 = sync_sp_to_ym(log_callback, pending_callback)
    return f"{res1}\n{res2}"

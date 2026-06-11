import logging
import json
import os
from yandex_music import Client
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from fuzzywuzzy import fuzz
from config import (
    SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI,
    YANDEX_MUSIC_TOKEN
)

MANIFEST_FILE = "manifest.json"

def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        with open(MANIFEST_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"yandex": {}, "spotify": {}}

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

def get_ym_likes(ym_client, manifest):
    tracks_short = ym_client.users_likes_tracks().tracks
    if not tracks_short:
        return []
    
    res = []
    track_ids_to_fetch = []
    
    # Check manifest first
    for t in tracks_short:
        tid = str(t.id)
        if tid in manifest["yandex"]:
            res.append({
                "id": tid,
                "search_query": manifest["yandex"][tid]
            })
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
            if not t: continue
            artists = ", ".join([a.name for a in t.artists]) if t.artists else "Unknown"
            query = f"{artists} {t.title}"
            manifest["yandex"][str(t.id)] = query
            res.append({
                "id": str(t.id),
                "search_query": query
            })
            
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
                res.append({
                    "id": tid,
                    "search_query": manifest["spotify"][tid]
                })
            else:
                artists = ", ".join([a['name'] for a in track['artists']])
                query = f"{artists} {track['name']}"
                manifest["spotify"][tid] = query
                res.append({
                    "id": tid,
                    "search_query": query
                })
                new_tracks_found = True
                
        if results['next']:
            results = sp_client.next(results)
        else:
            break
            
    if new_tracks_found:
        save_manifest(manifest)
    return res

def smart_match_score(query, target):
    q = query.lower()
    t = target.lower()
    set_score = fuzz.token_set_ratio(q, t)
    sort_score = fuzz.token_sort_ratio(q, t)
    
    # Penalize remix/cover mismatches heavily
    if ("remix" in q) != ("remix" in t):
        set_score -= 30
    if ("cover" in q) != ("cover" in t):
        set_score -= 30
    if ("live" in q) != ("live" in t):
        set_score -= 20
        
    return (set_score * 0.7) + (sort_score * 0.3)

def match_track(query, target_platform, sp_client=None, ym_client=None):
    if target_platform == "spotify":
        results = sp_client.search(q=query, limit=5, type='track')
        if not results['tracks']['items']:
            return None, None
        
        best_match = None
        best_score = 0
        best_name = ""
        
        for t in results['tracks']['items']:
            artists = ", ".join([a['name'] for a in t['artists']])
            title = t['name']
            target_str = f"{artists} {title}"
            score = smart_match_score(query, target_str)
            if score > best_score:
                best_score = score
                best_match = t['id']
                best_name = target_str
                
        if best_score >= 80:
            return best_match, best_name
        return None, None

    elif target_platform == "yandex":
        results = ym_client.search(query, type_='track')
        if not results.tracks or not results.tracks.results:
            return None, None
            
        best_match = None
        best_score = 0
        best_name = ""
        
        for t in results.tracks.results:
            artists = ", ".join([a.name for a in t.artists]) if t.artists else "Unknown"
            target_str = f"{artists} {t.title}"
            score = smart_match_score(query, target_str)
            if score > best_score:
                best_score = score
                best_match = t.id
                best_name = target_str
                
        if best_score >= 80:
            return best_match, best_name
        return None, None

def sync_ym_to_sp(log_callback=None):
    ym_client = get_ym_client()
    sp_client = get_sp_client()
    if not ym_client or not sp_client:
        return "Clients not configured"

    manifest = load_manifest()
    ym_likes = get_ym_likes(ym_client, manifest)
    sp_likes = get_sp_likes(sp_client, manifest)
    
    sp_search_queries = [t['search_query'].lower() for t in sp_likes]
    
    added = 0
    failed = []
    
    for ym_track in ym_likes:
        query = ym_track['search_query']
        if query.lower() in sp_search_queries:
            continue
            
        sp_id, best_name = match_track(query, "spotify", sp_client=sp_client)
        if sp_id:
            sp_client.current_user_saved_tracks_add(tracks=[sp_id])
            manifest["spotify"][sp_id] = best_name
            sp_search_queries.append(best_name.lower())
            logging.info(f"✅ Добавлен в Spotify: '{query}' -> как '{best_name}'")
            added += 1
        else:
            logging.warning(f"❌ Не найден в Spotify: '{query}'")
            failed.append(query)
            if log_callback:
                log_callback(f"Не найден в Spotify: {query}")
                
    save_manifest(manifest)
    return f"Яндекс -> Spotify завершено. Добавлено: {added}. Ошибок: {len(failed)}"

def sync_sp_to_ym(log_callback=None):
    ym_client = get_ym_client()
    sp_client = get_sp_client()
    if not ym_client or not sp_client:
        return "Clients not configured"

    manifest = load_manifest()
    sp_likes = get_sp_likes(sp_client, manifest)
    ym_likes = get_ym_likes(ym_client, manifest)
    
    ym_search_queries = [t['search_query'].lower() for t in ym_likes]
    
    added = 0
    failed = []
    
    for sp_track in sp_likes:
        query = sp_track['search_query']
        if query.lower() in ym_search_queries:
            continue
            
        ym_id, best_name = match_track(query, "yandex", ym_client=ym_client)
        if ym_id:
            ym_client.users_likes_tracks_add(track_ids=[ym_id])
            manifest["yandex"][str(ym_id)] = best_name
            ym_search_queries.append(best_name.lower())
            logging.info(f"✅ Добавлен в Яндекс: '{query}' -> как '{best_name}'")
            added += 1
        else:
            logging.warning(f"❌ Не найден в Яндексе: '{query}'")
            failed.append(query)
            if log_callback:
                log_callback(f"Не найден в Яндексе: {query}")

    save_manifest(manifest)
    return f"Spotify -> Яндекс завершено. Добавлено: {added}. Ошибок: {len(failed)}"

def full_two_way_sync(log_callback=None):
    res1 = sync_ym_to_sp(log_callback)
    res2 = sync_sp_to_ym(log_callback)
    return f"{res1}\n{res2}"

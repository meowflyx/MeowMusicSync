"""Collect platform search results and existing liked tracks without deciding matches."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from matching import THRESHOLDS, Track, latin_to_cyrillic, metadata_features, normalize


def search_queries(source: Track) -> list[str]:
    artist = source.artists[0] if source.artists else ""
    original = f"{artist} {source.title}".strip()
    latin = normalize(original)
    cyrillic = latin_to_cyrillic(original)
    return list(dict.fromkeys(query for query in (original, cyrillic, latin, source.title)
                              if query.strip()))


def spotify_track(item: dict) -> Track:
    album = item.get("album") or {}
    return Track("spotify", str(item.get("id") or ""), item.get("name") or "",
                 tuple(artist["name"] for artist in item.get("artists") or []),
                 album.get("name"), item.get("duration_ms") or None,
                 (item.get("external_ids") or {}).get("isrc"), item.get("explicit"))


def yandex_track(item, *, original: bool = False) -> Track:
    # A substituted result is a playable replacement; liked records retain original identity.
    selected = item.substituted if original and getattr(item, "substituted", None) else item
    album = (getattr(selected, "albums", None) or [None])[0]
    version = getattr(selected, "version", None)
    title = selected.title or ""
    if version and normalize(version) not in normalize(title):
        title = f"{title} ({version})"
    explicit = getattr(selected, "explicit", None)
    if getattr(selected, "content_warning", None) == "explicit":
        explicit = True
    return Track("yandex", str(item.id), title,
                 tuple(artist.name for artist in selected.artists or []),
                 getattr(album, "title", None), getattr(selected, "duration_ms", None) or None,
                 getattr(selected, "isrc", None), explicit, version)


def _existing_pool(source: Track, existing: Iterable[Track]) -> dict[str, Track]:
    # Restrict a potentially large library by cheap title relevance, never by legacy score.
    return {track.id: track for track in existing if track.id and
            metadata_features(source, track).title_similarity >= THRESHOLDS.min_title_similarity}


def discover_spotify(source: Track, client, existing: Iterable[Track] = (),
                     retry_call: Callable | None = None) -> list[Track]:
    pool = _existing_pool(source, existing)
    for query in search_queries(source):
        if retry_call:
            result, error = retry_call(client.search, q=query, limit=10, type="track")
            if error:
                raise error
        else:
            result = client.search(q=query, limit=10, type="track")
        for item in result["tracks"]["items"]:
            candidate = spotify_track(item)
            if candidate.id and candidate.title:
                pool.setdefault(candidate.id, candidate)
    return list(pool.values())


def discover_yandex(source: Track, client, existing: Iterable[Track] = (),
                    retry_call: Callable | None = None) -> list[Track]:
    pool = _existing_pool(source, existing)
    for query in search_queries(source):
        if retry_call:
            result, error = retry_call(client.search, query, type_="track")
            if error:
                raise error
        else:
            result = client.search(query, type_="track")
        for item in (getattr(getattr(result, "tracks", None), "results", None) or [])[:20]:
            candidate = yandex_track(item)
            if candidate.id and candidate.title:
                pool.setdefault(candidate.id, candidate)
    return list(pool.values())

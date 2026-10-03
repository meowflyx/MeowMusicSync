"""Jev match decision and encrypted API credential storage."""

import json
import math
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from matching import FEATURED, Track, censorship_features, title_features


KEY_FILE = ".jev.key"
PROVIDERS = {
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "jev-latest", "noul", "noul"),
    "openrouter": ("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "noul", "noul"),
    "vercel": ("https://ai-gateway.vercel.sh/v1/evaluate", "typesafe-ai/jev", "boolean", "probability"),
}


def _cipher(create=False):
    if create:
        try:
            fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(Fernet.generate_key())
    try:
        os.chmod(KEY_FILE, 0o600)
        with open(KEY_FILE, "rb") as stream:
            return Fernet(stream.read())
    except (OSError, ValueError) as exc:
        raise RuntimeError("Ключ шифрования Jev недоступен: восстановите .jev.key из резервной копии.") from exc


def encrypt_key(api_key):
    return _cipher(create=True).encrypt(api_key.encode()).decode()


def decrypt_key(ciphertext):
    try:
        return _cipher().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Не удалось расшифровать ключ Jev: проверьте .jev.key.") from exc


def _record(track: Track) -> dict:
    title = title_features(track)
    censorship = censorship_features(track)
    return {"platform": track.platform, "id": track.id, "title": track.title,
            "artists": track.artists, "album": track.album,
            "duration_ms": track.duration_ms, "isrc": track.isrc,
            "explicit": track.explicit, "version": track.version,
            "base_title": title.base_title, "version_markers": sorted(title.version_markers),
            "version_descriptions": sorted(title.version_descriptions),
            "censorship_hints": censorship.hints, "uncensored_evidence": censorship.uncensored,
            "featured_artists": [match.group(1) for match in FEATURED.finditer(track.title)]}


def match_probability(provider: str, api_key: str, yandex: Track, spotify: Track) -> float:
    """Return Jev's probability that two platform records describe the same recording."""
    if provider not in PROVIDERS:
        raise ValueError("Провайдер Jev: typesafe, openrouter или vercel.")
    url, model, question_type, answer_field = PROVIDERS[provider]
    body = {
        "model": model,
        "state": {"yandex": _record(yandex), "spotify": _record(spotify)},
        "questions": {"same_track": {
            "type": question_type,
            "instructions": (
                "Are these catalog entries the same musical recording/version and safe to treat "
                "as equivalent in library synchronization? A recording remains the same across "
                "a single, album, deluxe edition, or compilation if the audio is the same. "
                "Artist credits may put a guest in the artists list, the title's feat. clause, "
                "or both; count each person once. Nearly identical duration and the same title "
                "and full artist lineup are strong positive evidence when there is no contrary "
                "version evidence. Do not treat a different album name alone as evidence of a "
                "different recording. A remaster, remix, live, acoustic, radio edit, cover, "
                "instrumental, slowed, sped-up, demo, or rerecording is a different version. "
                "Version clues can occur in the track title, version field, or album title; "
                "compare them. Compare ISRCs when both are supplied. Missing fields are unknown, not mismatches. "
                "Different named remixes, remixers, mixes, edits or live venues are different versions; "
                "a shared generic marker such as remix does not establish recording identity. "
                "A clean, censored, bleeped, edited, family-friendly, radio or TV "
                "edit is not equivalent to its uncensored/explicit recording, even with similar "
                "duration or shared ISRC. A masked title (e.g. ***** Please II) is a censorship "
                "clue; consider the explicit flags and other metadata. Explicit, uncensored, "
                "dirty or unedited labels describe the uncut recording, not a remix. "
                "uncensored_evidence=false means no positive uncensored clue, not proof of a clean edit. "
                "explicit=false alone does not prove censorship: many originals contain no "
                "explicit lyrics. Preserve a clean source's version; do not upgrade it to explicit."
            ),
        }},
    }
    request = Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=10) as response:
                answer = json.load(response)["answers"]["same_track"][answer_field]
            break
        except HTTPError as exc:
            if exc.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Jev API вернул HTTP {exc.code}; проверьте провайдера и ключ.") from None
        except (URLError, TimeoutError):
            raise RuntimeError("Jev API недоступен; синхронизация остановлена.") from None
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Jev API вернул некорректный ответ; синхронизация остановлена.") from None
    if isinstance(answer, bool) or not isinstance(answer, (int, float)) or not math.isfinite(answer) or not 0 <= answer <= 1:
        raise RuntimeError("Jev API вернул некорректную вероятность; синхронизация остановлена.")
    return answer

"""Track features and the shared decision engine for both sync directions."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from typing import Callable, Iterable


ALGORITHM_VERSION = 3


class MatchingMode(str, Enum):
    HYBRID = "hybrid"
    JEV_ONLY = "jev_only"


class DecisionKind(str, Enum):
    MATCH = "match"
    REVIEW = "review"
    REJECT = "reject"
    NO_CANDIDATE = "no_candidate"


@dataclass(frozen=True)
class MatchingThresholds:
    jev_match: float = 0.85
    jev_review: float = 0.55
    jev_margin: float = 0.08
    metadata_review: float = 55.0
    metadata_margin: float = 5.0
    min_title_similarity: float = 0.3
    duration_tolerance_ms: int = 5_000
    duration_conflict_ms: int = 15_000
    max_jev_candidates: int = 5


THRESHOLDS = MatchingThresholds()


@dataclass(frozen=True)
class Track:
    platform: str
    id: str
    title: str
    artists: tuple[str, ...]
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    explicit: bool | None = None
    version: str | None = None

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} — {self.title}"


@dataclass(frozen=True)
class Features:
    rank: float
    safe_exact: bool
    reasons: tuple[str, ...]
    title_similarity: float
    artist_similarity: float
    version_markers: tuple[str, ...]
    conflict: bool = False


@dataclass(frozen=True)
class CandidateEvidence:
    candidate: Track
    features: Features
    jev_probability: float | None = None


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    mode: MatchingMode
    candidate: Track | None = None
    source: str = "metadata"
    metadata_rank: float | None = None
    jev_probability: float | None = None
    reasons: tuple[str, ...] = ()
    considered: tuple[CandidateEvidence, ...] = ()


CYRILLIC = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ("a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n",
     "o", "p", "r", "s", "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"),
))
LATIN_DIGRAPHS = (("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("ch", "ч"),
                  ("sh", "ш"), ("ya", "я"), ("yu", "ю"), ("yo", "ё"),
                  ("kh", "х"), ("ts", "ц"))
LATIN_LETTERS = dict(zip("abvgdezijklmnoprstufhcy", "абвгдезийклмнопрстуфхцы"))
ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*", re.I)
FEATURED = re.compile(r"[([]\s*(?:feat\.?|ft\.?|featuring)\s+([^])]+)[])]", re.I)
MARKERS = re.compile(
    r"\b(?:remaster(?:ed)?|remix|live|acoustic|instrumental|radio\s+edit|extended|"
    r"sped\s*up|slowed|demo|cover|re[ -]?record(?:ed|ing)?|version|edit|mix)\b", re.I)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(CYRILLIC.get(char, char) for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"\w+", value))


def latin_to_cyrillic(value: str) -> str:
    result = value.casefold()
    for latin, cyrillic in LATIN_DIGRAPHS:
        result = result.replace(latin, cyrillic)
    return "".join(LATIN_LETTERS.get(char, char) for char in result)


def artist_names(track: Track) -> frozenset[str]:
    names = list(track.artists)
    names.extend(match.group(1) for match in FEATURED.finditer(track.title))
    return frozenset(normalize(part) for value in names for part in ARTIST_SPLIT.split(value)
                     if normalize(part))


def title_features(track: Track) -> tuple[str, frozenset[str]]:
    title = FEATURED.sub("", track.title)
    text = f"{title} {track.version or ''}"
    markers = {match.group().casefold().replace(" ", "_") for match in MARKERS.finditer(text)}
    if any(marker.startswith("remaster") for marker in markers):
        markers = {"remaster" if marker.startswith("remaster") else marker for marker in markers}
        markers.update(f"remaster_{year}" for year in YEAR.findall(text))
    if track.album and re.search(r"\bremaster(?:ed)?\b", track.album, re.I):
        markers.add("remaster")
        markers.update(f"remaster_{year}" for year in YEAR.findall(track.album))
    base = title
    # Remove only a suffix whose content contains a version marker; keep its feature above.
    for separator in (r"\s*[-–—]\s*", r"\s*[([]\s*"):
        parts = re.split(separator, base, maxsplit=1)
        if len(parts) == 2 and MARKERS.search(parts[1]):
            base = parts[0]
            break
    return normalize(base), frozenset(markers)


def metadata_features(left: Track, right: Track) -> Features:
    left_title, left_markers = title_features(left)
    right_title, right_markers = title_features(right)
    title_ratio = SequenceMatcher(None, left_title, right_title).ratio() if left_title and right_title else 0.0
    left_artists, right_artists = artist_names(left), artist_names(right)
    artist_ratio = len(left_artists & right_artists) / len(left_artists | right_artists) if left_artists | right_artists else 0.0
    reasons = []
    conflict = False
    if left_markers != right_markers:
        reasons.append("version_markers_differ")
        conflict = True
    if left_artists != right_artists:
        reasons.append("artists_differ")
    if left.isrc and right.isrc:
        if left.isrc.casefold() == right.isrc.casefold():
            reasons.append("isrc_equal")
        else:
            reasons.append("isrc_conflict")
            conflict = True
    if left.duration_ms is not None and right.duration_ms is not None:
        delta = abs(left.duration_ms - right.duration_ms)
        if delta > THRESHOLDS.duration_conflict_ms:
            reasons.append("duration_conflict")
            conflict = True
        elif delta > THRESHOLDS.duration_tolerance_ms:
            reasons.append("duration_differs")
    if left_title == right_title and left_title:
        reasons.append("title_equal")
    rank = 65 * title_ratio + 35 * artist_ratio
    if "isrc_equal" in reasons:
        rank += 20
    if conflict:
        rank -= 35
    duration_confirmed = (left.duration_ms is not None and right.duration_ms is not None
                          and abs(left.duration_ms - right.duration_ms) <= THRESHOLDS.duration_tolerance_ms)
    safe_exact = bool(left_title and left_title == right_title and left_artists == right_artists
                      and left_markers == right_markers and not conflict
                      and (duration_confirmed or "isrc_equal" in reasons))
    if "isrc_equal" in reasons and not conflict and title_ratio >= .8 and artist_ratio > 0:
        safe_exact = True
    return Features(max(0.0, min(100.0, rank)), safe_exact, tuple(reasons),
                    title_ratio, artist_ratio, tuple(sorted(left_markers ^ right_markers)), conflict)


def _equivalent_releases(left: Track, right: Track) -> bool:
    """Resolve a Jev tie when only release identifiers differ."""
    features = metadata_features(left, right)
    return (features.title_similarity == 1 and features.artist_similarity == 1
            and not features.version_markers
            and left.duration_ms is not None and right.duration_ms is not None
            and abs(left.duration_ms - right.duration_ms) <= THRESHOLDS.duration_tolerance_ms
            and (left.explicit is None or right.explicit is None
                 or left.explicit == right.explicit))


class MatchingEngine:
    def __init__(self, mode: MatchingMode, verify: Callable[[Track, Track], float] | None = None):
        self.mode = MatchingMode(mode)
        self.verify = verify
        if self.mode is MatchingMode.JEV_ONLY and verify is None:
            raise ValueError("Jev не настроен для режима jev_only")

    def decide(self, source: Track, candidates: Iterable[Track]) -> Decision:
        unique = {candidate.id: candidate for candidate in candidates if candidate.id and candidate.title}
        ranked = sorted((CandidateEvidence(candidate, metadata_features(source, candidate))
                         for candidate in unique.values()), key=lambda item: item.features.rank, reverse=True)
        if not ranked:
            return Decision(DecisionKind.NO_CANDIDATE, self.mode, reasons=("catalog_empty",))
        # Jev sees plausible titles even when local scoring penalizes a version or guest artist.
        plausible = [item for item in ranked if item.features.title_similarity >= THRESHOLDS.min_title_similarity or
                     "isrc_equal" in item.features.reasons]
        if not plausible:
            return Decision(DecisionKind.REJECT, self.mode, reasons=("no_plausible_title",),
                            considered=tuple(ranked))
        if self.verify:
            evaluated = [CandidateEvidence(item.candidate, item.features,
                         self.verify(source, item.candidate))
                         for item in plausible[:THRESHOLDS.max_jev_candidates]]
            evaluated.sort(key=lambda item: item.jev_probability, reverse=True)
            best = evaluated[0]
            close = [item for item in evaluated[1:]
                     if item.jev_probability >= THRESHOLDS.jev_review
                     and best.jev_probability - item.jev_probability < THRESHOLDS.jev_margin]
            equivalent_releases = bool(close) and all(
                item.jev_probability >= THRESHOLDS.jev_match
                and not item.features.conflict
                and _equivalent_releases(best.candidate, item.candidate) for item in close)
            probability = best.jev_probability
            if probability >= THRESHOLDS.jev_match and (not close or equivalent_releases):
                kind = DecisionKind.MATCH if not best.features.conflict else DecisionKind.REVIEW
            elif probability >= THRESHOLDS.jev_review:
                kind = DecisionKind.REVIEW
            else:
                kind = DecisionKind.REJECT
            if probability < THRESHOLDS.jev_review:
                verdict = "jev_rejected"
            elif close and not equivalent_releases:
                verdict = "jev_close_candidates"
            elif equivalent_releases:
                verdict = "equivalent_release_candidates"
            else:
                verdict = "jev_evaluated"
            reasons = (*best.features.reasons, verdict)
            return Decision(kind, self.mode, best.candidate, "jev", best.features.rank,
                            probability, reasons, tuple(evaluated))
        best = plausible[0]
        next_rank = plausible[1].features.rank if len(plausible) > 1 else 0.0
        if best.features.safe_exact and next_rank < best.features.rank - THRESHOLDS.metadata_margin:
            kind = DecisionKind.MATCH
        elif best.features.rank >= THRESHOLDS.metadata_review:
            kind = DecisionKind.REVIEW
        else:
            kind = DecisionKind.REJECT
        return Decision(kind, self.mode, best.candidate, "metadata", best.features.rank,
                        reasons=best.features.reasons, considered=tuple(ranked))

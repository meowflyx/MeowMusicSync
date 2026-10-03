"""Track features and the shared decision engine for both sync directions."""

from __future__ import annotations

import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import Enum
from itertools import repeat
from typing import Callable, Iterable


ALGORITHM_VERSION = 5
JEV_PARALLELISM = 3


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
    jev_margin: float = 0.05
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
class TitleMetadata:
    base_title: str
    version_markers: frozenset[str]
    version_descriptions: frozenset[str]


@dataclass(frozen=True)
class Features:
    rank: float
    safe_exact: bool
    reasons: tuple[str, ...]
    title_similarity: float
    artist_similarity: float
    version_markers: tuple[str, ...]
    conflict: bool = False
    blocked: bool = False
    version_descriptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Censorship:
    """Version clues; uncensored means positive evidence, never 'not clean'."""

    hints: tuple[str, ...]
    uncensored: bool


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
    r"\b(?:remaster(?:ed)?|remix|live|acoustic|instrumental|radio[ -]+(?:edit|version|mix)|tv[ -]+edit|extended|"
    r"sped\s*up|slowed|demo|cover|re[ -]?record(?:ed|ing)?|version|edit|mix)\b", re.I)
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
VERSION_LABELS = re.compile(r"\(([^()]*)\)|\[([^\[\]]*)\]|\s*[-–—]\s*(.+)$")
CLEAN_LABELS = re.compile(
    r"\b(?:squeaky[ -]+clean|clean|censored|edited|amended|bleeped|family[ -]+friendly|"
    r"(?:non|not|no)[ -]+explicit|without[ -]+swearing|"
    r"без\s+мата|без\s+ненормативной\s+лексики|цензурная)"
    r"(?:[ -]+(?:version|edit|mix|lyrics|audio))?\b", re.I)
RADIO_LABELS = re.compile(r"\b(?:radio[ -]+(?:edit|version|mix)|tv[ -]+edit)\b", re.I)
UNCENSORED_LABELS = re.compile(
    r"\b(?:explicit|uncensored|unedited|dirty|без\s+цензуры)(?:[ -]+(?:version|edit|mix))?\b", re.I)
MASKED_TITLE = re.compile(r"\*{2,}|(?<=\w)\*+(?=\w)|\b[fbs][*#!@$]{2,}\w*", re.I)


def _has_version_label(pattern: re.Pattern[str], labels: Iterable[str]) -> bool:
    # Single words also occur in artist names, e.g. Clean Bandit / Dirty South.
    return any(match.group().casefold() not in {"clean", "dirty", "explicit"}
               or normalize(match.group()) == normalize(label)
               for label in labels for match in pattern.finditer(label))


def censorship_features(track: Track) -> Censorship:
    """Read version labels, not words in ordinary song names or explicit=False alone."""
    labels = [track.version or ""]
    title = FEATURED.sub("", track.title)
    for value in (title, track.album or ""):
        labels.extend(part for match in VERSION_LABELS.finditer(value)
                      for part in match.groups() if part)
        # Multiword version labels sometimes arrive without brackets/separators.
        labels.extend(match.group() for match in re.finditer(
            r"\b(?:clean|censored|edited|explicit|dirty)[ -]+(?:version|edit|mix)\b",
            value, re.I))
    if track.album and (CLEAN_LABELS.fullmatch(track.album) or
                        UNCENSORED_LABELS.fullmatch(track.album)):
        labels.append(track.album)
    hints = {"clean_version"} if _has_version_label(CLEAN_LABELS, labels) else set()
    if any(RADIO_LABELS.search(value) for value in (title, track.version or "", track.album or "")):
        hints.add("radio_edit")
    if MASKED_TITLE.search(title):
        hints.add("masked_title")
    uncensored = track.explicit is True or _has_version_label(UNCENSORED_LABELS, labels)
    return Censorship(tuple(sorted(hints)), uncensored and not hints)


def _censorship_conflict(source: Track, candidate: Track) -> bool:
    left, right = censorship_features(source), censorship_features(candidate)
    if ("radio_edit" in left.hints) != ("radio_edit" in right.hints):
        return True
    if not left.hints:
        return bool(right.hints) or (left.uncensored and candidate.explicit is False)
    if right.uncensored:
        return True
    # A known clean source may have an unlabeled counterpart marked nonexplicit.
    return not right.hints and candidate.explicit is not False


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


def _version_description(label: str) -> str | None:
    text = label
    for pattern in (CLEAN_LABELS, UNCENSORED_LABELS):
        if _has_version_label(pattern, [label]):
            text = pattern.sub("", text)
    markers = {"remaster" if match.group().casefold().startswith("remaster")
               else normalize(match.group()) for match in MARKERS.finditer(text)}
    if not markers:
        return None
    identity = normalize(MARKERS.sub("", text)).removeprefix("by ")
    return " ".join(part for part in (identity, *sorted(markers)) if part)


def title_features(track: Track) -> TitleMetadata:
    title = FEATURED.sub("", track.title)
    text = f"{title} {track.version or ''}"
    version_text = UNCENSORED_LABELS.sub("", CLEAN_LABELS.sub("", text))
    markers = {match.group().casefold().replace(" ", "_").replace("-", "_")
               for match in MARKERS.finditer(version_text)}
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
        if len(parts) == 2 and any(pattern.search(parts[1]) for pattern in
                                   (MARKERS, CLEAN_LABELS, UNCENSORED_LABELS)):
            base = parts[0]
            break
    labels = [part for match in VERSION_LABELS.finditer(title)
              for part in match.groups() if part]
    if track.version:
        labels.append(track.version)
    descriptions = frozenset(description for label in labels
                             if (description := _version_description(label)))
    return TitleMetadata(normalize(base), frozenset(markers), descriptions)


def metadata_features(left: Track, right: Track) -> Features:
    left_metadata, right_metadata = title_features(left), title_features(right)
    left_title, left_markers = left_metadata.base_title, left_metadata.version_markers
    right_title, right_markers = right_metadata.base_title, right_metadata.version_markers
    title_ratio = SequenceMatcher(None, left_title, right_title).ratio() if left_title and right_title else 0.0
    left_artists, right_artists = artist_names(left), artist_names(right)
    artist_ratio = len(left_artists & right_artists) / len(left_artists | right_artists) if left_artists | right_artists else 0.0
    reasons = []
    conflict = False
    blocked = _censorship_conflict(left, right)
    if blocked:
        reasons.append("censorship_conflict")
        conflict = True
    if left_markers != right_markers:
        reasons.append("version_markers_differ")
        conflict = True
    version_difference = left_metadata.version_descriptions ^ right_metadata.version_descriptions
    if version_difference and left_metadata.version_descriptions and right_metadata.version_descriptions:
        reasons.append("version_descriptions_differ")
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
    safe_exact = bool(left_title and left_title == right_title and left_artists and left_artists == right_artists
                      and left_markers == right_markers and not conflict
                      and not version_difference
                      and (duration_confirmed or "isrc_equal" in reasons))
    if "isrc_equal" in reasons and not conflict and title_ratio >= .8 and artist_ratio > 0:
        safe_exact = True
    return Features(max(0.0, min(100.0, rank)), safe_exact, tuple(reasons),
                    title_ratio, artist_ratio, tuple(sorted(left_markers ^ right_markers)), conflict, blocked,
                    tuple(sorted(version_difference)))


def _equivalent_releases(left: Track, right: Track) -> bool:
    """Resolve a Jev tie when only release identifiers differ."""
    features = metadata_features(left, right)
    return (features.title_similarity == 1 and features.artist_similarity == 1
            and not features.version_markers
            and not features.version_descriptions
            and left.duration_ms is not None and right.duration_ms is not None
            and abs(left.duration_ms - right.duration_ms) <= THRESHOLDS.duration_tolerance_ms
            and censorship_features(left).hints == censorship_features(right).hints)


class MatchingEngine:
    def __init__(self, mode: MatchingMode, verify: Callable[[Track, Track], float] | None = None):
        self.mode = MatchingMode(mode)
        self.verify = verify
        if self.mode is MatchingMode.JEV_ONLY and verify is None:
            raise ValueError("Jev не настроен для режима jev_only")

    def decide(self, source: Track, candidates: Iterable[Track]) -> Decision:
        unique = {candidate.id: candidate for candidate in candidates if candidate.id and candidate.title}
        ranked = sorted((CandidateEvidence(candidate, metadata_features(source, candidate))
                         for candidate in unique.values()), key=lambda item:
                        (item.features.rank, censorship_features(item.candidate).uncensored), reverse=True)
        if not ranked:
            return Decision(DecisionKind.NO_CANDIDATE, self.mode, reasons=("catalog_empty",))
        # Jev sees plausible titles even when local scoring penalizes a version or guest artist.
        plausible = [item for item in ranked if not item.features.blocked and
                     (item.features.title_similarity >= THRESHOLDS.min_title_similarity or
                      "isrc_equal" in item.features.reasons)]
        if not plausible:
            best = ranked[0]
            return Decision(DecisionKind.REJECT, self.mode, best.candidate,
                            metadata_rank=best.features.rank,
                            reasons=(*best.features.reasons, "no_eligible_candidate"), considered=tuple(ranked))
        policy_reasons = ("censored_candidates_excluded",) if any(item.features.blocked for item in ranked) else ()
        if self.verify:
            selected = plausible[:THRESHOLDS.max_jev_candidates]
            if len(selected) == 1:
                probabilities = [self.verify(source, selected[0].candidate)]
            else:
                with ThreadPoolExecutor(max_workers=JEV_PARALLELISM) as pool:
                    probabilities = list(pool.map(
                        self.verify, repeat(source), (item.candidate for item in selected)))
            evaluated = [CandidateEvidence(item.candidate, item.features, probability)
                         for item, probability in zip(selected, probabilities)]
            evaluated.sort(key=lambda item: item.jev_probability, reverse=True)
            best = evaluated[0]
            preferred = [item for item in evaluated
                         if not censorship_features(source).hints
                         and not item.features.conflict
                         and item.jev_probability >= THRESHOLDS.jev_match
                         and best.jev_probability >= THRESHOLDS.jev_match
                         and censorship_features(item.candidate).uncensored
                         and _equivalent_releases(best.candidate, item.candidate)]
            uncensored_preferred = bool(preferred) and not censorship_features(best.candidate).uncensored
            if uncensored_preferred:
                best = preferred[0]
                evaluated.remove(best)
                evaluated.insert(0, best)
            close = [item for item in evaluated[1:]
                     if item.jev_probability >= THRESHOLDS.jev_review
                     and best.jev_probability - item.jev_probability < THRESHOLDS.jev_margin
                     and not math.isclose(best.jev_probability - item.jev_probability,
                                          THRESHOLDS.jev_margin, abs_tol=1e-9)]
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
            reasons = (*best.features.reasons, verdict, *policy_reasons,
                       *(("uncensored_preferred",) if uncensored_preferred else ()))
            evaluated_ids = {item.candidate.id for item in evaluated}
            considered = (*evaluated, *(item for item in ranked if item.candidate.id not in evaluated_ids))
            return Decision(kind, self.mode, best.candidate, "jev", best.features.rank,
                            probability, reasons, tuple(considered))
        best = plausible[0]
        competitors = [item for item in plausible[1:] if not (
            best.features.safe_exact and item.features.safe_exact
            and _equivalent_releases(best.candidate, item.candidate))]
        next_rank = competitors[0].features.rank if competitors else 0.0
        if best.features.safe_exact and next_rank < best.features.rank - THRESHOLDS.metadata_margin:
            kind = DecisionKind.MATCH
        elif best.features.rank >= THRESHOLDS.metadata_review:
            kind = DecisionKind.REVIEW
        else:
            kind = DecisionKind.REJECT
        return Decision(kind, self.mode, best.candidate, "metadata", best.features.rank,
                        reasons=(*best.features.reasons, *policy_reasons), considered=tuple(ranked))

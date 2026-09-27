"""Offline contracts for track discovery, decisions, and persistence."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from matching import DecisionKind, MatchingEngine, MatchingMode, Track, metadata_features
from candidate_search import discover_spotify
from matching_store import MappingResult, migrate_matching, save_mapping


def track(id, title="Song", artists=("Artist",), **kwargs):
    kwargs.setdefault("duration_ms", 180000)
    return Track(platform="spotify", id=id, title=title, artists=artists, **kwargs)


class MetadataTests(unittest.TestCase):
    def test_same_recording_metadata_variants(self):
        pairs = [
            (track("1"), track("2")),
            (track("1", title="SONG"), track("2", title="song")),
            (track("1", artists=("Кино",)), track("2", artists=("Kino",))),
            (track("1", album="Album A"), track("2", album="Album B")),
            (track("1", artists=("Artist", "Guest")), track("2", artists=("Guest", "Artist"))),
            (track("1", title="Song (feat. Guest)", artists=("Artist",)),
             track("2", artists=("Artist", "Guest"))),
            (track("1", duration_ms=180000), track("2", duration_ms=182000)),
            (track("1", isrc="USABC1234567", duration_ms=None),
             track("2", isrc="USABC1234567", duration_ms=None)),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertTrue(metadata_features(left, right).safe_exact)

    def test_recording_differences_are_not_safe_exact(self):
        pairs = [
            (track("1"), track("2", title="Song - 2020 Remaster")),
            (track("1", album="Original Album"),
             track("2", album="Original Album (Remastered)")),
            (track("1", title="Song - 2020 Remaster"), track("2", title="Song - 2021 Remaster")),
            (track("1"), track("2", title="Song - Remix")),
            (track("1"), track("2", title="Song - Live")),
            (track("1"), track("2", title="Song - Acoustic")),
            (track("1"), track("2", title="Song - Instrumental")),
            (track("1"), track("2", title="Song - Radio Edit")),
            (track("1"), track("2", title="Song - Sped Up")),
            (track("1"), track("2", title="Song - Slowed")),
            (track("1"), track("2", title="Song - Demo")),
            (track("1"), track("2", title="Song - Re-recorded")),
            (track("1", artists=("Artist", "Guest A")), track("2", artists=("Artist", "Guest B"))),
            (track("1", title="Song (feat. Guest A)"), track("2", title="Song (feat. Guest B)")),
            (track("1", title="Song"), track("2", title="Songs")),
            (track("1", artists=("Artist",)), track("2", artists=("Other",), title="Song (Cover)")),
            (track("1", isrc="USABC1234567"), track("2", isrc="USABC1234568")),
            (track("1", duration_ms=180000), track("2", duration_ms=240000)),
        ]
        for left, right in pairs:
            with self.subTest(left=left, right=right):
                self.assertFalse(metadata_features(left, right).safe_exact)


class EngineTests(unittest.TestCase):
    def test_hybrid_chooses_second_candidate_after_comparing_pool(self):
        wrong = track("wrong", "Song - Remix")
        right = track("right")
        decision = MatchingEngine(MatchingMode.HYBRID).decide(track("source"), [wrong, right])
        self.assertEqual((decision.kind, decision.candidate.id), (DecisionKind.MATCH, "right"))
        self.assertEqual(len(decision.considered), 2)

    def test_hybrid_reviews_ambiguous_candidate_without_jev(self):
        decision = MatchingEngine(MatchingMode.HYBRID).decide(
            track("source"), [track("candidate", title="Song - 2020 Remaster")])
        self.assertEqual(decision.kind, DecisionKind.REVIEW)

    def test_jev_only_evaluates_multiple_candidates_even_with_low_rank(self):
        calls = []
        def verify(source, candidate):
            calls.append(candidate.id)
            return {"wrong": 0.01, "right": 0.99}[candidate.id]
        engine = MatchingEngine(MatchingMode.JEV_ONLY, verify)
        decision = engine.decide(track("source"), [track("wrong", "Song - Remix"), track("right")])
        self.assertEqual(decision.kind, DecisionKind.MATCH)
        self.assertEqual(decision.candidate.id, "right")
        self.assertEqual(set(calls), {"wrong", "right"})

    def test_jev_ranges_and_failure(self):
        source, candidate = track("source"), track("candidate")
        for probability, expected in [(0.99, DecisionKind.MATCH),
                                      (0.85, DecisionKind.MATCH),
                                      (0.84, DecisionKind.REVIEW),
                                      (0.55, DecisionKind.REVIEW),
                                      (0.54, DecisionKind.REJECT),
                                      (0.01, DecisionKind.REJECT)]:
            with self.subTest(probability=probability):
                decision = MatchingEngine(MatchingMode.JEV_ONLY,
                    lambda *_: probability).decide(source, [candidate])
                self.assertEqual(decision.kind, expected)
                if probability == .01:
                    self.assertIn("jev_rejected", decision.reasons)
        with self.assertRaisesRegex(RuntimeError, "offline"):
            MatchingEngine(MatchingMode.JEV_ONLY,
                lambda *_: (_ for _ in ()).throw(RuntimeError("offline"))).decide(source, [candidate])
        with self.assertRaisesRegex(ValueError, "Jev"):
            MatchingEngine(MatchingMode.JEV_ONLY)

    def test_two_strong_jev_candidates_require_review(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY, lambda *_: .99).decide(
            track("source"), [track("a"), track("b", artists=("Artist", "Guest"))])
        self.assertEqual(decision.kind, DecisionKind.REVIEW)

    def test_equivalent_releases_do_not_block_automatic_match(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY,
                                  lambda _, candidate: {"a": .93, "b": .96}[candidate.id]).decide(
            track("source", album="Original"),
            [track("a", album="Compilation", isrc="USABC1234567"),
             track("b", album="Original", isrc="USABC1234568")])
        self.assertEqual((decision.kind, decision.candidate.id), (DecisionKind.MATCH, "b"))
        self.assertIn("equivalent_release_candidates", decision.reasons)

    def test_lower_jev_candidate_cannot_resolve_competition(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY,
                                  lambda _, candidate: {"a": .86, "b": .80}[candidate.id]).decide(
            track("source"), [track("a"), track("b")])
        self.assertEqual(decision.kind, DecisionKind.REVIEW)

    def test_no_candidate(self):
        self.assertEqual(MatchingEngine(MatchingMode.HYBRID).decide(track("source"), []).kind,
                         DecisionKind.NO_CANDIDATE)

    def test_hybrid_reviews_exact_labels_when_recording_evidence_is_absent(self):
        source = track("source", duration_ms=None)
        candidate = track("candidate", duration_ms=None)
        self.assertEqual(MatchingEngine(MatchingMode.HYBRID).decide(source, [candidate]).kind,
                         DecisionKind.REVIEW)


class DiscoveryTests(unittest.TestCase):
    def test_discovery_deduplicates_and_does_not_stop_after_first_result(self):
        calls = []
        def search(**kwargs):
            calls.append(kwargs["q"])
            name = "Song - Remix" if len(calls) == 1 else "Song"
            return {"tracks": {"items": [{"id": "same" if len(calls) == 1 else "right",
                "name": name, "artists": [{"name": "Artist"}], "album": {"name": "Album"},
                "duration_ms": 180000, "external_ids": {"isrc": "USABC1234567"}}]}}
        existing = [track("same", "Song - Remix")]
        candidates = discover_spotify(track("source"), SimpleNamespace(search=search), existing)
        self.assertGreater(len(calls), 1)
        self.assertEqual({candidate.id for candidate in candidates}, {"same", "right"})
        self.assertEqual(next(c for c in candidates if c.id == "right").isrc, "USABC1234567")


class StoreTests(unittest.TestCase):
    def test_migration_removes_resolved_pending_but_preserves_other_reviews(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "sync.db")
            db.execute("CREATE TABLE mappings (ym_id TEXT UNIQUE, sp_id TEXT UNIQUE)")
            db.execute("CREATE TABLE pending_syncs (key TEXT PRIMARY KEY, score INTEGER)")
            db.execute("CREATE TABLE failed_syncs (key TEXT PRIMARY KEY, query TEXT)")
            db.execute("INSERT INTO mappings VALUES ('ym1', 'sp1')")
            db.executemany("INSERT INTO pending_syncs VALUES (?, 95)",
                           [("ym_to_sp:ym1",), ("sp_to_ym:sp1",), ("ym_to_sp:other",)])
            db.execute("INSERT INTO failed_syncs VALUES ('sp_to_ym:sp1', 'old rejection')")

            migrate_matching(db)

            self.assertEqual(db.execute("SELECT key FROM pending_syncs").fetchall(),
                             [("ym_to_sp:other",)])
            self.assertEqual(db.execute("SELECT count(*) FROM failed_syncs").fetchone()[0], 0)
            db.close()

    def test_saving_mapping_resolves_pending_in_both_directions(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "sync.db")
            db.execute("CREATE TABLE mappings (ym_id TEXT UNIQUE, sp_id TEXT UNIQUE)")
            db.execute("CREATE TABLE pending_syncs (key TEXT PRIMARY KEY, score INTEGER)")
            db.execute("CREATE TABLE failed_syncs (key TEXT PRIMARY KEY, query TEXT)")
            migrate_matching(db)
            db.executemany("INSERT INTO pending_syncs (key, score) VALUES (?, 95)",
                           [("ym_to_sp:ym1",), ("sp_to_ym:sp1",), ("ym_to_sp:other",)])
            db.execute("INSERT INTO failed_syncs (key, query) VALUES ('sp_to_ym:sp1', 'old rejection')")

            self.assertEqual(save_mapping(db, "ym1", "sp1", "hybrid", "jev"),
                             MappingResult.CREATED)

            self.assertEqual(db.execute("SELECT key FROM pending_syncs").fetchall(),
                             [("ym_to_sp:other",)])
            self.assertEqual(db.execute("SELECT count(*) FROM failed_syncs").fetchone()[0], 0)
            db.close()

    def test_migration_preserves_legacy_mapping_and_conflicts_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            db = sqlite3.connect(Path(directory) / "sync.db")
            db.execute("CREATE TABLE mappings (ym_id TEXT UNIQUE, sp_id TEXT UNIQUE)")
            db.execute("CREATE TABLE pending_syncs (key TEXT PRIMARY KEY, score INTEGER)")
            db.execute("CREATE TABLE failed_syncs (key TEXT PRIMARY KEY, query TEXT)")
            db.execute("INSERT INTO mappings VALUES ('ym1', 'sp1')")
            migrate_matching(db)
            self.assertEqual(db.execute("SELECT provenance, algorithm_version FROM mappings").fetchone(),
                             ("legacy", 0))
            self.assertEqual(save_mapping(db, "ym2", "sp1", "hybrid", "metadata"), MappingResult.CONFLICT)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mappings").fetchone()[0], 1)
            self.assertEqual(save_mapping(db, "ym2", "sp2", "hybrid", "metadata"), MappingResult.CREATED)
            db.row_factory = sqlite3.Row
            self.assertEqual(save_mapping(db, "ym2", "sp2", "hybrid", "manual_review"),
                             MappingResult.UPDATED)
            db.close()


if __name__ == "__main__":
    unittest.main()

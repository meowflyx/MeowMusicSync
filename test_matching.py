"""Offline contracts for track discovery, decisions, and persistence."""

import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from matching import DecisionKind, MatchingEngine, MatchingMode, Track, censorship_features, metadata_features
from candidate_search import discover_spotify, yandex_track
from matching_store import MappingResult, migrate_matching, save_mapping


def track(id, title="Song", artists=("Artist",), **kwargs):
    kwargs.setdefault("duration_ms", 180000)
    return Track(platform="spotify", id=id, title=title, artists=artists, **kwargs)


class MetadataTests(unittest.TestCase):
    def test_named_versions_preserve_recording_identity_in_both_modes(self):
        for version in ("Remix", "Mix", "Edit", "Live at", "Acoustic Take"):
            left = track("source", f"Song (Alice {version})")
            right = track("candidate", f"Song (Bob {version})")
            with self.subTest(version=version):
                features = metadata_features(left, right)
                self.assertFalse(features.safe_exact)
                self.assertIn("version_descriptions_differ", features.reasons)
                for mode in MatchingMode:
                    decision = MatchingEngine(mode, lambda *_: .99).decide(left, [right])
                    self.assertNotEqual(decision.kind, DecisionKind.MATCH)

    def test_same_named_version_survives_platform_formatting(self):
        left = track("source", "Song (Alice Remix)")
        for right in (track("candidate", "SONG - ALICE REMIX"),
                      track("candidate", version="Alice Remix"),
                      track("candidate", "Song [Remix by Alice]")):
            with self.subTest(right=right):
                self.assertTrue(metadata_features(left, right).safe_exact)

    def test_remixer_identity_keeps_words_that_resemble_censorship_labels(self):
        for left, right in (("Clean Bandit", "Bandit"), ("Dirty South", "South"),
                            ("Explicit", "Other")):
            with self.subTest(left=left, right=right):
                self.assertFalse(metadata_features(track("source", f"Song ({left} Remix)"),
                                                   track("candidate", f"Song ({right} Remix)")).safe_exact)

    def test_generic_remix_is_not_proof_of_a_specific_remixer(self):
        self.assertFalse(metadata_features(track("source", "Song (Remix)"),
                                          track("candidate", "Song (Alice Remix)")).safe_exact)

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
    def test_jev_checks_multiple_candidates_concurrently(self):
        started = threading.Barrier(3, timeout=3)

        def verify(source, candidate):
            started.wait()
            return {"first": .1, "best": .96, "third": .2}[candidate.id]

        decision = MatchingEngine(MatchingMode.JEV_ONLY, verify).decide(
            track("source"), [track("first"), track("best"), track("third")])
        self.assertEqual((decision.kind, decision.candidate.id),
                         (DecisionKind.MATCH, "best"))

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
                                  lambda _, candidate: {"a": .86, "b": .82}[candidate.id]).decide(
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
    def test_yandex_content_warning_supplies_explicit_flag(self):
        item = SimpleNamespace(id="1", title="Song", artists=[], albums=[],
                               content_warning="explicit", explicit=None)
        self.assertIs(yandex_track(item).explicit, True)
        item.content_warning = None
        self.assertIsNone(yandex_track(item).explicit)

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


class CensorshipTests(unittest.TestCase):
    def test_censored_versions_never_auto_match_an_unmarked_source(self):
        for mode in MatchingMode:
            for suffix in ("Clean", "Clean Version", "Censored", "Edited Version",
                           "Radio Edit", "Radio Version", "Radio Mix", "TV Edit",
                           "Bleeped", "Family Friendly", "Squeaky Clean", "без мата",
                           "Non-Explicit", "Amended", "Clean Lyrics"):
                with self.subTest(mode=mode, suffix=suffix):
                    engine = MatchingEngine(mode, lambda *_: .99)
                    decision = engine.decide(track("source"), [track("clean", f"Song ({suffix})")])
                    self.assertEqual(decision.kind, DecisionKind.REJECT)
                    self.assertIn("censorship_conflict", decision.reasons)

    def test_censored_candidate_does_not_block_original_in_either_mode(self):
        for mode in MatchingMode:
            for verify in (None, lambda *_: .99):
                if mode is MatchingMode.JEV_ONLY and verify is None:
                    continue
                with self.subTest(mode=mode, verify=verify):
                    decision = MatchingEngine(mode, verify).decide(track("source"),
                        [track("clean", "Song (Clean Version)"), track("original")])
                    self.assertEqual((decision.kind, decision.candidate.id),
                                     (DecisionKind.MATCH, "original"))
                    self.assertIn("clean", {item.candidate.id for item in decision.considered})

    def test_explicit_source_cannot_silently_match_nonexplicit_target(self):
        left = track("source", explicit=True, isrc="SAME")
        right = track("clean", explicit=False, isrc="SAME")
        self.assertFalse(metadata_features(left, right).safe_exact)
        decision = MatchingEngine(MatchingMode.JEV_ONLY, lambda *_: .99).decide(left, [right])
        self.assertEqual(decision.kind, DecisionKind.REJECT)

    def test_false_explicit_flag_alone_is_not_a_censorship_hint(self):
        for explicit in (None, False):
            with self.subTest(explicit=explicit):
                decision = MatchingEngine(MatchingMode.HYBRID).decide(
                    track("source", explicit=explicit), [track("original", explicit=False)])
                self.assertEqual(decision.kind, DecisionKind.MATCH)

    def test_clean_source_keeps_clean_version_instead_of_upgrading(self):
        for title in ("Song (Clean)", "Song (Radio Edit)", "***** Please II"):
            with self.subTest(title=title):
                source = track("source", title, explicit=False)
                clean = track("clean", title, explicit=False)
                original = track("original", "Song", explicit=True)
                decision = MatchingEngine(MatchingMode.JEV_ONLY, lambda *_: .99).decide(
                    source, [original, clean])
                self.assertEqual((decision.kind, decision.candidate.id), (DecisionKind.MATCH, "clean"))

    def test_radio_source_cannot_be_replaced_by_unlabeled_nonexplicit_original(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY, lambda *_: .99).decide(
            track("source", "Song (Radio Edit)", explicit=False),
            [track("original", explicit=False)])
        self.assertEqual(decision.kind, DecisionKind.REJECT)

    def test_local_matching_prefers_explicit_release_over_unknown_release(self):
        decision = MatchingEngine(MatchingMode.HYBRID).decide(track("source"),
            [track("unknown"), track("explicit", "Song (Explicit)", explicit=True)])
        self.assertEqual((decision.kind, decision.candidate.id), (DecisionKind.MATCH, "explicit"))

    def test_equivalent_original_or_clean_releases_are_not_false_competitors(self):
        for title in ("Song", "Song (Clean)", "Song (Radio Edit)"):
            for verify in (None, lambda *_: .95):
                with self.subTest(title=title, verify=verify):
                    decision = MatchingEngine(MatchingMode.HYBRID, verify).decide(
                        track("source", title), [track("single", title, album="Single"),
                                                track("album", title, album="Album")])
                    self.assertEqual(decision.kind, DecisionKind.MATCH)

    def test_explicit_labels_are_not_different_recordings(self):
        for suffix in ("Explicit", "Explicit Version", "Uncensored", "Unedited", "Dirty Version"):
            with self.subTest(suffix=suffix):
                left = track("source", explicit=True)
                right = track("original", f"Song ({suffix})", explicit=True)
                self.assertTrue(metadata_features(left, right).safe_exact)

    def test_clean_aliases_preserve_recording_identity(self):
        self.assertTrue(metadata_features(track("source", "Song (Clean Version)"),
                                          track("target", "Song (Censored Version)")).safe_exact)

    def test_masked_title_does_not_force_review(self):
        artists = ("Eminem", "Dr. Dre", "Snoop Dogg", "Xzibit", "Nate Dogg")
        source = track("source", "Bitch Please II", artists, explicit=True)
        decision = MatchingEngine(MatchingMode.JEV_ONLY,
            lambda _, candidate: {"original": .95, "masked": .88}[candidate.id]).decide(source,
            [track("masked", "***** Please II", artists, explicit=False),
             track("original", "Bitch Please II", artists, explicit=True)])
        self.assertEqual((decision.kind, decision.candidate.id), (DecisionKind.MATCH, "original"))

    def test_album_and_version_fields_supply_censorship_hints(self):
        for kwargs in ({"album": "Album (Clean)"}, {"version": "Clean Version"},
                       {"album": "Album - Radio Edit"}):
            with self.subTest(kwargs=kwargs):
                decision = MatchingEngine(MatchingMode.JEV_ONLY, lambda *_: .99).decide(
                    track("source"), [track("clean", **kwargs)])
                self.assertEqual(decision.kind, DecisionKind.REJECT)

    def test_clean_qualifier_without_spaces_around_dash_is_detected(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY, lambda *_: .99).decide(
            track("source"), [track("clean", "Song-Non-Explicit")])
        self.assertEqual(decision.kind, DecisionKind.REJECT)

    def test_song_named_clean_or_radio_is_not_a_version_label(self):
        for title in ("Clean", "Radio", "Dirty", "Explicit", "Uncensored"):
            with self.subTest(title=title):
                self.assertTrue(metadata_features(track("source", title), track("target", title)).safe_exact)

    def test_featured_artist_name_is_not_a_censorship_hint(self):
        for guest in ("Clean", "Dirty", "*****"):
            with self.subTest(guest=guest):
                self.assertTrue(metadata_features(track("source", f"Song (feat. {guest})"),
                    track("target", artists=("Artist", guest))).safe_exact)

    def test_remixer_names_are_not_censorship_labels(self):
        for remixer in ("Clean Bandit", "Dirty South", "Explicit"):
            with self.subTest(remixer=remixer):
                features = censorship_features(track("target", f"Song ({remixer} Remix)"))
                self.assertEqual(features.hints, ())
                self.assertFalse(features.uncensored)

    def test_explicit_release_is_preferred_only_for_the_same_recording(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY,
            lambda _, candidate: {"unknown": .98, "explicit": .9}[candidate.id]).decide(
                track("source"), [track("unknown"), track("explicit", explicit=True)])
        self.assertEqual((decision.kind, decision.candidate.id), (DecisionKind.MATCH, "explicit"))
        self.assertIn("uncensored_preferred", decision.reasons)
        for candidate in (track("explicit", "Song (Remix)", explicit=True),
                          track("explicit", "Songs", explicit=True),
                          track("explicit", explicit=True, artists=("Artist", "Guest"))):
            with self.subTest(candidate=candidate):
                decision = MatchingEngine(MatchingMode.JEV_ONLY,
                    lambda _, target: {"unknown": .98, "explicit": .9}[target.id]).decide(
                        track("source"), [track("unknown"), candidate])
                self.assertEqual(decision.candidate.id, "unknown")

    def test_explicit_release_preference_does_not_override_jev_rejection(self):
        decision = MatchingEngine(MatchingMode.JEV_ONLY,
            lambda _, candidate: {"unknown": .98, "explicit": .4}[candidate.id]).decide(
                track("source"), [track("unknown"), track("explicit", explicit=True)])
        self.assertEqual(decision.candidate.id, "unknown")

    def test_five_percentage_point_margin_and_boundary(self):
        for runner_up, expected in ((.88, DecisionKind.MATCH), (.90, DecisionKind.MATCH),
                                    (.91, DecisionKind.REVIEW)):
            with self.subTest(runner_up=runner_up):
                decision = MatchingEngine(MatchingMode.JEV_ONLY,
                    lambda _, candidate: .95 if candidate.id == "best" else runner_up).decide(
                        track("source"), [track("best"), track("other", "Songs")])
                self.assertEqual(decision.kind, expected)


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

"""Known-defect regressions; deliberately red until the audited bugs are fixed.

Run: .venv/bin/python -m unittest -v test_adversarial
Uses temporary SQLite/key files and fake music services; no live API calls.
"""

import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jev
import sync_logic as sync
from matching import DecisionKind, MatchingEngine, MatchingMode, Track


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        directory = self.resources.enter_context(tempfile.TemporaryDirectory())
        self.resources.enter_context(patch.object(sync, "DB_FILE", str(Path(directory) / "sync.db")))
        self.resources.enter_context(patch.object(jev, "KEY_FILE", str(Path(directory) / "jev.key")))
        sync.init_db()

    def test_different_remix_names_are_not_duplicates(self):
        left = {"id": "alice", "title": "Song (Alice Remix)", "artists": "Artist",
                "duration_ms": 180000}
        right = {**left, "id": "bob", "title": "Song (Bob Remix)"}
        self.assertFalse(sync.check_duplicate(left, right), "different remix identities were discarded")
        decision = MatchingEngine(MatchingMode.HYBRID).decide(
            Track("yandex", "alice", left["title"], ("Artist",), duration_ms=180000),
            [Track("spotify", "bob", right["title"], ("Artist",), duration_ms=180000)])
        self.assertNotEqual(decision.kind, DecisionKind.MATCH)

    def test_missing_artists_cannot_authorize_duplicate_deletion(self):
        library = {"new", "old"}
        tracks = [{"id": track_id, "title": "Intro", "artists": "", "duration_ms": 30000,
                   "timestamp": timestamp}
                  for track_id, timestamp in (("new", "2026-09-02"), ("old", "2026-09-01"))]
        client = SimpleNamespace(users_likes_tracks_remove=lambda ids: library.difference_update(ids))
        with patch.object(sync, "get_ym_client", return_value=client), \
             patch.object(sync, "get_ym_likes", return_value=tracks):
            sync.remove_yandex_duplicates()
        self.assertEqual(library, {"new", "old"}, "unknown artist identities caused a destructive match")

    def test_repeated_id_does_not_delete_the_only_saved_track(self):
        for platform in ("spotify", "yandex"):
            with self.subTest(platform=platform):
                library = {"one"}
                if platform == "spotify":
                    item = {"added_at": "2026-09-01T00:00:00Z", "track": {
                        "id": "one", "name": "Song", "artists": [{"name": "Artist"}],
                        "duration_ms": 180000}}
                    # A mutable offset-paginated library can repeat a record across pages.
                    client = SimpleNamespace(
                        current_user_saved_tracks=lambda **kw: {"items": [item], "next": "page2"},
                        next=lambda page: {"items": [item], "next": None},
                        current_user_saved_tracks_delete=lambda *, tracks: library.difference_update(tracks))
                    with patch.object(sync, "get_sp_client", return_value=client):
                        sync.remove_spotify_duplicates()
                else:
                    record = SimpleNamespace(id="one", title="Song", artists=[SimpleNamespace(name="Artist")],
                                             duration_ms=180000)
                    client = SimpleNamespace(
                        users_likes_tracks=lambda: SimpleNamespace(tracks=[
                            SimpleNamespace(id="one", album_id="album1", timestamp="2026-09-02"),
                            SimpleNamespace(id="one", album_id="album2", timestamp="2026-09-01")]),
                        tracks=lambda ids: [record, record],
                        users_likes_tracks_remove=lambda ids: library.difference_update(ids))
                    with patch.object(sync, "get_ym_client", return_value=client):
                        sync.remove_yandex_duplicates()
                self.assertEqual(library, {"one"}, "the ID selected for keeping was also sent for deletion")

    def _review_existing_pair(self):
        sync.configure_jev("openrouter", "offline-test-key")
        sync.set_matching_mode("jev_only")
        source = {"id": "ym", "title": "Song", "artists": "Artist", "duration_ms": 180000}
        target = {**source, "id": "sp"}
        for name, value in (("get_ym_client", object()), ("get_sp_client", object()),
                            ("get_ym_likes", [source]), ("get_sp_likes", [target])):
            self.resources.enter_context(patch.object(sync, name, return_value=value))
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO mappings (ym_id, sp_id) VALUES ('ym', 'sp')")
        with patch.object(jev, "match_probability", return_value=.6):
            sync.revalidate_mappings()
        self.assertEqual(set(sync.get_pending_tracks()), {"ym_to_sp:ym", "sp_to_ym:sp"})

    def test_reject_invalidates_reverse_revalidation_approval(self):
        self._review_existing_pair()
        self.assertTrue(sync.reject_pending("ym_to_sp:ym")[0])
        self.assertEqual(sync.get_status_stats()["mappings"], 0)
        approved, message = sync.approve_pending("sp_to_ym:sp", "ym")
        self.assertEqual((approved, sync.get_status_stats()["mappings"]), (False, 0), message)

    def test_explicit_revalidation_rechecks_existing_reviews(self):
        self._review_existing_pair()
        with patch.object(jev, "match_probability", return_value=.99):
            sync.revalidate_mappings()
        self.assertEqual(sync.get_pending_tracks(), {}, "explicit revalidation reused stale review decisions")
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT status FROM mappings").fetchone(), ("active",))

    def test_spotify_null_page_does_not_become_a_complete_snapshot(self):
        for first_page in (None, {"items": [], "next": "page2", "total": 1}):
            with self.subTest(first_page=first_page):
                client = SimpleNamespace(current_user_saved_tracks=lambda **kw: first_page,
                                         next=lambda page: None)
                with self.assertRaises(RuntimeError):
                    sync.get_sp_likes(client)

    def test_yandex_null_search_does_not_cache_a_permanent_negative(self):
        client = SimpleNamespace(search=lambda *args, **kw: None)
        source = {"id": "sp", "title": "Song", "artists": "Artist", "duration_ms": 180000}
        with patch.object(sync, "get_ym_client", return_value=client), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_ym_likes", return_value=[]), \
             patch.object(sync, "get_sp_likes", return_value=[source]):
            try:
                sync.sync_sp_to_ym()
            except RuntimeError:
                pass  # A malformed service response should stop the operation without caching it.
        self.assertEqual(sync.get_failed_tracks(), {}, "a malformed response was persisted as catalog_empty")


if __name__ == "__main__":
    unittest.main()

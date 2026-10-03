"""Offline Jev checks: .venv/bin/python -m unittest -v test_jev"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

import jev
import sync_logic as sync
from matching import Track


class JevTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = patch.object(sync, "DB_FILE", str(Path(self.temp.name) / "sync.db"))
        self.db.start()
        self.addCleanup(self.db.stop)
        self.secret = patch.object(jev, "KEY_FILE", str(Path(self.temp.name) / "jev.key"))
        self.secret.start()
        self.addCleanup(self.secret.stop)
        sync.init_db()

    def test_key_is_encrypted_and_switch_is_optional(self):
        sync.configure_jev("openrouter", "test-private-key")
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            ciphertext = db.execute("SELECT value FROM settings WHERE key = 'jev_api_key'").fetchone()[0]
        self.assertNotIn("test-private-key", ciphertext)
        self.assertEqual(jev.decrypt_key(ciphertext), "test-private-key")
        self.assertTrue(sync.get_jev_status()["enabled"])
        sync.set_jev_enabled(False)
        self.assertFalse(sync.get_jev_status()["enabled"])
        sync.set_jev_enabled(True)
        self.assertTrue(sync.get_jev_status()["enabled"])

    def test_provider_requests_and_reject_malformed_answers(self):
        cases = [
            ("typesafe", "https://api.typesafe.ai/v1/systemone", "jev-latest", "noul", "noul"),
            ("openrouter", "https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13", "noul", "noul"),
            ("vercel", "https://ai-gateway.vercel.sh/v1/evaluate", "typesafe-ai/jev", "boolean", "probability"),
        ]
        for provider, url, model, question_type, answer_field in cases:
            with self.subTest(provider=provider):
                def fake_open(request, timeout):
                    self.assertEqual(request.full_url, url)
                    self.assertEqual(request.get_header("Authorization"), "Bearer secret")
                    self.assertEqual(timeout, 10)
                    body = json.loads(request.data)
                    self.assertEqual(body["model"], model)
                    self.assertEqual(body["questions"]["same_track"]["type"], question_type)
                    self.assertEqual(body["state"]["yandex"]["title"], "Song")
                    self.assertEqual(body["state"]["yandex"]["duration_ms"], 180000)
                    self.assertIn("censorship_hints", body["state"]["yandex"])
                    self.assertIn("explicit=false alone", body["questions"]["same_track"]["instructions"])
                    self.assertIn("album name alone", body["questions"]["same_track"]["instructions"])
                    self.assertIn("count each person once", body["questions"]["same_track"]["instructions"])
                    return self._response({"answers": {"same_track": {answer_field: .97}}})
                yandex = Track("yandex", "ym", "Song", ("A",), duration_ms=180000)
                spotify = Track("spotify", "sp", "Song", ("A",))
                with patch.object(jev, "urlopen", side_effect=fake_open):
                    self.assertEqual(jev.match_probability(provider, "secret", yandex, spotify), .97)
                with patch.object(jev, "urlopen", return_value=self._response({"answers": {"same_track": {answer_field: 2}}})):
                    with self.assertRaises(RuntimeError):
                        jev.match_probability(provider, "secret", yandex, spotify)
                for bad in (True, float("nan")):
                    with patch.object(jev, "urlopen", return_value=self._response(
                        {"answers": {"same_track": {answer_field: bad}}})):
                        with self.assertRaises(RuntimeError):
                            jev.match_probability(provider, "secret", yandex, spotify)

    def test_transient_jev_failure_is_retried(self):
        yandex = Track("yandex", "ym", "Song", ("Artist",))
        spotify = Track("spotify", "sp", "Song", ("Artist",))
        unavailable = HTTPError("https://example.test", 503, "Unavailable", {}, None)
        answer = self._response({"answers": {"same_track": {"probability": .91}}})
        with patch.object(jev, "urlopen", side_effect=[unavailable, answer]) as open_api, \
             patch.object(jev.time, "sleep") as sleep:
            self.assertEqual(jev.match_probability("vercel", "secret", yandex, spotify), .91)
        self.assertEqual(open_api.call_count, 2)
        sleep.assert_called_once()

    def test_jev_only_requires_configuration_and_disallows_disable(self):
        with self.assertRaisesRegex(ValueError, "Jev"):
            sync.set_matching_mode("jev_only")
        sync.configure_jev("openrouter", "test-private-key")
        sync.set_matching_mode("jev_only")
        with self.assertRaisesRegex(ValueError, "hybrid"):
            sync.set_jev_enabled(False)

    def test_jev_only_full_pipeline_routes_low_review_and_high(self):
        sync.configure_jev("openrouter", "test-private-key")
        sync.set_matching_mode("jev_only")
        source = {"id": "ym1", "artists": "Artist", "title": "Song", "search_query": "Artist Song"}
        target = Track("spotify", "sp1", "Song", ("Artist",))
        added = []
        client = type("SP", (), {"current_user_saved_tracks_add": lambda self, **kw: added.append(kw)})()
        for probability, expected in ((.01, "не найдено 1"), (.6, "на одобрении 1"),
                                      (.99, "добавлено 1")):
            with self.subTest(probability=probability):
                sync.clear_failed_tracks()
                with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
                    db.execute("DELETE FROM pending_syncs")
                    db.execute("DELETE FROM mappings")
                with patch.object(sync, "get_ym_client", return_value=object()), \
                     patch.object(sync, "get_sp_client", return_value=client), \
                     patch.object(sync, "get_ym_likes", return_value=[source]), \
                     patch.object(sync, "get_sp_likes", return_value=[]), \
                     patch.object(sync, "discover_spotify", return_value=[target]), \
                     patch.object(jev, "match_probability", return_value=probability):
                    self.assertIn(expected, sync.sync_ym_to_sp())
        self.assertEqual(added, [{"tracks": ["sp1"]}])

    def test_existing_mapping_is_revalidated_without_liking(self):
        sync.configure_jev("openrouter", "test-private-key")
        sync.set_matching_mode("jev_only")
        source = {"id": "ym1", "artists": "Artist", "title": "Song", "search_query": "Artist Song"}
        target = {"id": "sp1", "artists": "Artist", "title": "Song", "search_query": "Artist Song"}
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO mappings (ym_id, sp_id) VALUES ('ym1', 'sp1')")
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_ym_likes", return_value=[source]), \
             patch.object(sync, "get_sp_likes", return_value=[target]), \
             patch.object(jev, "match_probability", return_value=.5):
            self.assertIn("на одобрении 1", sync.sync_ym_to_sp())
        self.assertEqual(sync.get_pending_tracks()["ym_to_sp:ym1"]["purpose"], "revalidate")
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT status FROM mappings").fetchone()[0], "needs_review")
        with patch.object(sync, "get_sp_client", side_effect=AssertionError("must not like again")):
            self.assertTrue(sync.approve_pending("ym_to_sp:ym1")[0])
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT status, provenance FROM mappings").fetchone(),
                             ("active", "manual_review"))

    def test_old_pending_retries_with_censorship_policy_in_both_directions(self):
        sync.configure_jev("openrouter", "test-private-key")
        sync.set_matching_mode("jev_only")
        for direction in ("ym_to_sp", "sp_to_ym"):
            with self.subTest(direction=direction):
                with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
                    db.execute("DELETE FROM mappings")
                    db.execute("DELETE FROM pending_syncs")
                    db.execute("INSERT INTO pending_syncs (key,algorithm_version,mode) VALUES (?,3,'jev_only')",
                               (f"{direction}:source",))
                row = {"id": "source", "title": "Song", "artists": "Artist", "explicit": True,
                       "duration_ms": 180000}
                platform = "spotify" if direction == "ym_to_sp" else "yandex"
                candidates = [Track(platform, "clean", "Song (Clean)", ("Artist",), duration_ms=180000),
                              Track(platform, "original", "Song", ("Artist",), duration_ms=180000,
                                    explicit=True)]
                added = []
                client = type("Client", (), {
                    "current_user_saved_tracks_add": lambda self, **kw: added.append(kw),
                    "users_likes_tracks_add": lambda self, **kw: added.append(kw)})()
                yandex_rows, spotify_rows = ([row], []) if direction == "ym_to_sp" else ([], [row])
                discover_name = "discover_spotify" if direction == "ym_to_sp" else "discover_yandex"
                with patch.object(sync, "get_ym_client", return_value=client), \
                     patch.object(sync, "get_sp_client", return_value=client), \
                     patch.object(sync, "get_ym_likes", return_value=yandex_rows), \
                     patch.object(sync, "get_sp_likes", return_value=spotify_rows), \
                     patch.object(sync, discover_name, return_value=candidates), \
                     patch.object(jev, "match_probability", return_value=.95) as verify:
                    self.assertIn("добавлено 1", sync._sync_direction(direction))
                self.assertEqual(verify.call_count, 1)
                self.assertEqual(added, [{"tracks" if direction == "ym_to_sp" else "track_ids": ["original"]}])
                self.assertEqual(sync.get_pending_tracks(), {})

    def test_clean_only_result_is_failed_instead_of_pending_or_added(self):
        sync.configure_jev("openrouter", "test-private-key")
        sync.set_matching_mode("jev_only")
        source = {"id": "source", "title": "Song", "artists": "Artist", "duration_ms": 180000}
        target = Track("yandex", "clean", "Song (Radio Edit)", ("Artist",), duration_ms=180000)
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_ym_likes", return_value=[]), \
             patch.object(sync, "get_sp_likes", return_value=[source]), \
             patch.object(sync, "discover_yandex", return_value=[target]), \
             patch.object(jev, "match_probability", side_effect=AssertionError("must prefilter clean")):
            self.assertIn("не найдено 1", sync.sync_sp_to_ym())
        self.assertEqual(sync.get_pending_tracks(), {})
        self.assertIn("sp_to_ym:source", sync.get_failed_tracks())

    def test_censored_library_result_does_not_bypass_catalog_search(self):
        source = {"id": "source", "title": "Song", "artists": "Artist", "duration_ms": 180000,
                  "explicit": True}
        clean = {"id": "clean", "title": "Song", "artists": "Artist", "duration_ms": 180000,
                 "explicit": False}
        original = Track("spotify", "original", "Song", ("Artist",), duration_ms=180000, explicit=True)
        added = []
        client = type("SP", (), {"current_user_saved_tracks_add": lambda self, **kw: added.append(kw)})()
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=client), \
             patch.object(sync, "get_ym_likes", return_value=[source]), \
             patch.object(sync, "get_sp_likes", return_value=[clean]), \
             patch.object(sync, "discover_spotify", return_value=[original]) as discover:
            self.assertIn("добавлено 1", sync.sync_ym_to_sp())
        discover.assert_called_once()
        self.assertEqual(added, [{"tracks": ["original"]}])

    def test_reverse_direction_uses_same_engine(self):
        source = {"id": "sp1", "artists": "Artist", "title": "Song",
                  "search_query": "Artist Song", "duration_ms": 180000}
        target = Track("yandex", "ym1", "Song", ("Artist",), duration_ms=180000)
        added = []
        client = type("YM", (), {"users_likes_tracks_add": lambda self, **kw: added.append(kw)})()
        with patch.object(sync, "get_ym_client", return_value=client), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_sp_likes", return_value=[source]), \
             patch.object(sync, "get_ym_likes", return_value=[]), \
             patch.object(sync, "discover_yandex", return_value=[target]):
            self.assertIn("добавлено 1", sync.sync_sp_to_ym())
        self.assertEqual(added, [{"track_ids": ["ym1"]}])
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT ym_id, sp_id FROM mappings").fetchone(),
                             ("ym1", "sp1"))

    def test_mapping_conflict_reviews_without_claiming_success(self):
        source = {"id": "ym1", "artists": "Artist", "title": "Song",
                  "search_query": "Artist Song", "duration_ms": 180000}
        target = {"id": "sp1", "artists": "Artist", "title": "Song",
                  "search_query": "Artist Song", "duration_ms": 180000}
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO mappings (ym_id, sp_id, algorithm_version) VALUES ('ym2', 'sp1', 2)")
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_ym_likes", return_value=[source]), \
             patch.object(sync, "get_sp_likes", return_value=[target]), \
             patch.object(sync, "discover_spotify", return_value=[Track("spotify", "sp1", "Song", ("Artist",), duration_ms=180000)]) as discover:
            self.assertIn("на одобрении 1", sync.sync_ym_to_sp())
        discover.assert_called_once()
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT ym_id, sp_id FROM mappings").fetchall(), [("ym2", "sp1")])
        self.assertIn("mapping_conflict", sync.get_pending_tracks()["ym_to_sp:ym1"]["reasons"])

    def test_old_failure_is_retried_by_new_algorithm(self):
        source = {"id": "ym1", "artists": "Artist", "title": "Song",
                  "search_query": "Artist Song", "duration_ms": 180000}
        target = {"id": "sp1", "artists": "Artist", "title": "Song",
                  "search_query": "Artist Song", "duration_ms": 180000}
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO failed_syncs (key, query) VALUES ('ym_to_sp:ym1', 'Artist Song')")
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_ym_likes", return_value=[source]), \
             patch.object(sync, "get_sp_likes", return_value=[target]), \
             patch.object(sync, "discover_spotify", return_value=[Track("spotify", "sp1", "Song", ("Artist",), duration_ms=180000)]):
            sync.sync_ym_to_sp()
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM failed_syncs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT ym_id, sp_id FROM mappings").fetchone(), ("ym1", "sp1"))

    def test_missing_mapped_target_is_searched_again(self):
        source = {"id": "ym1", "artists": "Artist", "title": "Song",
                  "search_query": "Artist Song", "duration_ms": 180000}
        new_target = Track("spotify", "sp2", "Song", ("Artist",), duration_ms=180000)
        added = []
        client = type("SP", (), {"current_user_saved_tracks_add": lambda self, **kw: added.append(kw)})()
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO mappings (ym_id, sp_id) VALUES ('ym1', 'sp1')")
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=client), \
             patch.object(sync, "get_ym_likes", return_value=[source]), \
             patch.object(sync, "get_sp_likes", return_value=[]), \
             patch.object(sync, "discover_spotify", return_value=[new_target]) as discover:
            self.assertIn("добавлено 1", sync.sync_ym_to_sp())
        discover.assert_called_once()
        self.assertEqual(added, [{"tracks": ["sp2"]}])
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT ym_id, sp_id FROM mappings").fetchall(),
                             [("ym1", "sp2")])

    def test_jev_outage_stops_before_liking(self):
        sync.configure_jev("openrouter", "test-private-key")
        sync.set_matching_mode("jev_only")
        source = {"id": "ym1", "artists": "Artist", "title": "Song", "search_query": "Artist Song"}
        target = Track("spotify", "sp1", "Song", ("Artist",))
        with patch.object(sync, "get_ym_client", return_value=object()), \
             patch.object(sync, "get_sp_client", return_value=object()), \
             patch.object(sync, "get_ym_likes", return_value=[source]), \
             patch.object(sync, "get_sp_likes", return_value=[]), \
             patch.object(sync, "discover_spotify", return_value=[target]), \
             patch.object(jev, "match_probability", side_effect=RuntimeError("Jev offline")):
            with self.assertRaisesRegex(RuntimeError, "Jev offline"):
                sync.sync_ym_to_sp()
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mappings").fetchone()[0], 0)

    @staticmethod
    def _response(data):
        from io import BytesIO
        return BytesIO(json.dumps(data).encode())


if __name__ == "__main__":
    unittest.main()

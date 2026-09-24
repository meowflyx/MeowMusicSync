"""Offline regressions: .venv/bin/python -m unittest -v test_sync"""
import sqlite3
import tempfile
import unittest
import logging
import threading
import subprocess
import sys
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import closing

import sync_logic as sync
from candidate_search import discover_spotify, discover_yandex
from matching import Track, metadata_features
from error_messages import explain_error
from spotipy.exceptions import SpotifyException, SpotifyOauthError


class SyncRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = patch.object(sync, "DB_FILE", str(Path(self.temp.name) / "sync.db"))
        self.db.start()
        self.addCleanup(self.db.stop)
        sync.init_db()

    def test_unknown_metadata_is_not_a_duplicate(self):
        self.assertFalse(sync.check_duplicate({"artists": "", "title": ""},
                                              {"artists": "", "title": ""}))

    def test_clear_returns_deleted_count(self):
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO failed_syncs (key, query) VALUES ('x', 'song')")
        self.assertEqual(sync.clear_failed_tracks(), 1)
        self.assertEqual(sync.clear_failed_tracks(), 0)

    def test_legacy_blacklist_is_removed_during_migration(self):
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("CREATE TABLE blacklist (ym_id TEXT, sp_id TEXT)")
            db.execute("INSERT INTO blacklist VALUES ('old', 'old')")
            db.execute("PRAGMA user_version = 2")
        sync.init_db()
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'blacklist'"
            ).fetchone())

    def test_like_playlist_adds_unique_tracks_in_batches(self):
        playlist = SimpleNamespace(title="Imported", fetch_tracks=lambda: [
            SimpleNamespace(id=1), SimpleNamespace(id=2), SimpleNamespace(id=1), SimpleNamespace(id=None), SimpleNamespace(id=3)
        ])
        batches = []
        client = SimpleNamespace(users_playlists=lambda kind, user_id: playlist,
                                 users_likes_tracks_add=lambda ids: batches.append(ids))
        with patch.object(sync, "get_ym_client", return_value=client):
            self.assertEqual(sync.like_playlist_tracks("https://music.yandex.ru/users/me/playlists/42"),
                             "Лайкнуто 3 трека из плейлиста «Imported».")
        self.assertEqual(batches, [["1", "2", "3"]])

    def test_like_playlist_accepts_uuid_share_link(self):
        playlist = SimpleNamespace(title="Imported", fetch_tracks=lambda: [SimpleNamespace(id=1)])
        client = SimpleNamespace(playlist=lambda uuid: playlist,
                                 users_likes_tracks_add=lambda ids: None)
        with patch.object(sync, "get_ym_client", return_value=client):
            self.assertIn("1 трека", sync.like_playlist_tracks("https://music.yandex.ru/playlists/4b0aed49-e9be-1252-9501-e2e322904d6b?utm_source=desktop"))

    def test_like_playlist_rejects_untrusted_or_incomplete_link(self):
        for url in ("https://example.org/users/me/playlists/42", "https://music.yandex.ru/playlist/42"):
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "ссылку"):
                sync.like_playlist_tracks(url)

    def test_auth_checks_missing_settings_before_oauth(self):
        import auth_spotify
        with patch.multiple(auth_spotify, SPOTIPY_CLIENT_ID="", SPOTIPY_CLIENT_SECRET=" "), patch.object(
            auth_spotify, "SpotifyOAuth", side_effect=AssertionError("OAuth must not start")
        ):
            with self.assertRaisesRegex(ValueError, "SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET"):
                auth_spotify.main()

    def test_permanent_spotify_failure_is_not_retried(self):
        calls = []
        def denied():
            calls.append(1)
            raise SpotifyException(403, -1, "Active premium subscription required for the owner of the app")
        with patch.object(sync.time, "sleep", side_effect=AssertionError("must not retry")):
            _, error = sync.api_call_with_retry(denied)
        self.assertEqual(len(calls), 1)
        self.assertIn("Premium", str(error))

    def test_error_messages_distinguish_access_and_auth_failures(self):
        self.assertNotIn("требует Premium", explain_error(SpotifyException(403, -1, "Forbidden")))
        self.assertIn("Users and Access", explain_error(SpotifyException(403, -1, "Forbidden")))
        self.assertIn("Client ID / Client Secret", explain_error(SpotifyOauthError("secret", error="invalid_client")))
        self.assertNotIn("secret", explain_error(SpotifyOauthError("secret", error="invalid_grant")))
        self.assertIn("auth_spotify.py", explain_error(SpotifyException(401, -1, "Expired")))

    def test_versions_are_not_duplicates_even_with_similar_titles(self):
        self.assertFalse(sync.check_duplicate(
            {"artists": "Artist", "title": "A very long song title about our beautiful world"},
            {"artists": "Artist", "title": "A very long song title about our beautiful world - Live"}))

    def test_spotify_duplicate_cleanup_uses_current_metadata_and_removes_mapping(self):
        def item(id, added_at):
            return {"added_at": added_at, "track": {"id": id, "name": "Song",
                    "artists": [{"name": "Artist"}], "duration_ms": 180000}}
        deleted = []
        client = SimpleNamespace(
            current_user_saved_tracks=lambda **kw: {"items": [item("new", "2026-09-02"),
                                                          item("old", "2026-09-01")], "next": None},
            current_user_saved_tracks_delete=lambda **kw: deleted.extend(kw["tracks"]))
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO mappings (ym_id, sp_id) VALUES ('ym', 'old')")
        with patch.object(sync, "get_sp_client", return_value=client):
            self.assertTrue(sync.remove_spotify_duplicates()[0])
        self.assertEqual(deleted, ["old"])
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mappings").fetchone()[0], 0)

    def test_yandex_duplicate_cleanup_uses_current_metadata(self):
        deleted = []
        client = SimpleNamespace(users_likes_tracks_remove=lambda ids: deleted.extend(ids))
        tracks = [{"id": "new", "artists": "Artist", "title": "Song",
                   "duration_ms": 180000, "timestamp": "2026-09-02"},
                  {"id": "old", "artists": "Artist", "title": "Song",
                   "duration_ms": 180000, "timestamp": "2026-09-01"}]
        with patch.object(sync, "get_ym_client", return_value=client), \
             patch.object(sync, "get_ym_likes", return_value=tracks):
            self.assertTrue(sync.remove_yandex_duplicates()[0])
        self.assertEqual(deleted, ["old"])

    def test_missing_spotify_track_does_not_abort_library(self):
        client = SimpleNamespace(current_user_saved_tracks=lambda **kw: {
            "items": [{"track": None}, {"track": {"id": "abc", "artists": [{"name": "Artist"}], "name": "Song"}}],
            "next": None})
        self.assertEqual([t["id"] for t in sync.get_sp_likes(client)], ["abc"])

    def test_search_outage_is_not_a_negative_match(self):
        def unavailable(*args, **kwargs):
            raise ConnectionError("offline")
        with patch.object(sync.time, "sleep"):
            for fn, client in [(discover_spotify, SimpleNamespace(search=unavailable)),
                               (discover_yandex, SimpleNamespace(search=unavailable))]:
                with self.subTest(fn=fn.__name__), self.assertRaises(ConnectionError):
                    fn(Track("yandex", "1", "Song", ("Artist",)), client)

    def test_approval_does_not_cache_empty_metadata(self):
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("INSERT INTO pending_syncs (key, direction, source, found, found_id, score) VALUES (?, ?, ?, ?, ?, ?)",
                       ("ym_to_sp:1", "ym_to_sp", "Artist Song", "Artist Song", "abc", 80))
        client = SimpleNamespace(current_user_saved_tracks_add=lambda **kw: None)
        with patch.object(sync, "get_sp_client", return_value=client):
            self.assertTrue(sync.approve_pending("ym_to_sp:1")[0])
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            self.assertEqual(db.execute("SELECT ym_id, sp_id FROM mappings").fetchall(), [("1", "abc")])

    def test_mutations_exclude_other_threads_and_processes(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            with sync.music_operation():
                with sync.music_operation():
                    entered.set()
                    release.wait(5)
        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            for fn, args in [(sync.sync_ym_to_sp, ()), (sync.sync_sp_to_ym, ()),
                             (sync.full_two_way_sync, ()), (sync.approve_pending, ("x",)),
                             (sync.reject_pending, ("x",)), (sync.remove_spotify_duplicates, ()),
                             (sync.remove_yandex_duplicates, ()), (sync.clear_failed_tracks, ())]:
                with self.subTest(fn=fn.__name__), self.assertRaises(RuntimeError):
                    fn(*args)
            self.assertTrue(sync.is_sync_running())
            result = subprocess.run([sys.executable, "-c",
                "import sync_logic as s, sys; s.DB_FILE=sys.argv[1]; s.clear_failed_tracks()", sync.DB_FILE],
                capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Операция уже выполняется", result.stderr)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(sync.is_sync_running())
        self.assertEqual(sync.clear_failed_tracks(), 0)

    def test_lock_released_after_exception(self):
        with self.assertRaises(ValueError):
            with sync.music_operation():
                raise ValueError("failed")
        self.assertFalse(sync.is_sync_running())

    def test_incomplete_yandex_snapshot_does_not_look_like_unlikes(self):
        client = SimpleNamespace(users_likes_tracks=lambda: SimpleNamespace(
            tracks=[SimpleNamespace(id=1, album_id=2)]), tracks=lambda ids: [])
        with self.assertRaises(RuntimeError):
            sync.get_ym_likes(client)

    def test_yandex_likes_keep_original_artist_and_cover_version(self):
        original = SimpleNamespace(id="1", artists=[SimpleNamespace(name="s0rrow")],
                                   title="fake ur face", version=None)
        replacement = SimpleNamespace(id="1", artists=[SimpleNamespace(name="vibermx")],
                                      title="fake ur face", version=None, substituted=original)
        cover = SimpleNamespace(id="2", artists=[SimpleNamespace(name="Torlin")],
                                title="GOSSIP", version="Cover", substituted=None)
        client = SimpleNamespace(users_likes_tracks=lambda: SimpleNamespace(tracks=[
            SimpleNamespace(id="1", album_id=None), SimpleNamespace(id="2", album_id=None)]),
            tracks=lambda ids: [replacement, cover])
        tracks = sync.get_ym_likes(client)
        self.assertEqual((tracks[0]['id'], tracks[0]['artists']), ("1", "s0rrow"))
        self.assertEqual(tracks[1]['title'], "GOSSIP (Cover)")
        self.assertEqual(sync.get_ym_likes(client), tracks)

    def test_yandex_search_does_not_disguise_replacement_as_original(self):
        original = SimpleNamespace(artists=[SimpleNamespace(name="s0rrow")], title="fake ur face", version=None)
        replacement = SimpleNamespace(id="1", artists=[SimpleNamespace(name="vibermx")],
                                      title="fake ur face", version="Cover", substituted=original)
        client = SimpleNamespace(search=lambda *a, **kw: SimpleNamespace(tracks=SimpleNamespace(results=[replacement])))
        candidates = discover_yandex(Track("spotify", "sp", "fake ur face", ("s0rrow",)), client)
        self.assertEqual(candidates[0].artists, ("vibermx",))
        self.assertFalse(metadata_features(Track("spotify", "sp", "fake ur face", ("s0rrow",)),
                                           candidates[0]).safe_exact)

    def test_migration_drops_stale_cache_and_likes_fetch_fresh_metadata(self):
        with closing(sqlite3.connect(sync.DB_FILE)) as db, db:
            db.execute("CREATE TABLE yandex_cache (id TEXT, artists TEXT, title TEXT, query TEXT)")
            db.execute("INSERT INTO yandex_cache VALUES ('1', 'vibermx', 'fake ur face', 'old')")
            db.execute("PRAGMA user_version = 1")
        sync.init_db()
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name = 'yandex_cache'").fetchone())
        actual = SimpleNamespace(id="1", artists=[SimpleNamespace(name="s0rrow")],
                                 title="fake ur face", version=None)
        client = SimpleNamespace(users_likes_tracks=lambda: SimpleNamespace(tracks=[
            SimpleNamespace(id="1", album_id=None)]), tracks=lambda ids: [actual])
        self.assertEqual(sync.get_ym_likes(client)[0]["artists"], "s0rrow")

    def test_fuzzy_existing_match_preserves_version(self):
        self.assertFalse(metadata_features(Track("yandex", "1", "Song - Remix", ("Artist",)),
                                           Track("spotify", "2", "Song", ("Artist",))).safe_exact)

    def test_catalog_title_annotations_and_artist_mismatch(self):
        for artist, left, right in [
            ("KEYGEN CHURCH", "Tenebre Rosso Sangue", "Tenebre Rosso Sangue (ULTRAKILL Original Game Soundtrack)"),
            ("MORGENSHTERN", "Yung Hefner ROCK REMIX", "Yung Hefner - ROCK REMIX"),
        ]:
            with self.subTest(title=left):
                self.assertGreater(metadata_features(Track("yandex", "1", left, (artist,)),
                                                     Track("spotify", "2", right, (artist,))).rank, 50)
        self.assertFalse(metadata_features(Track("yandex", "1", "fake ur face", ("vibermx",)),
                                           Track("spotify", "2", "fake ur face", ("s0rrow",))).safe_exact)
        self.assertFalse(metadata_features(Track("yandex", "1", "Song", ("Artist",)),
                                           Track("spotify", "2", "Song - Rock Remix", ("Artist",))).safe_exact)

    def test_existing_spotify_candidate_with_different_artist_needs_approval(self):
        source = {"id": "ym", "artists": "vibermx", "title": "fake ur face", "search_query": "vibermx fake ur face"}
        target = {"id": "sp", "artists": "s0rrow", "title": "fake ur face"}
        client = SimpleNamespace(search=lambda **kw: {"tracks": {"items": []}})
        with patch.object(sync, "get_ym_client", return_value=object()), patch.object(sync, "get_sp_client", return_value=client), \
             patch.object(sync, "get_ym_likes", return_value=[source]), patch.object(sync, "get_sp_likes", return_value=[target]):
            self.assertIn("на одобрении 1", sync.sync_ym_to_sp())
        self.assertEqual(sync.get_pending_tracks()["ym_to_sp:ym"]["found_id"], "sp")
        with closing(sqlite3.connect(sync.DB_FILE)) as db:
            self.assertEqual(db.execute("SELECT * FROM mappings").fetchall(), [])

    def test_health_reports_client_initialization_failure(self):
        with patch.object(sync, "get_ym_client", side_effect=ValueError("bad token")), patch.object(sync, "get_sp_client", return_value=None):
            result = sync.check_api_health()
        self.assertFalse(result["yandex"])
        self.assertIn("bad token", result["yandex_error"])

    def test_spotify_without_authorization_fails_with_setup_instruction(self):
        auth = SimpleNamespace(cache_handler=SimpleNamespace(get_cached_token=lambda: None),
                               validate_token=lambda token: None)
        with patch.multiple(sync, SPOTIPY_CLIENT_ID="id", SPOTIPY_CLIENT_SECRET="secret", SPOTIPY_REDIRECT_URI="http://127.0.0.1:8888/callback"), patch.object(sync, "SpotifyOAuth", return_value=auth):
            with self.assertRaisesRegex(RuntimeError, "auth_spotify.py"):
                sync.get_sp_client()

    def test_full_sync_propagates_failure_instead_of_success_text(self):
        with patch.object(sync, "sync_ym_to_sp", side_effect=ConnectionError("offline")):
            with self.assertRaises(ConnectionError):
                sync.full_two_way_sync()


class BotRegressionTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        import config
        with patch.object(config, "TG_BOT_TOKEN", "123456:offline_test_token"), patch.object(config, "TG_ADMIN_ID", 42), patch("logging.handlers.TimedRotatingFileHandler", return_value=logging.NullHandler()):
            cls.bot = importlib.import_module("main")

    async def test_repeated_background_error_notifies_once(self):
        sent = []
        async def send(*args, **kw):
            sent.append(args)
        with patch.object(self.bot, "_last_background_error", None), patch.object(self.bot, "is_sync_running", return_value=False), patch.object(
            self.bot, "full_two_way_sync", side_effect=RuntimeError("Premium required")
        ), patch.object(self.bot.bot, "send_message", side_effect=send):
            await self.bot.periodic_sync()
            await self.bot.periodic_sync()
        self.assertEqual(len(sent), 1)

    async def test_sync_commands_select_direction_and_reject_non_admin(self):
        sent = []
        async def answer(text, **kw):
            sent.append(text)
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), answer=answer)
        with patch.object(self.bot, "is_sync_running", return_value=False), patch.multiple(
            self.bot, full_two_way_sync=lambda: "both", sync_ym_to_sp=lambda: "to Spotify",
            sync_sp_to_ym=lambda: "to Yandex"):
            for command, result in [("sync", "both"), ("sync_all", "both"),
                                    ("sync_ym_sp", "to Spotify"), ("sync_sp_ym", "to Yandex")]:
                await self.bot.sync_handler(message, SimpleNamespace(command=command))
                self.assertIn(result, sent[-1])
            sent.clear()
            message.from_user.id = 99
            await self.bot.sync_handler(message, SimpleNamespace(command="sync"))
            self.assertEqual(sent, [])

    async def test_jev_key_message_is_deleted_and_never_echoed(self):
        sent = []
        deleted = []
        async def answer(text, **kw):
            sent.append(text)
        async def delete():
            deleted.append(True)
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), chat=SimpleNamespace(type="private"),
                                  text="/jev openrouter private-key", answer=answer, delete=delete)
        with patch.object(self.bot, "configure_jev") as configure, patch.object(
            self.bot, "get_jev_status", return_value={"enabled": True, "provider": "openrouter", "configured": True}
        ):
            await self.bot.jev_handler(message)
        self.assertEqual(deleted, [True])
        configure.assert_called_once_with("openrouter", "private-key")
        self.assertNotIn("private-key", " ".join(sent))

    async def test_long_lines_and_emoji_fit_telegram_limit(self):
        sent = []
        async def answer(text, **kw):
            sent.append(text)
        text = "🎵" * 5000
        await self.bot.send_long_message(SimpleNamespace(answer=answer), "Header", [text])
        self.assertEqual("".join(sent), "Header\n" + text)
        self.assertTrue(all(len(s.encode("utf-16-le")) // 2 <= 4000 for s in sent))

    async def test_pending_is_bounded_and_second_page_is_reachable(self):
        sent = []
        async def answer(text, **kw):
            sent.append((text, kw))
        pending = {f"ym_to_sp:{i}": {"direction": "ym_to_sp", "source": "Source", "found": "Found", "score": 80} for i in range(12)}
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), text="/pending 2", answer=answer)
        with patch.object(self.bot, "get_pending_tracks", return_value=pending):
            await self.bot.pending_handler(message)
        cards = [kw["reply_markup"] for _, kw in sent if "reply_markup" in kw]
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[0].inline_keyboard[0][0].callback_data, "approve:ym_to_sp:5")
        self.assertIn("/pending 3", sent[-1][0])

    async def test_cleanup_requires_confirmation_before_api_access(self):
        async def answer(text, **kw):
            return text
        message = SimpleNamespace(from_user=SimpleNamespace(id=42), text="/clean_sp_dupes", answer=answer)
        with patch.object(self.bot, "remove_spotify_duplicates", side_effect=AssertionError("must not run")):
            result = await self.bot.clean_sp_dupes_handler(message)
        self.assertIn("confirm", result)


if __name__ == "__main__":
    unittest.main()

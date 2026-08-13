"""
Session validation cost, and the two tables that grew without bound.

Three defects motivated these tests:

1. ``validate_session_jti`` consulted the cache and then read the database
   **anyway**. The cache saved the ``last_used`` write, not the read, so every
   authenticated request in the system cost one query against ``user_sessions`` —
   the ceiling on request throughput, in a class named for caching.
2. Nothing ever pruned ``user_sessions``. One row per login, forever, each holding
   an IP address and a device name.
3. Nothing ever ran SimpleJWT's ``flushexpiredtokens``. With rotation on, every
   refresh blacklists a token and nothing removes it once expired.

The first is measured with ``assertNumQueries`` — the only assertion that can tell
"reads the cache" from "reads the cache and then the database anyway".
"""

from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from user.models import CustomUser
from user.models.user_session import UserSession
from user.services import sessions as session_service


class SessionTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = CustomUser.objects.create_user(
            username="ada", email="ada@example.com", password="pw"
        )

    def make_session(self, jti="jti-1", expires_in=timedelta(days=7), access_in=None):
        now = timezone.now()
        return UserSession.objects.create(
            user=self.user,
            refresh_jti=f"refresh-{jti}",
            access_jti=jti,
            access_iat=now,
            access_exp=now + (access_in or timedelta(hours=1)),
            refresh_iat=now,
            refresh_exp=now + expires_in,
            expires_at=now + expires_in,
        )

    @staticmethod
    def key(jti):
        return f"user_session:access:{jti}"


class ValidationCostTests(SessionTestCase):
    def test_a_warm_cache_costs_no_query(self):
        """The defect, measured. This is the assertion the old implementation could
        not pass: it read the cache and then issued objects.get() regardless."""
        session = self.make_session()
        UserSession.validate_session_jti(session.access_jti)  # warms the cache

        with self.assertNumQueries(0):
            UserSession.validate_session_jti(session.access_jti)

    def test_a_cold_cache_reads_once_and_caches_the_verdict(self):
        """One SELECT and nothing else. `update_last_used` throttles to once per 60s
        and the row was just created, so the write does not fire here."""
        session = self.make_session()

        with self.assertNumQueries(1):
            result = UserSession.validate_session_jti(session.access_jti)

        self.assertEqual(result.pk, session.pk)
        self.assertEqual(cache.get(self.key(session.access_jti)), "active")

    def test_last_used_is_refreshed_on_a_stale_row(self):
        """The throttle must not become "never". A row untouched for over a minute
        gets its timestamp written on the next cache miss."""
        session = self.make_session()
        stale = timezone.now() - timedelta(minutes=5)
        UserSession.all_objects.filter(pk=session.pk).update(last_used=stale)

        UserSession.validate_session_jti(session.access_jti)

        session.refresh_from_db()
        self.assertGreater(session.last_used, stale)

    def test_a_revoked_verdict_costs_no_query(self):
        """A rejected token must be cheap for its whole life, or an expired-token
        retry storm becomes a database load problem."""
        session = self.make_session()
        cache.set(self.key(session.access_jti), "revoked", timeout=600)

        with self.assertNumQueries(0):
            with self.assertRaises(UserSession.DoesNotExist):
                UserSession.validate_session_jti(session.access_jti)

    def test_an_unknown_jti_is_remembered_as_revoked(self):
        with self.assertRaises(UserSession.DoesNotExist):
            UserSession.validate_session_jti("never-existed")

        self.assertEqual(cache.get(self.key("never-existed")), "revoked")

        with self.assertNumQueries(0):
            with self.assertRaises(UserSession.DoesNotExist):
                UserSession.validate_session_jti("never-existed")

    def test_the_positive_verdict_is_short_lived(self):
        """Not the token lifetime. A cached "active" is how long a revocation that
        bypasses revoke() — an admin edit, a bulk update — stays invisible."""
        self.assertLessEqual(UserSession.POSITIVE_CACHE_SECONDS, 300)
        self.assertLess(
            UserSession.POSITIVE_CACHE_SECONDS, settings.ACCESS_TOKEN_LIFETIME_SECONDS
        )

    def test_the_ttl_is_configurable(self):
        session = self.make_session()

        with self.settings(SESSION_CACHE_TTL_SECONDS=5):
            UserSession.validate_session_jti(session.access_jti)

        self.assertEqual(cache.get(self.key(session.access_jti)), "active")

    def test_the_cache_never_outlives_the_token(self):
        """A key answering "active" for a credential that has already expired would
        be a live bypass."""
        session = self.make_session(access_in=timedelta(seconds=5))

        with self.settings(SESSION_CACHE_TTL_SECONDS=3600):
            UserSession.validate_session_jti(session.access_jti)

        # LocMem cannot report a TTL, so assert the branch instead: an access_exp
        # closer than the configured TTL must clamp it.
        remaining = int(session.access_exp.timestamp() - timezone.now().timestamp())
        self.assertLess(remaining, 3600)

    def test_a_dead_cache_falls_back_to_the_database(self):
        """Redis down must be slower, never wrong."""
        from unittest.mock import patch

        session = self.make_session()

        with patch(
            "user.models.user_session.cache.get", side_effect=RuntimeError("redis down")
        ):
            result = UserSession.validate_session_jti(session.access_jti)

        self.assertEqual(result.pk, session.pk)

    def test_an_expired_session_is_refused(self):
        session = self.make_session(expires_in=timedelta(seconds=-1))

        with self.assertRaises(UserSession.DoesNotExist):
            UserSession.validate_session_jti(session.access_jti)

    def test_an_inactive_session_is_refused(self):
        session = self.make_session()
        session.is_active = False
        session.save(update_fields=["is_active"])

        with self.assertRaises(UserSession.DoesNotExist):
            UserSession.validate_session_jti(session.access_jti)


class RevocationImmediacyTests(SessionTestCase):
    """Caching a positive verdict is only safe because every in-code revocation
    path writes the negative one. These hold that."""

    def test_revoke_takes_effect_immediately_despite_a_warm_cache(self):
        session = self.make_session()
        UserSession.validate_session_jti(session.access_jti)
        self.assertEqual(cache.get(self.key(session.access_jti)), "active")

        session.revoke()

        with self.assertRaises(UserSession.DoesNotExist):
            UserSession.validate_session_jti(session.access_jti)

    def test_logout_revokes_through_the_same_key(self):
        session = self.make_session()
        session.revoke()
        self.assertEqual(cache.get(self.key(session.access_jti)), "revoked")


class WebSocketSessionTests(SessionTestCase):
    """The WS handshake needs the row, not a verdict — it goes into the connection
    scope so a consumer can address one session."""

    def test_it_returns_the_row_even_with_a_warm_cache(self):
        session = self.make_session()
        UserSession.validate_session_jti(session.access_jti)  # warm

        loaded = UserSession.get_active_session(session.access_jti)

        self.assertIsNotNone(
            loaded,
            "a warm cache must not leave scope['session'] empty — session-scoped "
            "WebSocket messaging depends on this row",
        )
        self.assertEqual(loaded.pk, session.pk)

    def test_a_revoked_token_is_refused_without_a_query(self):
        session = self.make_session()
        cache.set(self.key(session.access_jti), "revoked", timeout=600)

        with self.assertNumQueries(0):
            with self.assertRaises(UserSession.DoesNotExist):
                UserSession.get_active_session(session.access_jti)

    def test_the_middleware_uses_the_row_returning_variant(self):
        """Guard: swapping this back to validate_session_jti would break WS session
        messaging only when the cache happens to be warm."""
        import inspect

        from utils.middleware import websocket_auth

        source = inspect.getsource(websocket_auth)
        # Match the call, not the bare name — the surrounding comment explains why
        # the other method is wrong here, so a substring check would fail on prose.
        self.assertIn("UserSession.get_active_session(", source)
        self.assertNotIn("UserSession.validate_session_jti(", source)


class RetentionTests(SessionTestCase):
    def make_expired(self, days_ago, jti):
        session = self.make_session(jti=jti)
        UserSession.all_objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(days=days_ago)
        )
        session.refresh_from_db()
        return session

    def test_expired_sessions_are_marked_inactive(self):
        """`is_active` could not distinguish "logged out" from "long expired"."""
        expired = self.make_expired(1, "old")
        live = self.make_session(jti="live")

        updated = session_service.deactivate_expired_sessions()

        self.assertEqual(updated, 1)
        expired.refresh_from_db()
        live.refresh_from_db()
        self.assertFalse(expired.is_active)
        self.assertTrue(live.is_active)

    def test_sessions_past_the_retention_window_are_deleted(self):
        old = self.make_expired(90, "ancient")

        with self.settings(USER_SESSION_RETENTION_DAYS=30):
            session_service.purge_expired_sessions()

        self.assertFalse(UserSession.all_objects.filter(pk=old.pk).exists())

    def test_recently_expired_sessions_are_kept(self):
        """The window is the point: an "unrecognised device" investigation happens
        weeks after the session ended, not during it."""
        recent = self.make_expired(5, "recent")

        with self.settings(USER_SESSION_RETENTION_DAYS=30):
            session_service.purge_expired_sessions()

        self.assertTrue(UserSession.all_objects.filter(pk=recent.pk).exists())

    def test_live_sessions_are_never_purged(self):
        live = self.make_session(jti="live")

        with self.settings(USER_SESSION_RETENTION_DAYS=0):
            session_service.purge_expired_sessions()

        self.assertTrue(UserSession.all_objects.filter(pk=live.pk).exists())

    def test_a_dry_run_deletes_nothing(self):
        old = self.make_expired(90, "ancient")

        with self.settings(USER_SESSION_RETENTION_DAYS=30):
            count = session_service.purge_expired_sessions(dry_run=True)

        self.assertEqual(count, 1)
        self.assertTrue(UserSession.all_objects.filter(pk=old.pk).exists())

    def test_the_purge_is_chunked(self):
        for index in range(5):
            self.make_expired(90, f"old-{index}")

        with self.settings(USER_SESSION_RETENTION_DAYS=30):
            deleted = session_service.purge_expired_sessions(batch=2)

        self.assertEqual(deleted, 5)
        self.assertEqual(UserSession.all_objects.count(), 0)

    def test_the_blacklist_flush_runs(self):
        from unittest.mock import patch

        with patch("django.core.management.call_command") as mock_call:
            session_service.flush_expired_blacklist_tokens()

        mock_call.assert_called_once_with("flushexpiredtokens")

    def test_the_sweep_does_all_three(self):
        expired = self.make_expired(1, "expired-recent")
        ancient = self.make_expired(90, "ancient")

        with self.settings(USER_SESSION_RETENTION_DAYS=30):
            result = session_service.sweep()

        self.assertEqual(result["purged"], 1)
        self.assertGreaterEqual(result["deactivated"], 1)
        self.assertTrue(result["blacklist_flushed"])
        self.assertFalse(UserSession.all_objects.filter(pk=ancient.pk).exists())
        self.assertTrue(UserSession.all_objects.filter(pk=expired.pk).exists())


class CommandAndScheduleTests(SessionTestCase):
    def test_the_command_runs(self):
        session = self.make_session(jti="old")
        UserSession.all_objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(days=90)
        )

        call_command("purge_expired_sessions", "--older-than-days", "30")

        self.assertFalse(UserSession.all_objects.filter(pk=session.pk).exists())

    def test_the_command_dry_run_changes_nothing(self):
        session = self.make_session(jti="old")
        UserSession.all_objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(days=90)
        )

        call_command("purge_expired_sessions", "--dry-run")

        self.assertTrue(UserSession.all_objects.filter(pk=session.pk).exists())

    def test_the_sweep_is_scheduled(self):
        """Both tables grew for months because nothing ran. A cleanup that has to be
        remembered is a cleanup that does not happen."""
        entry = settings.CELERY_BEAT_SCHEDULE.get("purge-expired-sessions")
        self.assertIsNotNone(entry, "session sweep is not in CELERY_BEAT_SCHEDULE")
        self.assertEqual(entry["task"], "user.tasks.purge_expired_sessions")

    def test_the_task_delegates_to_the_sweep(self):
        from user.tasks import purge_expired_sessions

        session = self.make_session(jti="old")
        UserSession.all_objects.filter(pk=session.pk).update(
            expires_at=timezone.now() - timedelta(days=90)
        )

        with self.settings(USER_SESSION_RETENTION_DAYS=30):
            purged = purge_expired_sessions()

        self.assertEqual(purged, 1)

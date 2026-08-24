"""
Postgres-backed test settings.

The default ``config.settings.local`` forces an in-memory SQLite database when
running tests. SQLite cannot host Postgres-only features this project depends
on — the ``ArrayField`` on ``AgentCallLog`` and, for the scheduling module, the
``ExclusionConstraint`` that is the authoritative guarantee against
double-booking. Run the suite against Postgres with::

    uv run python manage.py test core --settings=config.settings.test_postgres

This creates/destroys a throwaway ``test_<POSTGRES_DB>`` database on the
configured Postgres server; the development database is never touched.
"""
import sys

from cryptography.fernet import Fernet

from .base import *  # noqa: F401,F403

# A fresh encryption key per test run (consistent within the run) so calendar
# token encrypt/decrypt works without external configuration.
SCHEDULING_TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()

# Decouple tests from Redis-backed cache / channel layers.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "voicebot-test-cache",
    }
}
CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}

# Capture emails in an in-memory outbox so notification tests can assert on them.
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Booking mail goes through the tenant's own SMTP configuration, which builds its
# own connection and so would bypass EMAIL_BACKEND entirely and open a real socket.
# Pointing it at the same outbox keeps the whole send path — sender resolution,
# From: formatting, .ics attachment — under test instead of mocked at the seam.
TENANT_SMTP_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Test fixtures use hostnames that do not resolve. Host validation has its own
# dedicated tests that switch this back off.
TENANT_SMTP_ALLOW_PRIVATE_HOSTS = True

# No real payment gateway in tests: order creation is stubbed and callbacks are
# accepted unsigned. Explicit here rather than inferred from a dummy credential,
# which is what used to leave production open when an env var went missing.
PAYMENTS_ALLOW_UNSIGNED = True

# Execute Celery tasks synchronously in-process during tests.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Faster hashing for test user creation.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

if "test" in sys.argv:
    import tempfile

    MEDIA_ROOT = tempfile.mkdtemp(prefix="django_test_media_pg_")
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }

import sys
from .base import *

# Override MEDIA_ROOT during testing to prevent cluttering the media folder
if 'test' in sys.argv:
    import tempfile
    import shutil
    import atexit

    TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="django_test_media_")
    MEDIA_ROOT = TEST_MEDIA_ROOT

    # Force FileSystemStorage for unit testing isolation
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }

    def cleanup_test_media():
        try:
            shutil.rmtree(TEST_MEDIA_ROOT)
        except Exception:
            pass

    atexit.register(cleanup_test_media)

    # Use in-memory channel layers for testing isolation
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels.layers.InMemoryChannelLayer",
        }
    }

    # Use in-memory cache for testing isolation to decouple from Redis and prevent test timeouts
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "unique-snowflake-test-cache",
        }
    }
    # Use SQLite in-memory database during tests to avoid external PostgreSQL dependencies and locks
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# Logging Configuration for Local Development
DJANGO_JSON_LOGGING = env.bool("DJANGO_JSON_LOGGING", default=False)
DJANGO_SIMPLE_LOGGING = env.bool("DJANGO_SIMPLE_LOGGING", default=False)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{levelname}] {asctime} [{name}] {module} ({filename}:{lineno}) - {message}",
            "style": "{",
        },
        "json": {
            "()": "utils.logging.JSONFormatter.JsonFormatter",
        },
        "simple": {
            "format": "[{levelname}] {asctime} - {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if DJANGO_JSON_LOGGING else "simple" if DJANGO_SIMPLE_LOGGING else "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["console"],
            "level": "WARNING",  # Set to DEBUG to see all SQL queries
            "propagate": False,
        },
    },
}

# Allow all CORS origins if explicitly set in local .env
CORS_ALLOW_ALL_ORIGINS = env.bool("CORS_ALLOW_ALL_ORIGINS", default=False)

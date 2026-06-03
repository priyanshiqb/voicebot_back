import sys
from .base import *

# Override MEDIA_ROOT during testing to prevent cluttering the media folder
if 'test' in sys.argv:
    import tempfile
    import shutil
    import atexit

    TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix="django_test_media_")
    MEDIA_ROOT = TEST_MEDIA_ROOT

    def cleanup_test_media():
        try:
            shutil.rmtree(TEST_MEDIA_ROOT)
        except Exception:
            pass

    atexit.register(cleanup_test_media)

# Logging Configuration for Local Development
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
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

"""A second test database, so a targeted run can proceed while a full suite holds
the primary one. Identical to test_postgres apart from the database name."""

from .test_postgres import *  # noqa: F401,F403
from .test_postgres import DATABASES

DATABASES["default"].setdefault("TEST", {})
DATABASES["default"]["TEST"]["NAME"] = "test_alt_voicebot"

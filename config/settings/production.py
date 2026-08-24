from .base import *
import environ
from django.core.exceptions import ImproperlyConfigured

DEBUG = False


SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=True)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=True)

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
USE_X_FORWARDED_HOST = True

# Hard-coded, not env-driven. Tenants supply the hostname our workers dial, and
# permitting private addresses turns that into an SSRF probe against cloud
# metadata endpoints and internal services. A control that can be flipped by an
# environment variable in production is not a control.
TENANT_SMTP_ALLOW_PRIVATE_HOSTS = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "utils.logging.JSONFormatter.JsonFormatter",
        },
    },
    "handlers": {
        "console_json": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["console_json"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console_json"],
            "level": "INFO",
            "propagate": False,
        },
        "django.security.DisallowedHost": {
            "handlers": ["console_json"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #
# Refuse to start rather than run a payment endpoint that accepts anything.
#
# `onboard/checks.py` covers this too, but a check can be skipped — `migrate
# --skip-checks`, a management command with `requires_system_checks = []`, a WSGI
# server that never runs them. This raises at import, so there is no path into a
# production process with verification disabled.
#
# The .env file has promised this guard for some time; until now it did not exist.
if PAYMENTS_ALLOW_UNSIGNED:  # noqa: F405
    raise ImproperlyConfigured(
        "PAYMENTS_ALLOW_UNSIGNED must not be enabled under production settings. "
        "It disables Razorpay signature verification, so any unsigned request to "
        "the webhook would be accepted as a genuine payment."
    )

if not RAZORPAY_WEBHOOK_SECRET:  # noqa: F405
    raise ImproperlyConfigured(
        "RAZORPAY_WEBHOOK_SECRET is required in production. Without it every "
        "genuine webhook fails signature verification and is rejected, so payments "
        "capture at Razorpay while nothing happens here."
    )

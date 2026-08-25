import os
from pathlib import Path
import environ
from urllib.parse import urlparse
from corsheaders.defaults import default_headers

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Initialize environ
env = environ.Env()
loaded_files = []

# 1. Read environment-specific overrides first (defaulting to .env.local)
env_file = os.environ.get("ENV_FILE", ".env.local")
if env_file != ".env":
    env_override_path = os.path.join(BASE_DIR, env_file)
    if os.path.exists(env_override_path):
        environ.Env.read_env(env_override_path)
        loaded_files.append(env_file)

# 2. Read default/base environment variables second (.env)
env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_path):
    environ.Env.read_env(env_path)
    loaded_files.append(".env")

# Log settings initialization details to console
import sys
import logging

boot_logger = logging.getLogger("django.boot")
if not boot_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('[Django Boot] %(message)s'))
    boot_logger.addHandler(handler)
    boot_logger.setLevel(logging.INFO)

settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings.local")
boot_logger.info(f"Loaded env files: {loaded_files or 'None (using system env)'}")
boot_logger.info(f"Active settings module: {settings_module}")


DEBUG = env.bool('DJANGO_DEBUG', default=False)
SECRET_KEY = env('DJANGO_SECRET_KEY')


ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

# Ensure localhost is allowed for Docker healthchecks
if "localhost" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("localhost")
if "127.0.0.1" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("127.0.0.1")
# Allow Docker Compose internal service names for Prometheus scraping and inter-container traffic
if "backend-wsgi" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("backend-wsgi")
if "backend-asgi" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("backend-asgi")

BACKEND_PROJECT_URL = env('BACKEND_PROJECT_URL')
be_parsed=urlparse(BACKEND_PROJECT_URL)
if be_parsed.hostname and be_parsed.hostname not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(be_parsed.hostname)

FRONTEND_PROJECT_URL = env('FRONTEND_PROJECT_URL')

CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=[FRONTEND_PROJECT_URL])

CORS_ALLOWED_ORIGINS.extend(
    filter(
        None,
        [
            FRONTEND_PROJECT_URL,
            BACKEND_PROJECT_URL,
        ]
    )
)

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[FRONTEND_PROJECT_URL,BACKEND_PROJECT_URL])
if FRONTEND_PROJECT_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(FRONTEND_PROJECT_URL)
if BACKEND_PROJECT_URL not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(BACKEND_PROJECT_URL)


INSTALLED_APPS = [
    "daphne",
    "channels",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    
    'django.contrib.postgres',

    # Third party apps
    "rest_framework",
    "rest_framework_simplejwt",
    'rest_framework_simplejwt.token_blacklist',

    "drf_spectacular",
    "corsheaders",
    "django_filters",
    "defender",
    "django_prometheus",
    
    # OTP & Two-Factor Authentication
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
    "import_export",
    
    # Local custom apps
    "user",
    "tenant",
    "onboard",
    "core",
    "wallet",
]

CORS_ALLOW_HEADERS = [
    *default_headers,
    'ngrok-skip-browser-warning',
    'accept',
    'accept-encoding',
    'authorization',
    'content-type',
    'dnt',
    'origin',
    'user-agent',
    'x-csrftoken',
    'x-requested-with',
    'x-recaptcha-token',
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "config.middleware.RequestTracingMiddleware",  # Dynamic Request ID tracking
    "config.middleware.ReCaptchaMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "defender.middleware.FailedLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django_prometheus.db.backends.postgresql",
        "NAME": env("POSTGRES_DB"),
        "USER": env("POSTGRES_USER"),
        "PASSWORD": env("POSTGRES_PASSWORD"),
        "HOST": env("POSTGRES_HOST"),
        "PORT": env("POSTGRES_PORT"),
    }
}

AUTH_USER_MODEL = "user.CustomUser"


AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static'),
]

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# S3 / MinIO / Cloudflare R2 Storage Configuration
S3_ENDPOINT_URL = env("S3_ENDPOINT_URL", default=None)
S3_ACCESS_KEY_ID = env("S3_ACCESS_KEY_ID", default=None)
S3_SECRET_ACCESS_KEY = env("S3_SECRET_ACCESS_KEY", default=None)
S3_BUCKET_NAME = env("S3_BUCKET_NAME", default=None)
S3_REGION_NAME = env("S3_REGION_NAME", default="us-east-1")
S3_CUSTOM_DOMAIN = env("S3_CUSTOM_DOMAIN", default=None)

USE_S3_STORAGE = all(
    [
        S3_ACCESS_KEY_ID,
        S3_SECRET_ACCESS_KEY,
        S3_BUCKET_NAME,
    ]
)

if USE_S3_STORAGE:
    AWS_ACCESS_KEY_ID = S3_ACCESS_KEY_ID
    AWS_SECRET_ACCESS_KEY = S3_SECRET_ACCESS_KEY
    AWS_STORAGE_BUCKET_NAME = S3_BUCKET_NAME
    AWS_S3_REGION_NAME = S3_REGION_NAME
    AWS_QUERYSTRING_AUTH = env.bool("AWS_QUERYSTRING_AUTH", default=False)

    if S3_ENDPOINT_URL:
        AWS_S3_ENDPOINT_URL = S3_ENDPOINT_URL

    if S3_CUSTOM_DOMAIN:
        custom_domain_parsed = urlparse(S3_CUSTOM_DOMAIN) if "://" in S3_CUSTOM_DOMAIN else urlparse(f"http://{S3_CUSTOM_DOMAIN}")
        AWS_S3_CUSTOM_DOMAIN = custom_domain_parsed.netloc
        AWS_S3_URL_PROTOCOL = f"{custom_domain_parsed.scheme}:"

    AWS_S3_SIGNATURE_VERSION = "s3v4"
    AWS_S3_FILE_OVERWRITE = False
    AWS_DEFAULT_ACL = None

    STORAGES = {
        "default": {
            "BACKEND": "storages.backends.s3.S3Storage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
else:
    STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# DRF Configurations
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "user.authentication.agent_token.AgentTokenAuthentication",
        "user.authentication.CachedJWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "EXCEPTION_HANDLER": "utils.exception.custom_exception_handler",
    # Only consulted where a view sets `throttle_scope`, so this adds no global
    # rate limit. Device registration is the one write a client is told to repeat
    # on every app start, which makes an unbounded loop there indistinguishable
    # from normal traffic until the table is full of junk.
    "DEFAULT_THROTTLE_RATES": {
        "device_token_register": env.str("DEVICE_TOKEN_REGISTER_RATE", default="600/hour"),
    },
    # Subclasses drf-spectacular's AutoSchema to document the X-Tenant-ID header on the
    # operations agent tokens can reach, driven off each viewset's
    # `vb_agent_allowed_actions` so Swagger cannot drift from what is enforced.
    "DEFAULT_SCHEMA_CLASS": "tenant.common.views.agent_tenant_scope.VBAgentAwareAutoSchema",
}

# OpenAPI Settings
SPECTACULAR_SETTINGS = {
    "TITLE": "Voicebot Backend API",
    "DESCRIPTION": "Enterprise production REST API for the Voicebot platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "ENUM_NAME_OVERRIDES": {
        "TransactionStatusEnum": "onboard.models.transaction.Transaction.TransactionStatusChoices",
        "SubscriptionStatusEnum": "onboard.models.subscription.Subscription.SubscriptionStatusChoices",

        # Credit wallet
        "CreditEntryTypeEnum": "wallet.models.credit_ledger.CreditLedger.EntryType",
        "CreditBucketEnum": "wallet.models.credit_ledger.CreditLedger.Bucket",
        "CreditOperationEnum": "wallet.models.credit_rate.CreditRate.Operation",
        "CreditUnitEnum": "wallet.models.credit_rate.CreditRate.Unit",
        "CreditRoundingEnum": "wallet.models.credit_rate.CreditRate.Rounding",
        "KBETLTrackerStatusEnum": "core.models.kb_etl_tracker.KBETLTracker.OverallStatus",
        
        # Types
        "ExceptionTypeEnum": "core.models.availability_exception.AvailabilityException.ExceptionType",
        "EventTypeEnum": "core.models.booking_event.BookingEvent.EventType",
        "LocationTypeEnum": "core.models.event_template.EventTemplate.LocationType",
        "NotificationTypeEnum": "core.models.notification_log.NotificationLog.NotificationType",
        "UserTypeEnum": "user.models.CustomUser.CustomUser.UserType",
        "PlanTypeEnum": "onboard.models.plan.Plan.PlanTypeChoices",

        # Statuses
        "CampaignMemberStatusEnum": "core.models.campaign_member.CampaignMember.Status",
        "BookingStatusEnum": "core.models.booking.Booking.Status",
        "ContactImportSessionStatusEnum": "core.models.contact_import.ContactImportSession.Status",
        "ContactImportRowStatusEnum": "core.models.contact_import.ContactImportRow.Status",
        "NotificationLogStatusEnum": "core.models.notification_log.NotificationLog.Status",
        "CampaignStatusEnum": "core.models.campaign.Campaign.Status",
        "BookingParticipantResponseStatusEnum": "core.models.booking_participant.BookingParticipant.ResponseStatus",
        "ContactConsentStatusEnum": "core.models.contact.Contact.ConsentStatus",
        
        # Sources
        "BookingSourceEnum": "core.models.booking.Booking.Source",
        "ContactSourceEnum": "core.models.contact.Contact.Source",
        "CampaignAudienceSourceEnum": "core.models.campaign.Campaign.AudienceSource",
    },
}

# ReCaptcha Verification Settings
RECAPTCHA_VERIFICATION_ON = env.bool("RECAPTCHA_VERIFICATION_ON", default=False)
RECAPTCHA_REQUIRED_PATHS = [
    "/api/v1/onboard/register-pending/",
    "/api/v1/onboard/verify-otp/",
    "/api/v1/onboard/resend-otp/",
    "/api/v1/onboard/change-email/",
    "/api/v1/onboard/resume-session/",
    "/api/v1/onboard/forgot-password/",
    "/api/v1/onboard/forgot-password/verify/",
    "/api/v1/onboard/forgot-password/resend/",
    "/api/v1/onboard/reset-password/",
    "/api/v1/onboard/check-email/",
    "/api/v1/auth/forgot-password/",
    "/api/v1/auth/forgot-password/verify/",
    "/api/v1/auth/forgot-password/resend/",
    "/api/v1/auth/reset-password/",
]


# SimpleJWT Configurations
from datetime import timedelta

ACCESS_TOKEN_LIFETIME_SECONDS = 60 * 60  # 60 minutes
REFRESH_TOKEN_LIFETIME_SECONDS = 60 * 60 * 24 * 7  # 7 days

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(seconds=ACCESS_TOKEN_LIFETIME_SECONDS),
    "REFRESH_TOKEN_LIFETIME": timedelta(seconds=REFRESH_TOKEN_LIFETIME_SECONDS),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    "SIGNING_KEY": SECRET_KEY,
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
    "CHECK_USER_IS_ACTIVE": True,
}

# --- Session hygiene --------------------------------------------------------
# How long an EXPIRED login session is kept before deletion, measured from expiry
# rather than creation. Rows hold an IP address and a device name, so this is a
# privacy window before a disk one: a 7-day session removed 30 days after it
# expired stayed visible for 37, which is what an "unrecognised device"
# investigation needs.
#
# The same daily task also runs SimpleJWT's `flushexpiredtokens`. With
# ROTATE_REFRESH_TOKENS and BLACKLIST_AFTER_ROTATION both on, every refresh writes
# an outstanding-token row and blacklists the old one — roughly eight per active
# user per day — and nothing else ever removes them.
USER_SESSION_RETENTION_DAYS = env.int("USER_SESSION_RETENTION_DAYS", default=30)
USER_SESSION_PURGE_BATCH = env.int("USER_SESSION_PURGE_BATCH", default=5000)

# How long a *positive* session check is trusted without re-reading the row.
# This is what keeps an authenticated request from costing a query: the previous
# implementation consulted the cache and then read the database anyway.
#
# Deliberately short, and deliberately not the token lifetime. Every in-code
# revocation path (logout, refresh rotation) writes "revoked" to the same key and
# so takes effect immediately; this bounds only the paths that do not — an admin
# edit, a bulk update, a direct SQL fix — to at most this many seconds.
SESSION_CACHE_TTL_SECONDS = env.int("SESSION_CACHE_TTL_SECONDS", default=60)

REDIS_HOST = env("REDIS_HOST")
REDIS_PORT = env("REDIS_PORT")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [
                (
                    str(REDIS_HOST),
                    int(REDIS_PORT),
                )
            ],
            "expiry": 120
        },
    }
}

CACHE_REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/1"

CACHES = {
    "default": {
        "BACKEND": "django_prometheus.cache.backends.redis.RedisCache",
        "LOCATION": CACHE_REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}


EMAIL_BACKEND = env.str("EMAIL_BACKEND", "")
EMAIL_HOST = env.str("EMAIL_HOST", "")
EMAIL_PORT = env.str("EMAIL_PORT", "")
EMAIL_USE_TLS = env.str("EMAIL_USE_TLS", "") 
EMAIL_HOST_USER = env.str("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD", "")
EMAIL_SENDER_NAME = env.str("EMAIL_SENDER_NAME", "VoiceBot")

# --- Tenant-owned SMTP ------------------------------------------------------
# The EMAIL_* settings above are the PLATFORM sender. A tenant may store its own
# credentials (tenant.TenantEmailConfiguration); these bound what it may point at.
#
# Platform authentication mail — signup OTP, email verification, password reset —
# must always use the platform sender, never a tenant's. A tenant does not exist
# yet at signup, and a tenant whose SMTP breaks must not be locked out of the
# reset flow that would let them fix it.

# Master switch. Off means every tenant falls back to the platform sender.
TENANT_SMTP_ENABLED = env.bool("TENANT_SMTP_ENABLED", default=True)

# Django's SMTP backend defaults to timeout=None, which blocks forever. A host
# that accepts the connection and goes silent would hang a worker permanently.
TENANT_SMTP_TIMEOUT_SECONDS = env.int("TENANT_SMTP_TIMEOUT_SECONDS", default=10)

# 25 is deliberately excluded: AWS, GCP and Azure block it outbound by default,
# so permitting it buys a timeout instead of an accurate field error. 587 is
# STARTTLS submission, 465 implicit TLS, 2525 the usual fallback.
TENANT_SMTP_ALLOWED_PORTS = env.list(
    "TENANT_SMTP_ALLOWED_PORTS", cast=int, default=[465, 587, 2525]
)

# Tenants supply the hostname we dial, which is SSRF by design. Hosts resolving to
# private, loopback, link-local, reserved or multicast addresses are rejected.
# Turn this on ONLY for local development against MailHog/Mailpit on localhost —
# production pins it False in production.py, because a control that can be flipped
# by environment in production is not a control.
TENANT_SMTP_ALLOW_PRIVATE_HOSTS = env.bool(
    "TENANT_SMTP_ALLOW_PRIVATE_HOSTS", default=False
)

# Test sends allowed per hour, PER USER (not per tenant — DRF's UserRateThrottle
# keys on the user id, so a tenant's budget is this number times its user count).
#
# Attempts are counted, not successes: throttling runs before the view, so a
# rejected login costs the same as a delivered message. That is what makes the
# default tight during first-time setup — Gmail in particular usually takes a few
# tries, since it needs an App Password rather than the account password.
#
# The default is the protection. Raising this by environment is for working
# through a provider's setup; it is not something to leave elevated, because the
# endpoint opens outbound SMTP connections to a host the caller supplies.
SMTP_TEST_THROTTLE_PER_HOUR = env.int("SMTP_TEST_THROTTLE_PER_HOUR", default=5)

# Razorpay Credentials
# Razorpay. No defaults, deliberately.
#
# RAZORPAY_KEY_ID used to default to "rzp_test_dummykey", and three views treated
# that exact value as "skip signature verification". A deploy that forgot the env var
# therefore did not fail — it silently accepted any unsigned webhook as genuine, so
# anyone able to POST to the endpoint could mint a paid subscription. env.str() with
# no default raises ImproperlyConfigured at import instead: an unset credential now
# stops the process rather than quietly disabling a security control.
RAZORPAY_KEY_ID = env.str("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env.str("RAZORPAY_KEY_SECRET")

# Required whenever signatures are actually verified. Kept optional at import so a
# dev box running with PAYMENTS_ALLOW_UNSIGNED does not need a secret it never uses;
# production.py and onboard/checks.py both refuse an empty value when it is needed.
RAZORPAY_WEBHOOK_SECRET = env.str("RAZORPAY_WEBHOOK_SECRET", default="")

# Whether unsigned payment callbacks are accepted. Defaults to False — a default that
# fails closed is fine, a default that fails open is what caused the hole above.
# Replaces the old "is the key the dummy one?" inference: whether signatures are
# checked is now a deliberate setting, not a side effect of what a credential equals.
PAYMENTS_ALLOW_UNSIGNED = env.bool("PAYMENTS_ALLOW_UNSIGNED", default=False)

# Django Defender Configuration (Brute Force Protection)
DEFENDER_REDIS_URL = CACHE_REDIS_URL
DEFENDER_LOGIN_LOCKOUT_LIMIT = 5
DEFENDER_COOLOFF_TIME = 3600  # 1 hour lockout
DEFENDER_BEHIND_REVERSE_PROXY = True
DEFENDER_STORE_USER_AGENT = True
DEFENDER_ACCESS_ATTEMPT_EXPIRY = 24

# Django Two-Factor Authentication Settings
LOGIN_URL = "two_factor:login"
LOGIN_REDIRECT_URL = "admin:index"
TWO_FACTOR_PATCH_ADMIN = True

# Celery Configurations
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", f"redis://{REDIS_HOST}:{REDIS_PORT}/2")
CELERY_RESULT_BACKEND = env.str("CELERY_RESULT_BACKEND", f"redis://{REDIS_HOST}:{REDIS_PORT}/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE

# ETL Server Configuration
ETL_SERVER_URL = env.str("ETL_SERVER_URL")
ETL_SYSTEM_TOKEN = env.str("ETL_SYSTEM_TOKEN", default="dummy_etl_system_token_for_local_dev")
CALLBACK_URL_BASE = env.str("CALLBACK_URL_BASE", default=None)

# File Upload Limits (10 MB + 1 MB buffer = 11 MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024

# OTP Settings
OTP_VALIDITY_MINUTES = env.int("OTP_VALIDITY_MINUTES", default=2)

# Scheduling & Booking
BOOKING_HOLD_TTL_SECONDS = env.int("BOOKING_HOLD_TTL_SECONDS", default=300)
BOOKING_REMINDER_OFFSETS_MINUTES = env.list(
    "BOOKING_REMINDER_OFFSETS_MINUTES", cast=int, default=[1440, 60]
)
BOOKING_RESCHEDULE_TOKEN_TTL_HOURS = env.int(
    "BOOKING_RESCHEDULE_TOKEN_TTL_HOURS", default=72
)

# NotificationLog retention. Rows hold customer email addresses and nothing else
# prunes them, so this is a privacy obligation as much as a disk one. Failures are
# kept longer: a FAILED row is the only trace of a message that never arrived, and
# is what someone goes looking for when a customer complains weeks later.
#
# Rows attached to a booking that has not happened yet are never purged regardless
# of these values — deleting one frees its dedup key and re-arms the notification.
NOTIFICATION_LOG_RETENTION_DAYS = env.int("NOTIFICATION_LOG_RETENTION_DAYS", default=90)
NOTIFICATION_LOG_FAILED_RETENTION_DAYS = env.int(
    "NOTIFICATION_LOG_FAILED_RETENTION_DAYS", default=180
)
NOTIFICATION_LOG_PURGE_BATCH = env.int("NOTIFICATION_LOG_PURGE_BATCH", default=5000)

# EmailDeliveryLog retention, per purpose. Rows hold an email address and the IP of
# whoever requested the message, so this is a privacy obligation before a disk one.
#
# Security-audit records (password reset, email change) are kept longest: they are
# the evidence someone reaches for after an account is compromised, which is rarely
# the week it happened. Signup OTP is support data with a shorter useful life; SMTP
# tests are operational noise.
#
# Unlike NotificationLog, nothing here is an idempotency key — deleting a row
# cannot cause anything to re-send.
EMAIL_DELIVERY_LOG_RETENTION_DAYS = {
    "PASSWORD_RESET": env.int("EMAIL_LOG_PASSWORD_RESET_RETENTION_DAYS", default=365),
    "EMAIL_CHANGE": env.int("EMAIL_LOG_EMAIL_CHANGE_RETENTION_DAYS", default=365),
    "SIGNUP_OTP": env.int("EMAIL_LOG_SIGNUP_RETENTION_DAYS", default=180),
    "SMTP_TEST": env.int("EMAIL_LOG_SMTP_TEST_RETENTION_DAYS", default=90),
}
EMAIL_DELIVERY_LOG_DEFAULT_RETENTION_DAYS = env.int(
    "EMAIL_LOG_DEFAULT_RETENTION_DAYS", default=180
)
EMAIL_DELIVERY_LOG_PURGE_BATCH = env.int("EMAIL_LOG_PURGE_BATCH", default=5000)

# Contacts & Contact Import
# Primary market, used to parse phone numbers that are not already in international
# form (ISO-3166 alpha-2). Numbers written as "+..." ignore this and parse on their own.
# Override per environment if a deployment serves a different country.
CONTACT_DEFAULT_PHONE_REGION = env.str("CONTACT_DEFAULT_PHONE_REGION", default="IN")
CONTACT_IMPORT_MAX_ROWS = env.int("CONTACT_IMPORT_MAX_ROWS", default=50_000)
CONTACT_IMPORT_MAX_FILE_BYTES = env.int(
    "CONTACT_IMPORT_MAX_FILE_BYTES", default=10 * 1024 * 1024
)
CONTACT_IMPORT_SESSION_TTL_HOURS = env.int(
    "CONTACT_IMPORT_SESSION_TTL_HOURS", default=24
)
CONTACT_IMPORT_MAX_OPEN_SESSIONS = env.int("CONTACT_IMPORT_MAX_OPEN_SESSIONS", default=3)
CONTACT_IMPORT_COMMIT_BATCH_SIZE = env.int(
    "CONTACT_IMPORT_COMMIT_BATCH_SIZE", default=1000
)
CONTACT_MAX_TAGS_PER_CONTACT = env.int("CONTACT_MAX_TAGS_PER_CONTACT", default=25)

# How a presigned upload ticket is issued.
#   POST — browser form upload. The per-flow size ceiling is signed into the policy as
#          a `content-length-range` condition, so the object store rejects an oversize
#          body *during* the upload. This is the only way the limit is actually a limit.
#   PUT  — single presigned PUT. Simpler for the client but unbounded: a URL issued for
#          a 1 MB file will accept a 500 MB one, and confirm-upload only discovers that
#          after the bytes have been transferred and stored.
#
# Defaults to PUT because switching is a BREAKING CHANGE for the frontend — POST needs
# multipart form data with the returned fields, file part last — so backend and client
# have to ship together. Verify the object store supports POST Object first: check
# Cloudflare R2 against its current S3 compatibility list before flipping. Note that
# Azure Blob has no POST-policy equivalent at all, so this option goes away with the
# migration — see docs/implementation-plan-scratch/azure-blob-migration-plan.md §2.
# `manage.py check` warns while this is PUT.
UPLOAD_PRESIGN_METHOD = env.str("UPLOAD_PRESIGN_METHOD", default="PUT")

# How long an upload ticket stays valid. Must stay well under PENDING_UPLOAD_TTL_HOURS
# below, or the sweep collects uploads that are still in flight — core.checks enforces
# that relationship at deploy time.
UPLOAD_URL_TTL_MINUTES = env.int("UPLOAD_URL_TTL_MINUTES", default=15)

# Check an uploaded object's leading bytes against the extension it claims, at confirm
# time. Costs one ranged GET of 64 bytes per confirm.
#
# Both the declared content_type and the filename's extension are client strings; the
# allowlists check the string, never the file. A .png holding a PDF passes both today.
#
# Only a POSITIVE contradiction rejects — a file we cannot identify, or one whose
# extension has no signature at all (.txt, .csv), is allowed through. Being unable to
# recognise a file is not evidence against it, and the alternative rejects legitimate
# uploads for no gain. This is not malware scanning and does not replace it.
UPLOAD_VERIFY_CONTENT = env.bool("UPLOAD_VERIFY_CONTENT", default=True)

# Presigned upload hygiene
# How long a reserved-but-unconfirmed upload row is kept before the nightly sweep
# deletes it AND the object it points at. This is the only thing that bounds bucket
# growth from uploads that succeeded but were never confirmed — a closed tab is
# enough to produce one, and nothing in the product ever surfaces those bytes.
#
# Must comfortably exceed the presigned URL lifetime (15 minutes) plus however long a
# slow client might take, or the sweep deletes an upload that is still in flight.
PENDING_UPLOAD_TTL_HOURS = env.int("PENDING_UPLOAD_TTL_HOURS", default=24)

# Product & Service Catalog
# All serializer-level ceilings. The category depth cap is deliberately NOT here:
# it is enforced by a CheckConstraint, and a constraint built from a runtime setting
# is frozen into the migration and then silently diverges from it. That bound lives
# in core.models.catalog_category.MAX_CATEGORY_DEPTH.
CATALOG_DEFAULT_CURRENCY = env.str("CATALOG_DEFAULT_CURRENCY", default="INR")
CATALOG_MAX_VARIANTS_PER_ITEM = env.int("CATALOG_MAX_VARIANTS_PER_ITEM", default=100)
CATALOG_MAX_TAGS_PER_ITEM = env.int("CATALOG_MAX_TAGS_PER_ITEM", default=25)
CATALOG_MAX_ALIASES_PER_ITEM = env.int("CATALOG_MAX_ALIASES_PER_ITEM", default=25)
CATALOG_MAX_ATTRIBUTE_BYTES = env.int("CATALOG_MAX_ATTRIBUTE_BYTES", default=8192)
CATALOG_MAX_MEDIA_PER_ITEM = env.int("CATALOG_MAX_MEDIA_PER_ITEM", default=10)
CATALOG_MAX_ADD_ONS_PER_ITEM = env.int("CATALOG_MAX_ADD_ONS_PER_ITEM", default=50)
# Checked before a presigned PUT is issued, so an oversize file never reaches the
# bucket. Separate from DATA_UPLOAD_MAX_MEMORY_SIZE: that bounds what passes through
# Django, and these bytes never do.
CATALOG_MAX_MEDIA_BYTES = env.int("CATALOG_MAX_MEDIA_BYTES", default=10 * 1024 * 1024)

# Campaigns
# MAX_SELECTED_CONTACTS bounds the request body on create/update; MAX_MEMBERS bounds
# what materialisation will build. They are separate ceilings because a future
# filter-based audience resolves server-side and never passes through a request body.
CAMPAIGN_MAX_SELECTED_CONTACTS = env.int("CAMPAIGN_MAX_SELECTED_CONTACTS", default=5_000)
CAMPAIGN_MAX_MEMBERS = env.int("CAMPAIGN_MAX_MEMBERS", default=100_000)
CAMPAIGN_MATERIALIZE_BATCH_SIZE = env.int("CAMPAIGN_MATERIALIZE_BATCH_SIZE", default=1000)
CAMPAIGN_MAX_CONCURRENT_RUNNING = env.int("CAMPAIGN_MAX_CONCURRENT_RUNNING", default=5)

# Availability materialization
# How many days of UTC availability windows to keep materialized ahead of today.
# A tenant whose event templates advertise a longer booking horizon raises this
# automatically (see availability_materializer.horizon_days_for), capped at
# EventTemplate.MAX_HORIZON_DAYS. Beyond the horizon the engine offers no slots,
# so the daily beat task below is not optional.
AVAILABILITY_MATERIALIZE_DAYS = env.int("AVAILABILITY_MATERIALIZE_DAYS", default=90)

# Celery Beat schedule for the scheduling module. Run with:
#   celery -A config beat -l info
CELERY_BEAT_SCHEDULE = {
    "scheduling-materialize-availability": {
        "task": "core.tasks.materialize_availability_horizon",
        "schedule": 86400.0,  # daily
    },
    "scheduling-send-due-reminders": {
        "task": "core.tasks.send_due_reminders",
        "schedule": 300.0,  # every 5 minutes
    },
    "scheduling-purge-notification-logs": {
        "task": "core.tasks.purge_notification_logs",
        "schedule": 86400.0,  # daily
    },
    "purge-email-delivery-logs": {
        "task": "user.tasks.purge_email_delivery_logs",
        "schedule": 86400.0,  # daily
    },
    "purge-expired-sessions": {
        "task": "user.tasks.purge_expired_sessions",
        "schedule": 86400.0,  # daily
    },
    "scheduling-cleanup-expired-holds": {
        "task": "core.tasks.cleanup_expired_holds",
        "schedule": 60.0,  # every minute
    },
    "scheduling-refresh-calendar-tokens": {
        "task": "core.tasks.refresh_calendar_tokens",
        "schedule": 900.0,  # every 15 minutes
    },
    "scheduling-renew-calendar-watches": {
        "task": "core.tasks.renew_calendar_watches",
        "schedule": 3600.0,  # hourly
    },
    "contacts-cleanup-expired-imports": {
        "task": "core.tasks.cleanup_expired_contact_imports",
        "schedule": 600.0,  # every 10 minutes
    },
    "uploads-cleanup-pending": {
        "task": "core.tasks.cleanup_pending_uploads",
        "schedule": 86400.0,  # daily
    },
    "device-tokens-sweep": {
        "task": "core.tasks.sweep_device_tokens",
        "schedule": 86400.0,  # daily
    },
    # 60s, not the 300s used for reminders: "starts automatically at the selected time"
    # is a user-facing promise, and a five-minute lag reads as broken.
    "campaigns-start-due": {
        "task": "core.tasks.start_due_campaigns",
        "schedule": 60.0,  # every minute
    },

    # --- Billing -----------------------------------------------------------
    # Hourly rather than daily: end_date is a moment, not a date, so a daily sweep
    # would let a lapsed subscription keep working for up to 24 hours — and delay a
    # scheduled downgrade by the same amount.
    "billing-process-cycle": {
        "task": "onboard.tasks.process_billing_cycle",
        "schedule": 3600.0,  # hourly
    },
    "billing-expire-stale-orders": {
        "task": "onboard.tasks.expire_stale_billing_orders",
        "schedule": 3600.0,  # hourly
    },
    "billing-renewal-reminders": {
        "task": "onboard.tasks.send_renewal_reminders",
        "schedule": 86400.0,  # daily
    },
    "wallet-low-balance-alerts": {
        "task": "wallet.tasks.send_low_balance_alerts",
        "schedule": 86400.0,  # daily
    },
    "wallet-report-unrated-calls": {
        "task": "wallet.tasks.report_unrated_calls",
        "schedule": 86400.0,  # daily
    },
    # Weekly: drift is rare and the check is a full scan. Reports only — it never
    # repairs, because auto-correcting would hide the bug that caused the drift.
    "wallet-reconcile": {
        "task": "wallet.tasks.reconcile_wallets",
        "schedule": 604800.0,  # weekly
    },
}

# Calendar OAuth token encryption (Fernet). Generate: Fernet.generate_key().decode()
SCHEDULING_TOKEN_ENCRYPTION_KEY = env.str("SCHEDULING_TOKEN_ENCRYPTION_KEY", default="")

# Google Calendar OAuth (Phase 3)
GOOGLE_CALENDAR_CLIENT_ID = env.str("GOOGLE_CALENDAR_CLIENT_ID", default="")
GOOGLE_CALENDAR_CLIENT_SECRET = env.str("GOOGLE_CALENDAR_CLIENT_SECRET", default="")
GOOGLE_CALENDAR_REDIRECT_URI = env.str("GOOGLE_CALENDAR_REDIRECT_URI", default="")
# Public HTTPS URL Google pushes calendar-change notifications to (watch channel
# address). Leave blank to disable webhooks and rely on on-demand free/busy.
GOOGLE_CALENDAR_WEBHOOK_URL = env.str("GOOGLE_CALENDAR_WEBHOOK_URL", default="")
GOOGLE_CALENDAR_SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/calendar.calendars.readonly",
]

EXTERNAL_FREEBUSY_CACHE_SECONDS = env.int("EXTERNAL_FREEBUSY_CACHE_SECONDS", default=60)

# Firebase Cloud Messaging (push notifications). Path to the service-account JSON.
# Leave blank to disable push (sends become no-ops).
FIREBASE_CREDENTIALS = env.str("FIREBASE_CREDENTIALS", default="")

# --- Device token lifecycle ---------------------------------------------------
# An FCM token has no expiry and cannot be validated offline, so reachability is
# inferred from how recently the client re-registered it. See
# core.services.device_tokens and docs/flows/push-notifications.md.

# Stop sending to a token not re-registered within this window (Google's own
# staleness guidance is ~2 months; FCM itself reaps app instances at ~270 days).
DEVICE_TOKEN_STALE_DAYS = env.int("DEVICE_TOKEN_STALE_DAYS", default=60)
# How long a dead row is kept before deletion, measured from last contact.
DEVICE_TOKEN_RETENTION_DAYS = env.int("DEVICE_TOKEN_RETENTION_DAYS", default=90)
# Ceiling on live registrations per user per tenant; the oldest are retired.
DEVICE_TOKEN_MAX_PER_USER = env.int("DEVICE_TOKEN_MAX_PER_USER", default=20)
# Consecutive INVALID_ARGUMENT failures before a token is retired. Not 1 — that
# error can mean a malformed message rather than a malformed token.
DEVICE_TOKEN_FAILURE_STRIKES = env.int("DEVICE_TOKEN_FAILURE_STRIKES", default=3)
DEVICE_TOKEN_PURGE_BATCH = env.int("DEVICE_TOKEN_PURGE_BATCH", default=5000)
# How long FCM should keep retrying before dropping a push. A stale operational
# alert is worse than none, so this is deliberately finite.
PUSH_TTL_SECONDS = env.int("PUSH_TTL_SECONDS", default=86400)

import os
from pathlib import Path
import environ
from urllib.parse import urlparse
from corsheaders.defaults import default_headers

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Initialize environ
env = environ.Env()
# 1. Read shared environment variables first
shared_env_path = os.path.join(BASE_DIR, ".env.shared")
if os.path.exists(shared_env_path):
    environ.Env.read_env(shared_env_path)
# 2. Read environment-specific overrides (defaulting to .env.django.local)
env_file = os.environ.get("ENV_FILE", ".env.django.local")
env_path = os.path.join(BASE_DIR, env_file)
if os.path.exists(env_path):
    environ.Env.read_env(env_path)


DEBUG = env.bool('DJANGO_DEBUG', default=False)
SECRET_KEY = env('DJANGO_SECRET_KEY')


ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=[])

BACKEND_PROJECT_URL = env('BACKEND_PROJECT_URL')
be_parsed=urlparse(BACKEND_PROJECT_URL)
if be_parsed.hostname not in ALLOWED_HOSTS:
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
    
    # OTP & Two-Factor Authentication
    "django_otp",
    "django_otp.plugins.otp_static",
    "django_otp.plugins.otp_totp",
    "two_factor",
    
    # Local custom apps
    "user",
    "tenant",
    "onboard",
    "core",
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
    "corsheaders.middleware.CorsMiddleware",
    "config.middleware.RequestTracingMiddleware",  # Dynamic Request ID tracking
    "config.middleware.ReCaptchaMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django_otp.middleware.OTPMiddleware",
    "defender.middleware.FailedLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
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
        "ENGINE": "django.db.backends.postgresql",
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# DRF Configurations
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
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
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# OpenAPI Settings
SPECTACULAR_SETTINGS = {
    "TITLE": "Voicebot Backend API",
    "DESCRIPTION": "Enterprise production REST API for the Voicebot platform.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
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
        "BACKEND": "django_redis.cache.RedisCache",
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

# Razorpay Credentials
RAZORPAY_KEY_ID = env.str("RAZORPAY_KEY_ID", "rzp_test_dummykey")
RAZORPAY_KEY_SECRET = env.str("RAZORPAY_KEY_SECRET", "dummysecret")

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
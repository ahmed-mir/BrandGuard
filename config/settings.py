"""Environment-based Django settings for local development and Render."""
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

DJANGO_ENV = os.environ.get("DJANGO_ENV", "development").strip().lower()
if DJANGO_ENV not in {"development", "production"}:
    raise ImproperlyConfigured("DJANGO_ENV must be 'development' or 'production'.")
IS_RENDER = os.environ.get("RENDER", "").strip().lower() == "true"
IS_PRODUCTION = DJANGO_ENV == "production" or IS_RENDER


def _env_bool(name, default=False):
    value = os.environ.get(name, str(default)).strip().lower()
    if value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise ImproperlyConfigured(f"{name} must be a boolean value.")
    return value in {"1", "true", "yes", "on"}


SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured("Set DJANGO_SECRET_KEY in the environment before starting Django.")
if IS_PRODUCTION and (
    len(SECRET_KEY) < 50
    or SECRET_KEY != SECRET_KEY.strip()
    or len(set(SECRET_KEY)) < 16
):
    raise ImproperlyConfigured("Production requires a randomly generated DJANGO_SECRET_KEY of at least 50 characters.")

DEBUG = _env_bool("DJANGO_DEBUG", default=False)
if IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured("DJANGO_DEBUG must be false in production.")
if IS_PRODUCTION:
    DEBUG = False


def _csv_env(name, default=""):
    return [part.strip() for part in os.environ.get(name, default).split(",") if part.strip()]


_HOST_LABEL_PATTERN = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _valid_host_port(host, *, allow_port=True):
    if not host or "*" in host or "/" in host:
        return False
    hostname = host
    port = None
    if ":" in host:
        if not allow_port or host.count(":") != 1:
            return False
        hostname, port = host.rsplit(":", 1)
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            return False
    if len(hostname) > 253 or hostname.startswith(".") or hostname.endswith("."):
        return False
    return all(_HOST_LABEL_PATTERN.fullmatch(label) for label in hostname.split("."))


def parse_allowed_hosts(value):
    """Parse concrete host names only; production never accepts Django's '*' host."""
    hosts = [part.strip() for part in value.split(",") if part.strip()]
    if any(not _valid_host_port(host) for host in hosts):
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must contain comma-separated host names without schemes or wildcards.")
    return hosts


def parse_csrf_trusted_origins(value):
    """Accept exact HTTPS origins, never broad wildcard origins."""
    origins = [part.strip().rstrip("/") for part in value.split(",") if part.strip()]
    for origin in origins:
        parsed = urlparse(origin)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or not _valid_host_port(parsed.netloc)
        ):
            raise ImproperlyConfigured("DJANGO_CSRF_TRUSTED_ORIGINS must contain exact HTTPS origins without paths or wildcards.")
    return origins


_allowed_hosts_setting = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
ALLOWED_HOSTS = parse_allowed_hosts(_allowed_hosts_setting)
_csrf_origins_value = os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "")
CSRF_TRUSTED_ORIGINS = parse_csrf_trusted_origins(_csrf_origins_value)
if IS_PRODUCTION:
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("Set DJANGO_ALLOWED_HOSTS to the exact deployed host name(s).")
    if not CSRF_TRUSTED_ORIGINS:
        raise ImproperlyConfigured("Set DJANGO_CSRF_TRUSTED_ORIGINS to the exact HTTPS site origin(s).")


def _bounded_int(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc
    if value < minimum or value > maximum:
        raise ImproperlyConfigured(f"{name} must be between {minimum} and {maximum}.")
    return value


DB_CONNECT_TIMEOUT = _bounded_int("DB_CONNECT_TIMEOUT", "5", 1, 30)
DB_CONN_MAX_AGE = _bounded_int("DB_CONN_MAX_AGE", "60", 0, 600)


def database_from_url(database_url, *, production=False, connect_timeout=5, conn_max_age=60):
    """Parse the supported SQLite/PostgreSQL URL forms into Django settings."""
    try:
        parsed = urlparse(database_url)
        query = parse_qs(parsed.query)
        if parsed.scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
            database_name = unquote(parsed.path.lstrip("/"))
            if production and (not parsed.hostname or not database_name):
                raise ImproperlyConfigured("Production DATABASE_URL must include a PostgreSQL host and database name.")
            sslmode = os.environ.get("DB_SSLMODE") or query.get("sslmode", [None])[0]
            sslmode = sslmode or ("require" if production else "prefer")
            if production and sslmode not in {"require", "verify-ca", "verify-full"}:
                raise ImproperlyConfigured("Production PostgreSQL connections must require TLS (sslmode=require or stricter).")
            options = {
                "connect_timeout": connect_timeout,
                "sslmode": sslmode,
            }
            for key in ("target_session_attrs", "application_name"):
                if query.get(key):
                    options[key] = query[key][0]
            return {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": database_name,
                "USER": unquote(parsed.username or ""),
                "PASSWORD": unquote(parsed.password or ""),
                "HOST": parsed.hostname or "",
                "PORT": str(parsed.port or "5432"),
                "CONN_MAX_AGE": conn_max_age,
                "CONN_HEALTH_CHECKS": True,
                "OPTIONS": options,
            }

        if parsed.scheme == "sqlite":
            if production:
                raise ImproperlyConfigured("Production requires PostgreSQL via DATABASE_URL; SQLite is local-development only.")
            path = unquote(parsed.path)
            if path in {"", "/", "/:memory:"}:
                return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
            # urlparse leaves a leading slash before Windows drive letters.
            if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            return {"ENGINE": "django.db.backends.sqlite3", "NAME": path}
    except (ValueError, UnicodeError) as exc:
        raise ImproperlyConfigured("DATABASE_URL is invalid. Check the URL format without exposing its credentials.") from exc

    raise ImproperlyConfigured("DATABASE_URL must use sqlite locally or postgresql in production.")


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if IS_PRODUCTION and not DATABASE_URL:
    raise ImproperlyConfigured("Production requires DATABASE_URL pointing to PostgreSQL; SQLite fallback is disabled.")
DATABASES = {
    "default": database_from_url(
        DATABASE_URL,
        production=IS_PRODUCTION,
        connect_timeout=DB_CONNECT_TIMEOUT,
        conn_max_age=DB_CONN_MAX_AGE,
    )
    if DATABASE_URL
    else {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


def configured_limit(name, default, maximum):
    return _bounded_int(name, str(default), 1, maximum)


BRAND_DOCUMENT_MAX_UPLOAD_BYTES = configured_limit(
    "BRAND_DOCUMENT_MAX_UPLOAD_BYTES", 5 * 1024 * 1024, 5 * 1024 * 1024
)
BRAND_DOCUMENT_MAX_PDF_PAGES = configured_limit("BRAND_DOCUMENT_MAX_PDF_PAGES", 10, 10)
BRAND_DOCUMENT_MAX_EXTRACTED_CHARS = configured_limit(
    "BRAND_DOCUMENT_MAX_EXTRACTED_CHARS", 80_000, 80_000
)
PRIVATE_DOCUMENT_ROOT = Path(
    os.environ.get("PRIVATE_DOCUMENT_ROOT", BASE_DIR / "private_uploads")
).resolve()

DEFAULT_PRIVATE_DOCUMENT_BACKEND = "django.core.files.storage.FileSystemStorage"
S3_PRIVATE_DOCUMENT_BACKEND = "storages.backends.s3.S3Storage"
PRIVATE_DOCUMENT_STORAGE_BACKEND = os.environ.get(
    "PRIVATE_DOCUMENT_STORAGE_BACKEND", DEFAULT_PRIVATE_DOCUMENT_BACKEND
).strip()
PRIVATE_DOCUMENT_STORAGE_OPTIONS = {}
PRIVATE_DOCUMENT_IMPORTS_ENABLED = True
PRIVATE_DOCUMENT_IMPORTS_DISABLED_REASON = ""

if IS_PRODUCTION:
    bucket = os.environ.get("PRIVATE_DOCUMENT_BUCKET", "").strip()
    region = os.environ.get("PRIVATE_DOCUMENT_REGION", "").strip()
    endpoint_url = os.environ.get("PRIVATE_DOCUMENT_ENDPOINT_URL", "").strip()
    if not bucket:
        PRIVATE_DOCUMENT_STORAGE_BACKEND = "brand_sources.storage.DisabledPrivateDocumentStorage"
        PRIVATE_DOCUMENT_IMPORTS_ENABLED = False
        PRIVATE_DOCUMENT_IMPORTS_DISABLED_REASON = (
            "Private document imports are unavailable because persistent private object storage is not configured. "
            "Uploaded originals cannot be stored safely on Render's ephemeral filesystem. You can still create a "
            "source by entering its text manually."
        )
    elif not region:
        PRIVATE_DOCUMENT_STORAGE_BACKEND = "brand_sources.storage.DisabledPrivateDocumentStorage"
        PRIVATE_DOCUMENT_IMPORTS_ENABLED = False
        PRIVATE_DOCUMENT_IMPORTS_DISABLED_REASON = (
            "Private document imports are unavailable because the S3-compatible storage region is not configured. "
            "You can still create a source by entering its text manually."
        )
    elif PRIVATE_DOCUMENT_STORAGE_BACKEND not in {DEFAULT_PRIVATE_DOCUMENT_BACKEND, S3_PRIVATE_DOCUMENT_BACKEND}:
        PRIVATE_DOCUMENT_STORAGE_BACKEND = "brand_sources.storage.DisabledPrivateDocumentStorage"
        PRIVATE_DOCUMENT_IMPORTS_ENABLED = False
        PRIVATE_DOCUMENT_IMPORTS_DISABLED_REASON = (
            "Private document imports are disabled because this deployment does not use the configured S3-compatible "
            "private storage backend."
        )
    elif endpoint_url and not endpoint_url.lower().startswith("https://"):
        PRIVATE_DOCUMENT_STORAGE_BACKEND = "brand_sources.storage.DisabledPrivateDocumentStorage"
        PRIVATE_DOCUMENT_IMPORTS_ENABLED = False
        PRIVATE_DOCUMENT_IMPORTS_DISABLED_REASON = "Private document imports are disabled because the object-storage endpoint must use HTTPS."
    else:
        PRIVATE_DOCUMENT_STORAGE_BACKEND = S3_PRIVATE_DOCUMENT_BACKEND
        PRIVATE_DOCUMENT_STORAGE_OPTIONS = {
            "bucket_name": bucket,
            "region_name": region or None,
            "endpoint_url": endpoint_url or None,
            "default_acl": None,
            "querystring_auth": True,
            "file_overwrite": False,
            "location": "brand-source-imports",
        }
else:
    # Preserve the existing local/custom storage configuration convention.
    PRIVATE_DOCUMENT_STORAGE_OPTIONS = {}
    try:
        PRIVATE_DOCUMENT_STORAGE_OPTIONS = json.loads(
            os.environ.get("PRIVATE_DOCUMENT_STORAGE_OPTIONS", "{}")
        )
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured("PRIVATE_DOCUMENT_STORAGE_OPTIONS must be a JSON object.") from exc
    if not isinstance(PRIVATE_DOCUMENT_STORAGE_OPTIONS, dict):
        raise ImproperlyConfigured("PRIVATE_DOCUMENT_STORAGE_OPTIONS must be a JSON object.")
    if PRIVATE_DOCUMENT_STORAGE_BACKEND == DEFAULT_PRIVATE_DOCUMENT_BACKEND:
        PRIVATE_DOCUMENT_STORAGE_OPTIONS.setdefault("location", str(PRIVATE_DOCUMENT_ROOT))
        PRIVATE_DOCUMENT_STORAGE_OPTIONS.setdefault("base_url", "/private-documents-not-served/")


INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "core.apps.CoreConfig",
    "accounts.apps.AccountsConfig",
    "workspaces.apps.WorkspacesConfig",
    "brand_sources.apps.BrandSourcesConfig",
    "content_reviews.apps.ContentReviewsConfig",
    "ai_analysis.apps.AIAnalysisConfig",
    "approvals.apps.ApprovalsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

AUTHENTICATION_BACKENDS = ["accounts.backends.UsernameOrEmailBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "app:home"
LOGOUT_REDIRECT_URL = "landing"
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# Optional, user-initiated advisory analysis. A key alone never enables calls.
BRANDGUARD_AI_ENABLED = _env_bool("BRANDGUARD_AI_ENABLED", False)
BRANDGUARD_AI_DEMO_MODE = _env_bool("BRANDGUARD_AI_DEMO_MODE", False)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
BRANDGUARD_AI_PROVIDER = os.environ.get("BRANDGUARD_AI_PROVIDER", "openai").strip().lower()
BRANDGUARD_AI_MODEL = os.environ.get("BRANDGUARD_AI_MODEL", "gpt-5.4-mini").strip()
BRANDGUARD_GROQ_MODEL = os.environ.get("BRANDGUARD_GROQ_MODEL", "openai/gpt-oss-20b").strip()
if not BRANDGUARD_AI_MODEL or len(BRANDGUARD_AI_MODEL) > 64:
    raise ImproperlyConfigured("BRANDGUARD_AI_MODEL must be a non-empty model identifier of at most 64 characters.")
if not BRANDGUARD_GROQ_MODEL or len(BRANDGUARD_GROQ_MODEL) > 64:
    raise ImproperlyConfigured("BRANDGUARD_GROQ_MODEL must be a non-empty model identifier of at most 64 characters.")
BRANDGUARD_AI_TIMEOUT_SECONDS = _bounded_int("BRANDGUARD_AI_TIMEOUT_SECONDS", "15", 3, 20)
BRANDGUARD_AI_MAX_OUTPUT_TOKENS = _bounded_int("BRANDGUARD_AI_MAX_OUTPUT_TOKENS", "1400", 100, 2000)
BRANDGUARD_AI_MAX_FINDINGS = _bounded_int("BRANDGUARD_AI_MAX_FINDINGS", "12", 1, 12)
BRANDGUARD_AI_MAX_CONTEXT_CHARS = _bounded_int("BRANDGUARD_AI_MAX_CONTEXT_CHARS", "10000", 1000, 12000)

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if IS_PRODUCTION
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
    "private_documents": {
        "BACKEND": PRIVATE_DOCUMENT_STORAGE_BACKEND,
        "OPTIONS": PRIVATE_DOCUMENT_STORAGE_OPTIONS,
    },
}
WHITENOISE_MANIFEST_STRICT = True
WHITENOISE_KEEP_ONLY_HASHED_FILES = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Security defaults apply in local development too; production requires HTTPS
# and exact host/origin allowlists. Render terminates HTTPS and sets
# X-Forwarded-Proto for requests that arrived via HTTPS.
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_SECURE = IS_PRODUCTION or not DEBUG
CSRF_COOKIE_SECURE = IS_PRODUCTION or not DEBUG
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if IS_RENDER else None
# Render's public edge redirects HTTP to HTTPS before forwarding to the app.
# Avoid a Django redirect here so Render's direct health probes remain healthy.
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = _bounded_int("DJANGO_HSTS_SECONDS", "0", 0, 63_072_000) if IS_PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("DJANGO_HSTS_INCLUDE_SUBDOMAINS", False) if IS_PRODUCTION else False
SECURE_HSTS_PRELOAD = _env_bool("DJANGO_HSTS_PRELOAD", False) if IS_PRODUCTION else False

# Render collects stderr from the web process into its private runtime logs.
# Keep exception class and stack frames for diagnosis while redacting exception
# messages, which may contain user supplied or database values.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "safe_console": {
            "()": "config.logging_utils.SafeTracebackFormatter",
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "stderr": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "formatter": "safe_console",
        },
    },
    "loggers": {
        "django.request": {"handlers": ["stderr"], "level": "ERROR", "propagate": False},
        "content_reviews.services": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
        "content_reviews.views": {"handlers": ["stderr"], "level": "ERROR", "propagate": False},
        "ai_analysis.services": {"handlers": ["stderr"], "level": "WARNING", "propagate": False},
    },
}

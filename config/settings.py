"""Environment-based Django settings for the Phase 0 foundation."""
import os
import json
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    raise ImproperlyConfigured(
        "Set DJANGO_SECRET_KEY in the environment before starting Django."
    )

DEBUG = os.environ.get("DJANGO_DEBUG", "false").strip().lower() in {
    "1", "true", "yes"
}
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1"
    ).split(",")
    if host.strip()
]

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
    "approvals.apps.ApprovalsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
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


def database_from_url(database_url):
    """Support SQLite and PostgreSQL URLs without an extra settings package."""
    parsed = urlparse(database_url)

    if parsed.scheme in {"postgres", "postgresql", "postgresql+psycopg"}:
        return {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(parsed.path.lstrip("/")),
            "USER": unquote(parsed.username or ""),
            "PASSWORD": unquote(parsed.password or ""),
            "HOST": parsed.hostname or "",
            "PORT": str(parsed.port or "5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"sslmode": os.environ.get("DB_SSLMODE", "prefer")},
        }

    if parsed.scheme == "sqlite":
        path = unquote(parsed.path)
        if path in {"", "/", "/:memory:"}:
            return {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}
        # urlparse leaves a leading slash before Windows drive letters.
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return {"ENGINE": "django.db.backends.sqlite3", "NAME": path}

    raise ValueError("DATABASE_URL must use sqlite or postgresql.")


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DATABASES = {
    "default": database_from_url(DATABASE_URL)
    if DATABASE_URL
    else {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}


def configured_limit(name, default, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"{name} must be an integer.") from exc
    if value < 1 or value > maximum:
        raise ImproperlyConfigured(f"{name} must be between 1 and {maximum}.")
    return value


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
PRIVATE_DOCUMENT_STORAGE_BACKEND = os.environ.get(
    "PRIVATE_DOCUMENT_STORAGE_BACKEND", "django.core.files.storage.FileSystemStorage"
).strip()
try:
    PRIVATE_DOCUMENT_STORAGE_OPTIONS = json.loads(
        os.environ.get("PRIVATE_DOCUMENT_STORAGE_OPTIONS", "{}")
    )
except json.JSONDecodeError as exc:
    raise ImproperlyConfigured("PRIVATE_DOCUMENT_STORAGE_OPTIONS must be a JSON object.") from exc
if not isinstance(PRIVATE_DOCUMENT_STORAGE_OPTIONS, dict):
    raise ImproperlyConfigured("PRIVATE_DOCUMENT_STORAGE_OPTIONS must be a JSON object.")
if PRIVATE_DOCUMENT_STORAGE_BACKEND == "django.core.files.storage.FileSystemStorage":
    PRIVATE_DOCUMENT_STORAGE_OPTIONS.setdefault("location", str(PRIVATE_DOCUMENT_ROOT))
    PRIVATE_DOCUMENT_STORAGE_OPTIONS.setdefault("base_url", "/private-documents-not-served/")

AUTHENTICATION_BACKENDS = ["accounts.backends.UsernameOrEmailBackend"]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "app:home"
LOGOUT_REDIRECT_URL = "landing"
SESSION_ENGINE = "django.contrib.sessions.backends.db"

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
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "private_documents": {
        "BACKEND": PRIVATE_DOCUMENT_STORAGE_BACKEND,
        "OPTIONS": PRIVATE_DOCUMENT_STORAGE_OPTIONS,
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Conservative defaults; a deployment should enforce HTTPS and set its host list.
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

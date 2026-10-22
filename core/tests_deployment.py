import os
import secrets
import subprocess
import sys
import logging
from pathlib import Path
from unittest import TestCase

from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, override_settings

from config.settings import database_from_url, parse_allowed_hosts, parse_csrf_trusted_origins


BASE_DIR = Path(__file__).resolve().parent.parent


class ProductionConfigurationTests(SimpleTestCase):
    def _settings_subprocess(self, overrides, *, remove=()):
        env = os.environ.copy()
        for key in remove:
            env.pop(key, None)
        env.update(overrides)
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings"],
            cwd=BASE_DIR,
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
        )
        return result

    def test_production_requires_a_strong_secret_without_echoing_values(self):
        result = self._settings_subprocess(
            {"DJANGO_ENV": "production", "DJANGO_DEBUG": "false"},
            remove=("DJANGO_SECRET_KEY",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY", result.stderr)
        self.assertNotIn("DATABASE_URL", result.stderr)

    def test_production_requires_database_url_instead_of_sqlite_fallback(self):
        result = self._settings_subprocess(
            {
                "DJANGO_ENV": "production",
                "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
                "DJANGO_DEBUG": "false",
                "DJANGO_ALLOWED_HOSTS": "brandguard.onrender.com",
                "DJANGO_CSRF_TRUSTED_ORIGINS": "https://brandguard.onrender.com",
            },
            remove=("DATABASE_URL",),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires DATABASE_URL", result.stderr)
        self.assertIn("SQLite fallback is disabled", result.stderr)

    def test_production_configuration_is_secure_and_disables_unbacked_uploads(self):
        env = os.environ.copy()
        env.update({
            "DJANGO_ENV": "production",
            "RENDER": "true",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": "brandguard.onrender.com",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://brandguard.onrender.com",
            "DATABASE_URL": "postgresql://service:secret@db.internal:5432/brandguard",
        })
        for key in ("PRIVATE_DOCUMENT_BUCKET", "PRIVATE_DOCUMENT_REGION", "PRIVATE_DOCUMENT_ENDPOINT_URL"):
            env.pop(key, None)
        script = (
            "import json, config.settings as s; "
            "print(json.dumps({"
            "'debug':s.DEBUG,'engine':s.DATABASES['default']['ENGINE'],"
            "'hosts':s.ALLOWED_HOSTS,'csrf':s.CSRF_TRUSTED_ORIGINS,"
            "'session_secure':s.SESSION_COOKIE_SECURE,'csrf_secure':s.CSRF_COOKIE_SECURE,"
            "'session_httponly':s.SESSION_COOKIE_HTTPONLY,'csrf_middleware':any('csrf.CsrfViewMiddleware' in m for m in s.MIDDLEWARE),"
            "'proxy_header':s.SECURE_PROXY_SSL_HEADER,'sslmode':s.DATABASES['default']['OPTIONS']['sslmode'],"
            "'connect_timeout':s.DATABASES['default']['OPTIONS']['connect_timeout'],"
            "'conn_max_age':s.DATABASES['default']['CONN_MAX_AGE'],"
            "'static_root':str(s.STATIC_ROOT),'static_backend':s.STORAGES['staticfiles']['BACKEND'],"
            "'import_enabled':s.PRIVATE_DOCUMENT_IMPORTS_ENABLED,"
            "'private_backend':s.STORAGES['private_documents']['BACKEND']"
            "}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=BASE_DIR, env=env,
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("secret", result.stdout.lower())
        import json
        config = json.loads(result.stdout)
        self.assertFalse(config["debug"])
        self.assertEqual(config["engine"], "django.db.backends.postgresql")
        self.assertEqual(config["hosts"], ["brandguard.onrender.com"])
        self.assertEqual(config["csrf"], ["https://brandguard.onrender.com"])
        self.assertTrue(config["session_secure"])
        self.assertTrue(config["csrf_secure"])
        self.assertTrue(config["session_httponly"])
        self.assertTrue(config["csrf_middleware"])
        self.assertEqual(config["proxy_header"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertEqual(config["sslmode"], "require")
        self.assertEqual(config["connect_timeout"], 5)
        self.assertEqual(config["conn_max_age"], 60)
        self.assertTrue(config["static_root"].endswith("staticfiles"))
        self.assertEqual(config["static_backend"], "whitenoise.storage.CompressedManifestStaticFilesStorage")
        self.assertFalse(config["import_enabled"])
        self.assertEqual(config["private_backend"], "brand_sources.storage.DisabledPrivateDocumentStorage")

    def test_forwarded_proto_is_not_trusted_for_non_render_production(self):
        env = os.environ.copy()
        env.pop("RENDER", None)
        env.update({
            "DJANGO_ENV": "production",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": "brandguard.example.invalid",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://brandguard.example.invalid",
            "DATABASE_URL": "postgresql://service:placeholder@db.example.invalid:5432/brandguard",
        })
        result = subprocess.run(
            [sys.executable, "-c", "import config.settings as s; print(s.SECURE_PROXY_SSL_HEADER)"],
            cwd=BASE_DIR, env=env, text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "None")

    def test_review_exception_logs_keep_trace_frames_but_redact_exception_values(self):
        from config.logging_utils import SafeTracebackFormatter

        private_value = "private marketing copy and connection password"
        try:
            raise RuntimeError(private_value)
        except RuntimeError:
            record = logging.LogRecord(
                "content_reviews.services", logging.ERROR, __file__, 1,
                "Review execution failed (error_type=RuntimeError).", (), sys.exc_info(),
            )
        formatted = SafeTracebackFormatter("%(levelname)s %(message)s").format(record)
        self.assertIn("RuntimeError: [exception message redacted]", formatted)
        self.assertIn("Traceback (most recent call last)", formatted)
        self.assertNotIn(private_value, formatted)

    def test_private_s3_storage_can_be_configured_without_network_credentials(self):
        env = os.environ.copy()
        env.update({
            "DJANGO_ENV": "production",
            "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": "brandguard.onrender.com",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://brandguard.onrender.com",
            "DATABASE_URL": "postgresql://service:placeholder@db.internal:5432/brandguard",
            "PRIVATE_DOCUMENT_STORAGE_BACKEND": "storages.backends.s3.S3Storage",
            "PRIVATE_DOCUMENT_BUCKET": "brandguard-private-test",
            "PRIVATE_DOCUMENT_REGION": "us-east-1",
            "PRIVATE_DOCUMENT_ENDPOINT_URL": "https://objects.example.invalid",
        })
        script = (
            "import json, config.settings as s; from django.core.files.storage import storages; "
            "storage=storages['private_documents']; "
            "print(json.dumps({'enabled':s.PRIVATE_DOCUMENT_IMPORTS_ENABLED,"
            "'backend':s.STORAGES['private_documents']['BACKEND'],"
            "'class':type(storage).__name__,'acl':storage.default_acl,"
            "'signed_urls':storage.querystring_auth,'bucket':storage.bucket_name,"
            "'endpoint':storage.endpoint_url}))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=BASE_DIR, env=env,
            text=True, capture_output=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        config = json.loads(result.stdout)
        self.assertTrue(config["enabled"])
        self.assertEqual(config["backend"], "storages.backends.s3.S3Storage")
        self.assertEqual(config["class"], "S3Storage")
        self.assertIsNone(config["acl"])
        self.assertTrue(config["signed_urls"])
        self.assertEqual(config["bucket"], "brandguard-private-test")
        self.assertEqual(config["endpoint"], "https://objects.example.invalid")

    def test_production_rejects_sqlite_database_url(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "requires PostgreSQL"):
            database_from_url("sqlite:///./production.sqlite3", production=True)

    def test_postgres_url_uses_timeout_connection_reuse_and_url_tls_setting(self):
        database = database_from_url(
            "postgresql://user:encoded%40secret@db.example:5433/brandguard?sslmode=require",
            production=True,
            connect_timeout=7,
            conn_max_age=45,
        )
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["PASSWORD"], "encoded@secret")
        self.assertEqual(database["OPTIONS"], {"connect_timeout": 7, "sslmode": "require"})
        self.assertEqual(database["CONN_MAX_AGE"], 45)
        self.assertTrue(database["CONN_HEALTH_CHECKS"])

    def test_production_rejects_postgres_tls_disabled(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "must require TLS"):
            database_from_url("postgresql://user:password@db.example/brandguard?sslmode=disable", production=True)

    def test_host_and_csrf_parsers_reject_wildcards_and_insecure_origins(self):
        with self.assertRaises(ImproperlyConfigured):
            parse_allowed_hosts("*")
        with self.assertRaises(ImproperlyConfigured):
            parse_allowed_hosts("https://brandguard.onrender.com")
        with self.assertRaises(ImproperlyConfigured):
            parse_csrf_trusted_origins("http://brandguard.onrender.com")
        self.assertEqual(parse_csrf_trusted_origins("https://brandguard.onrender.com"), ["https://brandguard.onrender.com"])

    @override_settings(DEBUG=False)
    def test_static_css_and_public_health_route_are_available_in_production_mode(self):
        from django.test import Client

        response = Client().get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class DemoBootstrapSafetyTests(TestCase):
    @override_settings(IS_PRODUCTION=True, DEBUG=False)
    def test_demo_bootstrap_requires_cli_and_environment_opt_ins(self):
        from unittest.mock import patch

        with patch.dict(os.environ, {"BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP": "YES"}):
            with self.assertRaisesRegex(CommandError, "Pass --confirm-production-demo-bootstrap"):
                call_command("bootstrap_demo", stdout=None)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP", None)
            with self.assertRaisesRegex(CommandError, "second explicit opt-in"):
                call_command("bootstrap_demo", confirm_production_demo_bootstrap=True, stdout=None)

    @override_settings(IS_PRODUCTION=True, DEBUG=False)
    def test_demo_bootstrap_refuses_sqlite_even_after_both_opt_ins(self):
        from unittest.mock import patch

        self.assertEqual(connection.vendor, "sqlite")
        with patch.dict(os.environ, {"BRANDGUARD_ALLOW_PRODUCTION_DEMO_BOOTSTRAP": "YES"}):
            with self.assertRaisesRegex(CommandError, "requires a PostgreSQL target"):
                call_command("bootstrap_demo", confirm_production_demo_bootstrap=True, stdout=None)

"""The scaffold: health, errors, headers, size limit, the Postgres rule."""

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.checks import Error
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.test import APIClient

from config import checks
from config.api.exceptions import exception_handler
from notes.tests.helpers import make_user

BASE_DIR = Path(settings.BASE_DIR)


class HealthTests(TestCase):
    def test_health_is_public_and_reports_ok(self):
        response = APIClient().get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})


class PgvectorTests(TestCase):
    def test_vector_extension_is_installed_by_migration(self):
        with connection.cursor() as cursor:
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cursor.fetchone()
        self.assertIsNotNone(row, "migrate should have created the vector extension")


class ErrorShapeTests(TestCase):
    def test_api_error_response_carries_detail_and_code(self):
        response = APIClient().post("/api/v1/health/")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["code"], "method_not_allowed")
        self.assertIn("detail", response.json())

    def test_drf_error_gains_code(self):
        response = exception_handler(NotFound(), {})
        self.assertEqual(response.data["code"], "not_found")
        self.assertIn("detail", response.data)

    def test_field_errors_keep_their_shape_and_gain_invalid(self):
        response = exception_handler(ValidationError({"title": ["Too long."]}), {})
        self.assertEqual(response.data["title"], ["Too long."])
        self.assertEqual(response.data["code"], "invalid")

    def test_bare_list_error_is_wrapped(self):
        response = exception_handler(ValidationError(["Nope."]), {})
        self.assertEqual(response.data["code"], "invalid")
        self.assertEqual(response.data["detail"], ["Nope."])

    def test_unknown_exception_is_left_to_django(self):
        self.assertIsNone(exception_handler(RuntimeError("boom"), {}))


class RequestIDTests(TestCase):
    def test_generated_when_absent(self):
        response = APIClient().get("/api/v1/health/")
        self.assertTrue(response["X-Request-ID"])

    def test_inbound_id_is_honoured_and_truncated(self):
        response = APIClient().get("/api/v1/health/", HTTP_X_REQUEST_ID="a" * 200)
        self.assertEqual(response["X-Request-ID"], "a" * 64)


class ContentSecurityPolicyTests(TestCase):
    def test_default_policy_is_strict(self):
        policy = APIClient().get("/api/v1/health/")["Content-Security-Policy"]
        self.assertIn("script-src 'self';", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_swagger_ui_gets_its_own_policy(self):
        response = APIClient().get("/api/v1/docs/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("cdn.jsdelivr.net", response["Content-Security-Policy"])

    def test_schema_is_served(self):
        response = APIClient().get("/api/v1/schema/")
        self.assertEqual(response.status_code, 200)


class MaxUploadSizeTests(TestCase):
    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10)
    def test_oversized_body_is_refused_before_the_view(self):
        response = APIClient().post(
            "/api/v1/health/", data="x" * 50, content_type="application/json"
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "too_large")

    @override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10)
    def test_malformed_length_is_ignored(self):
        response = APIClient().get("/api/v1/health/", CONTENT_LENGTH="abc")
        self.assertEqual(response.status_code, 200)


@override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=10, CORS_ALLOWED_ORIGINS=["http://app.test"])
class TooLargeCorsTests(TestCase):
    """A cross-origin browser must be able to read the 413 (D80)."""

    def post(self, path="/api/v1/notes/", origin="http://app.test"):
        extra = {"HTTP_ORIGIN": origin} if origin else {}
        return APIClient().post(path, data="x" * 50, content_type="application/json", **extra)

    def test_allowed_origin_can_read_it(self):
        response = self.post()
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response["Access-Control-Allow-Origin"], "http://app.test")
        self.assertIn("X-Request-ID", response["Access-Control-Expose-Headers"])
        self.assertIn("Origin", response["Vary"])

    def test_other_origin_gets_no_cors_header(self):
        response = self.post(origin="http://evil.test")
        self.assertEqual(response.status_code, 413)
        self.assertNotIn("Access-Control-Allow-Origin", response)
        self.assertIn("Origin", response["Vary"])

    def test_no_origin_gets_no_cors_header(self):
        self.assertNotIn("Access-Control-Allow-Origin", self.post(origin=None))

    def test_path_outside_the_api_gets_none(self):
        self.assertNotIn("Access-Control-Allow-Origin", self.post(path="/admin/login/"))


class DeepJsonTests(TestCase):
    def test_absurdly_nested_json_is_a_400_not_a_500(self):
        user = make_user()
        client = APIClient()
        client.force_authenticate(user)
        # json.loads only hits RecursionError past ~20k levels on 3.12; 100k is
        # still ~200 KB, far under the size limit.
        body = "[" * 100_000 + "]" * 100_000
        response = client.post("/api/v1/notes/", data=body, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "parse_error")

    def test_ordinary_malformed_json_is_still_a_400(self):
        client = APIClient()
        client.force_authenticate(make_user())
        response = client.post("/api/v1/notes/", data="{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "parse_error")


class PostgresRequiredCheckTests(SimpleTestCase):
    def test_passes_on_postgres(self):
        self.assertEqual(checks.postgres_required(None), [])

    def test_fails_on_sqlite(self):
        sqlite = {"default": {"ENGINE": "django.db.backends.sqlite3"}}
        with mock.patch.object(checks.settings, "DATABASES", sqlite):
            errors = checks.postgres_required(None)
        self.assertEqual([e.id for e in errors], ["config.E001"])
        self.assertIsInstance(errors[0], Error)

    def test_per_process_cache_warns_on_deploy(self):
        self.assertEqual([w.id for w in checks.shared_cache_for_rate_limits(None)], ["config.W001"])

    REDIS = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache"}}

    @override_settings(CACHES=REDIS)
    def test_shared_cache_passes(self):
        self.assertEqual(checks.shared_cache_for_rate_limits(None), [])


class DeploySettingsTests(SimpleTestCase):
    """check --deploy, from a subprocess, in both directions (reference D3).

    Settings are read once at import, so the only honest way to test what a
    production environment gets is a fresh process with that environment.
    """

    def run_check(self, **env):
        base = {
            "PATH": os.environ.get("PATH", ""),
            "SECRET_KEY": "x" * 60 + "deploy-check-only-4f9c2a7e1b",
            "DATABASE_URL": os.environ.get("DATABASE_URL", settings_database_url()),
            "ALLOWED_HOSTS": "example.com",
            "CACHE_URL": "rediscache://127.0.0.1:6379/2",
        }
        base.update(env)
        return subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=BASE_DIR,
            env=base,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_production_config_passes(self):
        result = self.run_check(DEBUG="False")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_debug_config_fails(self):
        result = self.run_check(DEBUG="True")
        self.assertNotEqual(result.returncode, 0)


def settings_database_url():
    db = settings.DATABASES["default"]
    # The test database's name is rewritten; the check needs any Postgres URL.
    user = db.get("USER") or ""
    password = f":{db['PASSWORD']}" if db.get("PASSWORD") else ""
    return f"postgres://{user}{password}@{db.get('HOST') or 'localhost'}:{db.get('PORT') or 5432}/x"

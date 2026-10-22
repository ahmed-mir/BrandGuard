from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse
from django.test import override_settings

from config.settings import database_from_url


class PhaseZeroSmokeTests(SimpleTestCase):
    def test_landing_page_loads_and_discloses_preview_state(self):
        response = self.client.get(reverse("landing"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Clarity before")
        self.assertContains(response, "Illustrative preview")
        self.assertContains(response, "LumaDesk")
        self.assertContains(response, "/static/core/css/site.css")
        self.assertNotContains(response, "Start reviewing")

    def test_health_endpoint_returns_process_status(self):
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_postgres_url_maps_to_django_database_settings(self):
        database = database_from_url("postgresql://reviewer@db.example:5433/brandguard")
        self.assertEqual(database["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(database["NAME"], "brandguard")
        self.assertEqual(database["HOST"], "db.example")
        self.assertEqual(database["PORT"], "5433")

    @override_settings(DEBUG=False)
    def test_custom_not_found_template_is_used(self):
        response = self.client.get("/not-a-real-page/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "This page isn’t", status_code=404)

    def test_application_shell_is_reusable_without_a_dashboard_route(self):
        html = render_to_string("core/app_shell.html", {"page_title": "Future screen"})
        self.assertIn("app-shell", html)
        self.assertIn("Future screen", html)

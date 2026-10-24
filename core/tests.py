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
        self.assertContains(response, "Review marketing against approved brand rules")
        self.assertContains(response, "cited evidence and human sign-off")
        self.assertContains(response, "Illustrative preview")
        self.assertContains(response, "EVIDENCE-LED REVIEW")
        self.assertContains(response, "Illustrative content - no live review")
        self.assertContains(response, "LumaDesk")
        self.assertContains(response, "Our tool is called Luma Desk.")
        self.assertContains(response, "Do not write Luma Desk as two words.")
        self.assertContains(response, "TERM-001")
        self.assertContains(response, "A claim, checked against its source.")
        self.assertContains(response, "LumaDesk guarantees 100% faster task completion for every team.")
        self.assertContains(response, "CONTRADICTED")
        self.assertContains(response, "FACT-02 · Approved fact")
        self.assertContains(response, "Up to 20% faster task completion in our internal pilot; results vary.")
        self.assertContains(response, "Product fact sheet · version 1")
        self.assertContains(response, "not a live submission or review run")
        self.assertContains(response, "Try the BrandGuard demo")
        self.assertContains(response, 'href="/accounts/login/?next=/app/"')
        self.assertContains(response, "Sign-in required")
        self.assertContains(response, "See how it works")
        self.assertContains(response, "Demo AI is simulated")
        self.assertContains(response, "no external AI provider is contacted")
        self.assertContains(response, "/static/core/css/site.css")
        self.assertNotContains(response, "Start reviewing")
        self.assertNotContains(response, "PHASE 0 PREVIEW")
        self.assertNotContains(response, "Review engine not connected")

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

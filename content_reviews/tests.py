import uuid
import io
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from brand_sources.models import ApprovedFact, BrandRule, BrandSource
from brand_sources.demo import seed_lumadesk_source_pack
from brand_sources.services import approve_source_version, create_draft_version, create_source
from workspaces.models import Workspace, WorkspaceMembership

from .models import ContentSubmission, ContentVersion, ReviewCheck, ReviewFinding, ReviewRun
from .services import _locked_review_submission, create_submission, evaluate_rule, execute_review


User = get_user_model()


class PostgreSQLReviewLockSQLTests(SimpleTestCase):
    def test_review_start_lock_query_does_not_join_nullable_current_version(self):
        """Compile the production lock query with Django's PostgreSQL backend, no DB connection."""
        from django.db.backends.postgresql.base import DatabaseWrapper

        database = DatabaseWrapper(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "compile_only",
                "HOST": "localhost",
                "USER": "compile_only",
                "PASSWORD": "compile_only",
            },
            alias="postgresql_compile_only",
        )
        database.get_autocommit = lambda: False
        database.in_atomic_block = True
        queryset = _locked_review_submission(
            submission_id=uuid.uuid4(),
            workspace_id=1,
        )
        sql, _params = queryset.query.get_compiler(connection=database).as_sql()
        database.close()

        self.assertIn("FOR UPDATE", sql.upper())
        self.assertNotIn("LEFT OUTER JOIN", sql.upper())


class ReviewFixtureMixin:
    source_text = (
        "Official product name: LumaDesk. Do not write Luma Desk as two words. "
        'When a marketing claim mentions the "internal pilot", include the exact qualification "Results vary." '
        'FACT-02: "Up to 20% faster task completion in our internal pilot; results vary."'
    )
    fact_excerpt = "Up to 20% faster task completion in our internal pilot; results vary."

    def setUp(self):
        self.workspace = Workspace.objects.create(name="LumaDesk Demo", slug=f"lumadesk-{uuid.uuid4().hex[:8]}")
        self.creator = self.make_user("creator")
        self.reviewer = self.make_user("reviewer")
        self.manager = self.make_user("manager")
        self.admin = self.make_user("admin")
        self.other_creator = self.make_user("othercreator")
        for user, role in [
            (self.creator, WorkspaceMembership.Role.CREATOR),
            (self.reviewer, WorkspaceMembership.Role.REVIEWER),
            (self.manager, WorkspaceMembership.Role.BRAND_MANAGER),
            (self.admin, WorkspaceMembership.Role.ADMIN),
            (self.other_creator, WorkspaceMembership.Role.CREATOR),
        ]:
            WorkspaceMembership.objects.create(user=user, workspace=self.workspace, role=role)
        self.policy_version, self.rules = self.create_policy()

    def make_user(self, username):
        return User.objects.create_user(username=username, email=f"{username}@example.test", password="A-strong-test-password-123!")

    def create_policy(self, *, workspace=None):
        workspace = workspace or self.workspace
        source, version = create_source(
            workspace=workspace,
            owner=self.manager if workspace == self.workspace else self.creator,
            title="Approved LumaDesk terminology and facts",
            source_type=BrandSource.SourceType.GUIDELINES,
            description="Synthetic policy used by deterministic review tests.",
            source_text=self.source_text,
        )
        rules = []
        rules.append(BrandRule.objects.create(
            source_version=version,
            identifier="TERM-001",
            category=BrandRule.Category.TERMINOLOGY,
            severity=BrandRule.Severity.HIGH,
            description="Use the approved product name.",
            rule_type=BrandRule.RuleType.PROHIBITED_TERM,
            matcher_data={"type": "PROHIBITED_TERM", "term": "Luma Desk", "preferred": "LumaDesk", "case_sensitive": False, "scope": "ALL"},
            evidence_excerpt="Do not write Luma Desk as two words.",
        ))
        rules.append(BrandRule.objects.create(
            source_version=version,
            identifier="TERM-002",
            category=BrandRule.Category.TERMINOLOGY,
            severity=BrandRule.Severity.LOW,
            description="Include official product naming in email copy.",
            rule_type=BrandRule.RuleType.REQUIRED_TERM,
            matcher_data={"type": "REQUIRED_TERM", "term": "LumaDesk", "case_sensitive": True, "scope": "EMAIL"},
            evidence_excerpt="Official product name: LumaDesk.",
        ))
        rules.append(BrandRule.objects.create(
            source_version=version,
            identifier="DISC-001",
            category=BrandRule.Category.DISCLAIMER,
            severity=BrandRule.Severity.HIGH,
            description="Qualify claims that reference the internal pilot.",
            rule_type=BrandRule.RuleType.REQUIRED_DISCLAIMER,
            matcher_data={"type": "REQUIRED_DISCLAIMER", "phrase": "Results vary.", "scope": "ALL", "trigger_phrase": "internal pilot"},
            evidence_excerpt='When a marketing claim mentions the "internal pilot", include the exact qualification "Results vary."',
        ))
        fact = ApprovedFact.objects.create(
            source_version=version,
            identifier="FACT-02",
            subject="Task completion speed",
            statement=self.fact_excerpt,
            comparison_operator=ApprovedFact.ComparisonOperator.UP_TO,
            comparison_value="20",
            comparison_unit="%",
            evidence_excerpt=self.fact_excerpt,
        )
        rules.append(BrandRule.objects.create(
            source_version=version,
            approved_fact=fact,
            identifier="FACT-02",
            category=BrandRule.Category.FACT,
            severity=BrandRule.Severity.MEDIUM,
            description="Compare task completion speed with the approved pilot fact.",
            rule_type=BrandRule.RuleType.APPROVED_FACT,
            matcher_data={"type": "APPROVED_FACT", "fact_identifier": "FACT-02"},
            evidence_excerpt=self.fact_excerpt,
        ))
        approve_source_version(source_id=source.pk, version_number=version.version_number, workspace=workspace, actor=self.manager if workspace == self.workspace else self.creator)
        version.refresh_from_db()
        return version, rules

    def create_submission_for(self, creator=None, text="Luma Desk makes task completion faster.", channel=ContentSubmission.Channel.INSTAGRAM, **kwargs):
        return create_submission(
            workspace=self.workspace,
            creator=creator or self.creator,
            title=kwargs.pop("title", "Campaign copy"),
            channel=channel,
            audience=kwargs.pop("audience", "Operations teams"),
            campaign_purpose=kwargs.pop("campaign_purpose", "Product announcement"),
            content_text=text,
            **kwargs,
        )

    def run_review(self, text, *, creator=None, channel=ContentSubmission.Channel.INSTAGRAM, token=None):
        submission, version = self.create_submission_for(creator=creator, text=text, channel=channel)
        run = execute_review(
            submission_id=submission.pk,
            version_number=version.version_number,
            workspace=self.workspace,
            actor=creator or self.creator,
            idempotency_key=token or uuid.uuid4(),
        )
        return submission, version, run


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class DeterministicEngineTests(ReviewFixtureMixin, TestCase):
    def test_prohibited_term_is_real_finding_with_exact_offsets_and_evidence(self):
        text = "Meet Luma Desk today."
        _, version, run = self.run_review(text)
        finding = run.findings.get(rule__identifier="TERM-001")
        start = text.index("Luma Desk")
        self.assertEqual((finding.text_start, finding.text_end), (start, start + len("Luma Desk")))
        self.assertEqual(version.content_text[finding.text_start:finding.text_end], "Luma Desk")
        self.assertEqual(finding.source_version_id, self.policy_version.pk)
        self.assertEqual(finding.evidence_excerpt, self.rules[0].evidence_excerpt)
        self.assertEqual(finding.suggested_correction, "LumaDesk")
        self.assertEqual(finding.resolution_status, ReviewFinding.ResolutionStatus.OPEN)

    def test_term_match_uses_word_boundaries(self):
        _, _, run = self.run_review("Luma Deskbook keeps notes together.")
        self.assertFalse(run.findings.filter(rule__identifier="TERM-001").exists())

    def test_required_term_only_finds_missing_term_in_defined_channel_scope(self):
        _, _, instagram_run = self.run_review("Keep work moving.", channel=ContentSubmission.Channel.INSTAGRAM)
        self.assertEqual(instagram_run.checks.get(rule__identifier="TERM-002").outcome, ReviewCheck.Outcome.NOT_APPLICABLE)
        _, _, email_run = self.run_review("Keep work moving.", channel=ContentSubmission.Channel.EMAIL)
        self.assertEqual(email_run.checks.get(rule__identifier="TERM-002").outcome, ReviewCheck.Outcome.FINDING)

    def test_broad_required_term_without_trigger_does_not_create_a_finding(self):
        broad_source, broad_version = create_source(
            workspace=self.workspace,
            owner=self.manager,
            title="Broad required term",
            source_type=BrandSource.SourceType.GUIDELINES,
            description="No explicit context in this rule.",
            source_text="For product copy, use LumaDesk as the official name.",
        )
        rule = BrandRule.objects.create(
            source_version=broad_version,
            identifier="TERM-003",
            category=BrandRule.Category.TERMINOLOGY,
            severity=BrandRule.Severity.MEDIUM,
            description="Use the product name in relevant branded copy.",
            rule_type=BrandRule.RuleType.REQUIRED_TERM,
            matcher_data={"type": "REQUIRED_TERM", "term": "LumaDesk", "scope": "ALL"},
            evidence_excerpt="For product copy, use LumaDesk as the official name.",
        )
        approve_source_version(source_id=broad_source.pk, version_number=broad_version.version_number, workspace=self.workspace, actor=self.manager)
        _, _, run = self.run_review("A calm start for the day.")
        self.assertEqual(run.checks.get(rule=rule).outcome, ReviewCheck.Outcome.NOT_APPLICABLE)
        self.assertFalse(run.findings.filter(rule=rule).exists())

    def test_disclaimer_is_required_only_when_saved_trigger_occurs(self):
        _, _, unrelated = self.run_review("A calm start for the workday.")
        self.assertEqual(unrelated.checks.get(rule__identifier="DISC-001").outcome, ReviewCheck.Outcome.NOT_APPLICABLE)
        _, _, triggered = self.run_review("Up to 20% faster task completion in our internal pilot.")
        finding = triggered.findings.get(rule__identifier="DISC-001")
        self.assertEqual(finding.finding_type, ReviewFinding.FindingType.DISCLAIMER_MISSING)
        self.assertEqual(finding.suggested_correction, "Results vary.")

    def test_exact_approved_fact_statement_is_supported(self):
        _, _, run = self.run_review(self.fact_excerpt)
        self.assertEqual(run.checks.get(rule__identifier="FACT-02").outcome, ReviewCheck.Outcome.SUPPORTED)
        self.assertFalse(run.findings.filter(rule__identifier="FACT-02").exists())

    def test_excess_claim_for_same_metric_is_contradicted(self):
        _, _, run = self.run_review("Task completion was 100% faster in our internal pilot.")
        finding = run.findings.get(rule__identifier="FACT-02")
        self.assertEqual(finding.classification, ReviewFinding.Classification.CONTRADICTED)

    def test_non_comparable_fact_claim_is_not_verified_not_false(self):
        _, _, run = self.run_review("Task completion speed reached 14 tasks per shift.")
        finding = run.findings.get(rule__identifier="FACT-02")
        self.assertEqual(finding.classification, ReviewFinding.Classification.NOT_VERIFIED)
        self.assertEqual(run.checks.get(rule__identifier="FACT-02").outcome, ReviewCheck.Outcome.NOT_VERIFIED)

    def test_unrelated_productivity_claim_is_not_equated_with_task_completion_speed(self):
        _, _, run = self.run_review("Team productivity increased by 100% this year.")
        self.assertEqual(run.checks.get(rule__identifier="FACT-02").outcome, ReviewCheck.Outcome.NOT_APPLICABLE)

    def test_negated_fact_language_is_sent_to_human_review(self):
        _, _, run = self.run_review("Task completion was not 20% faster in our pilot.")
        finding = run.findings.get(rule__identifier="FACT-02")
        self.assertEqual(finding.classification, ReviewFinding.Classification.NEEDS_HUMAN_REVIEW)

    def test_run_pins_exact_policy_and_old_run_survives_policy_change(self):
        _, _, run = self.run_review("Luma Desk is here.")
        pinned_hash = run.policy_snapshot_hash
        _, draft = create_source(
            workspace=self.workspace,
            owner=self.manager,
            title="Updated naming policy",
            source_type=BrandSource.SourceType.GUIDELINES,
            description="A second policy source.",
            source_text="The official name remains LumaDesk.",
        )
        approve_source_version(source_id=draft.source_id, version_number=draft.version_number, workspace=self.workspace, actor=self.manager)
        run.refresh_from_db()
        self.workspace.refresh_from_db()
        self.assertNotEqual(self.workspace.policy_snapshot_hash, pinned_hash)
        self.assertEqual(run.pinned_policy_versions.filter(applied=True).count(), 1)
        self.assertEqual(run.findings.get().source_version_id, self.policy_version.pk)

    def test_draft_and_superseded_versions_are_not_used(self):
        draft_source, draft = create_source(
            workspace=self.workspace,
            owner=self.manager,
            title="Unapproved draft",
            source_type=BrandSource.SourceType.POLICY,
            description="Draft must not be evaluated.",
            source_text="Never write Luma Desk.",
        )
        rule = BrandRule.objects.create(
            source_version=draft,
            identifier="DRAFT-01",
            category=BrandRule.Category.TERMINOLOGY,
            severity=BrandRule.Severity.HIGH,
            description="Draft-only rule.",
            rule_type=BrandRule.RuleType.PROHIBITED_TERM,
            matcher_data={"type": "PROHIBITED_TERM", "term": "Never", "scope": "ALL"},
            evidence_excerpt="Never write Luma Desk.",
        )
        replacement, _ = create_draft_version(
            source=self.policy_version.source,
            workspace=self.workspace,
        )
        replacement.rules.filter(identifier="TERM-001").delete()
        approve_source_version(
            source_id=self.policy_version.source_id,
            version_number=replacement.version_number,
            workspace=self.workspace,
            actor=self.manager,
        )
        _, _, run = self.run_review("Never use Luma Desk.")
        self.assertFalse(run.checks.filter(rule=rule).exists())
        self.assertFalse(run.checks.filter(rule__identifier="TERM-001").exists())
        self.assertNotIn(str(draft.pk), [item["version_id"] for item in run.policy_snapshot["versions"]])
        self.assertNotIn(str(self.policy_version.pk), [item["version_id"] for item in run.policy_snapshot["versions"]])

    def test_missing_evidence_fails_its_check_without_fabricating_a_finding(self):
        rule = self.rules[0]
        BrandRule.objects.filter(pk=rule.pk).update(evidence_excerpt="Not actually in the policy text")
        _, _, run = self.run_review("Luma Desk makes work easier.")
        self.assertEqual(run.status, ReviewRun.Status.PARTIAL)
        self.assertEqual(run.checks.get(rule_id=rule.pk).outcome, ReviewCheck.Outcome.FAILED)
        self.assertFalse(run.findings.filter(rule_id=rule.pk).exists())

    def test_content_version_and_completed_review_are_immutable(self):
        _, version, run = self.run_review("Luma Desk keeps work moving.")
        version.content_text = "changed"
        with self.assertRaises(ValidationError):
            version.save()
        run.failure_message = "rewritten"
        with self.assertRaises(ValidationError):
            run.save()

    def test_saved_content_version_preserves_submitted_whitespace(self):
        _, version = self.create_submission_for(text="  Luma Desk copy.\n")
        self.assertEqual(version.content_text, "  Luma Desk copy.\n")

    def test_repeat_post_with_same_idempotency_token_returns_same_run(self):
        submission, version = self.create_submission_for(text="Luma Desk.")
        token = uuid.uuid4()
        kwargs = dict(submission_id=submission.pk, version_number=version.version_number, workspace=self.workspace, actor=self.creator, idempotency_key=token)
        first = execute_review(**kwargs)
        second = execute_review(**kwargs)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ReviewRun.objects.filter(workspace=self.workspace).count(), 1)

    def test_review_without_approved_policy_fails_explicitly(self):
        empty_workspace = Workspace.objects.create(name="Empty", slug=f"empty-{uuid.uuid4().hex[:8]}")
        WorkspaceMembership.objects.create(user=self.creator, workspace=empty_workspace, role=WorkspaceMembership.Role.CREATOR)
        submission, version = create_submission(
            workspace=empty_workspace,
            creator=self.creator,
            title="No policy",
            channel=ContentSubmission.Channel.WEBSITE,
            audience="",
            campaign_purpose="",
            content_text="Actual copy.",
        )
        run = execute_review(submission_id=submission.pk, version_number=version.version_number, workspace=empty_workspace, actor=self.creator, idempotency_key=uuid.uuid4())
        self.assertEqual(run.status, ReviewRun.Status.FAILED)
        self.assertEqual(run.failure_code, "NO_APPROVED_POLICY")
        self.assertFalse(run.checks.exists())

    def test_future_effective_policy_is_pinned_but_not_evaluated(self):
        future_source, future_version = create_source(
            workspace=self.workspace,
            owner=self.manager,
            title="Future policy",
            source_type=BrandSource.SourceType.POLICY,
            description="Not effective until tomorrow.",
            source_text="Never say the word futurebrand.",
            effective_date=timezone.localdate() + timedelta(days=1),
        )
        future_rule = BrandRule.objects.create(
            source_version=future_version,
            identifier="FUTURE-01",
            category=BrandRule.Category.TERMINOLOGY,
            severity=BrandRule.Severity.HIGH,
            description="Future-only term.",
            rule_type=BrandRule.RuleType.PROHIBITED_TERM,
            matcher_data={"type": "PROHIBITED_TERM", "term": "futurebrand", "scope": "ALL"},
            evidence_excerpt="Never say the word futurebrand.",
        )
        approve_source_version(source_id=future_source.pk, version_number=future_version.version_number, workspace=self.workspace, actor=self.manager)
        _, _, run = self.run_review("futurebrand")
        pin = run.pinned_policy_versions.get(source_version=future_version)
        self.assertFalse(pin.applied)
        self.assertEqual(pin.exclusion_reason, "FUTURE_EFFECTIVE")
        self.assertFalse(run.checks.filter(rule=future_rule).exists())

    def test_future_effective_fact_is_not_used_for_claim_classification(self):
        future_fact_source, future_fact_version = create_source(
            workspace=self.workspace,
            owner=self.manager,
            title="Future fact",
            source_type=BrandSource.SourceType.FACT_SHEET,
            description="A fact effective tomorrow.",
            source_text="Future pilot fact: Up to 8% faster task completion in the future pilot.",
        )
        fact_excerpt = "Up to 8% faster task completion in the future pilot."
        fact = ApprovedFact.objects.create(
            source_version=future_fact_version,
            identifier="FACT-FUTURE",
            subject="Task completion speed",
            statement=fact_excerpt,
            comparison_operator=ApprovedFact.ComparisonOperator.UP_TO,
            comparison_value="8",
            comparison_unit="%",
            evidence_excerpt=fact_excerpt,
            effective_date=timezone.localdate() + timedelta(days=1),
        )
        future_rule = BrandRule.objects.create(
            source_version=future_fact_version,
            approved_fact=fact,
            identifier="FACT-FUTURE",
            category=BrandRule.Category.FACT,
            severity=BrandRule.Severity.MEDIUM,
            description="Future pilot fact.",
            rule_type=BrandRule.RuleType.APPROVED_FACT,
            matcher_data={"type": "APPROVED_FACT", "fact_identifier": "FACT-FUTURE"},
            evidence_excerpt=fact_excerpt,
        )
        approve_source_version(source_id=future_fact_source.pk, version_number=future_fact_version.version_number, workspace=self.workspace, actor=self.manager)
        _, _, run = self.run_review("Task completion was 100% faster in the future pilot.")
        self.assertEqual(run.checks.get(rule=future_rule).outcome, ReviewCheck.Outcome.NOT_APPLICABLE)
        self.assertFalse(run.findings.filter(rule=future_rule).exists())

    def test_service_enforces_active_creator_membership(self):
        with self.assertRaises(ValidationError):
            create_submission(
                workspace=self.workspace,
                creator=self.reviewer,
                title="Reviewer cannot submit",
                channel=ContentSubmission.Channel.EMAIL,
                audience="",
                campaign_purpose="",
                content_text="This must be denied.",
            )
        submission, version = self.create_submission_for()
        WorkspaceMembership.objects.filter(user=self.creator, workspace=self.workspace).update(is_active=False)
        with self.assertRaises(ValidationError):
            execute_review(submission_id=submission.pk, version_number=version.version_number, workspace=self.workspace, actor=self.creator, idempotency_key=uuid.uuid4())


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewPermissionAndFlowTests(ReviewFixtureMixin, TestCase):
    def test_end_to_end_creator_submission_review_and_evidence_inspection(self):
        self.client.force_login(self.creator)
        response = self.client.post(reverse("app:content_reviews:create"), {
            "title": "Spring campaign",
            "channel": ContentSubmission.Channel.EMAIL,
            "audience": "Operations teams",
            "campaign_purpose": "Announce the pilot",
            "content_text": "Luma Desk. Task completion was 100% faster in our internal pilot.",
        })
        self.assertEqual(response.status_code, 302)
        submission = ContentSubmission.objects.get(title="Spring campaign")
        version = submission.versions.get(version_number=1)
        version_url = reverse("app:content_reviews:version_detail", args=[submission.pk, 1])
        run_response = self.client.post(reverse("app:content_reviews:run", args=[submission.pk, 1]), {"idempotency_key": str(uuid.uuid4())})
        self.assertEqual(run_response.status_code, 302)
        run = ReviewRun.objects.get(content_version=version)
        self.assertEqual(run.status, ReviewRun.Status.COMPLETE)
        self.assertTrue(run.findings.filter(rule__identifier="TERM-001").exists())
        self.assertTrue(run.findings.filter(rule__identifier="DISC-001").exists())
        fact_finding = run.findings.get(rule__identifier="FACT-02")
        self.assertEqual(fact_finding.classification, ReviewFinding.Classification.CONTRADICTED)
        self.assertEqual(fact_finding.evidence_excerpt, self.fact_excerpt)
        self.assertEqual(fact_finding.source_version_id, self.policy_version.pk)
        detail_url = reverse("app:content_reviews:review_detail", args=[submission.pk, 1, run.pk])
        detail = self.client.get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, "View approved source evidence")
        self.assertContains(detail, self.fact_excerpt)
        self.assertEqual(detail.context["version"].content_hash, version.content_hash)
        self.assertEqual(detail.context["review"].policy_snapshot_hash, run.policy_snapshot_hash)
        self.assertEqual(self.client.get(version_url).status_code, 200)

    def test_anonymous_user_is_sent_to_login_and_creator_can_list_own_items(self):
        response = self.client.get(reverse("app:content_reviews:list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)
        self.client.force_login(self.creator)
        own, _ = self.create_submission_for(text="My own copy")
        other, _ = self.create_submission_for(creator=self.other_creator, text="Private copy", title="Private")
        response = self.client.get(reverse("app:content_reviews:list"))
        self.assertContains(response, own.title)
        self.assertNotContains(response, other.title)
        denied = self.client.get(reverse("app:content_reviews:detail", args=[other.pk]))
        self.assertEqual(denied.status_code, 404)

    def test_non_creators_can_view_workspace_reviews_but_cannot_create_or_run(self):
        submission, version = self.create_submission_for(text="Workspace submission", creator=self.creator)
        run = execute_review(
            submission_id=submission.pk,
            version_number=version.version_number,
            workspace=self.workspace,
            actor=self.creator,
            idempotency_key=uuid.uuid4(),
        )
        for user in [self.reviewer, self.manager, self.admin]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("app:content_reviews:list")).status_code, 200)
            self.assertEqual(self.client.get(reverse("app:content_reviews:detail", args=[submission.pk])).status_code, 302)
            review_url = reverse("app:content_reviews:review_detail", args=[submission.pk, version.version_number, run.pk])
            self.assertEqual(self.client.get(review_url).status_code, 200)
            self.assertEqual(self.client.get(reverse("app:content_reviews:create")).status_code, 403)
            self.assertEqual(self.client.post(reverse("app:content_reviews:run", args=[submission.pk, version.version_number]), {"idempotency_key": str(uuid.uuid4())}).status_code, 403)

    def test_workspace_isolation_for_known_submission_id(self):
        second_workspace = Workspace.objects.create(name="Other", slug=f"other-{uuid.uuid4().hex[:8]}")
        outsider = self.make_user("outsider")
        WorkspaceMembership.objects.create(user=outsider, workspace=second_workspace, role=WorkspaceMembership.Role.CREATOR)
        submission, _ = self.create_submission_for(text="Private workspace copy")
        self.client.force_login(outsider)
        response = self.client.get(reverse("app:content_reviews:detail", args=[submission.pk]), follow=True)
        self.assertEqual(response.status_code, 404)

    def test_navigation_links_only_to_existing_review_routes(self):
        self.client.force_login(self.creator)
        response = self.client.get(reverse("app:home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("app:content_reviews:list"))

    def test_invalid_or_empty_content_is_rejected(self):
        self.client.force_login(self.creator)
        response = self.client.post(reverse("app:content_reviews:create"), {
            "title": "Empty",
            "channel": ContentSubmission.Channel.EMAIL,
            "audience": "",
            "campaign_purpose": "",
            "content_text": "   ",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ContentSubmission.objects.filter(title="Empty").exists())
        self.assertContains(response, "Paste the marketing text you want checked.")

    def test_user_supplied_html_is_rendered_as_text(self):
        self.client.force_login(self.creator)
        self.client.post(reverse("app:content_reviews:create"), {
            "title": "HTML-like copy",
            "channel": ContentSubmission.Channel.WEBSITE,
            "audience": "",
            "campaign_purpose": "",
            "content_text": "<script>alert('x')</script>",
        })
        submission = ContentSubmission.objects.get(title="HTML-like copy")
        response = self.client.get(reverse("app:content_reviews:detail", args=[submission.pk]), follow=True)
        self.assertContains(response, "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;", html=False)
        self.assertNotContains(response, "<script>alert('x')</script>", html=False)

    def test_review_scenario_seed_is_idempotent_and_runs_the_real_engine(self):
        from django.test import override_settings

        demo_workspace = Workspace.objects.create(name="LumaDesk", slug="lumadesk-demo", is_demo=True)
        demo_creator = User.objects.create_user(username="lumadesk_creator", password="demo-test-password")
        demo_manager = User.objects.create_user(username="lumadesk_brand_manager", password="demo-test-password")
        WorkspaceMembership.objects.create(user=demo_creator, workspace=demo_workspace, role=WorkspaceMembership.Role.CREATOR, is_demo=True)
        WorkspaceMembership.objects.create(user=demo_manager, workspace=demo_workspace, role=WorkspaceMembership.Role.BRAND_MANAGER, is_demo=True)
        seed_lumadesk_source_pack(workspace=demo_workspace, owner=demo_manager)
        with override_settings(DEBUG=True):
            call_command("seed_review_scenarios", stdout=io.StringIO())
            call_command("seed_review_scenarios", stdout=io.StringIO())
        self.assertEqual(ContentSubmission.objects.filter(workspace=demo_workspace, is_demo=True).count(), 6)
        self.assertEqual(ReviewRun.objects.filter(workspace=demo_workspace).count(), 6)
        scenario_a = ReviewRun.objects.get(content_version__submission__title="Scenario A — Prohibited terminology")
        self.assertTrue(scenario_a.findings.filter(rule__identifier="TERM-001").exists())
        scenario_c = ReviewRun.objects.get(content_version__submission__title="Scenario C — Applicable disclaimer missing")
        self.assertTrue(scenario_c.findings.filter(rule__identifier="DISC-002").exists())
        scenario_d = ReviewRun.objects.get(content_version__submission__title="Scenario D — Supported approved fact")
        self.assertEqual(scenario_d.checks.get(rule__identifier="FACT-02").outcome, ReviewCheck.Outcome.SUPPORTED)
        scenario_e = ReviewRun.objects.get(content_version__submission__title="Scenario E — Non-comparable numeric unit")
        self.assertEqual(scenario_e.checks.get(rule__identifier="FACT-02").outcome, ReviewCheck.Outcome.NOT_VERIFIED)
        scenario_f = ReviewRun.objects.get(content_version__submission__title="Scenario F — No applicable violations")
        self.assertFalse(scenario_f.findings.exists())
        self.assertFalse(scenario_f.checks.filter(outcome__in=[
            ReviewCheck.Outcome.FINDING,
            ReviewCheck.Outcome.FAILED,
            ReviewCheck.Outcome.NEEDS_HUMAN_REVIEW,
            ReviewCheck.Outcome.NOT_VERIFIED,
            ReviewCheck.Outcome.CONTRADICTED,
        ]).exists())

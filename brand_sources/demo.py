from django.core.management.base import CommandError
from django.db import transaction

from .models import ApprovedFact, BrandRule, BrandSource, SourceVersion
from .services import approve_source_version, create_source


LUMADESK_SOURCE_PACK = (
    {
        "slug": "lumadesk-product-naming",
        "title": "Product naming and terminology",
        "source_type": BrandSource.SourceType.GUIDELINES,
        "description": "Fictional approved naming guidance for the LumaDesk demo brand.",
        "text": (
            "Official product name: LumaDesk. Always write the product name as one word, with a capital L and D. "
            "Do not write Luma Desk as two words."
        ),
        "rules": (
            {
                "identifier": "TERM-001",
                "category": BrandRule.Category.TERMINOLOGY,
                "severity": BrandRule.Severity.MEDIUM,
                "description": "Use the official product name as one word.",
                "rule_type": BrandRule.RuleType.PROHIBITED_TERM,
                "matcher_data": {"type": "PROHIBITED_TERM", "term": "Luma Desk", "preferred": "LumaDesk", "case_sensitive": False, "scope": "ALL"},
                "evidence_excerpt": "Do not write Luma Desk as two words.",
            },
            {
                "identifier": "TERM-002",
                "category": BrandRule.Category.TERMINOLOGY,
                "severity": BrandRule.Severity.LOW,
                "description": "Use the official product name in branded copy.",
                "rule_type": BrandRule.RuleType.REQUIRED_TERM,
                "matcher_data": {"type": "REQUIRED_TERM", "term": "LumaDesk", "case_sensitive": True, "scope": "ALL"},
                "evidence_excerpt": "Official product name: LumaDesk.",
            },
        ),
    },
    {
        "slug": "lumadesk-brand-voice",
        "title": "Brand voice guidance",
        "source_type": BrandSource.SourceType.GUIDELINES,
        "description": "Synthetic direction for clear, calm and useful LumaDesk communications.",
        "text": (
            "Brand voice: clear, calm and confident. Prefer useful specifics and plain language. "
            "Avoid alarmist phrasing and claims that imply guaranteed results."
        ),
        "rules": (),
    },
    {
        "slug": "lumadesk-pilot-disclosure",
        "title": "Internal pilot claim disclosure",
        "source_type": BrandSource.SourceType.POLICY,
        "description": "Fictional channel-wide qualification for internal pilot performance claims.",
        "text": (
            "Pilot claims: When marketing refers to the internal pilot's performance results, include the exact "
            "qualification \"Results vary.\" This language must accompany every channel-specific numerical result."
        ),
        "rules": (
            {
                "identifier": "DISC-001",
                "category": BrandRule.Category.DISCLAIMER,
                "severity": BrandRule.Severity.HIGH,
                "description": "Include the approved qualification alongside internal pilot performance claims.",
                "rule_type": BrandRule.RuleType.REQUIRED_DISCLAIMER,
                "matcher_data": {"type": "REQUIRED_DISCLAIMER", "phrase": "Results vary.", "scope": "ALL"},
                "evidence_excerpt": 'When marketing refers to the internal pilot\'s performance results, include the exact qualification "Results vary."',
            },
        ),
    },
    {
        "slug": "lumadesk-pilot-disclosure-trigger",
        "title": "Pilot claim disclaimer trigger",
        "source_type": BrandSource.SourceType.POLICY,
        "description": "Synthetic applicability condition for claims about the internal pilot.",
        "text": (
            'When a marketing claim mentions the "internal pilot", include the exact qualification '
            '"Results vary." alongside the claim.'
        ),
        "rules": (
            {
                "identifier": "DISC-002",
                "category": BrandRule.Category.DISCLAIMER,
                "severity": BrandRule.Severity.HIGH,
                "description": "Add the approved qualification to claims that mention the internal pilot.",
                "rule_type": BrandRule.RuleType.REQUIRED_DISCLAIMER,
                "matcher_data": {
                    "type": "REQUIRED_DISCLAIMER",
                    "phrase": "Results vary.",
                    "scope": "ALL",
                    "trigger_phrase": "internal pilot",
                },
                "evidence_excerpt": (
                    'When a marketing claim mentions the "internal pilot", include the exact qualification '
                    '"Results vary." alongside the claim.'
                ),
            },
        ),
    },
    {
        "slug": "lumadesk-product-facts",
        "title": "Product fact sheet",
        "source_type": BrandSource.SourceType.FACT_SHEET,
        "description": "Synthetic product fact used to exercise evidence-grounded review in later phases.",
        "text": (
            "FACT-02 — Task completion speed: “Up to 20% faster task completion in our internal pilot; results vary.” "
            "This is a fictional demonstration claim and has not been independently verified."
        ),
        "fact": {
            "identifier": "FACT-02",
            "subject": "Task completion speed",
            "statement": "Up to 20% faster task completion in our internal pilot; results vary.",
            "comparison_operator": ApprovedFact.ComparisonOperator.UP_TO,
            "comparison_value": "20",
            "comparison_unit": "%",
            "evidence_excerpt": "Up to 20% faster task completion in our internal pilot; results vary.",
        },
    },
)


@transaction.atomic
def seed_lumadesk_source_pack(*, workspace, owner):
    """Seed only missing demo-owned source identities; never edit an existing version."""
    for fixture in LUMADESK_SOURCE_PACK:
        existing = BrandSource.objects.filter(workspace=workspace, slug=fixture["slug"]).first()
        if existing and not existing.is_demo:
            raise CommandError(
                f"The source slug '{fixture['slug']}' is already used by non-demo workspace content; no source pack data was changed."
            )
        if existing and not existing.versions.filter(version_number=1, is_demo=True).exists():
            raise CommandError(
                f"Demo source '{fixture['slug']}' has no seed-owned version 1; no source pack data was changed."
            )

    seeded = []
    for fixture in LUMADESK_SOURCE_PACK:
        source = BrandSource.objects.filter(workspace=workspace, slug=fixture["slug"]).first()
        if source:
            seeded.append(source)
            continue
        source, version = create_source(
            workspace=workspace,
            owner=owner,
            title=fixture["title"],
            source_type=fixture["source_type"],
            description=fixture["description"],
            source_text=fixture["text"],
            is_demo=True,
            slug=fixture["slug"],
        )
        for rule_data in fixture.get("rules", ()):
            BrandRule.objects.create(source_version=version, **rule_data)
        fact_data = fixture.get("fact")
        if fact_data:
            fact = ApprovedFact.objects.create(
                source_version=version,
                effective_date=None,
                **fact_data,
            )
            BrandRule.objects.create(
                source_version=version,
                approved_fact=fact,
                identifier=fact.identifier,
                category=BrandRule.Category.FACT,
                severity=BrandRule.Severity.MEDIUM,
                description=f"Approved factual statement about {fact.subject}.",
                rule_type=BrandRule.RuleType.APPROVED_FACT,
                matcher_data={"type": "APPROVED_FACT", "fact_identifier": fact.identifier},
                evidence_excerpt=fact.evidence_excerpt,
                evidence_start=fact.evidence_start,
                evidence_end=fact.evidence_end,
            )
        approve_source_version(
            source_id=source.pk,
            version_number=version.version_number,
            workspace=workspace,
            actor=owner,
        )
        seeded.append(source)
    return seeded

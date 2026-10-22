from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError

from .models import ApprovedFact, BrandRule, BrandSource, SourceVersion, validate_matcher_data


class BrandDocumentImportForm(forms.Form):
    title = forms.CharField(max_length=160, label="Source title")
    source_type = forms.ChoiceField(choices=BrandSource.SourceType.choices, label="Source type")
    description = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 3}))
    document = forms.FileField(
        label="Brand document",
        help_text="TXT (UTF-8), DOCX, or text-based PDF. Maximum 5 MB; PDFs up to 10 pages.",
        widget=forms.ClearableFileInput(attrs={"accept": ".txt,.docx,.pdf"}),
    )

    def clean_document(self):
        document = self.cleaned_data["document"]
        if document.size > settings.BRAND_DOCUMENT_MAX_UPLOAD_BYTES:
            raise forms.ValidationError("The document exceeds the 5 MB upload limit.")
        return document


class ImportedTextConfirmationForm(forms.Form):
    confirm_text = forms.BooleanField(
        required=True,
        label="I reviewed the extracted text and confirm it is accurate enough to serve as an authoritative brand source.",
        error_messages={"required": "Review the complete draft text and check the confirmation box before continuing."},
    )


class BrandSourceCreateForm(forms.ModelForm):
    source_text = forms.CharField(
        required=False,
        label="Authoritative source text",
        max_length=100_000,
        widget=forms.Textarea(attrs={"rows": 12, "autocomplete": "off"}),
        help_text="Paste the exact guidance that rules and facts will cite. File uploads are not available in this phase.",
    )
    effective_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    class Meta:
        model = BrandSource
        fields = ["title", "source_type", "description"]
        widgets = {
            "title": forms.TextInput(attrs={"maxlength": 160}),
            "description": forms.Textarea(attrs={"rows": 3, "maxlength": 500}),
        }

    def clean_source_text(self):
        value = self.cleaned_data["source_text"]
        if not value.strip():
            raise forms.ValidationError("Add the full authoritative source text before saving.")
        return value


class SourceVersionForm(forms.ModelForm):
    class Meta:
        model = SourceVersion
        fields = ["source_text", "effective_date"]
        widgets = {
            "source_text": forms.Textarea(attrs={"rows": 16, "maxlength": 100_000, "autocomplete": "off"}),
            "effective_date": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {"source_text": "Rules and facts cite exact passages from this version."}

    def clean_source_text(self):
        value = self.cleaned_data["source_text"]
        if not value.strip():
            raise forms.ValidationError("Add the full authoritative source text before saving.")
        return value


class BrandRuleForm(forms.ModelForm):
    term = forms.CharField(max_length=200, required=False, label="Term or required phrase")
    trigger_phrase = forms.CharField(
        max_length=200,
        required=False,
        label="Content applicability trigger (optional)",
        help_text="For required terms or disclaimers, enter a phrase that must appear before the absence can be reported. Leave blank when this matcher does not need a trigger.",
    )
    preferred_term = forms.CharField(max_length=200, required=False, label="Preferred wording", help_text="Optional replacement for a prohibited term.")
    case_sensitive = forms.BooleanField(required=False, initial=False, label="Match capitalization exactly")
    scope = forms.ChoiceField(
        required=False,
        choices=[
            ("ALL", "All channels"),
            ("INSTAGRAM", "Instagram"),
            ("LINKEDIN", "LinkedIn"),
            ("EMAIL", "Email"),
            ("WEBSITE", "Website"),
            ("PAID_AD", "Paid advertising"),
        ],
    )

    class Meta:
        model = BrandRule
        fields = ["identifier", "category", "severity", "rule_type", "description", "evidence_excerpt", "evidence_start", "evidence_end"]
        widgets = {
            "identifier": forms.TextInput(attrs={"maxlength": 40, "placeholder": "TERM-01"}),
            "description": forms.Textarea(attrs={"rows": 3, "maxlength": 1000}),
            "evidence_excerpt": forms.Textarea(attrs={"rows": 4, "maxlength": 2000}),
            "evidence_start": forms.NumberInput(attrs={"min": 0}),
            "evidence_end": forms.NumberInput(attrs={"min": 0}),
        }
        help_texts = {
            "identifier": "Stable across versions when this rule is revised. Use uppercase letters, numbers, dots, underscores or hyphens.",
            "evidence_excerpt": "Copy a precise passage from this draft's source text. It is verified before saving and publishing.",
        }

    def __init__(self, *args, source_version, **kwargs):
        self.source_version = source_version
        super().__init__(*args, **kwargs)
        self.instance.source = source_version.source
        if source_version.status != SourceVersion.Status.DRAFT:
            raise ValidationError("Only draft source versions can be edited.")
        if self.instance and self.instance.pk:
            config = self.instance.matcher_data
            self.fields["term"].initial = config.get("term") or config.get("phrase", "")
            self.fields["trigger_phrase"].initial = config.get("trigger_phrase", "")
            self.fields["preferred_term"].initial = config.get("preferred", "")
            self.fields["case_sensitive"].initial = config.get("case_sensitive", False)
            self.fields["scope"].initial = config.get("scope", "ALL")

    def clean(self):
        cleaned = super().clean()
        rule_type = cleaned.get("rule_type")
        term = cleaned.get("term", "").strip()
        scope = cleaned.get("scope") or "ALL"
        config = {"type": rule_type}
        if rule_type in {BrandRule.RuleType.PROHIBITED_TERM, BrandRule.RuleType.REQUIRED_TERM}:
            if not term:
                self.add_error("term", "Enter the exact term this rule checks.")
            config.update(term=term, case_sensitive=cleaned.get("case_sensitive", False), scope=scope)
            trigger_phrase = cleaned.get("trigger_phrase", "").strip()
            if rule_type == BrandRule.RuleType.REQUIRED_TERM and trigger_phrase:
                config["trigger_phrase"] = trigger_phrase
            if rule_type == BrandRule.RuleType.PROHIBITED_TERM and cleaned.get("preferred_term", "").strip():
                config["preferred"] = cleaned["preferred_term"].strip()
        elif rule_type == BrandRule.RuleType.REQUIRED_DISCLAIMER:
            if not term:
                self.add_error("term", "Enter the exact disclaimer text that must appear.")
            config.update(phrase=term, scope=scope)
            trigger_phrase = cleaned.get("trigger_phrase", "").strip()
            if trigger_phrase:
                config["trigger_phrase"] = trigger_phrase
        elif rule_type == BrandRule.RuleType.APPROVED_FACT:
            self.add_error("rule_type", "Add approved facts through the fact form so the statement and evidence stay linked.")
        if rule_type:
            try:
                validate_matcher_data(config)
            except ValidationError as exc:
                self.add_error("rule_type", exc.messages[0])
        self.cleaned_config = config
        self.instance.matcher_data = config
        evidence = cleaned.get("evidence_excerpt", "")
        start, end = cleaned.get("evidence_start"), cleaned.get("evidence_end")
        if evidence and (not evidence.strip() or evidence not in self.source_version.source_text):
            self.add_error("evidence_excerpt", "This exact excerpt does not appear in this draft's source text.")
        if start is not None or end is not None:
            if start is None or end is None or end < start or self.source_version.source_text[start:end] != evidence:
                self.add_error("evidence_excerpt", "The evidence excerpt does not match the selected source text offsets.")
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.source_version = self.source_version
        obj.approved_fact = None
        obj.matcher_data = self.cleaned_config
        if commit:
            obj.save()
        return obj


class ApprovedFactForm(forms.ModelForm):
    identifier = forms.CharField(max_length=40, widget=forms.TextInput(attrs={"maxlength": 40, "placeholder": "FACT-02"}))

    class Meta:
        model = ApprovedFact
        fields = [
            "identifier", "subject", "statement", "comparison_operator", "comparison_value",
            "comparison_max", "comparison_unit", "evidence_excerpt", "evidence_start",
            "evidence_end", "effective_date",
        ]
        widgets = {
            "subject": forms.TextInput(attrs={"maxlength": 160}),
            "statement": forms.Textarea(attrs={"rows": 4, "maxlength": 1000}),
            "comparison_value": forms.NumberInput(attrs={"step": "0.0001"}),
            "comparison_max": forms.NumberInput(attrs={"step": "0.0001"}),
            "comparison_unit": forms.TextInput(attrs={"maxlength": 40, "placeholder": "%"}),
            "evidence_excerpt": forms.Textarea(attrs={"rows": 4, "maxlength": 2000}),
            "evidence_start": forms.NumberInput(attrs={"min": 0}),
            "evidence_end": forms.NumberInput(attrs={"min": 0}),
            "effective_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, source_version, **kwargs):
        self.source_version = source_version
        super().__init__(*args, **kwargs)
        self.instance.source_version = source_version
        if source_version.status != SourceVersion.Status.DRAFT:
            raise ValidationError("Only draft source versions can be edited.")
        self.fields["comparison_operator"].required = False
        self.fields["comparison_operator"].choices = [("", "No structured comparison"), *ApprovedFact.ComparisonOperator.choices]
        if not self.instance._state.adding:
            self.fields["identifier"].disabled = True

    def clean_identifier(self):
        identifier = self.cleaned_data["identifier"]
        conflict = BrandRule.objects.filter(source_version=self.source_version, identifier=identifier)
        if not self.instance._state.adding:
            conflict = conflict.exclude(approved_fact=self.instance)
        if conflict.exists():
            raise forms.ValidationError("This identifier is already used by a different rule in this source version.")
        return identifier

    def clean(self):
        cleaned = super().clean()
        evidence = cleaned.get("evidence_excerpt", "")
        if evidence and (not evidence.strip() or evidence not in self.source_version.source_text):
            self.add_error("evidence_excerpt", "This exact excerpt does not appear in this draft's source text.")
        start, end = cleaned.get("evidence_start"), cleaned.get("evidence_end")
        if start is not None or end is not None:
            if start is None or end is None or end < start or self.source_version.source_text[start:end] != evidence:
                self.add_error("evidence_excerpt", "The evidence excerpt does not match the selected source text offsets.")
        return cleaned

    def save(self, commit=True):
        fact = super().save(commit=False)
        fact.source_version = self.source_version
        if commit:
            fact.save()
            rule_defaults = {
                "source_version": self.source_version,
                "approved_fact": fact,
                "identifier": fact.identifier,
                "category": BrandRule.Category.FACT,
                "severity": BrandRule.Severity.MEDIUM,
                "description": f"Approved factual statement about {fact.subject}.",
                "rule_type": BrandRule.RuleType.APPROVED_FACT,
                "matcher_data": {"type": BrandRule.RuleType.APPROVED_FACT, "fact_identifier": fact.identifier},
                "evidence_excerpt": fact.evidence_excerpt,
                "evidence_start": fact.evidence_start,
                "evidence_end": fact.evidence_end,
            }
            rule, created = BrandRule.objects.get_or_create(
                source_version=self.source_version,
                identifier=fact.identifier,
                defaults=rule_defaults,
            )
            if not created:
                rule.approved_fact = fact
                rule.description = rule_defaults["description"]
                rule.matcher_data = rule_defaults["matcher_data"]
                rule.evidence_excerpt = fact.evidence_excerpt
                rule.evidence_start = fact.evidence_start
                rule.evidence_end = fact.evidence_end
                rule.save()
        return fact

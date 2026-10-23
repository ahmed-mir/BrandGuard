"""Repository-controlled, source-grounded Demo AI output; never contacts a provider."""
import re


_QUOTED_TEXT = re.compile(r"[\u201c\"]([^\u201d\"]+)[\u201d\"]")
_GUARANTEED_CLAIM = re.compile(r"[^.!?\n]*\b(?:guarantee|guaranteed|promise|promises)\b[^.!?\n]*[.!?]?", re.I)
_MIRACLE_WORDING = re.compile(r"\bproductivity\s+miracle\b", re.I)


def _passage(context, predicate):
    for item in context:
        if isinstance(item, dict) and isinstance(item.get("excerpt"), str) and predicate(item["excerpt"]):
            if item.get("source_version_id"):
                return item
    return None


def _finding(category, classification, explanation, matched_text, source, evidence):
    return {
        "category": category,
        "classification": classification,
        "explanation": explanation,
        "matched_text": matched_text,
        "source_version_id": str(source["source_version_id"]) if source else "",
        "evidence_excerpt": evidence if source else "",
        "suggested_wording": "",
    }


def _exact_text_span(haystack, needle):
    """Find a case-insensitive phrase but return its exact original spelling."""
    start = haystack.casefold().find(needle.casefold())
    return haystack[start:start + len(needle)] if start >= 0 else ""


def request_demo_analysis(payload):
    """Return bounded demo advisories grounded only in the supplied approved excerpts."""
    content = payload.get("content", "")
    context = payload.get("approved_source_passages", [])
    if not isinstance(content, str) or not isinstance(context, list):
        return {"data": {"findings": []}, "input_tokens": None, "output_tokens": None}

    findings = []
    fact_source = _passage(
        context,
        lambda excerpt: "task completion" in excerpt.casefold()
        and "up to" in excerpt.casefold()
        and "results vary" in excerpt.casefold(),
    )
    if fact_source:
        quoted = _QUOTED_TEXT.search(fact_source["excerpt"])
        approved_claim = quoted.group(1) if quoted else ""
        matched_claim = _exact_text_span(content, approved_claim) if approved_claim else ""
        if matched_claim:
            findings.append(_finding(
                "CLAIM", "SUPPORTED",
                "This wording matches the supplied approved internal-pilot statement, including its 'up to' limit and 'results vary' qualification. The source is fictional demo guidance, not independent factual verification.",
                matched_claim, fact_source, fact_source["excerpt"],
            ))

        guarantee = _GUARANTEED_CLAIM.search(content)
        if guarantee and re.search(r"\d", guarantee.group(0)):
            findings.append(_finding(
                "CLAIM", "POTENTIAL_CONFLICT",
                "The approved source supports only an 'up to 20%' internal-pilot statement qualified by 'results vary'; it does not support this broader guaranteed numerical claim. This is a conflict with supplied evidence, not proof that the claim is false.",
                guarantee.group(0).strip(), fact_source, fact_source["excerpt"],
            ))

    voice_source = _passage(
        context,
        lambda excerpt: "brand voice" in excerpt.casefold()
        and "plain language" in excerpt.casefold(),
    )
    miracle = _MIRACLE_WORDING.search(content)
    if voice_source and miracle:
        findings.append(_finding(
            "TONE", "POTENTIAL_CONFLICT",
            "The promotional phrase may be less specific and measured than the supplied guidance to prefer useful specifics and plain language. This is simulated tone advice for demonstration, not an objective rule violation.",
            miracle.group(0), voice_source, voice_source["excerpt"],
        ))

    return {"data": {"findings": findings}, "input_tokens": None, "output_tokens": None}

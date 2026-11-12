"""Layer 2: Static output filters — SLA hallucination check, PII leak detection."""

import re
from dataclasses import dataclass, field
from typing import Optional

from app.guardrails.static_filters import _PII_PATTERNS, check_output_pii

# SLA-related numbers/terms to validate against context.
# Durations are matched after normalisation (see _norm): "2-hour", "two hours"
# and "2 hours" all become "2 hours", so a hyphen or a spelled-out number can't
# slip a promise past the check.
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "fifteen": "15", "twenty": "20", "twenty four": "24",
    "thirty": "30", "forty eight": "48", "sixty": "60", "seventy two": "72",
    "ninety": "90",
}
_NUMBER_WORD_RE = re.compile(
    r"\b(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)
_TIME_PATTERN = re.compile(
    r"\b(\d+)\s*(minutes?|hours?|business days?|days?)\b", re.IGNORECASE
)
_PERCENTAGE_PATTERN = re.compile(r"\b(\d+\.?\d*)\s*%\b")
_WINDOW_RE = re.compile(r"\b(last|past|previous|recent|every|each|per)\s*$")


def _norm(text: str) -> str:
    """Lower-case, hyphens/underscores to spaces, number words to digits."""
    text = re.sub(r"[-_]+", " ", text.lower())
    text = _NUMBER_WORD_RE.sub(lambda m: _NUMBER_WORDS[m.group(1).lower()], text)
    return re.sub(r"\s+", " ", text)


@dataclass
class OutputValidation:
    passed: bool = True
    pii_leaks: list = field(default_factory=list)
    authorized_disclosures: list = field(default_factory=list)
    hallucinated_sla: list = field(default_factory=list)
    flags: list = field(default_factory=list)


def validate_output(
    response: str,
    context_chunks: list,
    tool_results: Optional[list] = None,
    user_input: str = "",
) -> OutputValidation:
    result = OutputValidation()

    # Grounding corpus: retrieved KB chunks + raw tool results + the user's own
    # message. Tool results (check_sla, lookup_asset, ...) are authoritative
    # database output, so values quoted from them are grounded. A number the
    # USER said ("promise me 30 minutes") echoed back in a refusal is not an
    # invention either — whether it became a commitment is the judge's job.
    tool_text = " ".join(str(t) for t in (tool_results or []))
    context_text = " ".join(c.get("text", "") for c in context_chunks)
    grounding_text = context_text + " " + tool_text + " " + (user_input or "")

    # PII values present in tool results are authorized disclosures
    # (e.g. an employee email returned by lookup_asset), not leaks.
    authorized_values = set()
    for pattern in _PII_PATTERNS.values():
        for match in pattern.finditer(tool_text):
            authorized_values.add(match.group())

    for pii in check_output_pii(response):
        if pii["value"] in authorized_values:
            result.authorized_disclosures.append(pii)
        else:
            result.pii_leaks.append(pii)

    if result.pii_leaks:
        result.passed = False
        result.flags.append("PII detected in model output")
    if result.authorized_disclosures:
        disclosed_types = sorted({p["type"] for p in result.authorized_disclosures})
        result.flags.append(
            f"note_authorized_disclosure: {', '.join(disclosed_types)} (grounded in tool result)"
        )

    # Check SLA numbers are grounded in context or tool results.
    # Word-boundary match on number+unit — a plain substring check would
    # let "15 minutes" ground a claim of "5 minutes". Both sides are
    # normalised so "2-hour" / "two hours" compare equal to "2 hours".
    norm_grounding = _norm(grounding_text)
    norm_response = _norm(response)
    for match in _TIME_PATTERN.finditer(norm_response):
        term = match.group(0)
        number = match.group(1)
        unit = match.group(2).rstrip("s")
        # "in the last 24 hours" / "over the past 7 days" are reporting
        # windows, not service commitments.
        if _WINDOW_RE.search(norm_response[max(0, match.start() - 24):match.start()]):
            continue
        loose_pattern = re.compile(rf"\b{number} ?{re.escape(unit)}s?\b")
        if not loose_pattern.search(norm_grounding):
            result.hallucinated_sla.append(term)

    # Extract percentages from response
    for match in _PERCENTAGE_PATTERN.finditer(response):
        term = match.group(0)
        if term not in grounding_text:
            result.hallucinated_sla.append(term)

    if result.hallucinated_sla:
        result.flags.append(f"Possible hallucinated SLA values: {result.hallucinated_sla}")

    return result

"""Layer 1: Static input filters — PII detection, injection detection, length limits."""

import re
from dataclasses import dataclass, field

MAX_INPUT_LENGTH = 2000

# PII patterns
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"(?:\+\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d{4}[-\s]?){3}\d{4}\b")
# Slovak/EU national ID patterns (rodné číslo: YYMMDD/XXXX)
_SK_ID_RE = re.compile(r"\b\d{6}/\d{3,4}\b")
# Generic EU-style ID numbers
_EU_ID_RE = re.compile(r"\b[A-Z]{2}\d{6,10}\b")

_PII_PATTERNS = {
    "email": _EMAIL_RE,
    "phone": _PHONE_RE,
    "credit_card": _CREDIT_CARD_RE,
    "slovak_id": _SK_ID_RE,
    "eu_id": _EU_ID_RE,
}

# Prompt injection patterns
_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(your\s+|previous\s+|prior\s+)?instructions", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+", re.IGNORECASE),
    re.compile(r"forget\s+(all\s+)?(your\s+)?instructions", re.IGNORECASE),
    re.compile(r"disregard\s+(all\s+)?(your\s+)?(previous\s+)?instructions", re.IGNORECASE),
    re.compile(r"(reveal|show|print|output|tell\s+me)\s+(the\s+|your\s+)?(system\s+)?prompt", re.IGNORECASE),
    re.compile(r"what\s+(is|are)\s+your\s+(system\s+)?instructions", re.IGNORECASE),
    re.compile(r"act\s+as\s+(if\s+you\s+are|a)\s+", re.IGNORECASE),
    re.compile(r"pretend\s+(you\s+are|to\s+be)\s+", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"DAN\s+mode", re.IGNORECASE),
]


@dataclass
class FilterResult:
    passed: bool = True
    blocked: bool = False
    sanitized_input: str = ""
    pii_detected: list[dict] = field(default_factory=list)
    injection_detected: bool = False
    block_reason: str = ""


def run_input_filters(text: str) -> FilterResult:
    result = FilterResult(sanitized_input=text)

    # Length check
    if len(text) > MAX_INPUT_LENGTH:
        result.passed = False
        result.blocked = True
        result.block_reason = f"Input exceeds maximum length of {MAX_INPUT_LENGTH} characters."
        return result

    # Injection detection
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(text):
            result.passed = False
            result.blocked = True
            result.injection_detected = True
            result.block_reason = "Your message was flagged by our safety system. Please rephrase your request."
            return result

    # PII detection and redaction
    sanitized = text
    for pii_type, pattern in _PII_PATTERNS.items():
        for match in pattern.finditer(sanitized):
            result.pii_detected.append({"type": pii_type, "value": match.group()})
        sanitized = pattern.sub(f"[REDACTED-{pii_type.upper()}]", sanitized)

    if result.pii_detected:
        result.passed = False  # flag but don't block
    result.sanitized_input = sanitized
    return result


def check_output_pii(text: str) -> list[dict]:
    """Check LLM output for PII leaks."""
    found = []
    for pii_type, pattern in _PII_PATTERNS.items():
        for match in pattern.finditer(text):
            found.append({"type": pii_type, "value": match.group()})
    return found

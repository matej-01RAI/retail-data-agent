"""Last-line PII scrubbing for text that leaves the system.

The primary PII control is structural: PII columns cannot be queried at all
(sql_guard.py), so query results never contain them. This module is defence
in depth for everything else that reaches a screen, a saved report or a log:
user input (someone pastes an email address), model output, and stored text.

It masks direct identifiers that have a recognisable shape: email addresses,
phone numbers, street addresses, payment card numbers (Luhn-checked) and IP
addresses. Names are not pattern-detectable reliably; in production the same
call goes to Google Cloud Sensitive Data Protection (DLP), which detects
PERSON_NAME, STREET_ADDRESS and ~150 other infoTypes (docs/architecture.md).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}\b")
# Phone numbers need separators or a leading +, so plain figures such as
# 1234567890 (revenue, ids) are not mistaken for one.
_PHONE = re.compile(
    r"(?<![\w.])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{2,4}\)[\s.-]?|\d{2,4}[\s.-])\d{3,4}[\s.-]\d{3,4}(?![\w.])"
)
_STREET = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][A-Za-z'.-]*\s+){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl|Terrace|Parkway|Pkwy)\b\.?"
)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


_THOUSANDS = re.compile(r"^\d{1,3}(?:([ .,])\d{3})(?:\1\d{3})*$")


def _phone_ok(candidate: str) -> bool:
    """Reject figures written with thousands separators (12 345 678, 12.345.678)."""
    return not _THOUSANDS.match(candidate.strip())


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


@dataclass
class ScrubResult:
    text: str
    counts: Counter = field(default_factory=Counter)

    @property
    def redacted(self) -> int:
        return sum(self.counts.values())


def scrub(text: str) -> ScrubResult:
    if not text:
        return ScrubResult(text or "")
    counts: Counter = Counter()

    def sub(pattern: re.Pattern, label: str, value: str, check=None) -> str:
        def repl(m: re.Match) -> str:
            if check and not check(m.group(0)):
                return m.group(0)
            counts[label] += 1
            return f"[REDACTED {label}]"

        return pattern.sub(repl, value)

    out = sub(_EMAIL, "EMAIL", text)
    out = sub(_CARD, "CARD", out, _luhn_ok)
    out = sub(_STREET, "ADDRESS", out)
    out = sub(_PHONE, "PHONE", out, _phone_ok)
    out = sub(_IPV4, "IP", out)
    return ScrubResult(out, counts)

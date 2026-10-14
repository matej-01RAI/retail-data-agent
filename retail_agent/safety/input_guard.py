"""Cheap, deterministic screening of user input before any model call.

This is not where safety lives: the agent cannot reach PII, other users'
products or a write path no matter what it is told (see sql_guard.py and the
report tools). The input guard exists to (1) refuse the obvious attacks
without spending a model call on them, (2) keep raw PII the user pastes out
of the prompt and the logs, and (3) cap input size. Topic scoping ("only
analysis questions") is the system prompt's job, where the model can refuse
politely with context.

In production this stage is Google Cloud Model Armor (prompt-injection and
jailbreak detection, malicious URLs, sensitive data), configured as a
template so security can tune it without a deploy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .pii import scrub

MAX_INPUT_CHARS = 4000

_INJECTION_PATTERNS = [
    r"\bignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|rules|prompts?)",
    r"\b(?:disregard|forget)\s+(?:all\s+|your\s+)?(?:previous\s+|prior\s+)?(?:instructions|rules|guidelines)",
    r"\b(?:reveal|show|print|repeat|output)\s+(?:me\s+)?(?:(?:your|the)\s+(?:system|hidden)\s+prompt|your\s+(?:instructions|rules))",
    r"\byou\s+are\s+now\s+(?:in\s+)?(?:developer|dan|jailbreak|god)\s*mode",
    r"\b(?:developer|jailbreak)\s+mode\s+(?:enabled|on)\b",
    r"\bact\s+as\s+(?:an?\s+)?(?:unrestricted|unfiltered)\b",
    r"\bpretend\s+(?:that\s+)?(?:there\s+are\s+)?no\s+(?:rules|restrictions)",
    r"</?\s*(?:system|developer)\s*>",
]
_INJECTION = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)


@dataclass(frozen=True)
class InputDecision:
    allowed: bool
    text: str  # text to send on (PII-masked)
    reason: str | None = None
    pii_redacted: int = 0
    reply: str | None = None  # canned reply when not allowed


def screen(text: str) -> InputDecision:
    text = (text or "").strip()
    if not text:
        return InputDecision(False, text, "empty", reply="Please type a question about the sales data.")
    if len(text) > MAX_INPUT_CHARS:
        return InputDecision(
            False,
            text[:200],
            "too_long",
            reply=f"That message is too long ({len(text)} characters, limit {MAX_INPUT_CHARS}). "
            "Please shorten it or split it into separate questions.",
        )
    if _INJECTION.search(text):
        return InputDecision(
            False,
            text,
            "prompt_injection",
            reply="I can't change how I work or share my instructions. I'm here to analyse sales, "
            "customer and product data. What would you like to know?",
        )
    masked = scrub(text)
    return InputDecision(True, masked.text, pii_redacted=masked.redacted)

"""Guardrails: what the automation may touch, which actions are risky, and what never gets persisted.

Policy is enforced outside the artifact: a capability can declare a step `irreversible`, but it can
never declare its way *out* of the policy's classification.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from cua.schema import CssStrategy, RoleStrategy, Step, TextAnchorStrategy


class PolicyViolation(Exception):
    pass


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str]
    denied_paths: list[str] = []
    allowed_actions: list[str]
    irreversible_patterns: list[str]

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def allows_url(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in ("about", "data"):
            return True
        return (parts.scheme in ("http", "https") and parts.hostname in self.allowed_hosts
                and not any(re.search(p, parts.path) for p in self.denied_paths))

    def check_url(self, url: str) -> None:
        if not self.allows_url(url):
            raise PolicyViolation(f"URL outside allowlist: {url}")

    def check_action(self, action: str) -> None:
        if action not in self.allowed_actions:
            raise PolicyViolation(f"action {action!r} not in allowlist {self.allowed_actions}")

    def is_irreversible(self, label: str) -> bool:
        return any(re.search(p, label) for p in self.irreversible_patterns)

    def risk(self, step: Step) -> str:
        """Effective risk = the stricter of what the artifact declares and what policy infers."""
        if step.risk == "irreversible" or step.target is None:
            return step.risk
        labels = [step.target.description]
        for s in step.target.strategies:
            if isinstance(s, RoleStrategy):
                labels.append(s.name)
            elif isinstance(s, TextAnchorStrategy):
                labels.append(s.anchor)
            elif isinstance(s, CssStrategy):
                labels.append(s.selector)
        return "irreversible" if any(self.is_irreversible(x) for x in labels) else "safe"


_PATTERNS: list[tuple[re.Pattern, Any]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "***-**-****"),                                  # SSN
    (re.compile(r"\b[A-Z]{1,2}-\d{5,}-\d{2}\b"), lambda m: "****" + m.group()[-4:]),        # account no.
    (re.compile(r"\b\d{9,17}\b"), lambda m: "****" + m.group()[-4:]),                       # long digit runs
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
]


class Redactor:
    """One redactor for everything that leaves memory: logs, LLM observations, human-action capture.

    Known sensitive values (secrets, declared-sensitive inputs/outputs) are replaced wherever they
    appear; well-known PII shapes are masked by pattern. Over-redaction is the accepted failure mode.
    """

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def add(self, value: Any, sensitivity: str, label: str) -> None:
        value = str(value) if value is not None else ""
        if len(value) < 3 or sensitivity == "none":
            return
        self._values[value] = {
            "secret": "[SECRET]",
            "pii_id": "***" + value[-2:],
            "pii": f"[PII:{label}]",
            "financial": f"[FIN:{label}]",
        }[sensitivity]

    def __call__(self, text: str) -> str:
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        for raw in sorted(self._values, key=len, reverse=True):
            text = text.replace(raw, self._values[raw])
        return text

    def scrub(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self(obj)
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub(v) for v in obj]
        return obj

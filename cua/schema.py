"""Capability artifact + replay result contract.

A capability is what an AI agent calls: a typed contract (inputs, outputs, success, outcomes)
on top of an ordered, reviewable step list. It is decoupled from the model transcript — nothing
here refers to the LLM run except `provenance`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|secrets|tenant)\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

Sensitivity = Literal["none", "pii", "pii_id", "financial", "secret"]
ValueType = Literal["string", "integer", "decimal", "boolean"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Locators: how a control is identified. Ordered most-semantic first; replay takes the
# first strategy that matches exactly one visible element. -----------------------------------

class RoleStrategy(Strict):
    """Accessible role + name — what a screen reader (and a human) would call it."""
    by: Literal["role"] = "role"
    role: str
    name: str


class TextAnchorStrategy(Strict):
    """A control of `role` in the table row labelled by visible text `anchor` (label-less legacy forms)."""
    by: Literal["text_anchor"] = "text_anchor"
    anchor: str
    role: str


class TableCellStrategy(Strict):
    """A data cell: row whose first cell reads `row`; column headed `column`, or the next cell if None."""
    by: Literal["table_cell"] = "table_cell"
    row: str
    column: str | None = None


class CssStrategy(Strict):
    """Structural last resort; breaks on markup changes, so it only ever ranks last."""
    by: Literal["css"] = "css"
    selector: str


Strategy = Annotated[RoleStrategy | TextAnchorStrategy | TableCellStrategy | CssStrategy, Field(discriminator="by")]


class Locator(Strict):
    frame: list[str] = Field(default_factory=list, description="frame names from the top document; [] = top")
    strategies: list[Strategy] = Field(min_length=1)
    description: str = ""


class Check(Strict):
    """A state assertion. All fields that are set must hold."""
    frame: list[str] | None = Field(None, description="None = any frame (text checks only)")
    text: str | None = Field(None, description="visible text contained in the frame")
    element: Locator | None = Field(None, description="exactly one visible match")

    @model_validator(mode="after")
    def _non_empty(self) -> Check:
        if self.text is None and self.element is None:
            raise ValueError("check needs `text` and/or `element`")
        return self


class Detector(Strict):
    """A known runtime state and how replay responds to it. Used by capabilities (`outcomes`)
    and by app profiles (app-wide states shared by every capability of that product)."""
    code: str
    kind: Literal["business", "recoverable", "hard", "human_required"]
    when: Check
    message: str
    recover: Literal["click", "restart"] | None = Field(
        None, description="recoverable only: click `target` (dismiss) or restart from entry")
    target: Locator | None = None

    @model_validator(mode="after")
    def _recovery(self) -> Detector:
        if (self.kind == "recoverable") != (self.recover is not None):
            raise ValueError("`recover` is required for, and only for, recoverable detectors")
        if self.recover == "click" and self.target is None:
            raise ValueError("recover=click needs a `target`")
        return self


# --- Capability -------------------------------------------------------------------------------

class Param(Strict):
    type: ValueType
    description: str
    pattern: str | None = None
    sensitivity: Sensitivity = "none"


class Output(Strict):
    type: ValueType
    description: str
    sensitivity: Sensitivity = "none"
    source: Locator
    transform: Literal["text", "currency"] = "text"


class Step(Strict):
    id: str = Field(pattern=r"^[a-z0-9_]+$", description="stable id; tenant overlays patch steps by id")
    action: Literal["goto", "click", "fill", "select", "press"]
    target: Locator | None = None
    value: str | None = Field(None, description="literal or {{inputs.x}} / {{secrets.X}} / {{tenant.x}}")
    risk: Literal["safe", "irreversible"] = "safe"
    expect: Check | None = Field(None, description="checkpoint that must hold after the action")
    timeout_ms: int = 10_000
    intent: str = Field(description="what a human reviewer should understand this step does")

    @model_validator(mode="after")
    def _shape(self) -> Step:
        if (self.action == "goto") == (self.target is not None):
            raise ValueError("goto takes a `value` URL and no target; every other action needs a target")
        if self.action in ("goto", "fill", "select", "press") and self.value is None:
            raise ValueError(f"{self.action} needs a `value`")
        return self


class AppRef(Strict):
    product: str
    versions: str = Field(description="product versions this capability is known to work on")
    surface: Literal["web", "desktop"] = "web"
    profile: str = Field(description="app profile holding app-wide detectors, e.g. mockbank-core@2")


class Provenance(Strict):
    recorded_at: str
    recorded_by: str
    run_id: str | None = None
    goal: str | None = Field(None, description="redacted discovery goal")


class Capability(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$", description="semver; input/output changes bump major")
    status: Literal["draft", "approved"] = "draft"
    description: str
    app: AppRef
    entry: str = Field(description="entry URL template, e.g. {{tenant.base_url}}/")
    inputs: dict[str, Param] = Field(default_factory=dict)
    outputs: dict[str, Output] = Field(default_factory=dict)
    steps: list[Step] = Field(min_length=1)
    success: list[Check] = Field(min_length=1)
    outcomes: list[Detector] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def _references(self) -> Capability:
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        for text in [self.entry, *(s.value or "" for s in self.steps)]:
            for ns, name in TEMPLATE_RE.findall(text):
                if ns == "inputs" and name not in self.inputs:
                    raise ValueError(f"template references undeclared input {name!r}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> Capability:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")


class Profile(Strict):
    """App-wide knowledge for one vendor product line, shared by all its capabilities and tenants."""
    id: str
    product: str
    detectors: list[Detector]

    @classmethod
    def load(cls, path: str | Path) -> Profile:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --- Replay result contract -------------------------------------------------------------------

class Outcome(Strict):
    code: str
    message: str


class Failure(Strict):
    code: Literal["input_invalid", "locator_not_found", "locator_ambiguous", "checkpoint_failed",
                  "unclassified_state", "app_error", "policy_violation", "recovery_exhausted",
                  "extraction_failed", "aborted"]
    step_id: str | None = None
    expected: str
    observed: str
    evidence: list[str] = Field(default_factory=list)


class RunResult(Strict):
    run_id: str
    capability: str
    version: str
    status: Literal["success", "business_outcome", "failed", "needs_human"]
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Outcome | None = None
    error: Failure | None = None
    recoveries: list[dict[str, Any]] = Field(default_factory=list)
    handoffs: list[dict[str, Any]] = Field(default_factory=list)
    evidence_dir: str
    duration_ms: int = 0

"""Requirement classification (A83): deterministic, evidence-carrying.

The Architect has to decide what shape of project it is being asked for, and
that decision drives the plan. It is made from the words in the requirement
plus what is actually in the repository — never from a model's opinion, so the
same requirement always classifies the same way and a wrong classification can
be traced to the terms that caused it.

A requirement with no recognisable signal classifies as ``unknown`` with
confidence 0. The Architect then asks instead of guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence, Tuple

from forge.architect.models import ProjectKind

#: kind -> (weight, trigger terms)
SIGNALS: Dict[str, Tuple[Tuple[int, Tuple[str, ...]], ...]] = {
    ProjectKind.OPERATING_SYSTEM.value: (
        (5, ("operating system", "os kernel", "kernel", "bootloader",
             "boot process", "scheduler", "page tables", "paging",
             "virtual memory", "system call", "syscall", "initrd",
             "initramfs", "long mode", "x86-64", "x86_64", "bios", "uefi",
             "grub", "limine", "multiboot")),
    ),
    ProjectKind.DRIVER.value: (
        (5, ("driver", "device driver", "pci driver", "usb driver",
             "gpu driver", "network driver", "audio driver", "hda",
             "ahci", "nvme", "xhci", "ehci")),
    ),
    ProjectKind.KERNEL_MODULE.value: (
        (5, ("kernel module", "loadable module", "lkm", "out-of-tree module")),
    ),
    ProjectKind.WEB_APPLICATION.value: (
        (3, ("web app", "web application", "website", "web ui", "frontend",
             "dashboard", "single page", "spa", "react", "vue", "svelte",
             "html", "browser app")),
    ),
    ProjectKind.API_SERVICE.value: (
        (3, ("api", "rest", "graphql", "grpc", "endpoint", "microservice",
             "backend service", "webhook", "openapi", "swagger")),
    ),
    ProjectKind.MOBILE_APPLICATION.value: (
        (4, ("mobile app", "android", "ios app", "flutter", "react native",
             "apk", "swiftui")),
    ),
    ProjectKind.DESKTOP_APPLICATION.value: (
        (4, ("desktop app", "desktop application", "electron", "tauri",
             "gtk", "qt", "win32", "native desktop")),
    ),
    ProjectKind.AI_APPLICATION.value: (
        (3, ("llm", "agent", "chatbot", "embedding", "inference",
             "fine-tune", "finetune", "rag", "retrieval augmented",
             "neural", "model serving", "prompt")),
    ),
    ProjectKind.CLI_TOOL.value: (
        (3, ("cli", "command line", "command-line", "terminal tool",
             "executable", "subcommand")),
    ),
    ProjectKind.LIBRARY.value: (
        (3, ("library", "sdk", "package", "module for", "api surface",
             "reusable")),
    ),
    ProjectKind.SYSTEM_SOFTWARE.value: (
        (3, ("firmware", "embedded", "bare metal", "freestanding",
             "hypervisor", "runtime", "compiler", "linker", "assembler",
             "file system", "filesystem")),
    ),
    ProjectKind.SMALL_PROJECT.value: (
        (1, ("script", "small tool", "quick", "prototype", "one file",
             "simple")),
    ),
}

SCALE_SIGNALS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("large", ("operating system", "kernel", "full stack", "platform",
               "suite", "ecosystem", "everything", "complete")),
    ("small", ("script", "small", "simple", "quick", "tiny", "one file",
               "single file", "minimal")),
)

#: Phrases that state a constraint rather than a goal.
CONSTRAINT_RE = re.compile(
    r"\b(?:must|should|has to|needs to|cannot|can't|only|without|no "
    r"external|offline|portable|compatible|support(?:s|ing)?)\b[^.;\n]{0,160}",
    re.IGNORECASE)
GOAL_RE = re.compile(
    r"\b(?:build|create|make|implement|write|develop|add|port|design)\b"
    r"[^.;\n]{0,160}", re.IGNORECASE)
MAX_ITEMS = 24


@dataclass
class Classification:
    """What the requirement is, and why we think so."""

    kind: str
    confidence: float
    evidence: List[str] = field(default_factory=list)
    #: Every kind that scored, with its score — a reviewer can overrule.
    scores: Dict[str, int] = field(default_factory=dict)
    scale: str = "medium"
    scale_evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind, "confidence": round(self.confidence, 3),
            "evidence": list(self.evidence),
            "scores": {key: self.scores[key] for key in sorted(self.scores)},
            "scale": self.scale, "scale_evidence": list(self.scale_evidence),
        }


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def classify(requirement: str, *,
             observations: Sequence[str] = ()) -> Classification:
    """Classify a requirement string, returning the evidence for the call."""
    text = _normalize(requirement)
    extra = " ".join(_normalize(item) for item in observations)
    scores: Dict[str, int] = {}
    evidence: Dict[str, List[str]] = {}
    for kind, tiers in SIGNALS.items():
        for weight, terms in tiers:
            for term in terms:
                if term in text:
                    scores[kind] = scores.get(kind, 0) + weight
                    evidence.setdefault(kind, [])
                    if term not in evidence[kind]:
                        evidence[kind].append(term)
                elif extra and term in extra:
                    # Repository evidence counts for less than the user's own
                    # words: a repo containing a Makefile is not a request for
                    # an operating system.
                    scores[kind] = scores.get(kind, 0) + max(1, weight // 3)
                    evidence.setdefault(kind, [])
                    tag = "%s (repo)" % term
                    if tag not in evidence[kind]:
                        evidence[kind].append(tag)

    if not scores:
        return Classification(kind=ProjectKind.UNKNOWN.value, confidence=0.0,
                              evidence=[], scores={}, scale=_scale(text)[0],
                              scale_evidence=_scale(text)[1])

    best = max(sorted(scores), key=lambda key: scores[key])
    total = sum(scores.values())
    # Confidence is the winner's share of the total signal: a requirement
    # that matches several shapes is genuinely ambiguous and says so.
    confidence = scores[best] / float(total) if total else 0.0
    scale, scale_evidence = _scale(text)
    return Classification(
        kind=best, confidence=confidence,
        evidence=evidence.get(best, [])[:MAX_ITEMS],
        scores=scores, scale=scale, scale_evidence=scale_evidence)


def _scale(text: str) -> Tuple[str, List[str]]:
    for label, terms in SCALE_SIGNALS:
        hits = [term for term in terms if term in text]
        if hits:
            return label, hits
    return "medium", []


def goals_from(requirement: str) -> List[str]:
    """Extract goal-shaped clauses from the requirement."""
    text = re.sub(r"\s+", " ", (requirement or "").strip())
    out: List[str] = []
    for match in GOAL_RE.finditer(text):
        clause = match.group(0).strip(" ,;")
        if clause and clause.lower() not in [item.lower() for item in out]:
            out.append(clause[:400])
        if len(out) >= MAX_ITEMS:
            break
    return out or ([text[:400]] if text else [])


def constraints_from(requirement: str) -> List[str]:
    """Extract constraint-shaped clauses from the requirement."""
    text = re.sub(r"\s+", " ", (requirement or "").strip())
    out: List[str] = []
    for match in CONSTRAINT_RE.finditer(text):
        clause = match.group(0).strip(" ,;")
        if clause and clause.lower() not in [item.lower() for item in out]:
            out.append(clause[:400])
        if len(out) >= MAX_ITEMS:
            break
    return out


def acceptance_criteria_for(classification: Classification,
                            requirement: str) -> List[str]:
    """Default acceptance criteria for a classified project.

    These are deliberately checkable statements, not aspirations: each one
    names something a gate in Forge can actually evaluate.
    """
    criteria = [
        "The project builds with its own declared build command, exit code 0, "
        "and no error-level diagnostics.",
        "Every required test level runs and passes, with at least one test "
        "collected at each required level.",
        "Any performance claim is backed by a before/after measurement that "
        "does not regress beyond the plan's tolerance.",
    ]
    kind = classification.kind
    if kind in (ProjectKind.OPERATING_SYSTEM.value,
                ProjectKind.KERNEL_MODULE.value, ProjectKind.DRIVER.value,
                ProjectKind.SYSTEM_SOFTWARE.value):
        criteria.append(
            "The artifact boots in a virtual machine and prints its readiness "
            "marker on the serial console; compilation alone is not "
            "acceptance.")
        criteria.append(
            "Every hardware capability claim carries a status of SUPPORTED, "
            "PARTIALLY_SUPPORTED, UNSUPPORTED, or UNKNOWN, with the probe "
            "that produced it.")
    if kind in (ProjectKind.WEB_APPLICATION.value,
                ProjectKind.API_SERVICE.value):
        criteria.append(
            "Each declared route or page responds as specified in an "
            "integration test.")
    if kind == ProjectKind.LIBRARY.value:
        criteria.append(
            "The public API is documented and covered by unit tests.")
    if kind == ProjectKind.AI_APPLICATION.value:
        criteria.append(
            "Model routing decisions are recorded with the capability, model, "
            "latency, and outcome that were actually observed.")
    if classification.scale == "small":
        criteria.append(
            "The scope stays within the components listed in this plan; "
            "anything larger is a plan change, not an implementation detail.")
    return criteria

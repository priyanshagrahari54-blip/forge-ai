"""Turn real Forge execution traces into a validated fine-tuning dataset.

The source of truth is evidence, not invention: a fleet run writes per-agent
records (``agent``, ``role``, ``required_capability``, ``output``) alongside the
prompt the specialist was given. Those pairs are what a specialist adapter
learns from.

Records are validated by the Model Studio's own validator before anything is
written, so secret-bearing or malformed rows are rejected rather than trained
on, and the dataset carries a fingerprint that a manifest can quote.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from forge.models.model_studio import ModelStudio

#: The brief every specialist answers in a fleet run. Kept here so a dataset
#: records the exact question the answers belong to.
DEFAULT_SPECIALIST_BRIEF = (
    "A small web service is being prepared for production: it must handle ten "
    "times its current traffic, ship safely, and stay secure. Acting as the "
    "{role} specialist, state the first concrete action you would take and why."
)


@dataclass
class DatasetBundle:
    """A written dataset plus the report that justifies trusting it."""

    path: Path
    records: list[dict[str, Any]]
    report: dict[str, Any]
    fingerprint: str
    by_role: dict[str, int] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "records": len(self.records),
            "fingerprint": self.fingerprint,
            "by_role": self.by_role,
            "sources": self.sources,
            "report": self.report,
        }


def records_from_evidence(payload: Mapping[str, Any],
                          *, brief: str = DEFAULT_SPECIALIST_BRIEF,
                          include_blocked: bool = False) -> list[dict[str, str]]:
    """Instruction pairs from one fleet-run evidence artifact.

    Only real outputs are used: a blocked specialist has no answer to learn
    from, and inventing one would train the adapter on fiction.
    """
    records: list[dict[str, str]] = []
    for entry in payload.get("records", []) or []:
        if not isinstance(entry, Mapping):
            continue
        if not entry.get("success") and not include_blocked:
            continue
        output = str(entry.get("output") or "").strip()
        role = str(entry.get("role") or "").strip()
        if not output or not role:
            continue
        records.append({
            "prompt": brief.format(role=role).strip(),
            "response": output,
            "role": role,
            "capability": str(entry.get("required_capability") or ""),
            "agent": str(entry.get("agent") or ""),
        })
    return records


def _fingerprint(records: Sequence[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(str(record.get("prompt", "")).encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(record.get("response", "")).encode("utf-8"))
        digest.update(b"\x01")
    return digest.hexdigest()


def export_instruction_records(
    sources: Iterable[str | Path],
    *,
    brief: str = DEFAULT_SPECIALIST_BRIEF,
) -> tuple[list[dict[str, str]], list[str]]:
    """Read every evidence file given and return the merged instruction pairs."""
    records: list[dict[str, str]] = []
    used: list[str] = []
    for source in sources:
        path = Path(source)
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if not isinstance(payload, Mapping) or "records" not in payload:
            continue
        found = records_from_evidence(payload, brief=brief)
        if found:
            records.extend(found)
            used.append(str(path))
    return records, used


def _normalise_role(name: str) -> str:
    """Role names are spelled with ``-``, ``_`` or spaces across the codebase."""
    return " ".join(str(name).strip().lower().replace("-", " ").replace("_", " ")
                    .split())


def _normalise_roles(roles: Iterable[str] | None) -> set[str]:
    return {_normalise_role(role) for role in (roles or []) if str(role).strip()}


def build_dataset(
    sources: Iterable[str | Path],
    *,
    studio: ModelStudio | None = None,
    output: str | Path | None = None,
    brief: str = DEFAULT_SPECIALIST_BRIEF,
    strict: bool = False,
    roles: Iterable[str] | None = None,
) -> DatasetBundle:
    """Validate, deduplicate and write a fine-tuning dataset as JSONL.

    Validation is the studio's: it rejects secret-bearing records, so a dataset
    built here can be shared without leaking a credential that a task happened
    to contain.

    ``roles`` narrows the dataset to the specialists a single adapter is being
    trained for (case-insensitive, ``-``/``_``/space interchangeable). Without
    it every role in the evidence is mixed together, which teaches a
    specialisation nothing in particular: a ``security`` adapter trained on the
    whole fleet is not a security adapter. The unfiltered counts stay in
    ``report["roles_before_filter"]`` so a narrowed dataset can still be
    audited against what the evidence contained.
    """
    studio = studio or ModelStudio(root=".")
    records, used = export_instruction_records(sources, brief=brief)
    accepted, report = studio.validate_dataset(records)
    #: A secret-bearing row is never trained on. By default it is dropped and
    #: counted in the report (visible, and the rest of the dataset survives);
    #: ``strict=True`` refuses the whole dataset instead, for callers who want
    #: a leak to stop the pipeline rather than shrink it.
    if strict and not report.ok:
        raise ValueError(
            "dataset rejected by validation: "
            + "; ".join(issue.message for issue in report.issues if issue.fatal))

    # The studio's validator normalises records to instruction/output/context
    # and drops the rest, so the role breakdown is taken from the source rows
    # that survived validation (matched by their instruction text).
    role_by_instruction = {
        str(record.get("prompt", "")): str(record.get("role") or "unknown")
        for record in records
    }
    by_role: dict[str, int] = {}
    for record in accepted:
        role = role_by_instruction.get(str(record.get("instruction", "")),
                                       "unknown")
        by_role[role] = by_role.get(role, 0) + 1

    roles_before_filter = dict(sorted(by_role.items()))
    report_dict = report.to_dict()
    report_dict["roles_before_filter"] = roles_before_filter
    wanted = _normalise_roles(roles)
    if wanted:
        accepted = [
            record for record in accepted
            if _normalise_role(role_by_instruction.get(
                str(record.get("instruction", "")), "unknown")) in wanted
        ]
        by_role = {}
        for record in accepted:
            role = role_by_instruction.get(str(record.get("instruction", "")),
                                           "unknown")
            by_role[role] = by_role.get(role, 0) + 1
    report_dict["role_filter"] = sorted(wanted)

    target = Path(output) if output else (
        Path(studio.directory) / "finetune-dataset.jsonl")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for record in accepted:
            handle.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True) + "\n")

    return DatasetBundle(
        path=target,
        records=list(accepted),
        report=report_dict,
        fingerprint=_fingerprint(accepted),
        by_role=dict(sorted(by_role.items())),
        sources=used,
    )

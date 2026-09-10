"""Desktop autonomy profiles (A35).

Profiles decide which *authorized* actions may run without an approval
prompt. They never create authority: the A33 policy gate still decides
ALLOW / DENY / REQUIRE_APPROVAL first, and hard risk invariants always
win. Profiles can only make execution *more* interactive than policy.

- ``SAFE``       — observation only; every actuation is denied.
- ``ASSISTED``   — observation automatic; actuation needs approval
                   (default).
- ``AUTONOMOUS`` — low-risk actuation inside a granted task scope is
                   automatic; MEDIUM and above still need approval.
- ``CUSTOM``     — a named base mode plus per-action overrides that may
                   only *tighten* (auto → approval → deny). Hard
                   invariants can never be overridden.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.desktop.actions import DesktopActionKind, OBSERVATION_KINDS
from forge.desktop.risk import LOW, risk_rank


class ProfileVerdict(str):
    AUTO = "AUTO"
    APPROVAL = "APPROVAL"
    DENY = "DENY"


#: Order of restrictiveness, for tighten-only validation.
_TIGHTNESS = {"AUTO": 0, "APPROVAL": 1, "DENY": 2}


class ProfileError(ValueError):
    """An invalid profile definition (fail closed)."""


@dataclass(frozen=True)
class DesktopProfile:
    mode: str = "assisted"  # safe | assisted | autonomous | custom
    base: str = "assisted"  # base mode for CUSTOM profiles
    overrides: dict[str, str] = field(default_factory=dict)
    autonomous_risk_ceiling: str = LOW  # AUTO at/below this risk

    def __post_init__(self) -> None:
        if self.mode not in ("safe", "assisted", "autonomous", "custom"):
            raise ProfileError(f"unknown desktop profile mode {self.mode!r}")
        if self.autonomous_risk_ceiling not in ("NONE", "LOW"):
            raise ProfileError(
                "autonomous_risk_ceiling must be NONE or LOW: autonomous "
                "execution never auto-approves MEDIUM risk or above")
        if self.mode == "custom":
            if self.base not in ("safe", "assisted", "autonomous"):
                raise ProfileError(
                    f"CUSTOM profile needs a valid base mode, "
                    f"got {self.base!r}")
            if self.base == "custom":
                raise ProfileError("CUSTOM profile base cannot be custom")
        for action, verdict in self.overrides.items():
            try:
                DesktopActionKind(action)
            except ValueError:
                raise ProfileError(
                    f"unknown desktop action override {action!r}") from None
            if verdict not in ("AUTO", "APPROVAL", "DENY"):
                raise ProfileError(
                    f"override verdict for {action!r} must be "
                    "AUTO/APPROVAL/DENY")
        # CUSTOM overrides must only tighten relative to the base mode,
        # compared at each action's lowest possible (base) risk.
        if self.mode == "custom":
            from forge.desktop.risk import BASE_RISK
            base_profile = DesktopProfile(
                mode=self.base,
                autonomous_risk_ceiling=self.autonomous_risk_ceiling)
            for action, verdict in self.overrides.items():
                kind = DesktopActionKind(action)
                base_verdict = base_profile.verdict_for(
                    kind, BASE_RISK.get(kind.value, "MEDIUM"))
                if _TIGHTNESS[verdict] < _TIGHTNESS[base_verdict]:
                    raise ProfileError(
                        f"CUSTOM override {action}={verdict} loosens the "
                        f"{self.base} base profile ({base_verdict})")

    # -- verdicts -------------------------------------------------------------

    def verdict_for(self, action: DesktopActionKind,
                    risk: str) -> str:
        """The profile verdict for an action/risk pair.

        Observations are AUTO in every non-SAFE profile. This only
        determines prompt behavior — policy and invariants still apply.
        """
        kind = action.value
        base = self.base if self.mode == "custom" else self.mode
        if base == "safe":
            if kind in OBSERVATION_KINDS:
                verdict = "AUTO"
            else:
                verdict = "DENY"
        elif base == "assisted":
            verdict = ("AUTO" if kind in OBSERVATION_KINDS
                       else "APPROVAL")
        else:  # autonomous
            if kind in OBSERVATION_KINDS:
                verdict = "AUTO"
            elif risk_rank(risk) <= risk_rank(self.autonomous_risk_ceiling):
                verdict = "AUTO"
            else:
                verdict = "APPROVAL"
        if self.mode == "custom":
            verdict = self.overrides.get(kind, verdict)
        return verdict

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "base": self.base,
            "overrides": dict(self.overrides),
            "autonomous_risk_ceiling": self.autonomous_risk_ceiling,
            "observations": "auto",
        }


def profile_from_session(mode: str) -> DesktopProfile:
    """Map a cockpit session profile onto a desktop profile (tighten-only)."""
    if mode == "safe":
        return DesktopProfile(mode="safe")
    if mode == "autonomous":
        return DesktopProfile(mode="autonomous")
    if mode == "locked":
        # Locked sessions may only observe; actuation is always denied.
        return DesktopProfile(mode="safe")
    return DesktopProfile(mode="assisted")  # assisted + anything unknown


# Export stable labels for the cockpit.
PROFILE_LABELS = {
    "safe": "SAFE — observation only; actuation denied",
    "assisted": "ASSISTED — actuation requires approval (default)",
    "autonomous": "AUTONOMOUS — low-risk actuation automatic in scope",
    "locked": "LOCKED — observation only",
}

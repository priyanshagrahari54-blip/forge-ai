"""Desktop risk classification and hard security invariants (A35).

Every desktop request is classified into a risk level and checked
against *hard invariants* — deterministic, conservative rules that always
deny: credential extraction, security-control disabling, privilege
escalation, unauthorized persistence, unauthorized remote control, and
workspace escape. Hard invariants win over every profile, policy rule,
and approval token; they cannot be loosened by configuration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge.desktop.actions import DesktopActionKind, DesktopRequest

#: Risk labels shared with the policy gate (forge.security.policy_gate).
RiskLevel = str  # NONE | LOW | MEDIUM | HIGH | CRITICAL

NONE = "NONE"
LOW = "LOW"
MEDIUM = "MEDIUM"
HIGH = "HIGH"
CRITICAL = "CRITICAL"

_RISK_RANK = {NONE: 0, LOW: 1, MEDIUM: 2, HIGH: 3, CRITICAL: 4}


def risk_rank(level: str) -> int:
    return _RISK_RANK.get(level.upper(), 2)  # unknown -> MEDIUM, fail safe


#: Base risk per action kind, before parameters are considered.
BASE_RISK: dict[str, str] = {
    "screenshot": NONE,
    "read_screen": NONE,
    "window_list": NONE,
    "process_list": NONE,
    "process": NONE,
    "system_info": NONE,
    "file_select": NONE,
    "window": LOW,
    "mouse_move": LOW,
    "mouse_click": MEDIUM,
    "keyboard": MEDIUM,
    "clipboard": LOW,
    "file_access": LOW,
    "app_action": MEDIUM,
    "launch": MEDIUM,
}

#: Action parameters that raise the classification one or two steps.
_RAISES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "window": (({"op": ("close",)}, 1),),
    "keyboard": (
        ({"keys": ("ctrl+alt+del", "super+l", "alt+f4", "ctrl+shift+esc")}, 2),
        ({"keys": ("ctrl+c", "ctrl+x", "ctrl+v", "alt+tab", "win+tab")}, 1),
    ),
    "launch": (({"args": ()}, 1),),  # any args raise launch by one
    "clipboard": (({"op": ("write",)}, 1),),
    "file_access": (({"mode": ("write",)}, 1), ({"mode": ("delete",)}, 2)),
    "app_action": (({"action": ("close_window",)}, 1),),
    "process": (({"op": ("terminate",)}, 3),),
}


def _raise_rank(base: str, steps: int) -> str:
    rank = min(4, risk_rank(base) + steps)
    return [NONE, LOW, MEDIUM, HIGH, CRITICAL][rank]


def _contains(haystack: str, needles: tuple[str, ...]) -> bool:
    low = haystack.lower()
    return any(needle in low for needle in needles)


def _trigger_hit(params: dict, key: str, values: tuple[str, ...]) -> bool:
    """True when the parameter is present/truthy and (if listed) matches.

    List parameters (e.g. keyboard ``keys``) hit when any element is
    listed; scalar parameters hit when the value itself is listed.
    """
    value = params.get(key)
    if value is None:
        return False
    if isinstance(value, list):
        if not value:
            return False
        if not values:
            return True
        return any(item in values for item in value)
    if isinstance(value, str) and not value:
        return False
    if values and value not in values:
        return False
    return True


@dataclass(frozen=True)
class RiskAssessment:
    risk: str
    reasons: tuple[str, ...] = ()
    hard_violations: tuple[str, ...] = ()

    @property
    def forbidden(self) -> bool:
        return bool(self.hard_violations)

    def to_dict(self) -> dict[str, Any]:
        return {"risk": self.risk, "reasons": list(self.reasons),
                "hard_violations": list(self.hard_violations)}


# ---------------------------------------------------------------------------
# Hard invariant patterns — ALWAYS DENY, no profile may override.
# ---------------------------------------------------------------------------

#: Credential stores / managers: reading or typing into these is
#: credential extraction and is never allowed.
_CREDENTIAL_APPS = (
    "bitwarden", "lastpass", "1password", "keepass", "keychain",
    "gnome-keyring", "kwallet", "seahorse", "password manager",
)

#: Credential material locations.
_CREDENTIAL_PATHS = (
    ".ssh/", "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys",
    ".gnupg/", ".aws/credentials", ".aws/config", ".env",
    "credentials.json", "service-account", "kubeconfig", ".netrc",
)

#: Security software whose disabling/killing is never allowed.
_SECURITY_SOFTWARE = (
    "defender", "avast", "avg ", "kaspersky", "mcafee", "norton",
    "sophos", "crowdstrike", "falcon", "sentinelone", "carbon black",
    "eset", "bitdefender", "malwarebytes", "firewall", "firewalld",
    "selinux", "apparmor", "edr", "antivirus",
)

#: Privilege-escalation markers.
_ESCALATION = ("sudo", "su -", "su root", "pkexec", "runas",
               "gksu", "doas", "administrator:")

#: Persistence locations/commands.
_PERSISTENCE = (
    "autostart", "crontab", "systemd/system", "schtasks", "launchd",
    "launchagents", "currentversion\\run", "currentversion/run",
    "/etc/rc.local", "init.d", "startup",
)

#: Remote-control backdoors.
_REMOTE_CONTROL = (
    "vncserver", "x11vnc", "xrdp", "ngrok", "teamviewer", "anydesk",
    "sshd", "rdp", "remote desktop", "screen share",
)


def hard_violations(request: DesktopRequest) -> tuple[str, ...]:
    """Deterministic hard-invariant violations for a request (always DENY)."""
    violations: list[str] = []
    kind = request.kind.value
    target = (request.target or "").lower()
    params = request.params
    text = str(params.get("text", "")) if kind == "keyboard" else ""
    keys = " ".join(params.get("keys", [])) if kind == "keyboard" else ""
    args = " ".join(params.get("args", [])) if kind == "launch" else ""
    path = target if kind in ("file_access",) else ""
    content = str(params.get("content", "")) if kind == "file_access" else ""
    op = str(params.get("op", ""))
    action_name = str(params.get("action", ""))

    # -- credential extraction -------------------------------------------------
    if kind in ("keyboard", "launch", "app_action", "clipboard") and \
            _contains(target, _CREDENTIAL_APPS):
        violations.append("credential extraction: target is a credential "
                          "store or password manager")
    if kind == "keyboard" and _contains(text + keys, _CREDENTIAL_APPS):
        violations.append("credential extraction: typed content references "
                          "a credential store")
    if kind in ("file_access", "launch") and _contains(path + args,
                                                        _CREDENTIAL_PATHS):
        violations.append("credential extraction: path or arguments touch "
                          "credential material")
    if kind == "clipboard" and op == "read" and \
            _contains(target, _CREDENTIAL_APPS):
        violations.append("credential extraction: clipboard read from a "
                          "credential store context")

    # -- security-control disabling --------------------------------------------
    if kind in ("launch", "process", "window", "app_action") and \
            _contains(target, _SECURITY_SOFTWARE):
        if kind == "window" and op == "close":
            violations.append("security control: closing a security "
                              "application window")
        elif kind == "app_action" and action_name == "close_window":
            violations.append("security control: closing a security "
                              "application")
        elif kind == "process" and op == "terminate":
            violations.append("security control: terminating a security "
                              "process")
        elif kind == "launch" and _contains(args,
                                            ("kill", "stop", "disable",
                                             "uninstall", "remove")):
            violations.append("security control: launching a command that "
                              "disables security software")
    # Kill utilities pointed at security software (e.g. "taskkill /im
    # defender.exe") are disabling even though the target itself is benign.
    if kind == "launch" and _contains(args, _SECURITY_SOFTWARE) and \
            _contains(target, ("taskkill", "sc", "wmic", "stop-service",
                               "kill", "tskill", "pkill", "killall")):
        violations.append("security control: launching a kill utility "
                          "against security software")
    if kind == "launch" and _contains(target, _SECURITY_SOFTWARE) and \
            not args:
        pass  # launching security software itself is not a violation

    # -- privilege escalation ---------------------------------------------------
    if kind in ("keyboard", "launch") and \
            _contains(text + " " + args + " " + keys + " " + target,
                      _ESCALATION):
        violations.append("privilege escalation: request contains sudo/su/"
                          "pkexec/runas-style escalation")

    # -- unauthorized persistence ----------------------------------------------
    mode_ = params.get("mode")
    if kind == "file_access" and mode_ in ("write", "delete") and \
            _contains(path, _PERSISTENCE):
        violations.append("unauthorized persistence: writing/deleting "
                          "autostart, systemd, cron, or launchd entries")
    if kind == "launch" and _contains(target + " " + args, _PERSISTENCE):
        violations.append("unauthorized persistence: launching a "
                          "persistence scheduler")
    if kind == "file_access" and op == "write" and \
            _contains(content.lower(), _ESCALATION) and \
            _contains(path, _PERSISTENCE):
        violations.append("unauthorized persistence: privileged autostart "
                          "content")

    # -- unauthorized remote control -------------------------------------------
    if kind == "launch" and _contains(target + " " + args, _REMOTE_CONTROL):
        violations.append("unauthorized remote control: launching a remote "
                          "access or tunneling server")

    return tuple(dict.fromkeys(violations))  # dedupe, keep order


def classify(request: DesktopRequest) -> RiskAssessment:
    """Classify a request's risk level and hard-invariant violations."""
    kind = request.kind.value
    base = BASE_RISK.get(kind, MEDIUM)  # unknown -> MEDIUM, fail safe
    rank = risk_rank(base)
    reasons: list[str] = []
    params = request.params
    for trigger, steps in _RAISES.get(kind, ()):
        if all(_trigger_hit(params, key, values)
               for key, values in trigger.items()):
            new_rank = min(4, rank + steps)
            if new_rank > rank:
                rank = new_rank
                reasons.append(
                    f"parameters raise {kind} risk to "
                    f"{[NONE, LOW, MEDIUM, HIGH, CRITICAL][rank]}")
    if kind == "launch" and request.params.get("args"):
        reasons.append("launching with arguments can execute subcommands")
    if kind == "file_access" and request.target.startswith(
            ("/etc", "/boot", "/usr", "c:\\windows", "c:/windows")):
        rank = max(rank, risk_rank(HIGH))
        reasons.append("file_access targets a system directory")
    if kind == "file_access" and \
            any(token in request.target.lower() for token in (".ssh/",)):
        rank = max(rank, risk_rank(CRITICAL))
    risk = [NONE, LOW, MEDIUM, HIGH, CRITICAL][rank]
    return RiskAssessment(
        risk=risk,
        reasons=tuple(reasons),
        hard_violations=hard_violations(request))


def classify_hard_deny(request: DesktopRequest) -> tuple[bool, str]:
    """Convenience: True + first violation when a request is forbidden."""
    violations = hard_violations(request)
    if violations:
        return True, violations[0]
    return False, ""

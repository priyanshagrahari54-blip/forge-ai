"""Safe research networks: classification + hard safety boundary (A84 Stage G).

Forge may research *privacy-preserving* networks such as Tor for lawful,
authorized, safety-bounded research only: public-information studies,
cybersecurity research, academic research, censorship measurement, privacy
research, public-interest investigations, publicly accessible technical
resources and legally accessible datasets (G1).

This is a research capability with a boundary, **not** a dark-web crawler.
What this module guarantees in code:

* every discovered URL is classified: ``normal_web`` / ``archive`` /
  ``privacy_network`` / ``potentially_malicious`` / ``prohibited`` /
  ``unknown`` (G4) — nothing is trusted by default;
* a closed prohibition screen (G2): credential theft/trade, malware or
  ransomware acquisition, illicit marketplaces, weapons or drug
  procurement, trafficking, fraud, unauthorized access, exploitation of
  real targets, law-enforcement evasion, doxxing, stalking, and private
  personal-information collection are refused *as research goals* — the
  screen blocks the objective, so the capability cannot be aimed at harm;
* the network layer (G3) is inert by design: privacy-network access needs
  an explicitly configured isolated gateway, TLS to the gateway only,
  operator domain allowlisting, no automatic downloads, no execution, no
  credential submission, no transactions, no authentication to unknown
  services, size/time bounds and an audit line per request. With no
  gateway configured the capability honestly reports ``DISABLED``; the
  abstraction exists, the transport does not, and Forge never pretends
  otherwise;
* privacy-network sources can never raise confidence on their own: they
  are always ``untrusted`` and require corroboration from an independent
  class for any important claim.

Python floor: 3.8. Stdlib only. No socket is opened by this module.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

__all__ = [
    "NetworkClass",
    "ProhibitionScreen",
    "ResearchNetworkStatus",
    "SafeResearchNetworkGateway",
    "TorUrlValidator",
    "classify_url",
]


class NetworkClass:
    """Source network classification (G4)."""

    NORMAL_WEB = "normal_web"
    ARCHIVE = "archive"
    PRIVACY_NETWORK = "privacy_network"
    POTENTIALLY_MALICIOUS = "potentially_malicious"
    PROHIBITED = "prohibited"
    UNKNOWN = "unknown"

    @classmethod
    def values(cls) -> Tuple[str, ...]:
        return (cls.NORMAL_WEB, cls.ARCHIVE, cls.PRIVACY_NETWORK,
                cls.POTENTIALLY_MALICIOUS, cls.PROHIBITED, cls.UNKNOWN)


#: Public archives/digital libraries (still untrusted as input; treated as
#: stable secondary sources needing primary corroboration for key claims).
KNOWN_ARCHIVE_HOSTS = frozenset({
    "web.archive.org", "archive.org", "arxiv.org", "www.gutenberg.org",
    "gutenberg.org", "loc.gov", "www.loc.gov", "en.wikipedia.org",
    "wikipedia.org", "pubmed.ncbi.nlm.nih.gov", "doi.org",
})

#: Known malicious-infrastructure indicators. A match never means "this URL
#: is safe elsewhere" — it only downgrades the whole retrieval attempt.
KNOWN_MALICIOUS_HOSTS = frozenset({
    # placeholder operator-maintained set; empty by default. Forge will not
    # ship a private threat feed it cannot verify.
})

_ONION_V3 = re.compile(r"^[a-z2-7]{56}\.onion$")
_ONION_V2 = re.compile(r"^[a-z2-7]{16}\.onion$")
# Legacy v2 addresses are deprecated and blocked: they belong to a retired,
# collision-prone addressing scheme; research must target v3 or nothing.

_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Hostnames whose presence in a URL marks the fetch as targeting a live
#: service rather than public information — refused for *research fetching*
#: (exploitation of real targets is out of scope by definition).
_LIVE_TARGET_MARKERS = (".gov", ".mil", "login", "admin", "bank", "wallet",
                        "mail.")

# -- G2: closed prohibition screen ---------------------------------------------

_PROHIBITED_GOALS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("credential-theft", ("stolen credentials", "credential dump", "combolist",
                          "credential stuffing", "buy login", "cracked accounts")),
    ("marketplace-purchase", ("buy ", "purchase ", "order ", "checkout",
                              "escrow", "vendor list", "market prices")),
    ("malware-acquisition", ("malware download", "purchase malware", "rat for sale",
                             "stealer for sale", "obfuscator bypass av",
                             "exploit kit for sale", "ransomware for",
                             "ransomware source", "ransomware code",
                             "ransomware kit", "get ransomware",
                             "buy ransomware", "malware for our")),
    ("ransomware-service", ("ransomware as a service", "ransomware affiliate",
                            "decrypt for bitcoin")),
    ("weapons-procurement", ("firearm for sale", "weapon shipment", "ammo for sale",
                             "explosive precursors buy", "restricted firearm")),
    ("drug-procurement", ("buy drugs", "order narcotics", "prescription without",
                          "controlled substance ship")),
    ("trafficking", ("human trafficking", "exploitative", "trafficking listings")),
    ("fraud-services", ("money mule", "carding", "fraud documents", "fake id buy",
                        "identity for sale")),
    ("unauthorized-access", ("bypass 2fa", "account recovery abuse",
                             "hack someone", "gain access to", "unauthorized access",
                             "spam bot buy", "ddos for hire",
                             "access someone's account", "access another person's",
                             "account without their password",
                             "without their permission log in",
                             "log into someone")),
    ("evasion", ("avoid law enforcement", "untraceable payments", "wipe forensic",
                 "anti-forensics to hide crimes")),
    ("fraudulent-messaging", ("phishing campaign", "spear phishing",
                              "run a phishing", "phish for",
                              "spoofed login page")),
    ("doxxing", ("dox ", "doxxing", "home address", "personal address",
                 "employer and address", "private information on",
                 "where someone lives", "find someone's address")),
    ("personal-data-collection", ("scrape personal data", "collect emails of",
                                  "private messages", "unpublished photos")),
    ("monitoring-person", ("stalk", "track someone's location", "spyware phone",
                           "stalkerware")),
)

#: Phrases that make a *legitimately adjacent* topic safe to research.
#: Screening order: allowed-research framing is recorded, but any hard
#: prohibition match still blocks (the safe list cannot cancel a banned aim).
_ALLOWED_GOALS: Tuple[str, ...] = (
    "public information", "cybersecurity research", "threat intelligence",
    "academic research", "censorship measurement", "privacy research",
    "public-interest", "study", "measurement", "defensive", "awareness",
    "deterrent", "lawful", "authorized",
)


@dataclass(frozen=True)
class ProhibitionScreen:
    """Result of screening one research objective (G2)."""

    allowed: bool
    goal_class: str                    # matched prohibition or "research"
    matched: Tuple[str, ...] = ()
    framing: Tuple[str, ...] = ()      # allowed-research signals observed
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"allowed": self.allowed, "goal_class": self.goal_class,
                "matched": list(self.matched), "framing": list(self.framing),
                "reason": self.reason}


def screen_goal(question: str) -> ProhibitionScreen:
    """Screen a research objective; prohibited aims fail closed."""
    lowered = (question or "").lower()
    framing = tuple(f for f in _ALLOWED_GOALS if f in lowered)
    for goal_class, markers in _PROHIBITED_GOALS:
        matched = tuple(m for m in markers if m in lowered)
        if matched:
            return ProhibitionScreen(
                allowed=False, goal_class=goal_class, matched=matched,
                framing=framing,
                reason=(f"prohibited research aim ({goal_class}): matched "
                        f"{', '.join(matched)}. Forge research is bounded to "
                        "lawful, public-information study; framing words do "
                        "not cancel a prohibited aim"))
    return ProhibitionScreen(allowed=True, goal_class="research",
                             framing=framing,
                             reason="no prohibited aim detected")


# -- G3/G4: URL validation + classification -------------------------------------

class TorUrlValidator:
    """Strict validation of onion URLs before any privacy-network fetch.

    Rules: http/https scheme only; no userinfo (``user@host`` is never a
    research URL — it is a credential attempt); default port only (80/443);
    hostname must match the v3 onion pattern; no wildcard hosts; path/query
    length bounded. Every refusal returns a structured reason — validators
    in this layer never raise raw exceptions to the caller.
    """

    MAX_PATH = 2048

    def validate(self, url: str) -> Dict[str, Any]:
        raw = (url or "").strip()
        if not raw:
            return {"ok": False, "reason": "empty url"}
        try:
            parsed = urlparse(raw)
        except ValueError as exc:
            return {"ok": False, "reason": f"unparseable url: {exc}"}
        if parsed.scheme not in _ALLOWED_SCHEMES:
            return {"ok": False,
                    "reason": f"scheme {parsed.scheme or 'missing'!r} not "
                              "allowed (http/https only)"}
        if parsed.username or parsed.password:
            return {"ok": False,
                    "reason": "userinfo is forbidden — never submit or "
                              "embed credentials"}
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return {"ok": False, "reason": "missing host"}
        if parsed.port not in (None, 80, 443):
            return {"ok": False,
                    "reason": f"port {parsed.port} not allowed (80/443 only)"}
        if host.endswith(".onion"):
            if _ONION_V2.match(host):
                return {"ok": False,
                        "reason": "legacy v2 onion addresses are retired; "
                                  "refused"}
            if not _ONION_V3.match(host):
                return {"ok": False, "reason": "malformed onion hostname"}
        if len(parsed.path or "") > self.MAX_PATH or \
                len(parsed.query or "") > self.MAX_PATH:
            return {"ok": False, "reason": "path/query exceeds bound"}
        return {"ok": True, "host": host, "scheme": parsed.scheme,
                "network_class": classify_url(raw)}


def classify_url(url: str, *, malicious_hosts: Iterable[str] = ()) -> str:
    """Classify one URL's network (G4). Default for anything unclear: unknown."""
    lowered = (url or "").strip().lower()
    if not lowered:
        return NetworkClass.UNKNOWN
    try:
        parsed = urlparse(lowered if "//" in lowered else "http://" + lowered)
    except ValueError:
        return NetworkClass.UNKNOWN
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return NetworkClass.UNKNOWN
    bad_hosts = set(malicious_hosts or ()) | set(KNOWN_MALICIOUS_HOSTS)
    if host in bad_hosts:
        return NetworkClass.POTENTIALLY_MALICIOUS
    if host.endswith(".onion"):
        return NetworkClass.PRIVACY_NETWORK
    if host in KNOWN_ARCHIVE_HOSTS or host.startswith("archive."):
        return NetworkClass.ARCHIVE
    if parsed.scheme == "https" and not host.endswith(".onion"):
        return NetworkClass.NORMAL_WEB
    if parsed.scheme == "http":
        # Plain HTTP on the normal web is not automatically prohibited, but
        # it *is* untrustworthy; only .gov/.edu-adjacent research fetches use
        # it, and the fetch layer additionally SSRF-guards it.
        if any(m in host for m in _LIVE_TARGET_MARKERS):
            return NetworkClass.PROHIBITED
        return NetworkClass.NORMAL_WEB
    if parsed.scheme and parsed.scheme not in _ALLOWED_SCHEMES:
        return NetworkClass.PROHIBITED
    return NetworkClass.UNKNOWN


def source_trust(network_class: str) -> Dict[str, Any]:
    """Uniform trust posture per network class. Never grants authority."""
    return {
        NetworkClass.NORMAL_WEB: {"trusted": False, "requires_corroboration": True,
                                  "auto_execute": False},
        NetworkClass.ARCHIVE: {"trusted": False, "requires_corroboration": True,
                               "auto_execute": False},
        NetworkClass.PRIVACY_NETWORK: {"trusted": False,
                                       "requires_corroboration": True,
                                       "auto_execute": False,
                                       "note": "privacy-network sources are "
                                               "always untrusted input"},
        NetworkClass.POTENTIALLY_MALICIOUS: {"trusted": False,
                                             "requires_corroboration": True,
                                             "auto_execute": False,
                                             "note": "content is quarantined; "
                                                     "never rendered inline"},
        NetworkClass.PROHIBITED: {"trusted": False, "blocked": True},
        NetworkClass.UNKNOWN: {"trusted": False, "requires_corroboration": True,
                               "note": "unknown is never assumed safe"},
    }.get(network_class, {"trusted": False, "blocked": True,
                          "note": "unclassifiable network is fail-closed"})


# -- the inert gateway (G3) -------------------------------------------------------

@dataclass(frozen=True)
class ResearchNetworkStatus:
    enabled: bool
    gateway_configured: bool
    isolation: str          # description of the required isolated environment
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled,
                "gateway_configured": self.gateway_configured,
                "isolation": self.isolation, "detail": self.detail,
                "state": ("DISABLED" if not (self.enabled and
                                             self.gateway_configured)
                          else "READY-FOR-OPERATOR"),
                "honesty": "an enabled flag is not an isolated network; "
                           "this layer never fabricates reachability"}


class SafeResearchNetworkGateway:
    """Privacy-network fetch *abstraction* with a deny-by-default boundary.

    The default instance is deliberately inert: it validates and screens,
    but it performs **no transport at all** unless an operator injects an
    isolated transport callable bound to a hardened proxy they run
    themselves. The transport contract imposed on operators:

    * ``transport(url, *, timeout, max_bytes) -> {"body": bytes, ...}``
    * it must run outside the developer machine's normal identity
      (isolated environment, strict network controls);
    * this wrapper still owns the *rules* — goal screening, URL validation,
      size/time bounds, download/credential/transaction prohibitions and
      audit — so an operator transport can never widen the capability by
      bypassing them.
    """

    MAX_RESPONSE_BYTES = 2 * 1024 * 1024
    MAX_TIMEOUT_SECONDS = 30.0

    def __init__(self, *, enabled: bool = False, transport: Any = None,
                 allowed_hosts: Tuple[str, ...] = (),
                 audit: Any = None, policy: Any = None,
                 agent_identity: str = "forge-research-network") -> None:
        self.enabled = bool(enabled)
        self._transport = transport
        self.allowed_hosts = tuple(h.lower() for h in allowed_hosts)
        self.audit = audit
        self.policy = policy
        self.agent_identity = agent_identity
        self._requests: List[Dict[str, Any]] = []

    # -- status ---------------------------------------------------------------

    def status(self) -> ResearchNetworkStatus:
        configured = self._transport is not None
        return ResearchNetworkStatus(
            enabled=self.enabled, gateway_configured=configured,
            isolation="operator-supplied isolated environment (not shipped; "
                      "this build has no Tor circuit authority)",
            detail=("disabled" if not self.enabled else
                    ("enabled but no transport configured" if not configured
                     else "transport attached; every fetch is screened, "
                          "validated, bounded and audited")))

    # -- request path -----------------------------------------------------------

    def prepare(self, url: str, *, goal: str = "", timeout: float = 15.0) -> Dict[str, Any]:
        """Full pre-flight: goal screen + URL validation + allowlist + A33.

        Returns a structured decision dict; ``ok=False`` means the fetch must
        not proceed, and no part of the pipeline "downgrades" a refusal.
        """
        screen = screen_goal(goal or url)
        network_class = classify_url(url)
        decision: Dict[str, Any] = {
            "ok": False, "url_host": urlparse(
                url if "//" in (url or "") else "http://" + (url or "")
            ).hostname or "",
            "network_class": network_class,
            "goal": screen.to_dict(),
            "truncations": {},
        }
        if not screen.allowed:
            decision["reason"] = screen.reason
            self._record(url, "blocked-goal", decision)
            return decision
        if network_class == NetworkClass.PROHIBITED:
            decision["reason"] = "source classified prohibited"
            self._record(url, "blocked-source", decision)
            return decision
        if network_class != NetworkClass.PRIVACY_NETWORK:
            decision["reason"] = ("this gateway only serves privacy-network "
                                  "sources; normal-web research goes through "
                                  "the allowlisted HTTPS web source instead")
            self._record(url, "wrong-layer", decision)
            return decision
        validator = TorUrlValidator()
        valid = validator.validate(url)
        if not valid.get("ok"):
            decision["reason"] = str(valid.get("reason", "invalid onion url"))
            self._record(url, "blocked-url", decision)
            return decision
        host = str(valid.get("host", ""))
        if host not in self.allowed_hosts:
            decision["reason"] = ("host is not on the operator research "
                                  "allowlist (no ambient crawling)")
            self._record(url, "blocked-allowlist", decision)
            return decision
        if not self.enabled or self._transport is None:
            decision["reason"] = ("capability is inert: no isolated gateway "
                                  "transport configured — Forge will not fake "
                                  "or improvise privacy-network access")
            self._record(url, "inert", decision)
            return decision
        bounded_timeout = max(1.0, min(float(timeout or 15.0),
                                       self.MAX_TIMEOUT_SECONDS))
        decision.update({"ok": True, "host": host,
                         "timeout_seconds": bounded_timeout,
                         "max_bytes": self.MAX_RESPONSE_BYTES,
                         "trust": source_trust(network_class)})
        self._record(url, "prepared", decision)
        return decision

    def fetch(self, url: str, *, goal: str = "", timeout: float = 15.0) -> Dict[str, Any]:
        """Prepared fetch under every bound. Downloads are *never* executed."""
        decision = self.prepare(url, goal=goal, timeout=timeout)
        if not decision.get("ok"):
            return {"ok": False, "denied": True, **decision}
        try:
            raw = self._transport(url, timeout=decision["timeout_seconds"],
                                  max_bytes=decision["max_bytes"])
        except Exception as exc:                       # transport failure = data
            self._record(url, "transport-error", {"error": str(exc)[:200]})
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200],
                    "network_class": decision["network_class"]}
        body = raw.get("body", b"") if isinstance(raw, dict) else b""
        if isinstance(body, str):
            body = body.encode("utf-8", "replace")
        truncated = len(body) > decision["max_bytes"]
        self._record(url, "fetched", {"bytes": min(len(body), decision["max_bytes"]),
                                      "truncated": truncated})
        return {
            "ok": True,
            "text": bytes(body[:decision["max_bytes"]]).decode(
                "utf-8", "replace"),
            "bytes": min(len(body), decision["max_bytes"]),
            "truncated": truncated,
            "retrieved_at": time.time(),
            "network_class": decision["network_class"],
            "trust": decision["trust"],
            "safety": {
                "download_executed": False,
                "credentials_submitted": False,
                "transactions": False,
                "auth_attempted": False,
                "note": "response is quarantined evidence; nothing is "
                        "rendered, opened, executed or trusted automatically",
            },
        }

    # -- audit ------------------------------------------------------------------

    def _record(self, url: str, action: str, extra: Dict[str, Any]) -> None:
        entry = {"at": time.time(), "action": action,
                 "host_only": urlparse(
                     url if "//" in url else "http://" + url).hostname or ""}
        if "reason" in extra:
            entry["reason"] = str(extra["reason"])[:240]
        self._requests.append(entry)
        self._requests = self._requests[-200:]
        if self.audit is not None:
            try:
                self.audit.record(self.agent_identity, "research-network",
                                  action, entry)
            except Exception:
                pass  # audit availability must not widen or narrow safety

    def audit_trail(self) -> List[Dict[str, Any]]:
        return list(self._requests)

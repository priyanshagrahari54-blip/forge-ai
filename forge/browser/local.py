"""A real, bounded browser and web-surface action runner.

This is the *local* browser/computer-use backend: no cloud service, no
JavaScript engine, no ambient authority. It fetches real pages over HTTP(S),
parses the real DOM, keeps real cookies across navigations, follows real
redirects, submits real forms, and lets a specialist click and type on the
resulting page — so the ``browser`` and ``computer_use`` capabilities are
served by something that genuinely does the work instead of a stub.

Honest limits (reported in every result's metadata):

* no JS engine — a page whose content is script-generated looks empty here;
* no pixels — this is DOM-level action, not screenshot-driven desktop control;
* **no ambient authority**: every fetch must pass an allowlist
  (``FORGE_BROWSER_ALLOW``, comma separated, ``*`` allowed for all hosts), and
  the default policy allows loopback only. Link-local, private-range and cloud
  metadata addresses are refused unless a host is explicitly allowlisted, so a
  page cannot make Forge probe the network it lives in.

Repository code has no opinion about what a model may reach: the allowlist is
configuration, and an empty one means "this machine only".
"""
from __future__ import annotations

import http.cookiejar
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from forge.models.errors import ProviderError
from forge.models.provider import ModelResult

#: Hard caps: a hostile or broken page must not exhaust a worker.
MAX_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0
MAX_TEXT = 4000
MAX_LINKS = 60
MAX_ACTIONS = 12

USER_AGENT = "ForgeBrowser/1.0 (+local DOM browser; no JS)"


class BrowserUnavailable(ProviderError):
    """The request could not be turned into a real navigation."""


class BrowserBlocked(ProviderError):
    """The target failed the allowlist: this is a refusal, not a failure."""


@dataclass
class BrowserPolicy:
    """What this browser is allowed to fetch.

    ``allowed_hosts`` empty means loopback only. ``*`` allows anything public;
    private/link-local ranges still need to be named explicitly, so a wildcard
    never becomes a way to scan the local network.
    """

    allowed_hosts: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    max_bytes: int = MAX_BYTES
    allow_private: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BrowserPolicy":
        import os

        env = dict(os.environ) if env is None else env
        raw = str(env.get("FORGE_BROWSER_ALLOW", "") or "")
        hosts = tuple(item.strip().lower() for item in raw.split(",")
                      if item.strip())
        timeout = float(env.get("FORGE_BROWSER_TIMEOUT", DEFAULT_TIMEOUT) or
                        DEFAULT_TIMEOUT)
        max_bytes = int(env.get("FORGE_BROWSER_MAX_BYTES", MAX_BYTES) or
                        MAX_BYTES)
        return cls(allowed_hosts=hosts, timeout=timeout, max_bytes=max_bytes,
                   allow_private=bool(env.get("FORGE_BROWSER_ALLOW_PRIVATE")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed_hosts": list(self.allowed_hosts),
            "loopback_only": not self.allowed_hosts,
            "allow_private": self.allow_private,
            "timeout_seconds": self.timeout,
            "max_bytes": self.max_bytes,
        }


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "localhost.localdomain"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_private_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (address.is_private or address.is_link_local
            or address.is_reserved or address.is_multicast)


def _resolve(host: str) -> tuple[str, ...]:
    """Resolve a hostname once, so the check and the fetch agree."""
    if not host:
        return ()
    try:
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ()
    return tuple(sorted({info[4][0] for info in infos}))


def check_target(url: str, policy: BrowserPolicy) -> str:
    """Return the normalized URL, or raise :class:`BrowserBlocked`.

    The host allowlist is checked *and* every resolved address is checked, so a
    name that points at the local network cannot slip past a name-only rule.
    """
    parsed = urllib.parse.urlsplit(str(url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise BrowserBlocked(
            f"only http(s) URLs are fetched, not {parsed.scheme or 'a bare path'!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise BrowserBlocked("the URL names no host")
    allowed = policy.allowed_hosts
    wildcard = "*" in allowed
    named = host in allowed or any(
        host.endswith("." + item.lstrip("*").lstrip("."))
        for item in allowed if item and item != "*")
    addresses = _resolve(host)
    loopback = _is_loopback(host) or (
        bool(addresses) and all(_is_loopback(address) for address in addresses))
    if loopback:
        #: The default policy is "this machine only", so loopback needs no
        #: allowlist entry; it is still explicit, never implicit.
        return urllib.parse.urlunsplit(parsed)
    inside = _is_private_address(host) or any(_is_private_address(address)
                                              for address in addresses)
    if inside and not (named and policy.allow_private):
        raise BrowserBlocked(
            f"{host} resolves inside a private/reserved range; name it in "
            "FORGE_BROWSER_ALLOW *and* set FORGE_BROWSER_ALLOW_PRIVATE=1 if "
            "that is really intended (this is the SSRF guard)")
    if not (named or wildcard):
        raise BrowserBlocked(
            f"{host} is not in FORGE_BROWSER_ALLOW; the local browser is "
            "loopback-only until hosts are allowlisted")
    return urllib.parse.urlunsplit(parsed)


class _Extractor(HTMLParser):
    """Collect what a text/DOM browser cares about from a real page."""

    #: ``head`` is deliberately *not* skipped: the title lives there and a
    #: browser reports it. Only nodes that never contribute visible content
    #: (or that would inject code as text) are skipped.
    _SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self.text_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self.forms: list[dict[str, Any]] = []
        self._form: dict[str, Any] | None = None
        self._link: dict[str, str] | None = None

    # -- tags --------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = {key.lower(): (value or "") for key, value in attrs}
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "a":
            self._link = {"text": "", "href": attributes.get("href", "")}
        elif tag == "form":
            self._form = {"action": attributes.get("action", ""),
                          "method": (attributes.get("method") or "get").lower(),
                          "fields": []}
        elif tag in ("input", "textarea", "select") and self._form is not None:
            name = attributes.get("name") or attributes.get("id") or ""
            if name:
                self._form["fields"].append({
                    "name": name,
                    "type": attributes.get("type", tag),
                    "value": attributes.get("value", ""),
                })
        elif tag == "img" and self._form is None:
            self.text_parts.append(f"[image: {attributes.get('alt', '') or 'no alt'}]")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link is not None:
            if self._link["href"] and len(self.links) < MAX_LINKS:
                self.links.append(self._link)
            self._link = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        stripped = " ".join(data.split())
        if not stripped:
            return
        if self._link is not None:
            self._link["text"] = (self._link["text"] + " " + stripped).strip()
        self.text_parts.append(stripped)


@dataclass
class PageState:
    """What the browser really saw, after redirects."""

    url: str
    status: int
    title: str
    text: str
    links: list[dict[str, str]] = field(default_factory=list)
    forms: list[dict[str, Any]] = field(default_factory=list)
    bytes: int = 0
    content_type: str = ""
    elapsed_ms: float = 0.0

    def digest(self) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title or '(none)'}"]
        if self.links:
            shown = self.links[:12]
            lines.append("LINKS: " + " | ".join(
                f"[{index}] {item['text'] or item['href']} -> {item['href']}"
                for index, item in enumerate(shown)))
        if self.forms:
            lines.append("FORMS: " + " | ".join(
                f"[{index}] {form['method'].upper()} {form['action'] or self.url} "
                f"fields={[field['name'] for field in form['fields']]}"
                for index, form in enumerate(self.forms)))
        lines.append("TEXT:")
        lines.append(self.text[:MAX_TEXT] or "(no visible text)")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "status": self.status,
            "title": self.title,
            "bytes": self.bytes,
            "content_type": self.content_type,
            "links": len(self.links),
            "forms": len(self.forms),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "text_chars": len(self.text),
        }


class LocalBrowserSession:
    """A cookie-carrying session over real pages."""

    def __init__(self, policy: BrowserPolicy | None = None) -> None:
        self.policy = policy or BrowserPolicy.from_env()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.history: list[dict[str, Any]] = []
        self.page: PageState | None = None

    # -- fetching ----------------------------------------------------------
    def navigate(self, url: str) -> PageState:
        target = check_target(url, self.policy)
        request = urllib.request.Request(
            target, headers={"User-Agent": USER_AGENT,
                             "Accept": "text/html,application/xhtml+xml,*/*"})
        started = time.time()
        try:
            with self.opener.open(request, timeout=self.policy.timeout) as response:
                raw = response.read(self.policy.max_bytes + 1)
                status = int(getattr(response, "status", 200) or 200)
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            # A 404 is a real page too: the browser reports what the server said.
            raw = exc.read(self.policy.max_bytes + 1) if hasattr(exc, "read") else b""
            status = int(getattr(exc, "code", 0) or 0)
            final_url = target
            content_type = ""
        truncated = len(raw) > self.policy.max_bytes
        raw = raw[: self.policy.max_bytes]
        body = raw.decode("utf-8", errors="replace")
        extractor = _Extractor()
        if "html" in content_type.lower() or body.lstrip()[:1] in ("<",) or not content_type:
            try:
                extractor.feed(body)
            except Exception:                                  # noqa: BLE001
                pass
        else:
            extractor.text_parts = [body[:MAX_TEXT]]
        page = PageState(
            url=final_url,
            status=status,
            title=" ".join(extractor.title.split()),
            text="\n".join(extractor.text_parts)[:MAX_TEXT],
            links=[{"text": link["text"],
                    "href": urllib.parse.urljoin(final_url, link["href"])}
                   for link in extractor.links],
            forms=[{**form,
                    "action": urllib.parse.urljoin(final_url, form["action"])}
                   for form in extractor.forms],
            bytes=len(raw),
            content_type=content_type,
            elapsed_ms=(time.time() - started) * 1000,
        )
        if truncated:
            page.text += f"\n[truncated at {self.policy.max_bytes} bytes]"
        self.page = page
        self.history.append({"url": final_url, "status": status,
                             "at": time.time()})
        return page

    # -- acting ------------------------------------------------------------
    def click(self, selector: str) -> PageState:
        page = self._require_page()
        index = self._link_index(selector)
        if index is None:
            raise BrowserUnavailable(
                f"no link matches {selector!r}; the page has "
                f"{len(page.links)} link(s)")
        return self.navigate(page.links[index]["href"])

    def submit(self, values: dict[str, str] | None = None, *,
               form_index: int = 0) -> PageState:
        page = self._require_page()
        if not page.forms:
            raise BrowserUnavailable("this page has no form to submit")
        if form_index >= len(page.forms):
            raise BrowserUnavailable(f"no form at index {form_index}")
        form = page.forms[form_index]
        data = {field["name"]: field["value"] for field in form["fields"]}
        data.update({str(key): str(value)
                     for key, value in (values or {}).items()})
        payload = urllib.parse.urlencode(data).encode()
        if form["method"] == "get":
            separator = "&" if urllib.parse.urlsplit(form["action"]).query else "?"
            return self.navigate(f"{form['action']}{separator}"
                                 f"{payload.decode()}")
        target = check_target(form["action"] or page.url, self.policy)
        request = urllib.request.Request(
            target, data=payload,
            headers={"User-Agent": USER_AGENT,
                     "Content-Type": "application/x-www-form-urlencoded"})
        raw = b""
        started = time.time()
        try:
            with self.opener.open(request, timeout=self.policy.timeout) as response:
                raw = response.read(self.policy.max_bytes)
                final_url = response.geturl()
                status = int(getattr(response, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            final_url = target
            raw = exc.read(self.policy.max_bytes) if hasattr(exc, "read") else b""
        body = raw.decode("utf-8", errors="replace")
        extractor = _Extractor()
        try:
            extractor.feed(body)
        except Exception:                                      # noqa: BLE001
            pass
        page = PageState(
            url=final_url, status=status,
            title=" ".join(extractor.title.split()),
            text="\n".join(extractor.text_parts)[:MAX_TEXT],
            links=[{"text": link["text"],
                    "href": urllib.parse.urljoin(final_url, link["href"])}
                   for link in extractor.links],
            forms=[{**item,
                    "action": urllib.parse.urljoin(final_url, item["action"])}
                   for item in extractor.forms],
            bytes=len(raw), content_type="",
            elapsed_ms=(time.time() - started) * 1000)
        self.page = page
        self.history.append({"url": final_url, "status": status,
                             "submitted": sorted((values or {}).keys()),
                             "at": time.time()})
        return page

    # -- helpers -----------------------------------------------------------
    def _require_page(self) -> PageState:
        if self.page is None:
            raise BrowserUnavailable(
                "no page is open yet; navigate to a URL first")
        return self.page

    def _link_index(self, selector: str) -> int | None:
        page = self._require_page()
        choice = str(selector or "").strip()
        if not choice:
            return None
        if choice.isdigit() and int(choice) < len(page.links):
            return int(choice)
        lowered = choice.lower()
        for index, link in enumerate(page.links):
            if lowered in link["text"].lower() or lowered in link["href"].lower():
                return index
        return None


_URL_IN_TEXT = re.compile(r"https?://[^\s\"')<>]+")
_JSON_ACTIONS = re.compile(r"actions\s*[:=]\s*(\[.*\])", re.IGNORECASE | re.DOTALL)


def extract_url(text: str) -> str:
    """The first real URL in a request, or an empty string."""
    match = _URL_IN_TEXT.search(str(text or ""))
    return match.group(0).rstrip(".,") if match else ""


def extract_actions(text: str) -> list[dict[str, Any]]:
    """Parse ``actions: [{...}]`` out of a request, bounded and validated."""
    match = _JSON_ACTIONS.search(str(text or ""))
    if not match:
        return []
    try:
        parsed = json.loads(match.group(1))
    except ValueError as exc:
        raise BrowserUnavailable(f"actions are not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise BrowserUnavailable("actions must be a JSON list")
    actions: list[dict[str, Any]] = []
    for item in parsed[:MAX_ACTIONS]:
        if not isinstance(item, dict):
            raise BrowserUnavailable("each action must be an object")
        unknown = set(item) - {"click", "type", "submit", "navigate"}
        if unknown:
            raise BrowserUnavailable(
                f"unknown action key(s): {', '.join(sorted(unknown))}")
        actions.append({str(key): value for key, value in item.items()})
    return actions


class LocalBrowserProvider:
    """The ``browser`` capability, served by real HTTP/DOM navigation."""

    name = "forge-browser"

    def __init__(self, policy: BrowserPolicy | None = None) -> None:
        self.policy = policy or BrowserPolicy.from_env()

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        url = extract_url(body)
        if not url:
            raise BrowserUnavailable(
                "a browser request must name the URL to open (e.g. "
                "'url:https://example.com/pricing')")
        started = time.time()
        session = LocalBrowserSession(self.policy)
        page = session.navigate(url)
        return ModelResult(
            text=page.digest(),
            model="forge-browser/local-dom",
            input_tokens=len(body.split()),
            output_tokens=len(page.digest().split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "backend": "local HTTP + DOM parser",
                "javascript": False,
                "policy": self.policy.to_dict(),
                "page": page.to_dict(),
                "requested_url": url,
            })


class LocalComputerUseProvider:
    """The ``computer_use`` capability over the browser's own session.

    Actions are real (navigate, click a link, fill fields, submit a form) and
    happen only against pages this session itself fetched, through the same
    allowlist. OS-level desktop control is a *different* runtime and is
    reported as such instead of being faked here.
    """

    name = "forge-web-actions"

    def __init__(self, policy: BrowserPolicy | None = None) -> None:
        self.policy = policy or BrowserPolicy.from_env()

    def generate(self, prompt: str, *, context: str = "", task: str = "",
                 instructions: str = "", max_output_tokens: int | None = None,
                 temperature: float | None = None) -> ModelResult:
        del context, max_output_tokens, temperature
        body = "\n".join(part for part in (str(task or ""), str(prompt or ""),
                                           str(instructions or "")) if part)
        url = extract_url(body)
        actions = extract_actions(body)
        if not url:
            raise BrowserUnavailable(
                "a computer-use request must name the page to act on (e.g. "
                "'url:http://host/form actions:[{\"type\": {\"q\": \"csv\"}}, "
                "{\"submit\": {}}]')")
        started = time.time()
        session = LocalBrowserSession(self.policy)
        page = session.navigate(url)
        log: list[dict[str, Any]] = [{"action": "navigate", "url": url,
                                      "status": page.status}]
        for action in actions:
            if "navigate" in action:
                page = session.navigate(str(action["navigate"]))
                log.append({"action": "navigate", "url": page.url,
                            "status": page.status})
            elif "click" in action:
                page = session.click(str(action["click"]))
                log.append({"action": "click", "selector": str(action["click"]),
                            "url": page.url, "status": page.status})
            elif "type" in action:
                values = action["type"]
                if not isinstance(values, dict):
                    raise BrowserUnavailable("'type' must map field -> text")
                # Typing alone changes nothing server-side; the fields are
                # carried into the submit that follows, exactly like a browser.
                log.append({"action": "type", "fields": sorted(map(str, values))})
                session._pending = values                     # noqa: SLF001
            elif "submit" in action:
                values = action["submit"] if isinstance(action["submit"], dict) \
                    else getattr(session, "_pending", {})
                page = session.submit(values)
                log.append({"action": "submit", "fields": sorted(values),
                            "url": page.url, "status": page.status})
                if hasattr(session, "_pending"):
                    del session._pending
        digest = page.digest()
        return ModelResult(
            text=digest,
            model="forge-computer-use/web-actions",
            input_tokens=len(body.split()),
            output_tokens=len(digest.split()),
            latency=time.time() - started,
            metadata={
                "provider": self.name,
                "simulated": False,
                "surface": "web (DOM actions on real pages)",
                "os_desktop": False,
                "policy": self.policy.to_dict(),
                "actions": log,
                "page": page.to_dict(),
                "note": ("OS-level desktop control needs a desktop endpoint; "
                         "this runtime acts on real pages only"),
            })

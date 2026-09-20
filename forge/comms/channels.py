"""Outbound channels: WhatsApp, SMS and voice calls — on your own credentials.

Forge already recognises the intents (``send_whatsapp``, ``send_email``,
``make_call``); what was missing was any way to actually deliver them. This
module adds real adapters with an honest configuration model:

* a channel is **registered only when its configuration is complete**, so the
  registry can never claim a channel that cannot send;
* an unconfigured channel is reported with the exact environment variables it
  needs (``requirements()``), which is what the readiness report and the API
  surface quote;
* a send returns a structured :class:`SendResult` carrying the provider's own
  status code and message id — never a bare ``True``;
* nothing here reads or logs a credential, and no credential is ever written to
  a result.

Providers, all plain HTTPS with urllib:

``WhatsAppCloudChannel``  Meta WhatsApp Business Cloud API
                          (``graph.facebook.com``, bearer token, template- or
                          text-message send).
``TwilioSmsChannel``      Twilio Messages API (Basic auth).
``TwilioVoiceChannel``    Twilio Calls API — a real outbound phone call.
"""
from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

REQUEST_TIMEOUT = 30.0


@dataclass(frozen=True)
class SendResult:
    """What a provider actually answered."""

    ok: bool
    channel: str
    status: int = 0
    provider_id: str = ""
    error: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "channel": self.channel,
            "status": self.status,
            "provider_id": self.provider_id,
            "error": self.error,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ChannelStatus:
    """Whether one channel can send, and what is missing if it cannot."""

    name: str
    configured: bool
    requirements: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "configured": self.configured,
            "requirements": list(self.requirements),
        }


def _post_json(url: str, payload: Mapping[str, Any], headers: Mapping[str, str],
               *, timeout: float = REQUEST_TIMEOUT) -> tuple[int, dict]:
    body = json.dumps(dict(payload)).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **dict(headers)})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            return response.status, _parse(raw)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")
        except Exception:                                     # noqa: BLE001
            pass
        return exc.code, _parse(detail)


def _parse(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        return {"raw": raw[:500]}
    return data if isinstance(data, dict) else {"raw": raw[:500]}


def _provider_error(data: Mapping[str, Any]) -> str:
    error = data.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or error.get("code") or error)[:300]
    if error:
        return str(error)[:300]
    return str(data.get("message") or "")[:300]


class WhatsAppCloudChannel:
    """Meta WhatsApp Business Cloud API."""

    name = "whatsapp-cloud"
    REQUIRED = ("FORGE_WHATSAPP_TOKEN", "FORGE_WHATSAPP_PHONE_ID")

    def __init__(self, token: str = "", phone_id: str = "",
                 to: str = "", version: str = "",
                 base_url: str = "") -> None:
        env = os.environ
        self.token = token or env.get("FORGE_WHATSAPP_TOKEN", "")
        self.phone_id = phone_id or env.get("FORGE_WHATSAPP_PHONE_ID", "")
        self.default_to = to or env.get("FORGE_WHATSAPP_TO", "")
        self.version = version or env.get("FORGE_WHATSAPP_API_VERSION",
                                          "v21.0")
        self.base_url = base_url or env.get("FORGE_WHATSAPP_BASE_URL",
                                            "https://graph.facebook.com")

    def configured(self) -> bool:
        return bool(self.token and self.phone_id)

    def status(self) -> ChannelStatus:
        missing = tuple(name for name in self.REQUIRED
                        if not os.environ.get(name, "").strip()
                        and not (name == "FORGE_WHATSAPP_TOKEN" and self.token)
                        and not (name == "FORGE_WHATSAPP_PHONE_ID" and self.phone_id))
        return ChannelStatus(self.name, self.configured(), missing)

    def send(self, text: str, *, to: str = "") -> SendResult:
        recipient = str(to or self.default_to).strip()
        if not self.configured():
            return SendResult(False, self.name, error="not_configured",
                              detail="missing " + ", ".join(self.REQUIRED))
        if not recipient:
            return SendResult(False, self.name, error="no_recipient",
                              detail="pass a recipient or set FORGE_WHATSAPP_TO")
        url = f"{self.base_url}/{self.version}/{self.phone_id}/messages"
        status, data = _post_json(url, {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": str(text)[:4096]},
        }, {"Authorization": f"Bearer {self.token}"})
        ok = 200 <= status < 300 and not _provider_error(data)
        message_id = ""
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            message_id = str(messages[0].get("id") or "")
        return SendResult(ok, self.name, status=status, provider_id=message_id,
                          error="" if ok else "provider_error",
                          detail=_provider_error(data))


class TwilioSmsChannel:
    """Twilio Messages API."""

    name = "twilio-sms"
    REQUIRED = ("FORGE_TWILIO_ACCOUNT_SID", "FORGE_TWILIO_AUTH_TOKEN",
                "FORGE_TWILIO_FROM")

    def __init__(self, account_sid: str = "", auth_token: str = "",
                 sender: str = "", base_url: str = "") -> None:
        env = os.environ
        self.account_sid = account_sid or env.get("FORGE_TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or env.get("FORGE_TWILIO_AUTH_TOKEN", "")
        self.sender = sender or env.get("FORGE_TWILIO_FROM", "")
        self.base_url = base_url or env.get("FORGE_TWILIO_BASE_URL",
                                            "https://api.twilio.com")

    def configured(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.sender)

    def status(self) -> ChannelStatus:
        present = {
            "FORGE_TWILIO_ACCOUNT_SID": self.account_sid,
            "FORGE_TWILIO_AUTH_TOKEN": self.auth_token,
            "FORGE_TWILIO_FROM": self.sender,
        }
        missing = tuple(name for name, value in present.items() if not value)
        return ChannelStatus(self.name, self.configured(), missing)

    def _headers(self) -> dict[str, str]:
        raw = f"{self.account_sid}:{self.auth_token}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def send(self, text: str, *, to: str = "") -> SendResult:
        recipient = str(to).strip()
        if not self.configured():
            return SendResult(False, self.name, error="not_configured",
                              detail="missing " + ", ".join(self.REQUIRED))
        if not recipient:
            return SendResult(False, self.name, error="no_recipient",
                              detail="pass a recipient number")
        url = f"{self.base_url}/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        body = urllib.parse.urlencode({
            "From": self.sender, "To": recipient, "Body": str(text)[:1600],
        }).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={**self._headers(),
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = _parse(response.read().decode("utf-8", "replace"))
                return SendResult(True, self.name, status=response.status,
                                  provider_id=str(data.get("sid") or ""),
                                  detail=str(data.get("status") or ""))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:                                 # noqa: BLE001
                pass
            data = _parse(detail)
            return SendResult(False, self.name, status=exc.code,
                              error="provider_error",
                              detail=_provider_error(data) or detail[:300])
        except Exception as exc:                              # noqa: BLE001
            return SendResult(False, self.name, error="transport_error",
                              detail=str(exc)[:300])


class TwilioVoiceChannel(TwilioSmsChannel):
    """Outbound phone call with a spoken message (Twilio Calls API)."""

    name = "twilio-voice"

    def call(self, message: str, *, to: str = "",
             voice: str = "alice", language: str = "en-IN") -> SendResult:
        recipient = str(to).strip()
        if not self.configured():
            return SendResult(False, self.name, error="not_configured",
                              detail="missing " + ", ".join(self.REQUIRED))
        if not recipient:
            return SendResult(False, self.name, error="no_recipient",
                              detail="pass a recipient number")
        # Spoken via TwiML, so no external audio hosting is required.
        escaped = (str(message)[:1400].replace("&", "&amp;")
                   .replace("<", "&lt;").replace(">", "&gt;"))
        twiml = (f'<Response><Say voice="{voice}" language="{language}">'
                 f"{escaped}</Say></Response>")
        url = f"{self.base_url}/2010-04-01/Accounts/{self.account_sid}/Calls.json"
        body = urllib.parse.urlencode({
            "From": self.sender, "To": recipient, "Twiml": twiml,
        }).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, method="POST",
            headers={**self._headers(),
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                data = _parse(response.read().decode("utf-8", "replace"))
                return SendResult(True, self.name, status=response.status,
                                  provider_id=str(data.get("sid") or ""),
                                  detail=str(data.get("status") or ""))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:                                 # noqa: BLE001
                pass
            data = _parse(detail)
            return SendResult(False, self.name, status=exc.code,
                              error="provider_error",
                              detail=_provider_error(data) or detail[:300])
        except Exception as exc:                              # noqa: BLE001
            return SendResult(False, self.name, error="transport_error",
                              detail=str(exc)[:300])


class SmtpEmailChannel:
    """Outbound email over SMTP (STARTTLS, implicit TLS, or a local relay)."""

    name = "smtp-email"
    REQUIRED = ("FORGE_SMTP_HOST", "FORGE_SMTP_FROM")

    def __init__(self, host: str = "", port: int = 0, username: str = "",
                 password: str = "", sender: str = "",
                 starttls: bool | None = None, implicit_tls: bool = False,
                 timeout: float = REQUEST_TIMEOUT) -> None:
        env = os.environ
        self.host = host or env.get("FORGE_SMTP_HOST", "")
        self.sender = sender or env.get("FORGE_SMTP_FROM", "")
        self.username = username or env.get("FORGE_SMTP_USER", "")
        self.password = password or env.get("FORGE_SMTP_PASSWORD", "")
        self.implicit_tls = implicit_tls or _truthy(env.get("FORGE_SMTP_SSL"))
        raw_port = port or int(env.get("FORGE_SMTP_PORT", "0") or 0)
        if raw_port:
            self.port = int(raw_port)
        elif self.implicit_tls:
            self.port = 465
        else:
            self.port = 587
        if starttls is None:
            explicit = env.get("FORGE_SMTP_STARTTLS", "")
            starttls = (_truthy(explicit) if explicit
                        else self.port in (587, 25, 2525) and not self.implicit_tls)
        self.starttls = bool(starttls) and not self.implicit_tls
        self.timeout = float(timeout)

    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def status(self) -> ChannelStatus:
        missing = []
        if not self.host:
            missing.append("FORGE_SMTP_HOST")
        if not self.sender:
            missing.append("FORGE_SMTP_FROM")
        return ChannelStatus(self.name, self.configured(), tuple(missing))

    def _connect(self):
        import smtplib

        if self.implicit_tls:
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            if self.starttls:
                client.starttls()
        return client

    def send(self, text: str, *, to: str = "",
             subject: str = "Forge notification") -> SendResult:
        import smtplib
        from email.message import EmailMessage

        recipient = str(to).strip()
        if not self.configured():
            return SendResult(False, self.name, error="not_configured",
                              detail="missing " + ", ".join(self.REQUIRED))
        if not recipient:
            return SendResult(False, self.name, error="no_recipient",
                              detail="pass a recipient address")
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = str(subject or "Forge notification")[:200]
        message.set_content(str(text)[:100_000])
        try:
            with self._connect() as client:
                if self.username:
                    #: Kept separate from the send: an authentication failure is
                    #: a credential problem the operator must fix, and reporting
                    #: it as a transport error would send them after the wrong
                    #: fault.
                    try:
                        client.login(self.username, self.password)
                    except (smtplib.SMTPException, OSError) as exc:
                        code = int(getattr(exc, "smtp_code", 0) or 0)
                        detail = str(getattr(exc, "smtp_error", "") or exc)[:300]
                        #: No AUTH support is also a credential-path failure:
                        #: the configured username cannot be used by this server.
                        kind = ("auth_failed" if "auth" in str(exc).lower()
                                or "auth" in detail.lower()
                                or not getattr(exc, "smtp_code", None)
                                else "provider_error")
                        return SendResult(False, self.name, status=code,
                                          error=kind, detail=detail)
                refused = client.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            return SendResult(False, self.name, status=int(exc.smtp_code or 0),
                              error="auth_failed",
                              detail=str(exc.smtp_error or exc)[:300])
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(False, self.name, error="recipient_refused",
                              detail=str(exc.recipients)[:300])
        except (smtplib.SMTPException, OSError) as exc:
            return SendResult(False, self.name, error="transport_error",
                              detail=str(exc)[:300])
        #: ``send_message`` returns only the *refused* recipients; an empty
        #: mapping means the server accepted the message for delivery. The
        #: response code is not exposed by smtplib, so it is reported as 0
        #: rather than invented.
        return SendResult(not refused, self.name, status=0,
                          detail=(f"accepted by {self.host} for {recipient}"
                                  if not refused
                                  else f"refused: {sorted(refused)}"))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _all_channels() -> tuple[Any, ...]:
    """Every outbound channel Forge knows how to speak (configured or not)."""
    return (WhatsAppCloudChannel(), TwilioSmsChannel(), TwilioVoiceChannel(),
            SmtpEmailChannel())


@dataclass
class ChannelRegistry:
    """The outbound channels this deployment actually has."""

    channels: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ChannelRegistry":
        """Register only the channels whose configuration is complete.

        A half-configured channel is *not* registered: it would be a route that
        always fails, and reporting it as available would be a lie. Its
        requirements are still reportable through :meth:`status`.
        """
        if env is not None:
            # Scoped configuration (tests, per-tenant deployments): read the
            # mapping instead of the process environment.
            previous = dict(os.environ)

            def restore() -> None:
                os.environ.clear()
                os.environ.update(previous)

            os.environ.update({str(k): str(v) for k, v in env.items()})
            try:
                registry = cls()
                for channel in (_all_channels()):
                    if channel.configured():
                        registry.channels[channel.name] = channel
                registry._statuses = {channel.name: channel.status()
                                      for channel in _all_channels()}
                return registry
            finally:
                restore()
        registry = cls()
        for channel in _all_channels():
            if channel.configured():
                registry.channels[channel.name] = channel
            registry._statuses[channel.name] = channel.status()
        return registry

    def __post_init__(self) -> None:
        if not hasattr(self, "_statuses"):
            self._statuses: dict[str, ChannelStatus] = {}

    def has(self, name: str) -> bool:
        return name in self.channels

    def send_whatsapp(self, text: str, *, to: str = "") -> SendResult:
        channel = self.channels.get("whatsapp-cloud")
        if channel is None:
            return SendResult(False, "whatsapp-cloud", error="not_configured",
                              detail="configure " + ", ".join(
                                  WhatsAppCloudChannel.REQUIRED))
        return channel.send(text, to=to)

    def send_sms(self, text: str, *, to: str = "") -> SendResult:
        channel = self.channels.get("twilio-sms")
        if channel is None:
            return SendResult(False, "twilio-sms", error="not_configured",
                              detail="configure " + ", ".join(
                                  TwilioSmsChannel.REQUIRED))
        return channel.send(text, to=to)

    def send_email(self, text: str, *, to: str = "",
                   subject: str = "Forge notification") -> SendResult:
        channel = self.channels.get("smtp-email")
        if channel is None:
            return SendResult(False, "smtp-email", error="not_configured",
                              detail="configure " + ", ".join(
                                  SmtpEmailChannel.REQUIRED))
        return channel.send(text, to=to, subject=subject)

    def make_call(self, message: str, *, to: str = "") -> SendResult:
        channel = self.channels.get("twilio-voice")
        if channel is None:
            return SendResult(False, "twilio-voice", error="not_configured",
                              detail="configure " + ", ".join(
                                  TwilioVoiceChannel.REQUIRED))
        return channel.call(message, to=to)

    def status(self) -> dict[str, Any]:
        """Honest channel state for the API/readiness surfaces."""
        rows = [status.to_dict() for status in
                sorted(self._statuses.values(), key=lambda item: item.name)]
        return {
            "channels": rows,
            "configured": [name for name in sorted(self.channels)],
            "available": bool(self.channels),
            "note": ("outbound channels are registered only when their "
                     "credentials are present; an unconfigured channel lists "
                     "the exact variables it needs and sends nothing"),
        }

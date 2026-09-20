"""Outbound channels: real provider calls, honest configuration reporting.

WhatsApp Cloud, Twilio SMS and Twilio voice calls are plain HTTPS integrations.
These tests drive them against real HTTP servers that stand in for the
providers, assert the exact request shape each provider requires, and pin the
honesty rules: a half-configured channel is not registered, an unconfigured
send says what is missing, and a provider error is reported with its status and
message instead of a bare success.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from forge.comms import (
    ChannelRegistry,
    TwilioSmsChannel,
    TwilioVoiceChannel,
    WhatsAppCloudChannel,
)


class _Provider:
    """A real HTTP server standing in for a provider API."""

    def __init__(self, *, status: int = 200, body: dict | None = None):
        self.status = status
        self.body = body if body is not None else {}
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode() if length else ""
                content_type = self.headers.get("Content-Type", "")
                if "json" in content_type:
                    payload = json.loads(raw or "{}")
                    outer.form = None
                else:
                    payload = None
                    outer.form = parse_qs(raw)
                outer.requests.append({
                    "path": self.path,
                    "payload": payload,
                    "form": outer.form,
                    "headers": dict(self.headers),
                })
                data = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                return

        outer = self
        self.form = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# -- WhatsApp ------------------------------------------------------------------

def test_whatsapp_sends_the_cloud_api_payload_and_reports_the_message_id():
    provider = _Provider(body={"messages": [{"id": "wamid.TEST123"}]})
    try:
        channel = WhatsAppCloudChannel(token="token-abc", phone_id="123456",
                                       to="919876543210",
                                       base_url=provider.url)
        assert channel.configured() is True
        result = channel.send("deployment finished")

        request = provider.requests[-1]
        assert request["path"] == "/v21.0/123456/messages"
        assert request["headers"]["Authorization"] == "Bearer token-abc"
        assert request["payload"] == {
            "messaging_product": "whatsapp", "recipient_type": "individual",
            "to": "919876543210", "type": "text",
            "text": {"preview_url": False, "body": "deployment finished"},
        }
        assert result.ok is True
        assert result.provider_id == "wamid.TEST123"
        assert result.status == 200
    finally:
        provider.close()


def test_whatsapp_reports_a_provider_error_with_its_own_message():
    provider = _Provider(status=401, body={"error": {
        "message": "Invalid OAuth access token", "code": 190}})
    try:
        channel = WhatsAppCloudChannel(token="bad", phone_id="1", to="9",
                                       base_url=provider.url)
        result = channel.send("hello")
        assert result.ok is False
        assert result.status == 401
        assert "Invalid OAuth access token" in result.detail
        assert result.provider_id == ""
    finally:
        provider.close()


def test_a_half_configured_channel_is_not_registered_and_lists_requirements():
    registry = ChannelRegistry.from_env({})
    status = registry.status()
    assert status["available"] is False
    assert status["configured"] == []
    names = {row["name"]: row for row in status["channels"]}
    assert names["whatsapp-cloud"]["configured"] is False
    assert "FORGE_WHATSAPP_TOKEN" in names["whatsapp-cloud"]["requirements"]
    assert "FORGE_TWILIO_AUTH_TOKEN" in names["twilio-sms"]["requirements"]

    # An unconfigured send is refused with the requirement, never attempted.
    refused = registry.send_whatsapp("hello")
    assert refused.ok is False and refused.error == "not_configured"
    assert "FORGE_WHATSAPP_TOKEN" in refused.detail


def test_only_complete_configurations_register_channels():
    registry = ChannelRegistry.from_env({
        "FORGE_WHATSAPP_TOKEN": "t",           # phone id missing -> not usable
        "FORGE_TWILIO_ACCOUNT_SID": "AC1",
        "FORGE_TWILIO_AUTH_TOKEN": "secret",
        "FORGE_TWILIO_FROM": "+15550000000",
    })
    assert registry.status()["configured"] == ["twilio-sms", "twilio-voice"]
    assert registry.has("whatsapp-cloud") is False
    assert registry.has("twilio-sms") is True


# -- Twilio SMS ----------------------------------------------------------------

def test_twilio_sms_uses_basic_auth_and_form_encoding():
    provider = _Provider(body={"sid": "SM123", "status": "queued"})
    try:
        channel = TwilioSmsChannel(account_sid="AC42", auth_token="tok",
                                   sender="+15551110000",
                                   base_url=provider.url)
        result = channel.send("build passed", to="+919876543210")

        request = provider.requests[-1]
        assert request["path"] == "/2010-04-01/Accounts/AC42/Messages.json"
        assert request["headers"]["Authorization"].startswith("Basic ")
        assert request["form"]["To"] == ["+919876543210"]
        assert request["form"]["From"] == ["+15551110000"]
        assert request["form"]["Body"] == ["build passed"]
        assert result.ok is True and result.provider_id == "SM123"
    finally:
        provider.close()


def test_twilio_sms_failure_carries_the_provider_message():
    provider = _Provider(status=400, body={"message": "unverified number"})
    try:
        channel = TwilioSmsChannel(account_sid="AC42", auth_token="tok",
                                   sender="+1", base_url=provider.url)
        result = channel.send("hi", to="+1")
        assert result.ok is False and result.status == 400
        assert "unverified number" in result.detail
    finally:
        provider.close()


# -- Twilio voice --------------------------------------------------------------

def test_a_call_is_placed_with_spoken_twiml():
    provider = _Provider(body={"sid": "CA99", "status": "queued"})
    try:
        channel = TwilioVoiceChannel(account_sid="AC42", auth_token="tok",
                                     sender="+15551110000",
                                     base_url=provider.url)
        result = channel.call("Deployment finished & healthy", to="+919876543210",
                              language="hi-IN")

        request = provider.requests[-1]
        assert request["path"] == "/2010-04-01/Accounts/AC42/Calls.json"
        twiml = request["form"]["Twiml"][0]
        assert twiml.startswith("<Response><Say")
        assert 'language="hi-IN"' in twiml
        # The message is XML-escaped: a spoken "&" must not break the document.
        assert "&amp;" in twiml and "Deployment finished" in twiml
        assert result.ok is True and result.provider_id == "CA99"
    finally:
        provider.close()


# -- registry dispatch ---------------------------------------------------------

def test_registry_dispatches_to_the_configured_channel(monkeypatch):
    provider = _Provider(body={"sid": "SM777", "status": "queued"})
    try:
        monkeypatch.setenv("FORGE_TWILIO_ACCOUNT_SID", "AC7")
        monkeypatch.setenv("FORGE_TWILIO_AUTH_TOKEN", "tok")
        monkeypatch.setenv("FORGE_TWILIO_FROM", "+15550001111")
        monkeypatch.setenv("FORGE_TWILIO_BASE_URL", provider.url)
        registry = ChannelRegistry.from_env()
        assert registry.has("twilio-sms")

        result = registry.send_sms("status ok", to="+919000000000")
        assert result.ok is True and result.provider_id == "SM777"
        # A channel that is not configured still answers honestly.
        assert registry.send_whatsapp("hello").error == "not_configured"
    finally:
        provider.close()


# -- control plane wiring ------------------------------------------------------

def test_an_approved_voice_intent_delivers_through_a_configured_channel(
        monkeypatch, tmp_path):
    """The intent path sends for real, and says so only when it did."""
    from helpers_a34 import make_plane

    provider = _Provider(body={"messages": [{"id": "wamid.VOICE1"}]})
    try:
        monkeypatch.setenv("FORGE_WHATSAPP_TOKEN", "tok")
        monkeypatch.setenv("FORGE_WHATSAPP_PHONE_ID", "123")
        monkeypatch.setenv("FORGE_WHATSAPP_TO", "919000000000")
        monkeypatch.setenv("FORGE_WHATSAPP_BASE_URL", provider.url)

        plane = make_plane(tmp_path, start=False)   # cached registry, real HTTP
        delivered = plane._deliver_voice_message(
            "send_whatsapp", {"text": "stand up finished", "to": "919000000000"})
        assert delivered["kind"] == "sent"
        assert delivered["result"]["provider_id"] == "wamid.VOICE1"
        assert provider.requests[-1]["payload"]["text"]["body"] == \
            "stand up finished"

        # Without a message body nothing is invented; the intent falls
        # through to the normal task path.
        assert plane._deliver_voice_message("send_whatsapp", {}) is None
        # An intent that has no channel configured falls through too.
        assert plane._deliver_voice_message("make_call",
                                            {"text": "call now"}) is None
    finally:
        provider.close()


def test_the_channels_endpoint_is_read_only_and_honest():
    from forge.api.app import served_route_paths
    from forge.api import routes_channels

    paths = {route.path for route in routes_channels.router.routes}
    assert paths == {"/channels"}
    assert all("POST" not in route.methods for route in routes_channels.router.routes)
    del served_route_paths          # imported so a rename breaks this test loudly

    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("FORGE_WHATSAPP_TOKEN", raising=False)
        patch.delenv("FORGE_TWILIO_ACCOUNT_SID", raising=False)
        status = ChannelRegistry.from_env().status()
        assert status["available"] is False
        assert len(status["channels"]) == 4

# -- SMTP email ----------------------------------------------------------------

def _address(argument: str) -> str:
    """The mailbox in a MAIL FROM/RCPT TO argument (esmtp params dropped)."""
    text = argument.strip()
    if "<" in text and ">" in text:
        return text[text.index("<") + 1:text.index(">")]
    return text.split()[0] if text.split() else ""


class _SmtpSink:
    """A real SMTP server: socket, greeting, EHLO/AUTH/MAIL/RCPT/DATA."""

    def __init__(self, *, auth: bool = True) -> None:
        import socket

        self.auth = auth
        self.messages: list[dict] = []
        self.commands: list[str] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.stopped = False
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return self.sock.getsockname()[1]

    def _serve(self):
        while not self.stopped:
            try:
                self.sock.settimeout(5)
                conn, _ = self.sock.accept()
            except OSError:
                return
            self._session(conn)

    def _session(self, conn):
        stream = conn.makefile("rwb")
        ended = False

        def reply(line: bytes):
            stream.write(line + b"\r\n")
            stream.flush()

        reply(b"220 forge-test ESMTP ready")
        in_data = False
        buffer: list[str] = []
        recipients: list[str] = []
        sender = ""
        while not ended:
            raw = stream.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            self.commands.append(text)
            upper = text.upper()
            if in_data:
                if text == ".":
                    in_data = False
                    self.messages.append({"from": sender, "to": list(recipients),
                                          "body": "\n".join(buffer)})
                    buffer.clear()
                    reply(b"250 2.0.0 queued")
                else:
                    buffer.append(text)
                continue
            if upper.startswith("EHLO") or upper.startswith("HELO"):
                if self.auth:
                    reply(b"250-forge-test")
                    reply(b"250-AUTH PLAIN LOGIN")
                    reply(b"250 SIZE 10485760")
                else:
                    reply(b"250 forge-test")
            elif upper.startswith("AUTH"):
                if self.auth:
                    reply(b"235 2.7.0 authentication successful")
                else:
                    reply(b"535 5.7.8 authentication rejected")
            elif upper.startswith("MAIL FROM"):
                sender = _address(text.split(":", 1)[1])
                recipients = []
                reply(b"250 2.1.0 sender ok")
            elif upper.startswith("RCPT TO"):
                recipients.append(_address(text.split(":", 1)[1]))
                reply(b"250 2.1.5 recipient ok")
            elif upper.startswith("DATA"):
                in_data = True
                reply(b"354 end data with <CR><LF>.<CR><LF>")
            elif upper.startswith("QUIT"):
                reply(b"221 2.0.0 bye")
                ended = True
            else:
                reply(b"250 2.0.0 ok")
        conn.close()

    def close(self):
        self.stopped = True
        try:
            self.sock.close()
        except OSError:
            pass


def test_smtp_email_is_really_sent_over_the_wire(monkeypatch):
    from forge.comms import SmtpEmailChannel

    sink = _SmtpSink(auth=True)
    try:
        monkeypatch.setenv("FORGE_SMTP_HOST", sink.host)
        monkeypatch.setenv("FORGE_SMTP_PORT", str(sink.port))
        monkeypatch.setenv("FORGE_SMTP_FROM", "forge@example.com")
        monkeypatch.setenv("FORGE_SMTP_USER", "forge")
        monkeypatch.setenv("FORGE_SMTP_PASSWORD", "secret")
        monkeypatch.setenv("FORGE_SMTP_STARTTLS", "0")
        channel = SmtpEmailChannel()
        assert channel.configured() is True

        result = channel.send("Deployment finished on the G560.",
                              to="operator@example.com",
                              subject="Forge: deploy done")

        assert result.ok is True, result.to_dict()
        assert sink.messages, "the SMTP server received no message"
        message = sink.messages[-1]
        assert message["from"] == "forge@example.com"
        assert message["to"] == ["operator@example.com"]
        assert "Subject: Forge: deploy done" in message["body"]
        assert "Deployment finished on the G560." in message["body"]
        # The credentials were used in a real AUTH exchange.
        assert any(command.upper().startswith("AUTH") for command in sink.commands)
    finally:
        sink.close()


def test_smtp_auth_failure_is_reported(monkeypatch):
    from forge.comms import SmtpEmailChannel

    sink = _SmtpSink(auth=False)
    try:
        monkeypatch.setenv("FORGE_SMTP_HOST", sink.host)
        monkeypatch.setenv("FORGE_SMTP_PORT", str(sink.port))
        monkeypatch.setenv("FORGE_SMTP_FROM", "forge@example.com")
        monkeypatch.setenv("FORGE_SMTP_USER", "forge")
        monkeypatch.setenv("FORGE_SMTP_PASSWORD", "wrong")
        monkeypatch.setenv("FORGE_SMTP_STARTTLS", "0")
        result = SmtpEmailChannel().send("hello", to="operator@example.com")
        assert result.ok is False
        assert result.error == "auth_failed"
        assert sink.messages == []
    finally:
        sink.close()


def test_a_half_configured_email_channel_lists_what_it_needs():
    from forge.comms import SmtpEmailChannel

    status = SmtpEmailChannel(host="mail.example.com").status()
    assert status.configured is False
    assert status.requirements == ("FORGE_SMTP_FROM",)
    refusal = ChannelRegistry.from_env({}).send_email("hello")
    assert refusal.error == "not_configured"
    assert "FORGE_SMTP_HOST" in refusal.detail

def test_an_approved_voice_intent_sends_real_email(monkeypatch, tmp_path):
    """The `send_email` intent delivers over SMTP, or falls through honestly."""
    from helpers_a34 import make_plane

    sink = _SmtpSink(auth=True)
    try:
        monkeypatch.setenv("FORGE_SMTP_HOST", sink.host)
        monkeypatch.setenv("FORGE_SMTP_PORT", str(sink.port))
        monkeypatch.setenv("FORGE_SMTP_FROM", "forge@example.com")
        monkeypatch.setenv("FORGE_SMTP_STARTTLS", "0")

        plane = make_plane(tmp_path, start=False)
        delivered = plane._deliver_voice_message("send_email", {
            "text": "Nightly build is green.", "to": "operator@example.com",
            "subject": "Forge status"})

        assert delivered["kind"] == "sent", delivered
        message = sink.messages[-1]
        assert message["to"] == ["operator@example.com"]
        assert "Subject: Forge status" in message["body"]
        assert "Nightly build is green." in message["body"]

        # A missing recipient is reported as a refusal, never as a send: the
        # provider's own error is surfaced instead of inventing an address.
        refused = plane._deliver_voice_message("send_email", {"text": "hi"})
        assert refused["kind"] == "send_failed"
        assert refused["result"]["error"] == "no_recipient"
        # With nothing to say there is no message at all: the intent falls
        # through to the normal task path.
        assert plane._deliver_voice_message("send_email", {}) is None
    finally:
        sink.close()

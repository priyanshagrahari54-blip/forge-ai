"""Outbound communication channels (WhatsApp, SMS, voice calls).

The channels are real HTTP integrations with the providers' own APIs. They are
registered only when fully configured, they report the exact variables they
need when they are not, and a send always returns the provider's own status —
never a bare success.
"""
from forge.comms.channels import (
    ChannelRegistry,
    SmtpEmailChannel,
    ChannelStatus,
    SendResult,
    TwilioSmsChannel,
    TwilioVoiceChannel,
    WhatsAppCloudChannel,
)

__all__ = [
    "ChannelRegistry",
    "ChannelStatus",
    "SendResult",
    "SmtpEmailChannel",
    "TwilioSmsChannel",
    "TwilioVoiceChannel",
    "WhatsAppCloudChannel",
]

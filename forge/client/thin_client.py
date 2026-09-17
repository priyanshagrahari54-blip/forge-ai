"""Thin-client profile for low-resource Forge endpoints such as old laptops.

The profile never pretends local hardware can perform remote workloads. It
controls UI polling, payload sizes and local feature use while keeping the
server as the execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ThinClientProfile:
    name: str = "thin-client"
    max_ui_payload_bytes: int = 256_000
    event_batch_size: int = 25
    event_poll_seconds: float = 2.0
    image_preview_max_pixels: int = 1_000_000
    local_compute_enabled: bool = False
    heavy_media_local: bool = False
    remote_execution_preferred: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "max_ui_payload_bytes": self.max_ui_payload_bytes,
            "event_batch_size": self.event_batch_size,
            "event_poll_seconds": self.event_poll_seconds,
            "image_preview_max_pixels": self.image_preview_max_pixels,
            "local_compute_enabled": self.local_compute_enabled,
            "heavy_media_local": self.heavy_media_local,
            "remote_execution_preferred": self.remote_execution_preferred,
        }


G560_PROFILE = ThinClientProfile(name="lenovo-g560-2gb", event_batch_size=15,
                                 event_poll_seconds=3.0,
                                 image_preview_max_pixels=600_000)


def select_profile(*, ram_mb: int = 0, cpu_threads: int = 0,
                   force_thin: bool = False) -> ThinClientProfile:
    """Select a conservative browser/client profile from hardware facts."""
    if force_thin or (ram_mb and ram_mb <= 3072) or (cpu_threads and cpu_threads <= 2):
        return G560_PROFILE
    return ThinClientProfile()

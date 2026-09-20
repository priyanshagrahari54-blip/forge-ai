"""Error types for the Model Fabric.

A small, stable hierarchy so callers can branch on *why* a model request failed
without depending on provider-specific exception text.
"""
from __future__ import annotations


class FabricError(RuntimeError):
    """Base class for all Model Fabric errors."""


class ModelUnavailableError(FabricError):
    """No usable model is registered or available for the request."""


class CapabilityNotSupportedError(FabricError):
    """No registered model advertises the required capability."""


class ProviderError(FabricError):
    """A provider failed to produce a result."""


class ConfigurationError(FabricError):
    """Model Fabric configuration is invalid."""


class ProviderExhaustedError(ProviderError):
    """A provider refused work because its quota or rate limit is spent.

    Raised only when the provider has no healthy endpoint left to try, so a
    caller can fail over to another provider instead of retrying a wall. The
    caller-supplied ``retry_after`` (seconds) is whatever the endpoint stated,
    or ``None`` when it stated nothing.
    """

    def __init__(self, message: str, *, provider: str = "", model: str = "",
                 retry_after: float | None = None,
                 status: int | None = None) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.retry_after = retry_after
        self.status = status

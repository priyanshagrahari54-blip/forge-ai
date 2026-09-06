import pytest

from forge.models.errors import (
    CapabilityNotSupportedError,
    ConfigurationError,
    FabricError,
    ModelUnavailableError,
    ProviderError,
)


def test_error_hierarchy():
    assert issubclass(ModelUnavailableError, FabricError)
    assert issubclass(CapabilityNotSupportedError, FabricError)
    assert issubclass(ProviderError, FabricError)
    assert issubclass(ConfigurationError, FabricError)
    # All are RuntimeErrors, so existing "except Exception" handlers keep working.
    for cls in (ModelUnavailableError, CapabilityNotSupportedError, ProviderError, ConfigurationError):
        assert issubclass(cls, RuntimeError)


def test_errors_are_catchable_by_base():
    with pytest.raises(FabricError):
        raise ProviderError("boom")

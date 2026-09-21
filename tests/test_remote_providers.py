from forge.models.credentials import CredentialStore
from forge.models.remote_providers import build_remote_providers


def test_credential_store_recognizes_all_hosted_providers():
    secret = "sk-test-secret-value"
    env = {
        "OPENAI_API_KEY": secret,
        "ANTHROPIC_API_KEY": secret,
        "GEMINI_API_KEY": secret,
        "OPENROUTER_API_KEY": secret,
        "GROQ_API_KEY": secret,
    }
    store = CredentialStore(env=env)
    providers = ("openai", "anthropic", "gemini", "openrouter", "groq")
    assert all(store.configured(name) for name in providers)
    assert all(store.providers()[name] for name in providers)
    # Never a value in the redacted views.
    assert secret not in repr(store)
    assert set(store.providers().values()) == {True}


def test_remote_provider_factory_registers_only_present_keys():
    providers = build_remote_providers({
        "ANTHROPIC_API_KEY": "a",
        "GEMINI_API_KEY": "g",
        "OPENROUTER_API_KEY": "o",
        "GROQ_API_KEY": "q",
    })
    assert {name for name, _, _ in providers} == {
        "anthropic", "gemini", "openrouter", "groq"
    }
    assert all(model for _, _, model in providers)

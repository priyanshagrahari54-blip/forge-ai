from forge.models.credentials import CredentialStore
from forge.models.remote_providers import build_remote_providers


def test_credential_store_recognizes_all_hosted_providers():
    env = {
        "OPENAI_API_KEY": "x",
        "ANTHROPIC_API_KEY": "x",
        "GEMINI_API_KEY": "x",
        "OPENROUTER_API_KEY": "x",
        "GROQ_API_KEY": "x",
    }
    store = CredentialStore(env=env)
    assert all(store.configured(name) for name in env)


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

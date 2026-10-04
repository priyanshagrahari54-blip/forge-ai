from forge.models.remote_providers import build_remote_providers


def test_huggingface_requires_token_and_explicit_model():
    providers = build_remote_providers({"HF_TOKEN": "secret", "HF_MODEL": "org/model"})
    item = next(x for x in providers if x[0] == "huggingface")
    assert item[2] == "org/model"


def test_huggingface_is_not_fabricated_without_configuration():
    providers = build_remote_providers({})
    assert all(name != "huggingface" for name, _, _ in providers)

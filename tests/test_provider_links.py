from forge.models.provider_links import enrich_provider_info, provider_link_catalog, provider_links


def test_known_provider_links_are_official_and_public():
    links = provider_links("openai")
    assert links["website"] == "https://openai.com/"
    assert links["api"] == "https://platform.openai.com/"
    assert links["docs"].startswith("https://platform.openai.com/")


def test_catalog_contains_multiple_provider_families():
    names = {item["name"] for item in provider_link_catalog()}
    assert {"openai", "anthropic", "google", "mistral", "ollama", "huggingface", "openrouter"} <= names


def test_unknown_provider_does_not_get_an_invented_link():
    assert provider_links("unknown-provider") == {}
    assert "links" not in enrich_provider_info({"name": "unknown-provider"})

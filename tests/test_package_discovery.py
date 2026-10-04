from forge.capabilities.package_discovery import discover_npm, discover_pypi

def test_package_discovery_is_metadata_only(monkeypatch):
    payloads = {
        "https://pypi.org/pypi/demo/json": {"info": {"name": "demo", "version": "1.2", "license": "MIT"}},
        "https://registry.npmjs.org/demo": {"name": "demo", "dist-tags": {"latest": "2.0"}, "license": "MIT"},
    }
    import forge.capabilities.package_discovery as module
    monkeypatch.setattr(module, "_json", lambda url: payloads[url])
    py = discover_pypi("demo")
    js = discover_npm("demo")
    assert py and py.verification_status == "unverified"
    assert js and js.verification_status == "unverified"

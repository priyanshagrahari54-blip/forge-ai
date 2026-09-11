"""Secure research engine tests — fully offline (no live web access).

Covers: provenance labels, honest web failure (no model backfill),
query planning, ranking, dedup, summarization/citations, cache
behaviour, config validation, SSRF policy of the web sources (via a
fake fetcher + the real ``forge.security.ssrf`` validators), local
source safety (no secrets, no path escape), planner/context
integration, control plane + API, and the CLI.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from helpers_a34 import login, make_client, make_plane, make_repo  # noqa: E402

from forge.core.planner import Planner  # noqa: E402
from forge.intelligence.context import ContextPack, ContextQuery  # noqa: E402
from forge.research.cache import ResearchCache, cache_key  # noqa: E402
from forge.research.config import (  # noqa: E402
    ResearchConfig, WebSourceConfig, load_research_config,
)
from forge.research.integration import (  # noqa: E402
    ResearchAwarePlanner, enrich_context_with_research, external_notes,
    research_context_items,
)
from forge.research.provenance import (  # noqa: E402
    Citation, Provenance, ResearchResult, SourceOutcome, STATE_SUCCESS,
)
from forge.research.query_planner import QueryPlanner  # noqa: E402
from forge.research.secure_engine import (  # noqa: E402
    SecureResearchEngine, deduplicate, rank_results, summarize,
)
from forge.research.sources import OfficialDocsSource, extract_text  # noqa: E402
from forge.security.ssrf import (  # noqa: E402
    FetchOutcome, FetchPolicy, SSRFError, parse_and_validate,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def make_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.3.0"\n'
        'requires-python = ">=3.8"\n'
        'dependencies = ["fastapi>=0.110", "httpx>=0.27"]\n'
        '[project.scripts]\ndemo = "demo.cli:main"\n')
    (root / "demo").mkdir(exist_ok=True)
    (root / "demo" / "__init__.py").write_text("")
    (root / "demo" / "client.py").write_text(
        "import httpx\n\n\n"
        "class RetryError(Exception):\n"
        "    pass\n\n\n"
        "def fetch_widget(url):\n"
        "    '''Fetch a widget with httpx and retry on RetryError.'''\n"
        "    return httpx.get(url)\n")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "widgets.md").write_text(
        "# Widget guide\n\nfetch_widget retries three times on RetryError. "
        "Configure the retry budget in settings.\n")
    (root / ".env").write_text("API_KEY=supersecretvalue123456\n")
    (root / "config.py").write_text(
        "RETRY_LIMIT = 3\napi_key = 'sk-verysecretkeyvalue0000'\n")
    return root


class FakeFetcher:
    """Deterministic stand-in for ``forge.security.ssrf.fetch``.

    Still runs the real URL validators (scheme/host/port/allowlist), so
    SSRF policy is exercised without any socket; ``responses`` maps a
    URL prefix to an outcome factory.
    """

    def __init__(self, responses=None) -> None:
        self.responses = responses or {}
        self.calls: list[str] = []

    def __call__(self, url, *, policy=None, audit=None):
        self.calls.append(url)
        policy = policy or FetchPolicy()
        try:
            parse_and_validate(url, policy)
        except Exception as exc:  # SSRFError
            return FetchOutcome(ok=False, blocked=True, blocked_reason=str(exc),
                                final_url=url)
        for prefix, factory in self.responses.items():
            if url.startswith(prefix):
                outcome = factory(url) if callable(factory) else factory
                if not outcome.final_url:
                    outcome.final_url = url
                return outcome
        return FetchOutcome(ok=False, status=0, final_url=url,
                            error_state="connection error: offline test")


def html_page(title: str, body: str) -> FetchOutcome:
    html = (f"<html><head><title>{title}</title><script>evil()</script></head>"
            f"<body><h1>{title}</h1><p>{body}</p>"
            f'<a href="https://fastapi.tiangolo.com/tutorial/handling-errors/">'
            f"Handling Errors HTTPException</a></body></html>")
    return FetchOutcome(ok=True, status=200, content_type="text/html",
                        body=html.encode(), bytes_read=len(html))


def config(**overrides) -> ResearchConfig:
    cfg = ResearchConfig(cache_ttl_seconds=0)
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def make_engine(root: Path, fetcher=None, **cfg_overrides) -> SecureResearchEngine:
    return SecureResearchEngine(
        root, config=config(**cfg_overrides), fetcher=fetcher or FakeFetcher(),
        cache=ResearchCache(None, ttl_seconds=0))


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Belt and braces: any accidental DNS/socket use fails loudly."""
    def _boom(*args, **kwargs):
        raise AssertionError("live network access attempted in tests")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)


# ---------------------------------------------------------------------------
# provenance model
# ---------------------------------------------------------------------------


def test_provenance_classes_are_distinct_and_flagged():
    values = {p.value for p in Provenance}
    assert values == {"MODEL_KNOWLEDGE", "LOCAL_SOURCE", "REAL_WEB_RESULT",
                      "USER_PROVIDED"}
    assert Provenance.LOCAL_SOURCE.trusted
    assert not Provenance.REAL_WEB_RESULT.trusted
    assert Provenance.REAL_WEB_RESULT.verified
    assert not Provenance.MODEL_KNOWLEDGE.verified
    result = ResearchResult(source="x", provenance="MODEL_KNOWLEDGE",
                            title="t", snippet="s", citation=Citation("model:m"))
    data = result.to_dict()
    assert data["provenance"] == "MODEL_KNOWLEDGE"
    assert data["verified"] is False and data["untrusted"] is True
    with pytest.raises(ValueError):
        ResearchResult(source="x", provenance="INVENTED", title="t",
                       snippet="s", citation=Citation("z"))


# ---------------------------------------------------------------------------
# query planning
# ---------------------------------------------------------------------------


def test_query_planner_classifies_and_extracts():
    planner = QueryPlanner(known_libraries=("fastapi", "httpx"))
    plan = planner.plan("httpx ReadTimeout when calling fastapi endpoint")
    assert plan.intent == "error"
    assert "ReadTimeout" in plan.error_names
    assert set(plan.libraries) == {"fastapi", "httpx"}
    assert plan.source_order[0] == "user_provided"
    assert "official_docs" in plan.source_order
    assert any("cause and fix" in q for q in plan.sub_queries)

    local_only = planner.plan("where is fetch_widget defined in this repo?",
                              allow_web=False)
    assert local_only.intent == "project"
    assert not any(s in local_only.source_order
                   for s in ("official_docs", "configured_web"))
    assert local_only.web_requested is False

    restricted = planner.plan("fastapi docs", enabled_sources=("local_docs",))
    assert restricted.source_order == ["local_docs"]

    with pytest.raises(ValueError):
        planner.plan("   ")
    with pytest.raises(ValueError):
        planner.plan("x" * 2001)


def test_query_planner_is_deterministic():
    planner = QueryPlanner(known_libraries=("pydantic",))
    a = planner.plan("pydantic ValidationError nested model parse").to_dict()
    b = planner.plan("pydantic ValidationError nested model parse").to_dict()
    assert a == b


# ---------------------------------------------------------------------------
# local sources
# ---------------------------------------------------------------------------


def test_local_sources_cite_real_files_with_provenance(tmp_path):
    root = make_project(tmp_path)
    engine = make_engine(root)
    report = engine.research("where is fetch_widget defined?", allow_web=False)
    assert report["results"], report["answer"]
    for item in report["results"]:
        assert item["provenance"] == "LOCAL_SOURCE"
        assert (root / item["citation"]["locator"]).is_file()
    top = report["results"][0]
    assert top["citation"]["locator"] == "demo/client.py"
    assert top["kind"] == "symbol" and top["citation"]["line"] == 8
    assert report["provenance_counts"]["LOCAL_SOURCE"] == len(report["results"])
    assert report["provenance_counts"]["MODEL_KNOWLEDGE"] == 0
    assert report["model_knowledge_used"] is False
    assert report["summary"] and all(s["cite"].startswith("[") for s in report["summary"])
    assert [c["index"] for c in report["citations"]] == list(
        range(1, len(report["results"]) + 1))


def test_local_docs_and_metadata_sources(tmp_path):
    root = make_project(tmp_path)
    engine = make_engine(root)
    report = engine.research("retry budget documentation", allow_web=False)
    by_source = {o["source"]: o for o in report["sources"]}
    assert by_source["local_docs"]["state"] == "SUCCESS"
    assert any(r["source"] == "local_docs" and r["cite"].startswith("docs/widgets.md")
               for r in report["results"])
    metadata = engine.research("which version of httpx dependency is declared?",
                               allow_web=False)
    hits = [r for r in metadata["results"] if r["source"] == "repository_metadata"]
    assert hits and "httpx>=0.27" in hits[0]["snippet"]
    assert hits[0]["cite"] == "pyproject.toml"


def test_local_sources_never_leak_secrets_or_escape_root(tmp_path):
    root = make_project(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}_outside.txt"
    outside.write_text("fetch_widget outside secret\n")
    engine = make_engine(root)
    report = engine.research("api_key fetch_widget", allow_web=False,
                             user_files=[str(outside), "../" + outside.name])
    dumped = json.dumps(report)
    assert "supersecretvalue" not in dumped
    assert "sk-verysecretkeyvalue" not in dumped
    assert ".env" not in {r["citation"]["locator"] for r in report["results"]}
    assert "outside secret" not in dumped
    assert not any(r["provenance"] == "USER_PROVIDED" for r in report["results"])


def test_user_provided_source_is_labeled(tmp_path):
    root = make_project(tmp_path)
    engine = make_engine(root)
    report = engine.research("RetryError budget", allow_web=False,
                             user_notes=["The RetryError budget is 5 in prod."],
                             user_files=["docs/widgets.md"])
    user = [r for r in report["results"] if r["provenance"] == "USER_PROVIDED"]
    assert len(user) == 2
    assert {r["kind"] for r in user} == {"note", "file"}
    assert all(r["untrusted"] for r in user)
    assert report["provenance_counts"]["USER_PROVIDED"] == 2


# ---------------------------------------------------------------------------
# web sources, provenance and honest failure
# ---------------------------------------------------------------------------


def test_real_web_result_only_when_bytes_were_fetched(tmp_path):
    root = make_project(tmp_path)
    fetcher = FakeFetcher({
        "https://fastapi.tiangolo.com/": html_page(
            "FastAPI", "HTTPException lets you return HTTP errors with status_code."),
    })
    engine = make_engine(root, fetcher=fetcher)
    report = engine.research("fastapi HTTPException status_code")
    web = [r for r in report["results"] if r["provenance"] == "REAL_WEB_RESULT"]
    assert web, report["sources"]
    page = next(r for r in web if r["kind"] == "page")
    assert page["citation"]["locator"] == "https://fastapi.tiangolo.com/"
    assert page["citation"]["retrieved_at"].endswith("Z")
    assert page["citation"]["status"] == 200
    assert "evil()" not in page["snippet"]
    assert page["untrusted"] is True
    assert fetcher.calls == ["https://fastapi.tiangolo.com/"]
    assert report["web_failed"] is False
    by_source = {o["source"]: o for o in report["sources"]}
    assert by_source["official_docs"]["state"] == "SUCCESS"


def test_failed_web_research_is_reported_not_replaced(tmp_path):
    root = make_project(tmp_path)
    fetcher = FakeFetcher({
        "https://fastapi.tiangolo.com/": FetchOutcome(
            ok=False, status=0, error_state="connection error: timed out"),
    })
    calls: list[str] = []

    def model(question):
        calls.append(question)
        return "FastAPI HTTPException takes status_code and detail."

    engine = SecureResearchEngine(root, config=config(), fetcher=fetcher,
                                  cache=ResearchCache(None, ttl_seconds=0),
                                  model_answer=model)
    report = engine.research("fastapi HTTPException status_code")
    assert report["web_failed"] is True
    assert report["web_failures"][0]["state"] == "TIMEOUT"
    assert report["provenance_counts"]["REAL_WEB_RESULT"] == 0
    assert report["provenance_counts"]["MODEL_KNOWLEDGE"] == 0
    assert report["model_knowledge_used"] is False
    assert calls == [], "model must never be consulted as a web fallback"
    assert "NOT backfilled" in report["answer"]
    model_outcome = next(o for o in report["sources"] if o["source"] == "model_knowledge")
    assert model_outcome["state"] == "SKIPPED" and model_outcome["attempted"] is False


def test_model_knowledge_only_on_explicit_opt_in_and_labeled(tmp_path):
    root = make_project(tmp_path)
    engine = SecureResearchEngine(
        root, config=config(), fetcher=FakeFetcher(),
        cache=ResearchCache(None, ttl_seconds=0),
        model_answer=lambda q: "Model says: use status_code=404.",
        model_name="test-model")
    report = engine.research("fastapi HTTPException status_code",
                             allow_model_knowledge=True)
    model_items = [r for r in report["results"] if r["provenance"] == "MODEL_KNOWLEDGE"]
    assert len(model_items) == 1
    assert model_items[0]["verified"] is False
    assert "unverified" in model_items[0]["title"].lower()
    assert model_items[0]["cite"] == "model:test-model"
    assert report["model_knowledge_used"] is True
    # ranked below every verified result
    ranks = [r["provenance"] for r in report["results"]]
    assert ranks[-1] == "MODEL_KNOWLEDGE"
    # web still honestly failed alongside it
    assert report["web_failed"] is True


def test_web_sources_refuse_private_and_http_targets():
    cfg = config()
    cfg.official_docs = {
        "fastapi": "https://fastapi.tiangolo.com/",
    }
    # Attempt to smuggle unsafe targets straight into the source (bypassing
    # config validation) — the SSRF chain must still refuse them.
    cfg.official_docs["evil1"] = "https://169.254.169.254/latest/meta-data/"
    cfg.official_docs["evil2"] = "https://localhost/admin"
    cfg.official_docs["evil3"] = "http://fastapi.tiangolo.com/"
    cfg.official_docs["evil4"] = "https://db.internal/"
    cfg.official_docs["evil5"] = "https://fastapi.tiangolo.com:6379/"
    fetcher = FakeFetcher({"https://fastapi.tiangolo.com/": html_page("FastAPI", "ok")})
    source = OfficialDocsSource(cfg, fetcher=fetcher)
    planner = QueryPlanner(known_libraries=tuple(cfg.official_docs))
    for lib in ("evil1", "evil2", "evil3", "evil4", "evil5"):
        outcome = source.search(planner.plan(f"{lib} question"))
        assert outcome.state == "POLICY_DENIED", (lib, outcome.error)
        assert outcome.results == []
    ok = source.search(planner.plan("fastapi question"))
    assert ok.state == "SUCCESS"


def test_question_urls_off_allowlist_are_never_fetched(tmp_path):
    root = make_project(tmp_path)
    fetcher = FakeFetcher({"https://": html_page("Anything", "fetch_widget")})
    engine = make_engine(root, fetcher=fetcher)
    engine.research("see https://attacker.example/steal and "
                    "https://127.0.0.1/admin for fetch_widget")
    assert all(url.split("/")[2] in engine.config.fetch_policy().host_allowlist
               for url in fetcher.calls), fetcher.calls
    assert not any("attacker" in u or "127.0.0.1" in u for u in fetcher.calls)


def test_fetch_policy_is_https_only_bounded_and_allowlisted():
    cfg = ResearchConfig(web_sources=[
        WebSourceConfig(name="docs", url="https://docs.example.org/search?q={query}")])
    policy = cfg.fetch_policy()
    assert policy.allow_http is False
    assert "docs.example.org" in policy.host_allowlist
    assert "docs.python.org" in policy.host_allowlist
    assert policy.timeout <= 15 and policy.max_bytes <= 2_000_000
    assert policy.max_redirects <= 5
    for bad in ("http://docs.example.org/", "https://localhost/",
                "https://10.0.0.1/", "https://[::1]/", "https://metadata/",
                "https://docs.example.org:8080/", "https://user:pw@docs.example.org/",
                "https://not-allowed.example.com/", "ftp://docs.example.org/"):
        with pytest.raises(SSRFError):
            parse_and_validate(bad, policy)
    parse_and_validate("https://docs.example.org/search?q=x", policy)


def test_configured_web_source_uses_templates_and_reports_errors(tmp_path):
    root = make_project(tmp_path)
    cfg = config(web_sources=[
        WebSourceConfig(name="docs", url="https://docs.example.org/search?q={query}"),
        WebSourceConfig(name="dead", url="https://dead.example.org/search?q={query}"),
    ])
    fetcher = FakeFetcher({
        "https://docs.example.org/": html_page("Docs", "RetryError explained here."),
        "https://dead.example.org/": FetchOutcome(ok=False, status=503,
                                                  error_state="http 503"),
    })
    engine = SecureResearchEngine(root, config=cfg, fetcher=fetcher,
                                  cache=ResearchCache(None, ttl_seconds=0))
    report = engine.research("RetryError explained")
    configured = next(o for o in report["sources"] if o["source"] == "configured_web")
    assert configured["state"] == "SUCCESS"
    assert "dead: ERROR" in configured["error"]
    assert any(u.startswith("https://docs.example.org/search?q=RetryError")
               for u in fetcher.calls)
    assert any(r["source"] == "configured_web" and r["provenance"] == "REAL_WEB_RESULT"
               for r in report["results"])


def test_extract_text_strips_scripts_and_decodes_entities():
    title, text = extract_text(
        b"<html><title>T &amp; U</title><style>x{}</style><script>a()</script>"
        b"<p>hello&nbsp;world &lt;ok&gt;</p></html>", "text/html")
    assert title == "T & U"
    assert text == "T & U hello world <ok>"
    assert "a()" not in text


# ---------------------------------------------------------------------------
# ranking / dedup / summary
# ---------------------------------------------------------------------------


def _result(prov, locator, snippet, score=1.0, source="project_files", kind="file",
            line=None):
    return ResearchResult(source=source, provenance=prov, title=locator,
                          snippet=snippet, citation=Citation(locator, line=line),
                          score=score, kind=kind)


def test_ranking_prefers_verified_local_over_web_over_model():
    plan = QueryPlanner().plan("widget retry")
    results = [
        _result(Provenance.MODEL_KNOWLEDGE, "model:m", "widget retry model", 5.0,
                source="model_knowledge", kind="model"),
        _result(Provenance.REAL_WEB_RESULT, "https://a.example/", "widget retry web", 5.0,
                source="configured_web", kind="page"),
        _result(Provenance.LOCAL_SOURCE, "a.py", "widget retry local", 5.0),
    ]
    ranked = rank_results(results, plan)
    assert [r.provenance for r in ranked] == [
        Provenance.LOCAL_SOURCE, Provenance.REAL_WEB_RESULT, Provenance.MODEL_KNOWLEDGE]
    assert all("rank_score" in r.metadata for r in ranked)


def test_deduplication_by_locator_and_content():
    results = [
        _result(Provenance.LOCAL_SOURCE, "a.py", "same text here about widgets", line=1),
        _result(Provenance.LOCAL_SOURCE, "a.py", "other text", line=1),        # same locator
        _result(Provenance.LOCAL_SOURCE, "b.py", "SAME   text here about widgets"),  # same content
        _result(Provenance.LOCAL_SOURCE, "c.py", "unique content"),
    ]
    deduped = deduplicate(results)
    assert [r.citation.locator for r in deduped] == ["a.py", "c.py"]


def test_summary_sentences_all_carry_citations():
    plan = QueryPlanner().plan("widget retry")
    results = [
        _result(Provenance.LOCAL_SOURCE, "a.py", "Intro sentence. Widget retry is bounded. Tail.", line=3),
        _result(Provenance.REAL_WEB_RESULT, "https://a.example/", "Widget docs online.",
                source="official_docs", kind="page"),
    ]
    summary, citations = summarize(results, plan)
    assert len(citations) == 2 and citations[1]["provenance"] == "REAL_WEB_RESULT"
    assert summary[0]["text"] == "Widget retry is bounded."
    assert summary[0]["cite"] == "[1] a.py:3"
    assert all(s["citation_index"] in (1, 2) for s in summary)


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def test_cache_stores_only_success_and_expires(tmp_path):
    now = [1000.0]
    cache = ResearchCache(tmp_path / "cache", ttl_seconds=60, max_entries=2,
                          clock=lambda: now[0])
    good = SourceOutcome(source="official_docs", provenance=Provenance.REAL_WEB_RESULT,
                         state=STATE_SUCCESS, results=[
                             _result(Provenance.REAL_WEB_RESULT, "https://x.example/",
                                     "text", source="official_docs", kind="page")])
    bad = SourceOutcome(source="official_docs", provenance=Provenance.REAL_WEB_RESULT,
                        state="TIMEOUT", error="timed out")
    assert cache.put("k1", good) is True
    assert cache.put("k2", bad) is False
    hit = cache.get("k1")
    assert hit is not None and hit.from_cache and hit.results[0].provenance == Provenance.REAL_WEB_RESULT
    assert cache.get("k2") is None
    # disk round trip
    fresh = ResearchCache(tmp_path / "cache", ttl_seconds=60, clock=lambda: now[0])
    assert fresh.get("k1") is not None
    now[0] += 61
    assert fresh.get("k1") is None
    # bounded
    for i in range(5):
        cache.put(f"z{i}", good)
    assert cache.stats()["entries"] <= 2
    assert cache_key("a", "Q ") == cache_key("a", "q")
    assert cache_key("a", "q") != cache_key("b", "q")


def test_engine_serves_web_results_from_cache_and_never_caches_failures(tmp_path):
    root = make_project(tmp_path)
    fetcher = FakeFetcher({"https://fastapi.tiangolo.com/": html_page("FastAPI", "HTTPException")})
    cfg = config()
    cfg.cache_ttl_seconds = 3600
    engine = SecureResearchEngine(root, config=cfg, fetcher=fetcher,
                                  cache=ResearchCache(tmp_path / "c", ttl_seconds=3600))
    first = engine.research("fastapi HTTPException")
    second = engine.research("fastapi HTTPException")
    assert len(fetcher.calls) == 1
    assert next(o for o in second["sources"] if o["source"] == "official_docs")["state"] == "CACHED"
    assert first["provenance_counts"]["REAL_WEB_RESULT"] == second["provenance_counts"]["REAL_WEB_RESULT"]
    failing = FakeFetcher({"https://fastapi.tiangolo.com/": FetchOutcome(
        ok=False, error_state="connection error: timed out")})
    engine2 = SecureResearchEngine(root, config=cfg, fetcher=failing,
                                   cache=ResearchCache(tmp_path / "c2", ttl_seconds=3600))
    engine2.research("fastapi HTTPException")
    engine2.research("fastapi HTTPException")
    assert len(failing.calls) == 2, "failures must not be cached"
    assert engine2.cache.stats()["entries"] == 0


# ---------------------------------------------------------------------------
# configuration validation
# ---------------------------------------------------------------------------


def test_config_rejects_unsafe_entries_without_widening(tmp_path):
    cfg = load_research_config(tmp_path, data={
        "web_enabled": True,
        "allow_http": "yes",              # not a bool -> default False
        "timeout_seconds": 999,           # out of range -> default
        "max_bytes": 10,                  # out of range -> default
        "web_sources": [
            {"name": "ok", "url": "https://docs.example.org/?q={query}"},
            {"name": "plain", "url": "http://docs.example.org/?q={query}"},
            {"name": "local", "url": "https://localhost/?q={query}"},
            {"name": "meta", "url": "https://metadata.google.internal/"},
            {"name": "creds", "url": "https://u:p@docs.example.org/"},
            "not-a-mapping",
        ],
        "official_docs": {"lib": "https://lib.example.org/", "bad": "ftp://x/"},
        "doc_paths": ["docs", "../etc", "/abs"],
    })
    assert cfg.allow_http is False
    assert cfg.timeout_seconds == ResearchConfig().timeout_seconds
    assert cfg.max_bytes == ResearchConfig().max_bytes
    assert [s.name for s in cfg.web_sources] == ["ok"]
    assert cfg.official_docs["lib"] == "https://lib.example.org/"
    assert "bad" not in cfg.official_docs
    assert cfg.doc_paths == ["docs"]
    assert len(cfg.rejected) >= 8
    assert "lib.example.org" in cfg.fetch_policy().host_allowlist


def test_config_file_is_loaded_from_dot_forge(tmp_path):
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "research.yaml").write_text(
        "web_enabled: false\nallow_model_knowledge: false\n"
        "web_sources:\n  - name: d\n    url: https://d.example.org/s?q={query}\n")
    cfg = load_research_config(tmp_path)
    assert cfg.web_enabled is False
    assert cfg.web_sources[0].render("a b") == "https://d.example.org/s?q=a+b"
    engine = SecureResearchEngine(tmp_path, config=cfg, fetcher=FakeFetcher(),
                                  cache=ResearchCache(None, ttl_seconds=0),
                                  build_intelligence=False)
    report = engine.research("fastapi HTTPException")
    assert "official_docs" not in report["plan"]["source_order"]
    assert "not attempted" in report["answer"]
    (tmp_path / ".forge" / "research.yaml").write_text(": : not yaml [")
    broken = load_research_config(tmp_path)
    assert broken.rejected and broken.allow_http is False


# ---------------------------------------------------------------------------
# planner + context engine integration
# ---------------------------------------------------------------------------


def test_research_aware_planner_adds_cited_step_and_keeps_base_shape(tmp_path):
    root = make_project(tmp_path)
    planner = ResearchAwarePlanner(make_engine(root), root=root, allow_web=False)
    steps = planner.create_plan("fix fetch_widget RetryError handling")
    ids = [s.id for s in steps]
    assert ids == ["1", "1a", "2", "3", "4", "5"]
    research = steps[1]
    assert research.depends_on == ("1",)
    assert "demo/client.py" in research.description
    assert "LOCAL_SOURCE" in research.description
    assert steps[2].depends_on == ("1a",)
    # base planner untouched
    assert [s.id for s in Planner().create_plan("x")] == ["1", "2", "3", "4", "5"]
    items = planner.context_items()
    assert items and all(i.kind == "research" for i in items)
    assert all((root / i.path).is_file() for i in items)
    with pytest.raises(ValueError):
        planner.create_plan("  ")


def test_context_enrichment_adds_only_local_verified_files(tmp_path):
    root = make_project(tmp_path)
    fetcher = FakeFetcher({"https://www.python-httpx.org/": html_page("HTTPX", "fetch_widget httpx")})
    engine = make_engine(root, fetcher=fetcher)
    report = engine.research("httpx fetch_widget RetryError",
                             user_notes=["fetch_widget note"])
    assert report["provenance_counts"]["REAL_WEB_RESULT"] >= 1
    items = research_context_items(report, root)
    assert items
    assert all(i.path.startswith("demo/") or i.path.endswith((".md", ".toml", ".py"))
               for i in items)
    assert not any(i.path.startswith("http") or i.path.startswith("user-note") for i in items)
    pack = ContextPack(query=ContextQuery(task="t"))
    added = enrich_context_with_research(pack, report, root)
    assert added == len(items)
    assert enrich_context_with_research(pack, report, root) == 0  # idempotent
    notes = external_notes(report)
    assert notes and all(n["untrusted"] for n in notes)
    assert {n["provenance"] for n in notes} <= {"REAL_WEB_RESULT", "USER_PROVIDED",
                                                 "MODEL_KNOWLEDGE"}


# ---------------------------------------------------------------------------
# control plane + API
# ---------------------------------------------------------------------------


def test_control_plane_research_query_is_audited(tmp_path, monkeypatch):
    import forge.research.secure_engine as se
    monkeypatch.setattr(se.SecureResearchEngine, "_build_sources",
                        lambda self: _offline_sources(self))
    plane = make_plane(tmp_path, start=True)
    root = Path(plane.projects["demo"].root)
    make_repo(root)
    client = make_client(plane)
    with client:
        payload, _token, _headers = login(client)
        session = plane.sessions.get(payload["session_id"])
        result = plane.research_query(session, "where is health defined?",
                                      allow_web=False)
        assert result["results"] and all(
            r["provenance"] == "LOCAL_SOURCE" for r in result["results"])
        status = plane.research_status(session)
        assert status["security"]["https_only"] is True
        audited = plane.audit.query(resource="research")
        assert any(a.operation == "query" for a in audited)
        from forge.control.control_plane import InvalidRequest
        with pytest.raises(InvalidRequest):
            plane.research_query(session, "")


def test_research_query_api(tmp_path, monkeypatch):
    import forge.research.secure_engine as se
    monkeypatch.setattr(se.SecureResearchEngine, "_build_sources",
                        lambda self: _offline_sources(self))
    plane = make_plane(tmp_path, start=True)
    make_repo(Path(plane.projects["demo"].root))
    client = make_client(plane)
    with client:
        assert client.get("/api/v1/research/status").status_code == 401
        _session, _token, headers = login(client)
        response = client.post("/api/v1/research/query", headers=headers,
                               json={"question": "where is health defined?",
                                     "allow_web": False})
        assert response.status_code == 200
        body = response.json()
        assert body["results"] and body["provenance_counts"]["MODEL_KNOWLEDGE"] == 0
        assert body["honest"] is True
        assert client.post("/api/v1/research/query", headers=headers,
                           json={"question": ""}).status_code in (400, 422)
        status = client.get("/api/v1/research/status", headers=headers)
        assert status.status_code == 200
        assert "sources" in status.json()


def _offline_sources(engine):
    """Source set with a fake fetcher (no network) for plane/API tests."""
    from forge.research.sources import (
        ConfiguredWebSource, LocalDocsSource, ProjectFilesSource,
    )
    fetcher = FakeFetcher()
    sources = [
        ProjectFilesSource(engine.root, engine.intelligence),
        LocalDocsSource(engine.root, engine.config.doc_paths),
        engine._metadata,
        OfficialDocsSource(engine.config, fetcher=fetcher),
        ConfiguredWebSource(engine.config, fetcher=fetcher),
    ]
    return {s.name: s for s in sources}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(*args, cwd):
    return subprocess.run(
        [sys.executable, "-m", "forge.cli", *args], cwd=str(cwd),
        capture_output=True, text=True, timeout=120,
        env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin",
             "FORGE_RESEARCH_OFFLINE": "1"})


def test_cli_research_status_plan_and_local_query(tmp_path):
    root = make_project(tmp_path)
    (root / ".forge").mkdir()
    (root / ".forge" / "research.yaml").write_text("web_enabled: false\n")

    status = _cli("research", "--root", str(root), cwd=REPO_ROOT)
    assert status.returncode == 0, status.stderr
    assert "Forge Research Engine" in status.stdout
    assert "Web research: disabled" in status.stdout

    status_json = _cli("research", "--root", str(root), "--json", "status", cwd=REPO_ROOT)
    assert status_json.returncode == 0, status_json.stderr
    assert json.loads(status_json.stdout)["security"]["https_only"] is True

    plan = _cli("research", "--root", str(root), "plan",
                "httpx ReadTimeout in fetch_widget", "--json", cwd=REPO_ROOT)
    assert plan.returncode == 0, plan.stderr
    assert json.loads(plan.stdout)["intent"] == "error"

    query = _cli("research", "--root", str(root), "query",
                 "where is fetch_widget defined", "--no-web", "--json", cwd=REPO_ROOT)
    assert query.returncode == 0, query.stderr
    payload = json.loads(query.stdout)
    assert payload["results"][0]["cite"] == "demo/client.py:8"
    assert payload["provenance_counts"]["REAL_WEB_RESULT"] == 0
    assert payload["model_knowledge_used"] is False

    human = _cli("research", "--root", str(root), "query",
                 "where is fetch_widget defined", "--no-web", "--limit", "2",
                 cwd=REPO_ROOT)
    assert human.returncode == 0, human.stderr
    assert "LOCAL_SOURCE" in human.stdout and "demo/client.py:8" in human.stdout

    cleared = _cli("research", "--root", str(root), "cache-clear", cwd=REPO_ROOT)
    assert cleared.returncode == 0 and "Cleared" in cleared.stdout

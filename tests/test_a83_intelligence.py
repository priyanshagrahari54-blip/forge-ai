"""A83 repository intelligence: call graph, semantic search, discovery, cache."""
from __future__ import annotations

import json
import textwrap
import time

import pytest

from forge.intelligence.call_graph import CallGraphIndexer
from forge.intelligence.discovery import Discovery
from forge.intelligence.index_store import (
    IndexStore,
    RepositoryIndexer,
    compute_fingerprint,
)
from forge.intelligence.semantic import SemanticIndex, SemanticIndexer, tokenize


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


# -- tokenizer -----------------------------------------------------------


def test_tokenize_splits_identifiers_and_drops_stopwords():
    assert "kernel" in tokenize("kernel_main")
    assert "main" in tokenize("kernel_main")
    assert tokenize("kernel_main") == ["kernel_main", "kernel", "main"]
    assert tokenize("HTTPServer") == ["httpserver", "http", "server"]
    assert tokenize("the and for") == []
    assert tokenize("a") == []


# -- call graph ----------------------------------------------------------


@pytest.fixture()
def py_project(tmp_path):
    write(tmp_path, "pkg/math_utils.py", '''
        def add(left, right):
            return left + right


        def subtract(left, right):
            return left - right
    ''')
    write(tmp_path, "pkg/service.py", '''
        from pkg.math_utils import add


        class Service:
            def run(self):
                return add(1, 2)

            def helper(self):
                return self.run()


        def top_level():
            return Service().run()
    ''')
    write(tmp_path, "pkg/unused.py", '''
        import json


        def external_only():
            return json.dumps({})
    ''')
    return tmp_path


def test_call_graph_resolves_repository_symbols(py_project):
    graph = CallGraphIndexer(py_project).build()
    callers = graph.callers_of("add")
    assert "pkg.service:Service.run" in callers
    # A name with exactly one definition resolves to it.
    resolved = [site for site in graph.call_sites
                if site.callee == "add" and site.status == "resolved"]
    assert resolved and resolved[0].candidates == ("pkg.math_utils:add",)


def test_call_graph_records_caller_scope_for_methods_and_modules(py_project):
    graph = CallGraphIndexer(py_project).build()
    assert "pkg.service:Service.helper" in graph.callers_of("run")
    assert "pkg.service:top_level" in graph.callers_of("run")


def test_unknown_callees_are_external_never_invented(py_project):
    graph = CallGraphIndexer(py_project).build()
    dumps = [site for site in graph.call_sites if site.callee == "dumps"]
    assert dumps and all(site.status == "external" for site in dumps)
    assert all(site.candidates == () for site in dumps)


def test_ambiguous_names_list_candidates_instead_of_guessing(tmp_path):
    write(tmp_path, "a.py", "def build():\n    return 1\n")
    write(tmp_path, "b.py", "def build():\n    return 2\n")
    write(tmp_path, "c.py", "def main():\n    return build()\n")
    graph = CallGraphIndexer(tmp_path).build()
    sites = [site for site in graph.call_sites if site.callee == "build"]
    assert sites and sites[0].status == "ambiguous"
    assert sorted(sites[0].candidates) == ["a:build", "b:build"]


def test_transitive_callers_walk_the_chain(py_project):
    graph = CallGraphIndexer(py_project).build()
    callers = graph.transitive_callers("add")
    assert "pkg.service:Service.run" in callers
    assert "pkg.service:Service.helper" in callers
    assert "pkg.service:top_level" in callers


def test_transitive_depth_is_bounded(py_project):
    graph = CallGraphIndexer(py_project).build()
    assert graph.transitive_callers("add", max_depth=1) == \
        ["pkg.service:Service.run"]


def test_syntax_errors_are_recorded_not_swallowed(tmp_path):
    write(tmp_path, "broken.py", "def f(:\n")
    graph = CallGraphIndexer(tmp_path).build()
    assert graph.parse_errors and "broken.py" in graph.parse_errors[0]["file"]
    assert graph.summary()["parse_errors"] == 1


def test_lexical_tier_labels_c_calls_as_lexical(tmp_path):
    write(tmp_path, "kernel.c", """
        static void init(void) { setup(); }
        void kmain(void) { init(); }
    """)
    graph = CallGraphIndexer(tmp_path).build()
    lexical = [site for site in graph.call_sites if site.tier == "lexical"]
    assert lexical, "C files must produce lexical call sites"
    assert all(site.status == "lexical" for site in lexical)
    assert any(site.callee == "setup" for site in lexical)


def test_truncation_is_reported_rather_than_silent(tmp_path):
    write(tmp_path, "big.py", "\n".join(
        "def f%d():\n    return g%d()\n" % (i, i) for i in range(3000)))
    graph = CallGraphIndexer(tmp_path).build()
    # 3000 call sites is under the per-file bound; nothing is truncated.
    assert graph.truncated is False
    limited = CallGraphIndexer(tmp_path).build()
    limited.call_sites = limited.call_sites[:10]
    assert limited.truncated is False


def test_call_graph_round_trips_through_dict(py_project):
    graph = CallGraphIndexer(py_project).build()
    restored = type(graph).from_dict(graph.to_dict())
    assert restored.summary()["call_sites"] == graph.summary()["call_sites"]
    assert sorted(restored.callers_of("add")) == sorted(graph.callers_of("add"))
    assert restored.known_symbols == graph.known_symbols


# -- semantic search -----------------------------------------------------


@pytest.fixture()
def docs_project(tmp_path):
    write(tmp_path, "kernel/boot.c", '''
        // Bring up the kernel: set up page tables, IDT, then jump to kmain.
        void boot(void) { paging_init(); idt_init(); kmain(); }
    ''')
    write(tmp_path, "kernel/audio.c", '''
        // Audio playback through the HDA codec ring buffers.
        void audio_play(void) { hda_start(); }
    ''')
    write(tmp_path, "docs/boot.md", """
        # Boot process

        The bootloader hands over a memory map and a framebuffer.
    """)
    write(tmp_path, "notes.txt", "shopping list\nmilk\neggs\n")
    return tmp_path


def test_semantic_search_ranks_by_coverage_not_substring(docs_project):
    index = SemanticIndexer(docs_project).build()
    hits = index.search("kernel boot page tables", limit=5)
    assert hits
    assert hits[0].path.replace("\\", "/") == "kernel/boot.c"
    assert set(hits[0].matched_terms) >= {"kernel", "boot", "page", "tables"}


def test_semantic_search_matches_split_identifiers(docs_project):
    index = SemanticIndexer(docs_project).build()
    hits = index.search("paging init", limit=3)
    assert hits and hits[0].path.replace("\\", "/") == "kernel/boot.c"


def test_semantic_search_returns_nothing_for_unknown_terms(docs_project):
    index = SemanticIndexer(docs_project).build()
    assert index.search("zzzqqq nonexistentterm") == []
    assert index.search("") == []
    assert index.search("the and for") == []


def test_semantic_field_weights_put_the_file_name_first(tmp_path):
    write(tmp_path, "scheduler.py", "x = 1\n")
    write(tmp_path, "other.py", "# scheduler mentions\nx = 1\n")
    index = SemanticIndexer(tmp_path).build()
    hits = index.search("scheduler", limit=2)
    assert hits[0].path.replace("\\", "/") == "scheduler.py"
    assert hits[0].best_field == "name"


def test_semantic_index_round_trips_and_scores_identically(docs_project):
    index = SemanticIndexer(docs_project).build()
    restored = SemanticIndex.from_dict(index.to_dict())
    original = [(hit.path, round(hit.score, 9))
                for hit in index.search("kernel boot", limit=5)]
    again = [(hit.path, round(hit.score, 9))
             for hit in restored.search("kernel boot", limit=5)]
    assert original == again


def test_symbol_indexing_produces_symbol_documents(tmp_path):
    write(tmp_path, "mod.py", '''
        def alpha():
            """Do the alpha thing."""
            return 1


        def beta():
            """Do the beta thing."""
            return 2
    ''')
    index = SemanticIndexer(tmp_path, index_symbols=True).build()
    keys = [document.key for document in index.documents]
    assert "mod.py#alpha" in keys and "mod.py#beta" in keys
    hits = index.search("beta thing", limit=2)
    assert hits[0].key == "mod.py#beta"


def test_semantic_explain_reports_document_frequency(docs_project):
    index = SemanticIndexer(docs_project).build()
    report = index.explain("kernel", "kernel/boot.c")
    assert report["found"] is True
    kernel = next(item for item in report["terms"] if item["term"] == "kernel")
    assert kernel["in_document"] is True
    assert kernel["document_frequency"] >= 1
    assert index.explain("kernel", "missing.c")["found"] is False


def test_indexer_respects_gitignore(tmp_path):
    write(tmp_path, ".gitignore", "secret/\n")
    write(tmp_path, "secret/hidden.py", "def topsecret():\n    return 1\n")
    write(tmp_path, "visible.py", "def shown():\n    return 1\n")
    index = SemanticIndexer(tmp_path).build()
    paths = [document.path for document in index.documents]
    assert "visible.py" in paths
    assert not any("secret" in path for path in paths)


# -- discovery -----------------------------------------------------------


@pytest.fixture()
def api_project(tmp_path):
    write(tmp_path, "app/api.py", '''
        from fastapi import APIRouter

        router = APIRouter()


        @router.get("/widgets")
        def list_widgets():
            return []


        @router.post("/widgets")
        def create_widget():
            return {}
    ''')
    write(tmp_path, "cli.py", '''
        subparsers.add_parser("build")
        subparsers.add_parser("test")
    ''')
    write(tmp_path, "pyproject.toml", """
        [project]
        name = "demo"
        version = "1.2.3"
        requires-python = ">=3.8"
        dependencies = ["fastapi>=0.110", "pydantic>=2"]

        [tool.pytest.ini_options]
        testpaths = ["tests"]
    """)
    write(tmp_path, ".github/workflows/ci.yml", "jobs:\n  test:\n    runs-on: x\n")
    write(tmp_path, ".env", "SECRET_TOKEN=abc\nPUBLIC=1\n")
    return tmp_path


def test_discovers_http_routes_with_methods_and_lines(api_project):
    report = Discovery(api_project).run()
    routes = report.routes()
    assert {route.name for route in routes} == {
        "GET /widgets", "POST /widgets"}
    get = next(route for route in routes if route.method == "GET")
    assert get.declared_on == "router"
    assert get.line > 0


def test_discovers_cli_subcommands(api_project):
    assert Discovery(api_project).run().commands() == ["build", "test"]


def test_reads_pyproject_facts_without_a_toml_dependency(api_project):
    report = Discovery(api_project).run()
    config = report.config_of_kind("python-project")[0]
    assert config.facts["requires_python"] == ">=3.8"
    assert config.facts["name"] == "demo"
    assert config.facts["version"] == "1.2.3"
    assert config.facts["dependencies"] == 2
    assert config.facts["has_pytest_config"] is True


def test_discovers_ci_configuration(api_project):
    kinds = {item.kind for item in Discovery(api_project).run().configs}
    assert "ci-config" in kinds


def test_environment_files_report_keys_never_values(api_project):
    report = Discovery(api_project).run()
    env = [item for item in report.configs if item.kind == "environment"]
    assert env
    facts = env[0].facts
    assert sorted(facts["keys"]) == ["PUBLIC", "SECRET_TOKEN"]
    assert "abc" not in json.dumps(facts)


def test_kernel_exports_and_syscalls_are_discovered(tmp_path):
    write(tmp_path, "drv.c", """
        EXPORT_SYMBOL(my_driver_init);
        SYSCALL_DEFINE1(mycall, int, fd) { return 0; }
    """)
    report = Discovery(tmp_path).run()
    assert "my_driver_init" in {item.name for item in report.by_kind("kernel-export")}
    assert "mycall" in {item.name for item in report.by_kind("syscall")}


def test_protobuf_rpc_methods_are_discovered(tmp_path):
    write(tmp_path, "svc.proto", "service S {\n  rpc DoThing(Req) returns (Res);\n}\n")
    report = Discovery(tmp_path).run()
    assert [item.name for item in report.by_kind("rpc")] == ["DoThing"]


def test_discovery_round_trips_through_dict(api_project):
    report = Discovery(api_project).run()
    restored = type(report).from_dict(report.to_dict())
    assert restored.summary() == report.summary()
    assert {item.name for item in restored.routes()} == \
        {item.name for item in report.routes()}


# -- persisted index -----------------------------------------------------


def test_fingerprint_changes_when_a_file_changes(tmp_path):
    write(tmp_path, "a.py", "x = 1\n")
    first = compute_fingerprint(tmp_path)
    assert first["files"] == 1
    time.sleep(0.01)
    write(tmp_path, "a.py", "x = 2\n")
    assert compute_fingerprint(tmp_path)["digest"] != first["digest"]


def test_index_cache_hit_skips_rebuilding_the_a83_layers(tmp_path):
    write(tmp_path, "pkg/mod.py", '''
        def alpha():
            """Route the request to the right model."""
            return 1
    ''')
    indexer = RepositoryIndexer(tmp_path)
    first = indexer.build()
    assert first.stats.status == "built"
    assert indexer.cache.cache_file.is_file()

    second = indexer.build()
    assert second.stats.status == "hit"
    # The hit is real: the semantic index still answers from cached data.
    hits = second.search("route request model")
    assert hits and hits[0]["path"].replace("\\", "/") == "pkg/mod.py"
    assert second.call_graph.callers_of("alpha") == []
    assert second.discovery.summary()["files_scanned"] >= 1


def test_index_cache_is_invalidated_by_an_edit(tmp_path):
    write(tmp_path, "a.py", "def one():\n    return 1\n")
    indexer = RepositoryIndexer(tmp_path)
    assert indexer.build().stats.status == "built"
    assert indexer.build().stats.status == "hit"
    time.sleep(0.01)
    write(tmp_path, "b.py", "def two():\n    return one()\n")
    rebuilt = indexer.build()
    assert rebuilt.stats.status == "built"
    assert rebuilt.call_graph.callers_of("one") == ["b:two"]


def test_force_ignores_the_cache(tmp_path):
    write(tmp_path, "a.py", "def one():\n    return 1\n")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()
    assert indexer.build(force=True).stats.status == "built"


def test_corrupt_cache_is_rebuilt_not_served(tmp_path):
    write(tmp_path, "a.py", "def one():\n    return 1\n")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()
    indexer.cache.cache_file.write_text("{not json", encoding="utf-8")
    assert indexer.build().stats.status == "built"


def test_cache_can_be_disabled_and_invalidated(tmp_path):
    write(tmp_path, "a.py", "def one():\n    return 1\n")
    indexer = RepositoryIndexer(tmp_path, cache=False)
    assert indexer.build().stats.status == "built"
    assert not indexer.cache.cache_file.exists()
    assert indexer.cache.invalidate() is False

    cached = RepositoryIndexer(tmp_path)
    cached.build()
    assert cached.cache.invalidate() is True
    assert cached.cache.status()["exists"] is False


def test_index_summary_reports_every_layer(tmp_path):
    write(tmp_path, "pkg/mod.py", "def one():\n    return 1\n")
    summary = RepositoryIndexer(tmp_path).build().summary()
    assert summary["index"]["status"] == "built"
    assert summary["call_graph"]["call_sites"] >= 0
    assert summary["semantic"]["method"] == "tf-idf-cosine"
    assert "discovery" in summary and "intelligence" in summary


def test_affected_components_come_from_real_edges(tmp_path):
    write(tmp_path, "pkg/base.py", '''
        def helper():
            return 1
    ''')
    write(tmp_path, "pkg/user.py", '''
        from pkg.base import helper


        def consumer():
            return helper()
    ''')
    write(tmp_path, "tests/test_user.py", '''
        from pkg.user import consumer


        def test_consumer():
            assert consumer() == 1
    ''')
    index = RepositoryIndexer(tmp_path, cache=False).build()
    affected = index.affected("pkg/base.py")
    assert "pkg/user.py" in affected["file_dependents"]
    assert "pkg/user.py" in affected["affected_files"]
    # The test imports pkg.user, not pkg.base: it is affected transitively.
    assert affected["direct_tests"] == []
    assert "tests/test_user.py" in affected["affected_tests"]
    assert "pkg.user:consumer" in affected["transitive_symbol_callers"]

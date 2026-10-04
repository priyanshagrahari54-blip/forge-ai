from forge.intelligence.multilanguage import build_multilanguage_index
from forge.intelligence.persistent_index import PersistentRepositoryIndex


def test_multilanguage_index_detects_common_sources(tmp_path):
    (tmp_path/"app.ts").write_text("export function buildApp() {}\n")
    (tmp_path/"main.go").write_text("package main\nfunc main() {}\n")
    index=build_multilanguage_index(tmp_path)
    assert {r.language for r in index.records} == {"typescript","go"}
    assert any("buildApp" in r.symbols for r in index.records)


def test_persistent_index_refreshes_and_searches(tmp_path):
    (tmp_path/"main.rs").write_text("fn launch() {}\n")
    idx=PersistentRepositoryIndex.open(tmp_path)
    result=idx.refresh()
    assert result["files"] == 1
    assert (tmp_path/".forge"/"repository-index.json").exists()
    assert idx.search("launch")

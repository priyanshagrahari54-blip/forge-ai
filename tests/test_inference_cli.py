"""Session 11 — the operator surface: ``forge models`` and ``forge infer``.

Every command here runs the real CLI against a real local artifact written
into ``tmp_path`` (nothing is downloaded, nothing ships in the repository).
The point of the suite is honesty of the *printed* result: a verified model is
reported as verified, an unverified one is refused rather than substituted, the
deterministic rung is labelled non-neural, and the G560 profile never becomes
an inference machine.

Labels: ``REAL_INFERENCE_TEST`` (reference engine, local artifact) and
``DETERMINISTIC_TEST`` (the non-neural rung).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers_s11 import (  # noqa: E402
    DETERMINISTIC_TEST,
    REAL_INFERENCE_TEST,
    isolate_env,
    reference_model_id,
    run_cli,
)

MODEL = reference_model_id()


@pytest.fixture()
def model_dir(tmp_path: Any, monkeypatch: Any) -> Path:
    """An isolated cwd with one real reference artifact on disk."""
    isolate_env(monkeypatch, tmp_path)
    directory = Path(tmp_path) / "models"
    directory.mkdir(parents=True, exist_ok=True)
    code = run_cli(["forge", "models", "create-reference",
                    str(directory / "reference-clm.forgeref")])
    assert code == 0
    return directory


def out(capsys: Any) -> str:
    captured = capsys.readouterr()
    return captured.out


def both(capsys: Any) -> str:
    captured = capsys.readouterr()
    return captured.out + captured.err


def json_out(capsys: Any) -> Dict[str, Any]:
    text = out(capsys).strip()
    assert text.startswith("{"), text[:200]
    return json.loads(text)


def argv(directory: Path, *rest: str) -> List[str]:
    return list(rest) + ["--reference-dir", str(directory)]


# -- artifact creation is explicit ---------------------------------------------------


def test_create_reference_writes_a_real_local_artifact(tmp_path, monkeypatch,
                                                      capsys):
    """REAL_INFERENCE_TEST: no download, no weights in git, fully local."""
    isolate_env(monkeypatch, tmp_path)
    destination = Path(tmp_path) / "artifacts" / "reference-clm.forgeref"
    code = run_cli(["forge", "models", "create-reference", str(destination)])
    assert code == 0
    printed = out(capsys)
    assert "Reference artifact written" in printed
    assert "trained=False" in printed or "trained" in printed
    assert destination.exists()
    assert destination.stat().st_size > 1024


def test_create_reference_never_overwrites_without_force(tmp_path, monkeypatch,
                                                         capsys):
    isolate_env(monkeypatch, tmp_path)
    destination = Path(tmp_path) / "reference-clm.forgeref"
    assert run_cli(["forge", "models", "create-reference",
                    str(destination)]) == 0
    capsys.readouterr()
    #: Second write without --force is a refusal, not a silent clobber.
    assert run_cli(["forge", "models", "create-reference",
                    str(destination)]) == 1
    assert "Could not write" in both(capsys)
    assert run_cli(["forge", "models", "create-reference", str(destination),
                    "--force"]) == 0
    assert "Reference artifact written" in out(capsys)


def test_create_reference_json_reports_shape_and_fingerprint(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    isolate_env(monkeypatch, tmp_path)
    destination = Path(tmp_path) / "reference-clm.forgeref"
    code = run_cli(["forge", "models", "create-reference", str(destination),
                    "--json", "--vocab-size", "64", "--hidden-size", "16",
                    "--seed", "7"])
    assert code == 0
    payload = json_out(capsys)
    assert payload["path"].endswith(".forgeref")
    assert payload["size_bytes"] == destination.stat().st_size
    #: A bare sha256 hex digest of the exact bytes that were written.
    assert len(payload["fingerprint"]) == 64
    assert all(char in "0123456789abcdef" for char in payload["fingerprint"])
    assert payload["trained"] is False
    assert payload["config"]["vocab_size"] == 64
    assert payload["config"]["hidden_size"] == 16


def test_create_reference_requires_a_destination(tmp_path, monkeypatch,
                                                 capsys):
    isolate_env(monkeypatch, tmp_path)
    #: argparse refuses the missing positional; run_cli reports its code.
    assert run_cli(["forge", "models", "create-reference"]) == 2
    assert "required" in both(capsys).lower()


# -- discovery, verification, status --------------------------------------------------


def test_models_discover_lists_a_real_model(model_dir, capsys):
    """REAL_INFERENCE_TEST"""
    code = run_cli(argv(model_dir, "forge", "models", "discover"))
    assert code == 0
    printed = out(capsys)
    assert "Discovery:" in printed
    assert MODEL in printed
    #: Discovered is not verified: the CLI says so.
    assert "availability=discovered verification=unverified" in printed


def test_models_discover_json_reports_states(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "discover", "--json"))
    assert code == 0
    payload = json_out(capsys)
    assert payload["count"] == 1
    entry = payload["models"][0]
    assert entry["model_id"] == MODEL
    assert entry["verification_state"] == "unverified"
    assert entry["local"] is True
    assert entry["capabilities"] == []
    assert payload["discovery"]["per_backend"]


def test_discovery_without_artifacts_is_honest(tmp_path, monkeypatch, capsys):
    isolate_env(monkeypatch, tmp_path)
    empty = Path(tmp_path) / "empty"
    empty.mkdir()
    code = run_cli(["forge", "models", "discover", "--reference-dir",
                    str(empty)])
    assert code == 0
    printed = out(capsys)
    assert "0 model(s) registered" in printed
    assert "Nothing is downloaded automatically" in printed


def test_models_verify_promotes_only_with_real_checks(model_dir, capsys):
    """REAL_INFERENCE_TEST: fingerprint + health + probe generation."""
    code = run_cli(argv(model_dir, "forge", "models", "verify"))
    assert code == 0
    printed = out(capsys)
    assert "Verification (1/1 verified)" in printed
    assert "verified=True" in printed
    #: The individual checks are printed, so a human can see what passed.
    assert "artifact_fingerprint" in printed or "fingerprint" in printed
    assert "backend_reachable" in printed


def test_models_verify_json_carries_the_checks(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "verify", "--json"))
    assert code == 0
    payload = json_out(capsys)
    assert payload["verified"] == 1
    result = payload["results"][0]
    assert result["verified"] is True
    assert result["model_id"] == MODEL
    names = [check["name"] for check in result["checks"]]
    assert "backend_reachable" in names
    assert all(check["ok"] or check.get("skipped") for check in
               result["checks"])


def test_verify_refuses_when_there_is_nothing_to_verify(tmp_path, monkeypatch,
                                                        capsys):
    isolate_env(monkeypatch, tmp_path)
    empty = Path(tmp_path) / "empty"
    empty.mkdir()
    code = run_cli(["forge", "models", "verify", "--reference-dir",
                    str(empty)])
    assert code == 1
    printed = both(capsys)
    assert "Verification (0/0 verified)" in printed
    assert "NOT VERIFIED" in printed

    #: `forge infer --verify` says the same thing in its own words.
    code = run_cli(["forge", "infer", "hello", "--verify", "--reference-dir",
                    str(empty)])
    assert code == 1
    assert "Nothing to verify" in both(capsys)


def test_a_corrupt_artifact_is_never_registered_as_a_model(tmp_path,
                                                           monkeypatch,
                                                           capsys):
    """REAL_INFERENCE_TEST: a broken container is not a model (§7, §25).

    Each CLI invocation builds a fresh registry, so a *changed* artifact is
    simply fingerprinted again; what must never happen is a corrupt or
    implausible container being offered as a verified model. (Detecting a
    change against a recorded fingerprint is covered in
    ``test_inference_reference_engine.py``.)
    """
    isolate_env(monkeypatch, tmp_path)
    directory = Path(tmp_path) / "models"
    directory.mkdir()
    path = directory / "reference-clm.forgeref"
    assert run_cli(["forge", "models", "create-reference", str(path)]) == 0
    capsys.readouterr()
    data = bytearray(path.read_bytes())
    data[0:8] = b"NOTFORGE"          # the magic bytes are gone
    path.write_bytes(bytes(data))

    code = run_cli(["forge", "models", "discover", "--reference-dir",
                    str(directory)])
    assert code == 0
    assert "0 model(s) registered" in out(capsys)

    code = run_cli(["forge", "models", "verify", "--reference-dir",
                    str(directory)])
    assert code == 1
    printed = both(capsys)
    assert "NOT VERIFIED" in printed
    assert "never promotes CONFIGURED to READY" in printed


def test_models_status_reports_registry_and_residency(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "status"))
    assert code == 0
    printed = out(capsys)
    assert "Model registry: 1 model(s)" in printed
    assert "Residency:" in printed
    assert "duplicate_loads_avoided=0" in printed
    assert "availability=discovered verification=unverified" in printed
    assert "unverified" in printed


def test_models_status_for_one_model_says_it_is_not_resident(model_dir,
                                                             capsys):
    code = run_cli(argv(model_dir, "forge", "models", "status", MODEL))
    assert code == 0
    printed = out(capsys)
    assert MODEL in printed
    assert "resident: no (not loaded)" in printed
    #: The CLI does not pretend a per-process cache is a shared one.
    assert "CLI state is per-process" in printed


def test_models_load_then_unload_is_honest_about_process_state(model_dir,
                                                               capsys):
    """REAL_INFERENCE_TEST"""
    code = run_cli(argv(model_dir, "forge", "models", "load", MODEL))
    assert code == 0
    printed = out(capsys)
    assert "Loaded %s on backend reference" % MODEL in printed
    assert "resource: allowed=True" in printed

    #: A fresh process has a fresh residency cache: the honest answer is
    #: "not resident", plus the note explaining why.
    code = run_cli(argv(model_dir, "forge", "models", "unload", MODEL))
    assert code == 1
    printed = both(capsys)
    assert "Not unloaded" in printed
    assert "fresh residency cache" in printed


def test_models_load_requires_an_id(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "load"))
    assert code == 2
    assert "model id is required" in both(capsys).lower()


def test_models_backends_reports_kind_and_reachability(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "backends", "--offline"))
    assert code == 0
    printed = out(capsys)
    assert "Backends (probed=False)" in printed
    assert "reference: kind=custom configured=True" in printed
    assert "local=True network=False" in printed
    #: The native adapter is listed as what it is: configured, not reachable.
    assert "native: kind=native configured=True reachable=False" in printed
    assert "will not fabricate output" in printed


def test_models_backends_json_is_machine_readable(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "backends", "--json",
                        "--offline"))
    assert code == 0
    payload = json_out(capsys)
    ids = [item["backend_id"] for item in payload["backends"]]
    assert "reference" in ids
    entry = [item for item in payload["backends"]
             if item["backend_id"] == "reference"][0]
    assert entry["local"] is True
    assert entry["requires_network"] is False
    #: ``ready`` is derived, never asserted.
    assert entry["ready"] is (entry["configured"] and entry["reachable"]
                              and entry["verified"] and not entry["denial"])


def test_models_evidence_is_empty_in_a_fresh_process(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "evidence"))
    assert code == 0
    printed = out(capsys)
    assert "proposal only" in printed
    assert "(empty)" in printed
    assert "GET /api/v1/inference/status" in printed


# -- forge infer ------------------------------------------------------------------------


def test_infer_with_verification_is_real_neural_output(model_dir, capsys):
    """REAL_INFERENCE_TEST: a real forward pass, labelled as such."""
    code = run_cli(argv(model_dir, "forge", "infer", "hello there",
                        "--verify", "--strict-neural", "-c", ""))
    assert code == 0
    printed = out(capsys)
    assert "provenance: model=%s backend=reference neural=True" % MODEL \
        in printed
    assert "state=succeeded" in printed
    assert "verification=verified availability=ready" in printed
    assert "NOTE: answered by the deterministic" not in printed


def test_infer_without_verification_falls_back_and_says_so(model_dir, capsys):
    """DETERMINISTIC_TEST: the honest rung, never a fake model answer."""
    code = run_cli(argv(model_dir, "forge", "infer", "hello there", "-c", ""))
    assert code == 0
    printed = out(capsys)
    assert "neural=False" in printed
    assert "backend=deterministic" in printed
    assert "NOTE: answered by the deterministic non-neural fallback" in printed
    assert "not model output" in printed


def test_infer_refuses_when_neural_output_is_required(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "infer", "hello",
                        "--no-deterministic", "-c", ""))
    assert code == 1
    printed = both(capsys)
    assert "REFUSED/FAILED: state=unverified" in printed
    assert "error_code=UNVERIFIED" in printed


def test_infer_strict_neural_refuses_the_deterministic_rung(model_dir, capsys):
    """DETERMINISTIC_TEST: a demanded model answer is not silently replaced."""
    code = run_cli(argv(model_dir, "forge", "infer", "hello",
                        "--strict-neural", "-c", ""))
    assert code == 1
    printed = both(capsys)
    assert "REFUSED: --strict-neural was set" in printed
    assert "backend=deterministic" in printed
    assert "neural=False" in printed


def test_infer_never_relaxes_a_capability_filter(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "infer", "draw a picture",
                        "--verify", "--no-deterministic", "-c",
                        "image_generation"))
    assert code == 1
    printed = both(capsys)
    assert "REFUSED/FAILED: state=needs_model" in printed
    explain = run_cli(argv(model_dir, "forge", "infer", "draw a picture",
                           "--verify", "--no-deterministic", "-c",
                           "image_generation", "--explain"))
    assert explain == 1
    detailed = both(capsys)
    assert "missing capabilities" in detailed


def test_infer_stream_prints_deltas_once(model_dir, capsys):
    """REAL_INFERENCE_TEST"""
    code = run_cli(argv(model_dir, "forge", "infer", "stream me", "--verify",
                        "--stream", "-c", ""))
    assert code == 0
    printed = out(capsys)
    assert "stream: events=" in printed
    assert "complete=True" in printed
    assert "ttft=" in printed
    #: The generated text is echoed as it streams, then reported once in the
    #: provenance block -- never printed a second time as a whole.
    body = printed.split("provenance:")[0]
    assert body.strip(), "nothing was streamed to stdout"
    assert printed.count(body.strip()) == 1


def test_infer_explain_prints_the_whole_decision(model_dir, capsys):
    """REAL_INFERENCE_TEST"""
    code = run_cli(argv(model_dir, "forge", "infer", "explain yourself",
                        "--verify", "--explain", "-c", ""))
    assert code == 0
    printed = out(capsys)
    assert "routing: model=%s" % MODEL in printed
    assert "selected=%s backend=reference state=routed" % MODEL in printed
    assert "resource: allowed=True profile=default" in printed
    assert "policy:" in printed
    assert "context: limit=" in printed
    assert "ladder: 1 preferred_verified/neural" in printed
    assert "observed: routing -> routed" in printed


def test_infer_json_is_machine_readable(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "infer", "json please", "--verify",
                        "--json", "-c", ""))
    assert code == 0
    payload = json_out(capsys)
    assert payload["success"] is True
    assert payload["neural"] is True
    assert payload["model_id"] == MODEL
    assert payload["backend_id"] == "reference"
    assert payload["verification_state"] == "verified"
    assert payload["state"] == "succeeded"
    assert payload["text"]
    assert payload["via"] == "local"
    assert payload["request_id"].startswith("inf-")
    #: Internal markers never reach the machine-readable payload.
    assert not [key for key in payload if key.startswith("_")]


def test_infer_requires_a_prompt(model_dir, capsys, monkeypatch):
    #: stdin is a terminal for this check, so nothing is read from it.
    monkeypatch.setattr(sys, "stdin", _TtyStdin())
    code = run_cli(argv(model_dir, "forge", "infer"))
    assert code == 2
    assert "A prompt is required" in both(capsys)


class _TtyStdin:
    """Stand-in for an interactive stdin (pytest replaces the real one)."""

    def isatty(self) -> bool:
        return True

    def read(self) -> str:
        return ""


def test_infer_reads_a_prompt_file(model_dir, tmp_path, capsys):
    path = Path(tmp_path) / "prompt.txt"
    path.write_text("from a file", encoding="utf-8")
    code = run_cli(argv(model_dir, "forge", "infer", "--verify", "-c", "",
                        "--prompt-file", str(path)))
    assert code == 0
    assert "neural=True" in out(capsys)


def test_infer_reports_an_unreadable_prompt_file(model_dir, tmp_path, capsys):
    code = run_cli(argv(model_dir, "forge", "infer",
                        "--prompt-file", str(Path(tmp_path) / "missing.txt")))
    assert code == 2
    assert "cannot read --prompt-file" in both(capsys)


def test_infer_under_g560_never_runs_a_local_model(model_dir, capsys):
    """REAL_INFERENCE_TEST under the Win7/32-bit/2GB profile (§12, §22)."""
    code = run_cli(argv(model_dir, "forge", "infer", "hello", "--verify",
                        "--no-deterministic", "--resource-profile", "g560",
                        "-c", ""))
    assert code == 1
    printed = both(capsys)
    assert "REFUSED/FAILED: state=resource_denied" in printed
    assert "neural=True" not in printed

    explain = run_cli(argv(model_dir, "forge", "infer", "hello", "--verify",
                           "--no-deterministic", "--resource-profile", "g560",
                           "--explain", "-c", ""))
    assert explain == 1
    detailed = out(capsys)
    assert "resource: allowed=False profile=g560" in detailed
    assert "model_loading=False" in detailed


def test_models_load_under_g560_is_refused(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "models", "load", MODEL,
                        "--resource-profile", "g560"))
    assert code == 1
    printed = both(capsys)
    assert "REFUSED to load" in printed
    assert "g560" in printed


def test_infer_bounds_are_clamped_not_trusted(model_dir, capsys):
    code = run_cli(argv(model_dir, "forge", "infer", "bounded", "--verify",
                        "--json", "-c", "", "--max-tokens", "16"))
    assert code == 0
    payload = json_out(capsys)
    assert payload["success"] is True
    assert payload["output_tokens"] <= 64
    #: An impossible token count is refused by the parser, not guessed at.
    assert run_cli(argv(model_dir, "forge", "infer", "bounded", "--verify",
                        "-c", "", "--max-tokens", "not-a-number")) == 2


def test_labels_are_declared():
    assert REAL_INFERENCE_TEST == "REAL_INFERENCE_TEST"
    assert DETERMINISTIC_TEST == "DETERMINISTIC_TEST"
    assert __doc__ and "REAL_INFERENCE_TEST" in __doc__

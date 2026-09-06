from pathlib import Path
from forge.self_development import BuildVerifier, SecurityScanner


def test_security_scanner_detects_secret(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "clean.py").write_text("x = 10\n", encoding="utf-8")

    scanner = SecurityScanner(root=repo_dir)
    base_res = scanner.scan()
    assert base_res.passed is True
    assert len(base_res.findings) == 0

    # Introduce hardcoded secret
    (repo_dir / "secret.py").write_text('api_key = "1234567890abcdef"\n', encoding="utf-8")
    cand_res = scanner.scan()
    assert cand_res.passed is False
    assert len(cand_res.findings) == 1

    comparison = scanner.compare(base_res, cand_res)
    assert comparison.passed is False
    assert len(comparison.new_findings) == 1


def test_build_verifier(tmp_path: Path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "good.py").write_text("x = 10\n", encoding="utf-8")

    verifier = BuildVerifier(root=repo_dir)
    res = verifier.verify_build()
    assert res.passed is True

    # Introduce syntax error
    (repo_dir / "bad_syntax.py").write_text("def foo(:\n", encoding="utf-8")
    bad_res = verifier.verify_build()
    assert bad_res.passed is False
    assert bad_res.exit_code != 0

from __future__ import annotations

import tempfile
from pathlib import Path
from forge.tools.checkpoint import CheckpointManager


def test_checkpoint_creates_and_rolls_back():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        (tmp_path / "file1.txt").write_text("original content")

        cm = CheckpointManager(tmpdir)
        chk_id = cm.create_checkpoint(target_files=["file1.txt", "file2.txt"])

        # Modify file1 and create file2
        (tmp_path / "file1.txt").write_text("modified content")
        (tmp_path / "file2.txt").write_text("new file")

        assert (tmp_path / "file1.txt").read_text() == "modified content"
        assert (tmp_path / "file2.txt").exists()

        # Rollback
        cm.rollback(chk_id)

        assert (tmp_path / "file1.txt").read_text() == "original content"
        assert not (tmp_path / "file2.txt").exists()

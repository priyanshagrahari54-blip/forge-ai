"""Project/session workspace isolation with deterministic filesystem boundaries."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import re

_SAFE=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

@dataclass(frozen=True)
class Workspace:
    root: Path
    project_id: str
    session_id: str

    @property
    def project_root(self) -> Path:
        return self.root / "projects" / self.project_id
    @property
    def session_root(self) -> Path:
        return self.project_root / "sessions" / self.session_id

class WorkspaceManager:
    def __init__(self, root: str|Path=".forge"):
        self.root=Path(root)

    def open(self, project_id: str, session_id: str) -> Workspace:
        if not _SAFE.fullmatch(project_id) or not _SAFE.fullmatch(session_id):
            raise ValueError("invalid project/session id")
        ws=Workspace(self.root,project_id,session_id)
        ws.session_root.mkdir(parents=True,exist_ok=True)
        return ws

    def contains(self, ws: Workspace, path: str|Path) -> bool:
        target=Path(path).resolve()
        base=ws.session_root.resolve()
        try: target.relative_to(base); return True
        except ValueError: return False

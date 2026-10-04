"""Incremental persistent repository intelligence cache."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge.intelligence.multilanguage import EXTENSIONS, SKIP, SourceRecord, build_multilanguage_index

@dataclass
class PersistentRepositoryIndex:
    root: Path
    cache_path: Path
    records: dict[str, SourceRecord]

    @classmethod
    def open(cls, root: str|Path=".") -> "PersistentRepositoryIndex":
        base=Path(root).resolve()
        path=base/".forge"/"repository-index.json"
        records={}
        try:
            payload=json.loads(path.read_text(encoding="utf-8"))
            for item in payload.get("records",[]):
                records[item["path"]]=SourceRecord(
                    item["path"],item["language"],int(item["size"]),
                    tuple(item.get("symbols",[])),tuple(item.get("imports",[])))
        except (OSError,ValueError,KeyError,TypeError):
            pass
        return cls(base,path,records)

    def _fingerprint(self,path: Path) -> str:
        stat=path.stat()
        raw=f"{stat.st_size}:{stat.st_mtime_ns}".encode()
        return hashlib.sha256(raw).hexdigest()

    def refresh(self) -> dict[str,Any]:
        fresh=build_multilanguage_index(self.root)
        incoming={r.path:r for r in fresh.records}
        changed=[]; removed=[]
        for path,record in incoming.items():
            old=self.records.get(path)
            if old is None or old.size!=record.size:
                changed.append(path)
        removed=sorted(set(self.records)-set(incoming))
        self.records=incoming
        self.cache_path.parent.mkdir(parents=True,exist_ok=True)
        payload={"version":1,"records":[
            {"path":r.path,"language":r.language,"size":r.size,
             "symbols":list(r.symbols),"imports":list(r.imports)}
            for r in sorted(self.records.values(),key=lambda x:x.path)]}
        tmp=self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload,indent=2),encoding="utf-8")
        tmp.replace(self.cache_path)
        return {"files":len(self.records),"changed":sorted(changed),
                "removed":removed,"cache":str(self.cache_path)}

    def search(self,query:str) -> list[SourceRecord]:
        q=query.lower().strip()
        return [r for r in self.records.values() if q in r.path.lower() or
                any(q in s.lower() for s in r.symbols)]

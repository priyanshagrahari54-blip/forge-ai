"""First-class engineering knowledge, project-scoped and provenance-aware."""
from __future__ import annotations
import json, sqlite3, time, uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

@dataclass(frozen=True)
class KnowledgeObject:
    project_id: str
    kind: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""
    confidence: float = 1.0
    id: str = ""
    updated_at: float = 0.0
    def materialized(self) -> "KnowledgeObject":
        return KnowledgeObject(self.project_id,self.kind,self.name,self.data,self.source,
            max(0.0,min(1.0,self.confidence)),self.id or uuid.uuid4().hex,
            self.updated_at or time.time())
    def to_dict(self):
        x=self.materialized()
        return {"id":x.id,"project_id":x.project_id,"kind":x.kind,"name":x.name,
                "data":x.data,"source":x.source,"confidence":x.confidence,"updated_at":x.updated_at}

class EngineeringKnowledgeStore:
    """SQLite-backed store; project_id is a hard isolation boundary."""
    def __init__(self, path: str|Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(str(self.path))
        self.db.execute("""CREATE TABLE IF NOT EXISTS knowledge(
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL,
          name TEXT NOT NULL, data TEXT NOT NULL, source TEXT, confidence REAL,
          updated_at REAL NOT NULL, UNIQUE(project_id,kind,name))""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_project_kind ON knowledge(project_id,kind)")
        self.db.commit()
    def upsert(self, obj: KnowledgeObject)->KnowledgeObject:
        x=obj.materialized()
        self.db.execute("""INSERT INTO knowledge VALUES(?,?,?,?,?,?,?,?)
          ON CONFLICT(project_id,kind,name) DO UPDATE SET
          data=excluded.data,source=excluded.source,confidence=excluded.confidence,
          updated_at=excluded.updated_at""",
          (x.id,x.project_id,x.kind,x.name,json.dumps(x.data,sort_keys=True),
           x.source,x.confidence,x.updated_at))
        self.db.commit(); return x
    def add_many(self, items: Iterable[KnowledgeObject])->int:
        for item in items: self.upsert(item)
        return len(list(items)) if not isinstance(items,list) else len(items)
    def query(self, project_id: str, kind: str|None=None, text: str|None=None)->list[KnowledgeObject]:
        sql="SELECT id,project_id,kind,name,data,source,confidence,updated_at FROM knowledge WHERE project_id=?"
        args=[project_id]
        if kind: sql+=" AND kind=?"; args.append(kind)
        if text: sql+=" AND (name LIKE ? OR data LIKE ?)"; args.extend([f"%{text}%",f"%{text}%"])
        rows=self.db.execute(sql+" ORDER BY updated_at DESC",args).fetchall()
        return [KnowledgeObject(r[1],r[2],r[3],json.loads(r[4]),r[5] or "",r[6],r[0],r[7]) for r in rows]
    def close(self): self.db.close()

def from_plan(plan, project_id: str)->list[KnowledgeObject]:
    out=[]
    for c in plan.components:
        out.append(KnowledgeObject(project_id,"architecture",c.name,c.to_dict(),"architect",.95))
    for d in plan.dependencies:
        out.append(KnowledgeObject(project_id,"dependency",d.name,d.to_dict(),"architect",.95))
    for d in plan.decisions:
        out.append(KnowledgeObject(project_id,"decision",d.id,d.to_dict(),"architect",.95))
    return out

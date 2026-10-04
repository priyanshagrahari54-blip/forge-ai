"""Project-isolated memory facade."""
class ProjectMemory:
    def __init__(self,store,project_id): self.store,self.project_id=store,project_id
    def put(self,item): return self.store.upsert(item)
    def query(self,kind=None): return self.store.query(project_id=self.project_id,kind=kind)

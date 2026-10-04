"""Bounded in-memory capability resolution cache."""
class ResolutionCache:
    def __init__(self,max_items=256): self.max_items=max_items; self._data={}
    def get(self,key): return self._data.get(key)
    def put(self,key,value):
        self._data[key]=value
        while len(self._data)>self.max_items:self._data.pop(next(iter(self._data)))

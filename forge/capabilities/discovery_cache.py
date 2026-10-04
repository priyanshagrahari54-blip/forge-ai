"""Bounded in-memory cache for metadata-only capability discovery."""
class DiscoveryCache:
    def __init__(self,max_items=256):self.max_items=max_items;self._data={}
    def get(self,key):return self._data.get(key)
    def put(self,key,value):
        if key not in self._data and len(self._data)>=self.max_items:self._data.pop(next(iter(self._data)))
        self._data[key]=value

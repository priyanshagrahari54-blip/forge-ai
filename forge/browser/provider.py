"""Provider-neutral browser/computer-use gateway."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Protocol

@dataclass(frozen=True)
class BrowserAction:
    kind:str
    target:str=""
    value:str=""

class BrowserProvider(Protocol):
    def health(self)->dict[str,Any]: ...
    def screenshot(self)->Any: ...
    def act(self,action:BrowserAction)->Any: ...

class BrowserGateway:
    def __init__(self,provider:BrowserProvider|None=None,*,enabled:bool=False):
        self.provider=provider; self.enabled=enabled
    def health(self):
        return dict(self.provider.health()) if self.provider else {"available":False,"reason":"no provider"}
    def act(self,action:BrowserAction,*,approved:bool=False):
        if not self.enabled or not self.provider: raise RuntimeError("browser provider unavailable")
        if not approved: raise PermissionError("browser action requires approval")
        return self.provider.act(action)

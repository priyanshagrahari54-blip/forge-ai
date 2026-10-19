"""Truthful hardware/VM capability profile; no guessed hardware support."""
from dataclasses import dataclass,field
import os,platform,shutil
@dataclass(frozen=True)
class HardwareProfile:
    os:str
    architecture:str
    cpu_count:int
    qemu_x86_64:bool
    tools:dict[str,bool]=field(default_factory=dict)
    def to_dict(self): return {"os":self.os,"architecture":self.architecture,"cpu_count":self.cpu_count,"qemu_x86_64":self.qemu_x86_64,"tools":dict(self.tools)}
def detect_hardware()->HardwareProfile:
    tools={x:bool(shutil.which(x)) for x in ("qemu-system-x86_64","git","docker","node","python")}
    return HardwareProfile(platform.system(),platform.machine(),os.cpu_count() or 1,tools["qemu-system-x86_64"],tools)

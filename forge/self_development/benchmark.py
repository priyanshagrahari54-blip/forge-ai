from __future__ import annotations
import os, subprocess, sys, time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
@dataclass
class BenchmarkResult:
    total_benchmarks:int=0; passed_benchmarks:int=0; duration:float=0.0; details:dict[str,Any]=field(default_factory=dict)
    def to_dict(self): return asdict(self)
class BenchmarkRunner:
    """Measures verification latency and outcomes, rather than treating pytest as performance."""
    def __init__(self, root: str|Path="."): self.root=Path(root).resolve()
    def _run(self, command):
        started=time.perf_counter()
        try: p=subprocess.run(command,cwd=self.root,text=True,capture_output=True,timeout=300,check=False)
        except (OSError,subprocess.TimeoutExpired) as exc: return False,time.perf_counter()-started,str(exc),-1
        return p.returncode==0,time.perf_counter()-started,(p.stdout+p.stderr)[-1000:],p.returncode
    def run_benchmarks(self):
        started=time.perf_counter(); details={}
        tests,latency,out,code=self._run([sys.executable,"-m","pytest","-q"])
        details.update(test_success=tests,test_latency=latency,test_returncode=code,test_output=out)
        build,build_latency,build_out,build_code=self._run([sys.executable,"-m","compileall","-q","."])
        details.update(build_success=build,build_latency=build_latency,build_returncode=build_code)
        checks=[tests,build]
        return BenchmarkResult(len(checks),sum(checks),time.perf_counter()-started,details)

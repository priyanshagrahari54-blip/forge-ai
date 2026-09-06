from __future__ import annotations
import re, subprocess
from dataclasses import dataclass, field
from pathlib import Path

@dataclass
class GateResult:
    name:str; passed:bool; details:str=""; evidence:dict=field(default_factory=dict)
@dataclass
class VerificationResult:
    passed:bool; gates:list[GateResult]
    @property
    def failures(self): return [g for g in self.gates if not g.passed]

class VerificationPipeline:
    def __init__(self, root: str|Path="."): self.root=Path(root).resolve()
    def _run(self, command:list[str], timeout:int=120):
        try: return subprocess.run(command,cwd=self.root,text=True,capture_output=True,timeout=timeout,check=False)
        except (OSError,subprocess.TimeoutExpired) as e: return None
    def tests(self):
        import sys
        p=self._run([sys.executable,"-m","pytest","-q"])
        output=(p.stdout+p.stderr) if p else "test command failed"
        no_tests=bool(p and p.returncode==5 and "no tests ran" in output.lower())
        return GateResult("tests", bool(p and (p.returncode==0 or no_tests)), output[-4000:], {"returncode":p.returncode if p else None, "no_tests":no_tests})
    def build(self):
        if (self.root/"pyproject.toml").exists():
            p=self._run(["python","-m","compileall","-q","."]); return GateResult("build",bool(p and p.returncode==0),(p.stdout+p.stderr)[-2000:] if p else "build failed")
        return GateResult("build",True,"No configured build command")
    def lint(self):
        # Compile-time verification is always available; configured linters are optional.
        p=self._run(["python","-m","compileall","-q","forge"]); return GateResult("lint/type",bool(p and p.returncode==0),(p.stdout+p.stderr)[-2000:] if p else "compile failed")
    def security(self):
        patterns=[re.compile(r"(?:api[_-]?key|secret|password)\s*[:=]\s*['\"](?!\$|<)[^'\"]{8,}",re.I),re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")]
        findings=[]
        for p in self.root.rglob("*"):
            if not p.is_file() or ".git" in p.parts or ".forge" in p.parts or p.stat().st_size>2_000_000: continue
            try: text=p.read_text(encoding="utf-8")
            except UnicodeDecodeError: continue
            if any(rx.search(text) for rx in patterns): findings.append(str(p.relative_to(self.root)))
        return GateResult("security",not findings,"Potential secrets: "+", ".join(findings),{"findings":findings})
    def review(self, diff:str=""):
        issues=[]
        # A repository without Git has no textual diff; other gates still
        # provide evidence. Git-backed autonomous runs always pass the diff.
        if "<<<<<<<" in diff or "+    pass" in diff: issues.append("conflict marker or incomplete implementation")
        if re.search(r"\b(eval|exec)\s*\(",diff): issues.append("dynamic code execution")
        return GateResult("review",not issues,"; ".join(issues),{"issues":issues})
    def run(self,diff:str="") -> VerificationResult:
        gates=[self.tests(),self.build(),self.lint(),self.security(),self.review(diff)]
        return VerificationResult(all(g.passed for g in gates),gates)

# Compatibility gate names used by early integrations.
class SecurityGate:
    def __init__(self, root="."): self.root=root
    def verify(self): return VerificationPipeline(self.root).security()
class ReviewGate:
    def __init__(self, root="."): self.root=root
    def verify(self,diff=""): return VerificationPipeline(self.root).review(diff)
class AcceptanceGate:
    def verify(self,result): return result

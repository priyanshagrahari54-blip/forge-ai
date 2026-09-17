"""Background runtime verification loop for Forge Server."""
from __future__ import annotations
import json, os, threading, time
from pathlib import Path
from typing import Any, Dict, Optional
from forge.models.configured_runtime import ConfiguredRuntime, ConfiguredRuntimeRegistry, RuntimeState
from forge.models.configured_runtime_bridge import sync_configured_runtimes
from forge.models.runtime_monitor import RuntimeMonitor
from forge.models.runtime_verification import RuntimeProbeResult, apply_probe_result

class RuntimeMonitorService:
    """Persistent runtime verification coordinator with live-registry feedback."""
    def __init__(self, fabric: Any, *, state_path: Any = ".forge/runtime-monitor.json", interval_seconds: float = 60.0, verification_ttl_seconds: float = 300.0) -> None:
        self.fabric= fabric; self.state_path=Path(state_path); self.interval_seconds=max(5.0,float(interval_seconds)); self.monitor=RuntimeMonitor(verification_ttl_seconds=verification_ttl_seconds); self.registry=ConfiguredRuntimeRegistry(); self._last_tick=0.0; self._lock=threading.RLock(); self._stop=threading.Event(); self._thread:Optional[threading.Thread]=None; self._load()
    def start(self)->None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive(): return
            self._stop.clear(); self._thread=threading.Thread(target=self._run,name="forge-runtime-monitor",daemon=True); self._thread.start()
    def stop(self,*,wait:bool=True)->None:
        with self._lock: thread=self._thread; self._thread=None; self._stop.set()
        if thread is not None and wait: thread.join(timeout=max(1.0,self.interval_seconds+1.0))
    @property
    def running(self)->bool:
        return bool(self._thread is not None and self._thread.is_alive())
    def _run(self)->None:
        while not self._stop.is_set():
            try:self.tick(force=True)
            except Exception:pass
            self._stop.wait(self.interval_seconds)
    def _set_model_available(self, model_registry: Any, runtime: ConfiguredRuntime, available: bool)->None:
        if model_registry is None:return
        try:
            model=model_registry.get(runtime.model_id)
            if model.provider == runtime.provider and not model.fallback: model.available=bool(available)
        except Exception:pass
    def tick(self,*,now:Optional[float]=None,force:bool=False)->Dict[str,Any]:
        checked_at=float(time.time() if now is None else now)
        with self._lock:
            if not force and checked_at-self._last_tick<self.interval_seconds:return self.snapshot()
            self._last_tick=checked_at; sync_configured_runtimes(self.fabric,self.registry); results=[]; model_registry=getattr(self.fabric,"registry",None)
            for runtime in self.registry.items():
                if runtime.state==RuntimeState.REVOKED.value:continue
                if runtime.state==RuntimeState.LIVE.value and not self.monitor.stale(runtime,now=checked_at):continue
                self._set_model_available(model_registry,runtime,False)
                try:
                    result=self.monitor.check(runtime,self._probe,now=checked_at)
                    if model_registry is not None: apply_probe_result(model_registry,result.probe)
                    results.append(result.to_dict())
                except Exception as exc:
                    runtime.mark_unavailable("runtime probe failed: %s"%type(exc).__name__); self._set_model_available(model_registry,runtime,False); results.append({"provider":runtime.provider,"model_id":runtime.model_id,"state":runtime.state,"changed":True,"error":type(exc).__name__})
            self._save(); return {"time":checked_at,"results":results,**self.snapshot()}
    def snapshot(self)->Dict[str,Any]:
        counts=self.registry.counts(); return {"schema_version":1,"running":self.running,"last_tick":self._last_tick,"interval_seconds":self.interval_seconds,"verification_ttl_seconds":self.monitor.verification_ttl_seconds,"counts":counts,"live":counts.get(RuntimeState.LIVE.value,0),"runtimes":self.registry.snapshot()}
    def _probe(self,provider_name:str,model_id:str)->RuntimeProbeResult:
        providers=getattr(self.fabric,"providers",None)
        if providers is None:return RuntimeProbeResult(model_id=model_id,ok=False,status="unavailable",reason="provider registry unavailable")
        provider=providers.get(provider_name)
        if provider is None:return RuntimeProbeResult(model_id=model_id,ok=False,status="unavailable",reason="provider is not registered")
        list_models=getattr(provider,"list_models",None)
        if callable(list_models):
            started=time.time(); models=list_models(); names=set()
            for item in models or []:
                value=(item.get("name") or item.get("id") or item.get("model")) if isinstance(item,dict) else item
                if value:names.add(str(value))
            latency=(time.time()-started)*1000.0
            if model_id not in names:return RuntimeProbeResult(model_id=model_id,ok=False,latency_ms=latency,status="not_found",reason="exact model is not available")
            return RuntimeProbeResult(model_id=model_id,ok=True,latency_ms=latency,status="healthy",reason="exact model listed by provider")
        return RuntimeProbeResult(model_id=model_id,ok=False,status="unverifiable",reason="provider has no exact model-list probe")
    def _save(self)->None:
        self.state_path.parent.mkdir(parents=True,exist_ok=True); temp=self.state_path.with_suffix(self.state_path.suffix+".tmp"); temp.write_text(json.dumps(self.snapshot(),sort_keys=True),encoding="utf-8"); os.replace(str(temp),str(self.state_path))
    def _load(self)->None:
        try:payload=json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError,ValueError,TypeError):return
        if not isinstance(payload,dict):return
        for item in payload.get("runtimes") or []:
            if not isinstance(item,dict):continue
            provider=str(item.get("provider") or "").strip(); model_id=str(item.get("model_id") or "").strip()
            if not provider:continue
            runtime=ConfiguredRuntime(provider=provider,model_id=model_id,endpoint=str(item.get("endpoint") or ""),state=str(item.get("state") or RuntimeState.UNCONFIGURED.value),capabilities=tuple(item.get("capabilities") or ()),verification_id=str(item.get("verification_id") or ""),last_reason=str(item.get("last_reason") or "")[:500],last_checked=float(item.get("last_checked") or 0.0))
            if self.registry.maybe_get(provider,model_id) is None:self.registry.register(runtime)
        self._last_tick=float(payload.get("last_tick") or 0.0)

"""Plugin SDK (A66): validated manifests, declarative registry.

Plugins are validated declarations. A plugin can only *claim*
capabilities; whether each claim is real is computed from what is
actually registered (agent executors, model providers) at bind
time — never from the manifest itself. Plugins never load foreign
code here, so installation cannot smuggle execution.
"""

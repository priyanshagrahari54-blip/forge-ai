# Forge repository cleanup policy

Tracked source should contain only reusable product code, tests, configuration, and durable architecture/documentation.

Generated or runtime-owned state belongs under `.forge/` and must stay untracked: databases, caches, indexes, model-training workspaces, and autobuild progress.

Historical audit documents may remain when they describe verified architecture or decisions; generated progress logs should not.

Model artifacts must never be committed. Model weights and adapters are operator-owned runtime assets and are loaded only through the Native Model Runtime and its verification gates.

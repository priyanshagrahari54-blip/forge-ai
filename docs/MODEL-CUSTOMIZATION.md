# Forge Model Customization

Forge keeps model customization separate from model execution and connects them through the same verified inference fabric.

Pipeline: collect high-quality engineering examples → sanitize and deduplicate → deterministic train/validation split → LoRA/QLoRA specialization → real training backend → benchmark → regression/promotion gate → artifact verification → normal Forge routing.

Specialization targets are `forge-coding`, `forge-debug`, `forge-security`, `forge-web`, and `forge-game3d`.

These names are training objectives, not claims that trained weights already exist. Model weights and adapters remain operator-owned runtime assets under `.forge/models/`, which is ignored by source control.

A customized model is production-usable only when it is registered, reachable, verified, resource-admissible, and policy-admissible. The deterministic reference backend remains a test mechanism and is never promoted as a real coding model.

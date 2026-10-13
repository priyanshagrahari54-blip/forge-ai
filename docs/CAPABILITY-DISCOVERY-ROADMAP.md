# Forge Capability Discovery & Adoption

## Runtime rule
Forge discovers capabilities when a task requires them. It does not bulk-install every framework or keep idle agents running.

1. Parse the requirement contract and identify capability gaps.
2. Search approved catalogs (Hugging Face Hub, GitHub/open-source, package registries, MCP catalogs, model-provider catalogs).
3. Collect provenance, maintainer/activity, version, license, dependencies, runtime requirements, price/quota, and compatibility.
4. Inspect source and dependency/supply-chain risks; external descriptions and files are untrusted data.
5. Run static checks and isolated smoke tests where executable code is involved.
6. Register the candidate as discovered/unverified; only the existing verification pipeline can promote it.
7. Build a narrow adapter and capability contract; never let discovery directly execute arbitrary code.
8. Record evidence, source revision, license, test results, limitations, and rollback route.
9. Re-evaluate stale candidates and provider availability before use.

## Hugging Face Hub
`forge.capabilities.huggingface.HuggingFaceHubDiscovery` provides bounded, read-only model metadata search. It does not download weights or run Spaces. Results are marked `discovered_unverified`; Hub visibility/download counts are not evidence of safety, license suitability, or runtime availability.

## Dynamic agent fabric
The historical agent catalogue is a seed, not a runtime ceiling. Task decomposition determines specialist demand; trusted executor availability, permissions, concurrency, memory, time, and provider quotas constrain actual execution. A logical agent definition is not reported as executable unless a real executor is bound.

## Large and free models
The model router may consider large hosted models, including 100B+ parameter classes, only when a configured provider confirms a model is reachable and usable. Parameter count is metadata, not proof of quality or availability. Free access is checked at runtime; no provider is treated as unlimited or permanently free. No paid use occurs silently.

## Build vs reuse
Use REUSE → ADAPT → COMPOSE → EXTEND → BUILD as a search order, not a ban on original engineering. If existing candidates fail the verified acceptance contract, Forge may implement missing components itself and must test them to the same quality bar.

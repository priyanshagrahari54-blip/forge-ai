# Forge Ecosystem Discovery

Forge uses mature external ecosystems as capability sources rather than rebuilding them.

Current discovery adapters:
- Hugging Face Hub: model metadata/search; inference-provider routing is handled by the HF provider adapter.
- Official MCP Registry: server discovery metadata.
- GitHub: repository discovery through the existing GitHub integration.

Discovery is metadata-only. Remote repositories, packages, model code and MCP servers are untrusted until license, provenance, dependency, security and compatibility checks pass.

The capability lifecycle is:
DISCOVER → INSPECT → LICENSE → SECURITY → QUALITY → COMPATIBILITY → VERIFY → REGISTER → ADAPT/COMPOSE/REUSE.

Hugging Face documents APIs for listing/searching models, datasets and Spaces and exposes inference-provider metadata. citeturn0search0turn0search1turn0search10

The official MCP Registry provides a registry/API for MCP servers. citeturn0search2

Forge should not promise unlimited external compute. It can maintain an unbounded task queue and rotate among actually available, policy-approved providers; hosted provider quotas still apply. citeturn0search3

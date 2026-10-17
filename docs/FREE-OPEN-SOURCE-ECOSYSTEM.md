# Free and Open-Source Adoption Policy

**Goal:** prefer genuinely free-to-use, open-source components that can be self-hosted and do not require a paid subscription or API key for their baseline functionality. "Open source" and "totally free to operate" are separate checks.

## Candidates added to Forge's ecosystem shortlist

| Project | License / openness | What Forge may use it for | Cost caveat | Current decision |
|---|---|---|---|---|
| [Open Dots](https://github.com/Anil-matcha/Open-Dots) | MIT, according to upstream README | Optional self-hosted agent workspace, UI and connector/action workflow | Live AI responses require an inference endpoint/key; hosting and computer-runtime resources may cost money. Early prototype; do not treat its action gateway as a production security boundary. | Candidate; not yet integrated |
| [Strata MCP](https://github.com/kytheros/strata) | Apache-2.0, according to upstream README | Optional persistent memory and local BM25/FTS5 search over MCP | Local FTS search works without an API key; vector/LLM features may require external model access. Node.js requirement is newer than the user's Node 13 environment. | Candidate; not yet integrated |

## Required cost labels

Every capability/provider must be classified separately:
- **FOSS:** source available under a recognized open-source license.
- **Free local baseline:** core function runs locally without a paid API, account, or metered service.
- **Free hosted tier:** a provider offers a limited free quota; quotas and policies can change.
- **Paid/usage-based:** requires payment, metered API usage, paid license, or paid hosting.
- **Unknown:** not yet verified; do not market as free.

A project is not classified as "totally free" merely because its source is public or its repository is free to clone. Include model inference, hosting, storage, network, GPU/CPU, license restrictions, optional dependencies, and quota limits in the cost assessment.

## Default integration policy

1. Search for mature FOSS options before building a new component.
2. Check the actual license and any dependency/model/dataset licenses.
3. Prefer offline/local mode when it meets the quality requirement; hosted services are optional fallbacks, never assumed unlimited.
4. Keep Forge's orchestration, permissions, provenance, security review, tests, acceptance and rollback as the source of truth.
5. Do not automatically execute downloaded repositories, package scripts, MCP servers, browser actions or models just because discovery found them.
6. Pin versions, review supply-chain risk, test in isolation, and record measured resource/cost requirements before enabling an adapter.
7. Keep optional integrations disabled until their acceptance gates pass. Never claim an integration is installed or working until tests demonstrate it.

## Hardware compatibility note

The user's Lenovo G560 (Windows 7 32-bit, Node 13, 2 GB RAM) is a thin client and is not a suitable target for many current Node/Python projects or large models. Do not install these candidates directly on that machine without checking runtime compatibility. Use a supported, actually available remote provider only if its free quota and terms are confirmed; do not assume the user has their own server or paid compute.

## Sources reviewed (2026-10-17)

- Open Dots upstream repository and README: https://github.com/Anil-matcha/Open-Dots
- Strata MCP upstream repository and README: https://github.com/kytheros/strata

Upstream claims are not independent security audits or guarantees of future pricing.

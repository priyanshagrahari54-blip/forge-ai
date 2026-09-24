# FORGE AI — PROVIDER SPECIFICATION
## Provider lifecycle
Credential detection → adapter registration → model discovery → health → minimal inference verification → production eligibility → telemetry.

## Adapters
Provider-specific HTTP/auth/request formats belong inside provider adapters. Core Model Fabric remains provider-agnostic.

## Required behavior
Bounded timeouts, response-size limits, structured errors, rate-limit handling, retry classification, health state and secret-safe logging.

## Providers currently intended
OpenAI, Anthropic, Gemini, OpenRouter, Groq and optional/local providers where actually configured.

## Rule
A configured API key is not proof of working inference. Every provider/model must be runtime-verified before production use.

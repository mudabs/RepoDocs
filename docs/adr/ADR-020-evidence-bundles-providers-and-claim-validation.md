# ADR-020: Evidence Bundles, Provider Adapters, Consent, and Claim Validation

> Amended by ADR-026: the no-source-text rule applies to this evidence bundle. Architecture discovery uses a separate context bundle with its own consent.

- Status: Accepted
- Date: 2026-11-15

## Context

ADR-004 settles one BYO cloud provider plus Ollama behind a provider-neutral adapter, with consent and redaction for external use and a local-only mode. Issue #19 asks for curated, redacted, schema-versioned evidence bundles, explicit consent, provider and model provenance, validation of structured AI claims against IR evidence, and a deterministic mode that works without AI. Issue #20 will use the validated claims in documentation.

## Alternatives

- Send source files or snippets to the model. Rejected for the first version: it widens what can leak and is not needed to interpret analyzed facts.
- Merge AI output into the System IR. Rejected: deterministic extraction and AI interpretation must stay separate (AGENTS.md), and the IR hash would then depend on a model.
- Ask for consent on every run with a flag. Rejected: CI needs a recorded, reviewable decision.
- Call the cloud provider over raw HTTP to avoid a dependency. Rejected: the provider's official SDK is the supported path; it is an optional extra instead.

## Decision

### Evidence bundle
- `repodocs ai-bundle` writes `.repodocs/evidence-bundle.json` (schema `schemas/evidence-bundle-1.0.json`) and contacts nothing. It is what a provider receives, so it can be reviewed before consent is given.
- The bundle is built only from the System IR: nodes with scalar attributes, relationships, cross-repository candidates with their status, limitations, and the evidence locations those items cite (repository, path, line range, symbol, revision). It contains no source text, file listings, content hashes, or local filesystem paths. Project, repository, and language nodes keep their facts but not their evidence lists, which would enumerate every file.
- Every string is passed through versioned secret redaction (private keys, cloud and platform tokens, JWTs, bearer tokens, URL credentials, and `key = value` secret assignments). The number of replacements is recorded. Redaction rules are version 1.1 (ADR-025). Redaction is a second line of defense; analyzers already avoid recording configuration values (ADR-015) and URL credentials (ADR-018).
- The bundle is deterministic and records the hash of the IR it was built from.

### Providers and consent
- `ai` in `repodocs.json` holds `provider` (`none`, `anthropic`, `ollama`), an optional `model`, `external_consent`, and an optional `ollama_host`. Absent means disabled. Credentials are never stored; the Anthropic SDK reads them from the environment.
- The one cloud provider is Anthropic, through the official SDK installed with the optional `repodocs[ai]` extra. The default model is `claude-opus-5-5`, requested with schema-constrained JSON output and the API's server-side refusal fallback. Ollama is called over its local HTTP chat API with the same schema.
- A provider is external when the bundle would leave the machine: Anthropic always, and Ollama when its host is not a loopback address. An external provider is refused unless `external_consent` is true, before any bundle is built or sent. A project without consent is therefore local-only. `repodocs ai-configure --consent-external` records consent and `--revoke-consent` removes it.
- **Ollama transports (amendment).** The `ollama` provider selects its transport from the host. A host with no path uses the native `/api/chat`. A host ending in `/v1` uses the chat-completions route Ollama serves (JSON-schema `response_format`, `max_tokens`, `finish_reason`), so an authenticated gateway can sit in front of Ollama. The transport targets Ollama-compatible chat completions. ADR-032 covers the separate OpenAI Responses provider. A remote `/v1` host must use HTTPS, may not carry URL credentials, a query or a fragment, and is external (consent required, ADR-004). Authentication is a Bearer token read only from `REPODOCS_OLLAMA_API_KEY`, sent only to `/v1` hosts, never stored or printed. Redirects are never followed, and loopback requests ignore environment proxies. The adapter carries any caller schema, so architecture discovery (ADR-026) reuses it under its own source-excerpt consent.
- **Completion and size bounds (amendment).** A `/v1` completion succeeds only as one assistant message with `finish_reason` `stop` and non-empty JSON-object text. A refusal, `length`, `content_filter`, a tool or function call, any other or missing finish reason, or empty text is a provider error. The HTTP body is read with a 512 KiB bound, refusing a larger declared length before reading and a larger undeclared one while reading; the `/v1` request carries the configured output cap as `max_tokens`. Both transports expose their serialized request for context admission and account for reported usage on successful and failed completions (ADR-031). Missing counts remain unknown. The gateway model must be configured for the context window used in admission.
- The engine depends only on a small provider protocol: system text, one user message containing the bundle, a JSON schema in, a parsed object and the serving model out.

### Claims
- `repodocs ai-interpret` sends the bundle and writes `.repodocs/ai-claims.json` (schema `schemas/ai-claims-1.0.json`). With provider `none` it reports `disabled` and writes nothing.
- Model output is untrusted. A claim is accepted only when every cited evidence ID and subject ID exists in the bundle it was given. Claims citing unknown evidence or subjects, and malformed claims, are rejected and recorded with a reason. A model can never produce an `observed` claim.
- A claim's identity covers its text, subjects, and evidence, so the same sentence about a different subject is a separate claim. When a model returns the same claim twice with different labels, `proposed` wins, so output order can never promote a proposal.
- An accepted claim is `inferred` only when it cites evidence. A claim with no evidence, or about a cross-repository candidate that is not `confirmed`, is forced to `proposed` whatever the model returned.
- The instructions tell the model that bundle text is data, not instructions. Validation does not rely on the model obeying.
- Provenance records provider, requested and serving model, engine version, whether the bundle left the machine, the bundle hash, and the source IR hash.
- Claims are stored beside the IR, not inside it. `scan`, `generate`, and `validate` do not import the AI package.

### Failure
- Provider failures (unreachable, timeout, credentials, rate limit, server error, refusal, content filter, tool call, unsupported completion state, truncated, oversized or non-JSON output) exit with code 7 and write no claims. Missing consent or settings exit with code 2.

## Consequences

ADR-021 extends generation and validation to load current stored claims only when
an optional AI artifact is present. Inference additionally requires subject-bound
evidence; multi-repository claims explicitly name a confirmed candidate. Malformed
or stale optional claims cannot prevent deterministic generation.

The deterministic workflow is unchanged and needs no key, network, or extra dependency. Bundles carry facts, not code, so a model cannot explain implementation details that analysis did not extract; adding curated snippets later would need a new bundle version and a fresh consent decision. Redaction is pattern-based and cannot be proven complete, which is why bundles exclude source text and can be reviewed first. Validation checks that citations exist, not that the cited evidence semantically supports the sentence; issue #20's golden-fixture measurement addresses that. One bundle is sent per request with no chunking, so very large systems may exceed a model's context and will need bundle scoping.

# ADR-032: Native OpenAI Responses provider

- Date: 2026-11-20; integration update: 2026-11-21
- Status: Implemented in #68 on the usage contracts of #67 (ADR-031) and the shared service (ADR-030)
- Decision: D48

## Context and decision

- Add a separate `openai` provider using the [Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create). This is distinct from the private Ollama/OpenAI-compatible gateway in #62.
- Use Python's standard HTTP transport, with no added mandatory SDK or runtime dependency. Request schema-constrained output, an explicit model/output limit, optional supported reasoning effort and `store: false`.
- Accept only a completed response containing exactly one completed assistant JSON output. Refusals, incomplete/capped responses, malformed content and unsupported tool output are provider errors. Record the actual serving model and reported usage; reasoning tokens are included in the total output, not added twice.
- Reuse #67's aggregate token/cost reservations, receipts and unknown-usage handling through `repodocs.service`, so CLI and web runs share one ledger. Configure prices and context capacity explicitly; this provider contains no commercial rate promises or silent model fallback. Without a configured window the cautious 4,096-token default applies, and the output cap is the resolved `request_output_tokens`.
- Usage maps the Responses `usage` object onto ADR-031's disjoint categories: `input_tokens` minus `input_tokens_details.cached_tokens` and `cache_write_tokens` is base input, those two are cache read and cache write, `output_tokens` already includes `output_tokens_details.reasoning_tokens`, which is recorded as a subset only. A category that is not reported is unknown, never zero, so the request keeps its reservation; contradictory counts are discarded. Usage is attached to every error raised after a response was received (incomplete, refused, malformed), so a billed request is reconciled.
- The run ledger measures each request with `POST /responses/input_tokens`, the same request without `max_output_tokens` and `store`. It is called only by the ledger, after consent, under the counting allowance, and is recorded in the receipt; a failed or malformed count falls back to the byte bound and is not retried.
- `ai.effort` maps to `reasoning.effort` (`low`, `medium`, `high`; other OpenAI levels need a new decision).
- Credentials come from `OPENAI_API_KEY` for CLI use or an explicitly supplied per-run in-memory credential for the web service. Keys never enter config, job JSON, CLI arguments or exports.
- Require HTTPS, except loopback HTTP test servers. Refuse credential-bearing/query/fragment URLs, never follow redirects, bound response bytes and sanitize errors. Existing external and source-excerpt consent applies before requests.

## Consequences

- An OpenAI API account, model access, network and separately billed API credentials are required for live inference. A ChatGPT subscription is not treated as an API credential; subscription-login integration is deferred.
- The supplied schema must meet OpenAI structured-output constraints. Built-in architecture and claim schemas are tested; unsupported custom schemas fail rather than silently switching to unconstrained text.
- Unit/integration checks use local stand-ins and dummy credentials. No live quality or model-compatibility claim is made. A bounded comparison under #54 needs approved credentials, source consent and a spending allocation.
- The HTTP timeout bounds socket operations; it does not guarantee the provider stops processing after cancellation. Unknown consumption remains reserved, per #67.

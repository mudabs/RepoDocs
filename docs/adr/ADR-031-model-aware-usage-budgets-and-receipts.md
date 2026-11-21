# ADR-031: Model-aware usage budgets and local receipts

- Date: 2026-11-20
- Status: Implemented for the Anthropic and Ollama providers (#67); the OpenAI provider (#68) and the web service (#69, #72) reuse the same contracts.
- Issue: #67; epic #63
- Amends: ADR-026 (a single run now has an aggregate token, request and cost bound, not only per-request byte and round limits). ADR-020 (the Anthropic adapter no longer asks the API for a server-side fallback model). D43 (usage contracts named in ADR-027).

## Context

Discovery bounded rounds, requests and bytes, but not tokens or money. A byte limit is not a token limit: a prompt that the project believed was small can be 14,946 tokens, and Ollama then truncates it silently when the runtime's window is 4,096. Retries and repairs also had no shared allowance, and nothing recorded what a run cost or why it stopped. The browser workflow needs an honest, bounded bill and a receipt.

## Decision

### Contracts (`repodocs.ai.budgets`)

- `Usage`: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` and an informational `reasoning_tokens`. `None` means the provider did not report it and is never treated as zero. Input, cache read and cache write are disjoint billed categories; `output_tokens` includes reasoning. Usage is `measured` only when all four billed categories are reported.
- `ProviderResponse(data, model, usage=None)` keeps the old two-argument form working. `ProviderError.usage` carries usage when a request completed but its output was refused, cut off or malformed.
- `RunLimits` (configuration and resolved form), `PriceProfile`, `Ledger`, `BudgetedProvider` and `write_receipt` are provider-neutral. A new provider supplies `generate`, optionally `request_body` (the exact serialized request) and optionally `count_input_tokens`.

### Limits

`ai.limits` in `repodocs.json` (every field optional):

| Field | Meaning | Default and ceiling |
|---|---|---|
| `context_window_tokens` | The runtime's window. A request must fit its prompt plus reserved output | 200,000 anthropic, 4,096 ollama; any positive value, because it describes the model |
| `request_output_tokens` | Explicit output cap sent with every request | Smaller of 16,000 and half the window; may only lower 16,000 |
| `max_requests` | Requests in the whole run, retries and repairs included | 8; may only lower |
| `max_input_tokens` | Prompt-side tokens in the whole run | 800,000; may only lower |
| `max_output_tokens` | Reserved or actual output tokens in the whole run | 64,000; may only lower |
| `max_cost_usd` | Aggregate cost cap | none; requires `ai.prices` |

`ai.effort` (`low`, `medium`, `high`) is sent only when set and only to Anthropic; Ollama refuses it. Higher effort levels need a new decision. Nothing raises a limit, changes the model or switches provider automatically.

### Preflight and reservation

- The complete serialized request (system, user message, schema, model and options) is measured before every request. A provider that can count tokens (Anthropic `count_tokens`, which counts the same request including the schema) is asked; otherwise a fallback estimate of at least two bytes per token plus fixed framing is used. The method is recorded per request (`provider-count` or `fallback-estimate`). The estimate is not an exact token count and can undercount dense input, so actual usage is still reconciled afterwards.
- A counting request is sent only by a wrapped provider, and a provider is wrapped only after the existing consent checks. Source consent for discovery is checked before a provider exists. `budgeted()` refuses an external provider without consent again.
- Before sending, the ledger reserves the worst case atomically under a lock: estimated prompt plus the full output cap must fit the window, the aggregate input, output and request caps, and the cost cap. Cost uses the highest of the input, cache-read and cache-write rates for prompt tokens. If the request does not fit, `RunStopped` is raised and nothing is sent, with a stop reason (`context-window`, `request-limit`, `input-token-limit`, `output-token-limit`, `cost-limit`, `cancelled`).
- After the response, complete usage replaces the reservation. Missing or partial usage, a provider error, a cancellation or an interrupt keeps the reservation, so totals are upper bounds. Cache tokens with no configured cache rate leave the cost at its reserved worst case. Actual usage above the estimate is recorded and counts against later requests.
- A capped, refused or truncated output is an error (Anthropic `max_tokens`, Ollama `done_reason: length`, or a prompt and output that fill the window). It is incomplete, is reconciled with its reported usage, and is never retried with a higher limit.
- Each run owns one ledger, so concurrent jobs have separate allowances; one ledger is thread-safe. `cancel()` stops further requests. A request already sent finishes and is reconciled; its billing cannot be recalled.

### Ollama

The window and output cap are sent explicitly (`options.num_ctx`, `options.num_predict`) so the runtime is not left to truncate silently. With no configured window the default is 4,096, the long-standing Ollama default. Raise `ai.limits.context_window_tokens` to what the model really supports to run larger contexts.

### Anthropic

`max_tokens` and `output_config.effort` come from the settings. The adapter no longer requests the server-side fallback beta: a substitute model would bill at a rate that was never reserved, and the model must not change without the user choosing it. A safety decline is reported as an error.

### Prices

`ai.prices` holds a project-supplied, versioned profile in US dollars per million tokens (`version`, `input_usd_per_mtok`, `output_usd_per_mtok`, optional `cache_read_usd_per_mtok` and `cache_write_usd_per_mtok`). RepoDocs ships no rates and scrapes none. Costs are integer millionths of a dollar, rounded up. Without a profile, cost is `null`, not zero.

### Receipts

`architecture discover` and `ai-interpret` write `.repodocs/receipts/<time>-<command>-<id>.json` (`schemas/usage-receipt-1.0.json`), including when the run fails, and add a `usage` summary to the CLI response: requests, charged tokens and cost, whether usage is complete, estimated maximum cost, remaining allowance and the stop reason. A receipt holds counts, limits and outcomes only, never a prompt, a response or a credential. It needs no hosted database. `Ledger.snapshot()` is the same data for the web service to poll.

## Alternatives

- Bytes as limits: not tokens, and the reason for the original failure. Retained only as the labeled fallback estimate.
- Built-in or scraped price tables: unverifiable and stale. A configured, versioned profile is auditable.
- Treating missing usage as zero or a fraction of the reservation: understates a paid bill.
- A separate repair counter: discovery already allows one repair; every repair is a request in the shared ledger and counts against all aggregate caps. A distinct counter would need the provider interface to know the request's purpose.

## Consequences and limitations

- Defaults change for existing Ollama users: a prompt that needs more than 4,096 tokens is refused with a message naming the setting, instead of being truncated silently.
- The fallback estimate can be wrong in either direction. It is labeled, and complete provider usage is the authority after a request.
- Unknown usage keeps a request at its worst case, which can end a run earlier than the true bill requires.
- Cancellation cannot abort a request already in flight and does not promise the provider stopped billing.
- Receipts are local files; an operator who deletes them loses the record. No quotas, billing, per-user limits or automatic provider fallback exist.
- Provider counting and live usage shapes are exercised with fixtures only. No live paid request is part of the tests.

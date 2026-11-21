# ADR-031: Model-aware usage budgets and local receipts

- Date: 2026-11-20; review update: 2026-11-21
- Status: Implemented for the Anthropic and Ollama providers and the shared service (#67, #69); the OpenAI provider (#68) and the HTTP adapter (#72) reuse the same contracts.
- Issue: #67; epic #63
- Amends: ADR-026 (a single run now has an aggregate token, request and cost bound, not only per-request byte and round limits). ADR-020 (the Anthropic adapter no longer asks the API for a server-side fallback model). D43 (usage contracts named in ADR-027).

## Context

Discovery bounded rounds, requests and bytes, but not tokens or money. A byte limit is not a token limit: a prompt that the project believed was small can be 14,946 tokens, and Ollama then truncates it silently when the runtime's window is 4,096. Retries and repairs also had no shared allowance, and nothing recorded what a run cost or why it stopped. The browser workflow needs an honest, bounded bill and a receipt.

## Decision

### Contracts (`repodocs.ai.budgets`)

- `Usage`: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens` and an informational `reasoning_tokens`. `None` means the provider did not report it and is never treated as zero. Input, cache read and cache write are disjoint billed categories; `output_tokens` includes reasoning. Usage is `measured` only when all four billed categories are reported.
- `ProviderResponse(data, model, usage=None)` keeps the old two-argument form working. `ProviderError.usage` carries usage when a request completed but its output was refused, cut off or malformed.
- `RunLimits` (configuration and resolved form), `PriceProfile`, `Ledger`, `BudgetedProvider`, `ledger_for`, `budgeted` and `write_receipt` are provider-neutral. A new provider supplies `generate`, optionally `request_body` (the exact serialized request) and optionally `count_input_tokens`. `RunLimits` checks its ceilings on construction, so no caller can raise one by building limits directly.

### Limits

`ai.limits` in `repodocs.json` (every field optional):

| Field | Meaning | Default and ceiling |
|---|---|---|
| `context_window_tokens` | The runtime's window. A request must fit its prompt plus reserved output | 200,000 anthropic, 4,096 ollama; any positive value, because it describes the model |
| `request_output_tokens` | Explicit output cap sent with every request | Smaller of 16,000 and half the window; may only lower 16,000 |
| `max_requests` | Generation requests in the whole run, retries and repairs included | 6, the ADR-026 provider request ceiling; may only lower |
| `max_count_requests` | Token-count requests in the whole run | `max_requests` (one per request); ceiling 6 |
| `max_input_tokens` | Prompt-side tokens in the whole run | 800,000; may only lower |
| `max_output_tokens` | Reserved or actual output tokens in the whole run | 64,000; may only lower |
| `max_cost_usd` | Aggregate cost cap | none; requires a complete `ai.prices` profile for the provider and model |

`ai.effort` (`low`, `medium`, `high`) is sent only when set and only to Anthropic; Ollama refuses it. Higher effort levels need a new decision. Nothing raises a limit, changes the model or switches provider automatically.

A run may carry its own limits (`DiscoverRequest.limits`, for example a web job's allowance). They are combined field by field with the project's limits and can only lower them.

### Preflight and reservation

- The complete serialized request (system, user message, schema, model and options) is measured before every request. A provider that can count tokens (Anthropic `count_tokens`, which counts the same request including the schema) is asked; otherwise the request is bounded at one token per UTF-8 byte of its serialized form plus 64 tokens of framing. The method is recorded per request (`provider-count` or `byte-bound`).
- The byte bound holds for byte-level BPE tokenizers (the Anthropic and OpenAI model families, Llama-family models served by Ollama) and for SentencePiece tokenizers with byte fallback, which never emit more pieces than the bytes they encode. It does not hold for a tokenizer that expands text while normalizing it. It is usually several times the real count, so it can refuse a request that would have fitted; it is never reported as a token count. An earlier two-bytes-per-token average was not a bound for hashes, punctuation or non-Latin scripts and was removed.
- A counting request is sent only by `BudgetedProvider`, which is created only after the existing consent checks, to the same destination as the request it measures. Each one is admitted under the run's lock against `max_count_requests` and the stop state, and recorded (`count_requests`); a run that has stopped or exhausted that allowance uses the byte bound instead. A failed count falls back to the bound and is never retried. Counting requests are not priced: check the provider's terms. `BudgetedProvider` does not offer `count_input_tokens` itself, so an outer wrapper can neither count nor reserve twice, and `budgeted()` returns an already budgeted provider unchanged.
- Before sending, the ledger reserves the worst case atomically under a lock: estimated prompt plus the full output cap must fit the window, the aggregate input, output and request caps, and the cost cap. Cost uses the highest of the input, cache-read and cache-write rates for prompt tokens. If the request does not fit, `RunStopped` is raised and nothing is sent, with a stop reason (`context-window`, `request-limit`, `input-token-limit`, `output-token-limit`, `cost-limit`, `cancelled`).
- After the response, complete usage replaces the reservation. Missing or partial usage, a provider error, a cancellation or an interrupt keeps the reservation, raised to any larger reported part, so totals are upper bounds and a reported count is never discarded. Usage above the reservation is marked `exceeded_reservation`.
- Reported usage that takes the run past a token or cost cap ends it with `reported-usage-over-limit`: the response is not used (the result is never a normal complete success), no further request is sent, and the receipt shows the overage. The provider has already billed that request; it cannot be undone.
- A stop is final. Once the ledger has refused or stopped, every later request is refused with the same reason, so a smaller request cannot resume a stopped run under a misleading receipt.
- A capped, refused or truncated output is an error (Anthropic `max_tokens`, Ollama `done_reason: length`, or a prompt and output that fill the window). It is incomplete, is reconciled with its reported usage, and is never retried with a higher limit.
- Each run owns one ledger, so concurrent jobs have separate allowances; one ledger is thread-safe. `cancel()` stops further requests. A request already sent finishes and is reconciled; its billing cannot be recalled.

### Ollama

The window and output cap are sent explicitly (`options.num_ctx`, `options.num_predict`) so the runtime is not left to truncate silently. With no configured window the default is 4,096, the long-standing Ollama default. Raise `ai.limits.context_window_tokens` to what the model really supports to run larger contexts. Ollama has no token-count endpoint, so its requests are always byte-bounded. Only a finished completion (`done: true` with `done_reason: stop`) is an answer; an unfinished frame or one that ended for another reason is incomplete, with its usage reconciled.

### Anthropic

`max_tokens` and `output_config.effort` come from the settings. The adapter no longer requests the server-side fallback beta: a substitute model would bill at a rate that was never reserved, and the model must not change without the user choosing it. The SDK client is built with `max_retries=0`, because its internal retries would be requests the ledger never reserved. A safety decline is reported as an error.

### Prices

`ai.prices` holds a project-supplied, versioned profile in US dollars per million tokens for one provider and model (`version`, `provider`, `model`, optional `served_models`, `input_usd_per_mtok`, `output_usd_per_mtok`, optional `cache_read_usd_per_mtok` and `cache_write_usd_per_mtok`). RepoDocs ships no rates and scrapes none. Costs are integer millionths of a dollar, rounded up. Without an applicable profile, cost is `null`, not zero.

- A profile for another provider or model is not applied: cost is unknown and the receipt says why; a cost-capped run refuses to start.
- A response from a model the profile does not list (`model` or `served_models`) is not priced: its cost stays at the reservation, and a cost-capped run stops, because later cost could no longer be bounded.
- A worst case can be reserved only from a complete profile. A cost cap therefore needs all four rates (0 for a category the provider does not bill); otherwise reserved and charged cost are `null`.

### Receipts

`architecture discover` and `ai-interpret` write `.repodocs/receipts/<time>-<command>-<id>.json` (`schemas/usage-receipt-1.0.json`), including when the run fails or is cancelled, and add a `usage` summary to the CLI response: requests and count requests, charged tokens and cost, whether usage is complete, estimated maximum cost, remaining allowance and the stop reason with its message. A receipt holds counts, limits and outcomes only, never a prompt, a response or a credential. It needs no hosted database. `Ledger.snapshot()` is the same data for the web service to poll.

### Shared service

Budgeting belongs to `repodocs.service.discover` (ADR-030), so a CLI run and any in-process adapter go through the same ledger. After both consent scopes are checked the service wraps the injected provider with `budgeted()` (or uses a caller's `BudgetedProvider` as it is), places the cancellation wrapper outside it, and writes a receipt on success, failure and cancellation. The result carries the `usage` summary; a failed or cancelled result carries it in `data.usage`. A budget stop ends discovery as a `partial` view with reason `usage-budget`, or a failure when no usable component exists, never as a provider error or complete success. The context preview reports the run's resolved limits and worst-case cost without counting or sending anything. `ai-interpret` is still budgeted in the CLI until it moves into the service.

## Alternatives

- Bytes as limits: not tokens, and the reason for the original failure. Retained only as the labeled one-token-per-byte bound.
- An average bytes-per-token estimate: smaller reservations, but not an upper bound, so caps could be passed.
- Counting requests inside the generation request budget: would halve the requests discovery can make; a separate, equally bounded and recorded allowance keeps both visible.
- Built-in or scraped price tables: unverifiable and stale. A configured, versioned profile is auditable.
- Treating missing usage as zero or a fraction of the reservation: understates a paid bill.
- A separate repair counter: discovery already allows one repair; every repair is a request in the shared ledger and counts against all aggregate caps. A distinct counter would need the provider interface to know the request's purpose.

## Consequences and limitations

- Defaults change for existing Ollama users: a prompt that needs more than 4,096 tokens is refused with a message naming the setting, instead of being truncated silently.
- The byte bound overestimates, often by several times, and can refuse a local request that would have fitted; lower the discovery context budgets or raise the configured window to what the model supports. Complete provider usage is the authority after a request.
- An overage is detected after the provider has billed it. The run stops and shows it; it cannot be prevented when the provider's count disagrees with the preflight.
- Unknown usage keeps a request at its worst case, which can end a run earlier than the true bill requires.
- Cancellation cannot abort a request already in flight and does not promise the provider stopped billing.
- Receipts are local files; an operator who deletes them loses the record. No quotas, billing, per-user limits or automatic provider fallback exist.
- Provider counting and live usage shapes are exercised with fixtures only. No live paid request is part of the tests.

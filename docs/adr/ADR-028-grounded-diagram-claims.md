# ADR-028: Valid citations are not supported claims

- Date: 2026-11-20; review update: 2026-11-21
- Status: Implementation for review under #65, epic #63
- Amends: ADR-026 classification presentation; preserves consent, IR authority and confirmations

## Context

A reviewed diagram assigned backend files to a UI and invented GET /bones and POST /bones while citing real files. Resolving citation IDs establishes where to inspect a claim, not that its role, responsibility or relationship is true.

## Decision

- Keep classification as member-citation inference/proposal. Compute separate support and reason codes; model labels never set them. Recompute on load so edited support cannot forge authority.
- Supported means a specific static fact matches: an analyzer-observed role, a matching observed relative-request and endpoint pair with correct method/path, direction and cited member files, or a bound confirmed candidate. It never verifies arbitrary responsibilities or runtime routing.
- Text co-occurrence, comments, quoted examples and two endpoint declarations cannot establish a call. Generic stacks remain unverified even when route text matches. Do not add broad parsers or a second model to manufacture semantic certainty.
- Missing role facts stay unverified, including a frontend represented by server-side/SSR files. Names and directories never decide roles; missing frontend facts do not disprove server-rendered UI behavior.
- Unsupported means the checked excerpts do not contain a claimed route/method, not that the route cannot exist elsewhere. Dynamic prefixes can produce false negatives. Keep those claims and reasons inspectable in the stored view, evidence map and page, but omit them from the default diagram.
- Unverified items remain explicitly marked and dashed. Solid styling indicates member inference plus a static fact match, never a correctness badge. Human review under #54 is still required.
- Cross-repository links still need the existing direction/evidence-bound confirmed candidate to gain support; matching files or repository pairs never substitute for confirmation.

## Persistence and validation

- The unreleased architecture-view 1.0 schema requires support and support_reasons. Earlier stored views become invalid and need an explicit refresh; the IR and provider response schemas are unchanged.
- Well-formed unsupported/uncertain claims retain their evidence and reason codes. Structurally rejected input remains counted by rejection reason without persisting unvalidated text, as in ADR-026.
- Tests cover comment/string/declaration false signals, actual observed request/endpoint matches, backend-only UI uncertainty, legitimate SSR, tampering, cross-repository binding, rendering and evidence inspection. Replays measure the contract, not live model usefulness.

## Consequences

Most unsupported frameworks produce unverified architecture, which is honest about their analytical limits. An observed role does not prove a component's responsibility sentence. A static pair does not prove deployed routing. Owner usefulness acceptance and #54 remain open, regardless of schema or renderer success.

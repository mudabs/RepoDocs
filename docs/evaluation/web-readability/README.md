# Web viewer readability evidence (#93)

Screenshots of the interactive viewer after a deterministic overview job, taken in headless Chrome at 1366x900.

| Item | Value |
|---|---|
| Engine | `feat/mermaid-rendering-sizing-93` at `551f4f49` (runtime code unchanged by the docs commit that adds this file) |
| Browser | Google Chrome 158.0.8127.37, headless |
| EventShare | `mudabs/EventShare` at `93970207969ecdb9b35cf5c1f8e811ef930a2a55`, registered as an allowlisted local source |
| Fixture | `tests/fixtures/express-shop-repo` (local, unversioned fixture) |
| Mode | Deterministic overview; no provider, no inference |

## Files

- `eventshare-initial.png`: first view. Node fills `rgb(236, 236, 255)`, label height 11 px at 60 %, no console or CSP errors.
- `eventshare-fit.png`: after Fit, the whole graph is visible at 25 %.
- `shop-initial.png`, `shop-fit.png`: the fixture, label height 19 px at 100 %.

## Observations

- Before the fix, the same flow rendered every node `rgb(0, 0, 0)` (reproduced by `tests/test_mermaid_rendering_93.py` against the previous CSP).
- The EventShare deterministic overview is a single-level fan-out of 15 nodes. Fit therefore makes labels too small to read; the 60 % first view is readable and needs horizontal panning. A more compact deterministic layout is a diagram-modeling change outside #93.
- The deterministic overview has no evidence panel data ("No evidence data available"); evidence appears for architecture views.

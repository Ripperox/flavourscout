# BudgetBite — the best-value food cart for your budget

Log in with your food account, enter a budget, and get the **highest-value cart
that lands just under it** — priced by the platform's *authoritative bill*, with
coupons discovered automatically and the order placeable in one tap.

It pairs a **provably optimal** offline optimizer (a coupon-aware
multiple-choice knapsack, checked against a brute-force oracle) with a
**resilient live pipeline** that calibrates to real prices, auto-discovers
working coupons, and reads back the real bill before anything is shown.

```
login → pick restaurant → set budget + profile → optimal cart(s) → (optional) place order
```

## What it does

- **Optimal cart under budget.** Maximum total preference, ties broken by lower
  price, with the final amount (subtotal − best coupon + delivery + fees + GST)
  within budget. Two options are shown: one strictly within budget, and an
  optional "worth the stretch" cart that just exceeds it when the value jump is
  real.
- **Personalization.** Vegetarian-only (fail-safe: only items confirmed veg),
  group size ("for N people" → ₹/head and a main per head), and an opt-in drink
  (added the cheapest way — usually by converting a burger to a meal, valued as a
  bundle, rather than bolting on a soft drink).
- **Coupons, discovered — never typed.** The app probes each candidate cart's
  auto-suggested coupon plus a small discovery list, credits a code **only** when
  it actually lowers the authoritative `to_pay`, and remembers winners per branch
  in a **shared ledger** so the next user at that branch gets them first.
- **Authoritative pricing.** The internal fee model only *ranks* carts; every
  total shown is read back from the platform's real cart bill.
- **One-tap order (COD-aware).** A Place Order button rebuilds the exact cart,
  re-verifies the bill (price-drift guard), and places it — only after an
  explicit confirmation dialog that states COD orders can't be cancelled.

## Why this isn't plain knapsack

Coupons break the knapsack assumption that adding an item only costs more.
With `FLAT100 (₹100 off above ₹199)`, adding a ₹60 side to a ₹150 cart drops the
final price from ₹150 to ₹110. The optimizer runs a multiple-choice knapsack DP
over *exact* spend levels and evaluates every coupon as a function of spend;
scoped coupons (e.g. "30% off pizzas") get a two-knapsack decomposition
(in-scope × rest). Exact, no heuristics — menus and budgets are small enough that
this is instant.

Each product (item or combo) is first expanded into its valid order *lines* —
variant × add-on selection × quantity (`choices.py`) — and the DP picks at most
one line per product. So add-ons, quantities, and combos all ride the same
machinery, and the step-function trick generalizes: adding cheese to a pizza can
push it past a scoped coupon's threshold and end up *cheaper*.

**Correctness is enforced by a brute-force oracle:** tests assert the DP equals
full enumeration on hundreds of random menus (`tests/test_equivalence.py`). Both
solvers share one pricing module so they cannot diverge on money math.

## Reliability (built for a real, rate-limited backend)

The live platform rate-limits aggressively, and its errors arrive wrapped in
async task-group `ExceptionGroup`s. The client layer handles this:

- **Global rate limiter** paces every call to stay under the 429 ceiling.
- **429 detection that walks the whole exception tree** (groups + cause chains),
  with exponential backoff — a wrapped 429 no longer slips past a string match.
- **Graceful transport teardown** so a poisoned connection can't turn a handled
  failure into a 500.
- **Graceful degradation end-to-end:** rate-limited → "busy, try again"; an
  unsupported store (cart won't build) → a clear message distinct from "nothing
  fits budget"; bad input → 422; unexpected → a clean JSON 500 (no traceback
  leak). Structured logging throughout.

## Safety

- **Order placement is never automatic.** `place_food_order` runs *only* from an
  explicit, confirmed user click — never during optimization/probing. The
  confirmation dialog shows the exact items + authoritative total and warns that
  COD orders can't be cancelled or refunded.
- Probe carts are **flushed after every probe**; coupons are auto-discovered, not
  user-entered; the beta's sub-₹1000 placement cap is enforced.

## Architecture

| Path | Role |
|---|---|
| `cart_optimizer/optimizer.py` | the exact coupon-aware DP (ships) |
| `cart_optimizer/brute_force.py` | enumeration oracle (tests only) |
| `cart_optimizer/pricing.py` | shared money math (eligibility, discount, bill) |
| `cart_optimizer/choices.py` | expand each product into valid order lines |
| `cart_optimizer/models.py` | typed domain model (Item/Variant/Combo/Coupon/Cart/Menu…) |
| `cart_optimizer/safe_eval.py` | sandboxed AST evaluator for coupon query strings |
| `cart_optimizer/swiggy_client.py` | async MCP client — rate limiter, 429 resilience |
| `cart_optimizer/adapters/swiggy.py` | live responses → normalized `Menu` (variants, veg, prices) |
| `cart_optimizer/discovery.py` / `run.py` | candidate proposal, real-price calibration, live verify |
| `cart_optimizer/coupon_ledger.py` | shared per-branch coupon memory (SQLite) |
| `webapp/server.py` | FastAPI backend (optimize, profiling, place-order, auth) |
| `webapp/oauth.py` | OAuth 2.1 + PKCE login flow |
| `webapp/static/index.html` | the UI (receipt aesthetic) |

## Run

```bash
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest                       # 490 tests (incl. DP-vs-oracle)

# web app (local)
venv/bin/uvicorn webapp.server:app --reload --port 8000   # http://localhost:8000
```

Runtime-only install: `pip install -r requirements.txt`. Deploy notes (Docker /
Render, env vars, the OAuth redirect): see `DEPLOY.md`.

## Engineering notes

- **490 tests**, property-style: the optimizer is checked against a brute-force
  oracle on random menus; the live client's 429/limiter logic and the web layer's
  profiling/validation/placement gating are unit-tested.
- Live shapes not yet seen (e.g. non-empty coupon payloads) raise rather than
  being guessed — the model never silently mis-prices.
- Design specs live in `docs/superpowers/specs/`.

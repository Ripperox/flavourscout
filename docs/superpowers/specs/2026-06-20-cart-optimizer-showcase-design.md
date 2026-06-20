# Cart Optimizer — Showcase Hardening & Profiling

- **Date:** 2026-06-20
- **Status:** Approved (design) — pending spec review
- **Timebox:** A few days
- **Author:** Rishit + Claude

## Context & Goal

The project builds the best-value Swiggy Food cart for a given budget: the user
logs into Swiggy, enters a budget, and gets the highest-preference cart that lands
just under budget, priced by Swiggy's authoritative bill, with coupons
auto-discovered. The optimizer core (exact multiple-choice-knapsack DP verified
against a brute-force oracle, 468 tests) is solid.

The goal of this effort is to make the project **good enough to showcase as a
portfolio piece** — a polished, *working*, deployed product that demonstrates,
in balance: (1) product thinking, (2) that it actually works live, and
(3) engineering rigor.

This was prompted by a relevant job opening, but the build is **brand-neutral**:
we do not special-case any restaurant chain. Any breakage on a specific store
(e.g. Faasos) is treated only as evidence that the app must handle *arbitrary*
stores gracefully.

### Invariants this design must preserve (safety & correctness)

- `place_food_order` is called **only** in direct response to an explicit user
  confirmation (the Place Order button + a confirmation dialog) — **never**
  automatically, never during price-probing/optimization, and never by the
  assistant during testing. The optimization pipeline itself still never places
  orders (it only probes and flushes). COD orders are non-cancellable, so the
  confirmation must state this prominently.
- The "best value" total **must** come from Swiggy's authoritative bill
  (`to_pay`), never an estimate.
- Cart-mutating tools are used only to *probe*; the cart is **flushed after every
  probe**.
- Coupons are **auto-discovered** per branch; the user never types a coupon.
- The DP optimizer must continue to equal the brute-force oracle. No change here
  touches that contract.

## Non-Goals (YAGNI)

- No Rebel-Foods-specific (or any brand-specific) features or positioning.
- No spice-level or cuisine/brand-lean profiling (parked).
- No full quantity-aware re-optimization for group size (soft constraint only).
- No multi-instance horizontal scaling work (single-instance deploy is fine).
- No new coupon-modeling beyond what already exists.
- No online/card/UPI payment integration — order placement is **Cash on Delivery
  only**.

## Workstreams

### 1. Reliability layer (eliminate the 429 → 500 cascade)

**Problem (root-caused from live logs):** A single Swiggy `429 Too Many Requests`
becomes a 500 because (a) the 429 surfaces wrapped in an anyio `ExceptionGroup`,
so `SwiggyClient.call`'s `if "429" in str(exc)` retry never matches; (b) the 429
poisons the MCP transport and teardown raises `RuntimeError: Attempted to exit
cancel scope in a different task`, which propagates unhandled. Driver: one
optimize fires ~40 Swiggy calls with no global pacing; testing several
budgets/stores back-to-back guarantees 429s.

**Changes (in `cart_optimizer/swiggy_client.py` unless noted):**
- `_is_rate_limited(exc)` — recursively inspect `ExceptionGroup.exceptions`,
  `__cause__`, `__context__`, and any `.response.status_code == 429`, with cycle
  protection. Replace the substring check in the retry loop with this.
- `_RateLimiter(min_interval)` — process-global async min-interval gate; every
  `call()` awaits `acquire()` before issuing. Interval from `SWIGGY_MIN_INTERVAL`
  env (default tuned for stability, e.g. 0.4–0.5s).
- Graceful `__aexit__` — swallow (and log) teardown errors from a poisoned
  transport (cancel-scope `RuntimeError`, `BaseExceptionGroup`), but re-raise
  `KeyboardInterrupt`/`SystemExit`/`CancelledError`.
- `webapp/server.py` `/api/optimize` — wrap the Swiggy interaction so a
  rate-limit/transport failure returns a clean, retryable response
  (`{"found": false, "message": "Swiggy is busy — try again in a moment."}`)
  instead of a 500.
- **Reduce calls per optimize** so pacing stays fast: verify fewer candidate
  carts and lighten the live greedy top-up (rely on the price-calibrated offline
  optimizer for budget-fill; verify only the top within-budget candidate(s) plus
  one stretch). Target: well under ~12 calls per optimize on a warm menu cache.

**Acceptance:** Rapid repeated optimizes across stores/budgets never 500; under
rate-limiting the user sees the retry message; a warm-cache optimize issues
materially fewer calls than today.

### 2. Robustness across stores (general "Bug B")

**Problem:** For some stores, `update_food_cart` does not actually add items, so
`get_food_cart` returns an envelope with no `data` block; `parse_cart_bill`
raises for *every* candidate and the user sees a misleading "No cart fits."

**Changes:**
- Distinguish failure modes in `/api/optimize`: if *no* candidate could be
  **built** (all raised), return *"We couldn't build a cart at <restaurant> —
  it may not be fully supported yet."*; reserve "No cart fits ₹X" for the case
  where carts built but none fit budget.
- Detect empty-cart-after-add explicitly (no `data` block, or `item_count == 0`)
  and surface it as a typed condition rather than a generic parse error.
- **Timeboxed investigation** of the root cause (live probe of a failing store):
  inspect the raw `update_food_cart` / `get_food_cart` responses to learn why the
  add no-ops (candidate hypotheses: wrong `menu_item_id` field for that store;
  required customizations/variants on add). If a low-risk fix broadens coverage,
  apply it; otherwise document the limitation and keep the graceful message.

**Acceptance:** A store that can't be built shows the correct message, never a
500 or a misleading "No cart fits." Findings documented either way.

### 3. Profiling (product thinking)

A small profile captured in the UI and persisted in the session, applied before
optimization.

- **Veg / non-veg.** Capture Swiggy's veg classifier into the `Item` model in the
  adapter. When "veg only" is selected, include only items **confirmed veg**;
  items with unknown/missing veg metadata are **excluded** when veg-only is on
  (fail safe — never serve a veg user something unverified). Non-veg mode is
  unrestricted.
- **Group size (N people).** Soft constraint, not full quantity optimization:
  the UI reframes the budget as ₹/head; among verified within-budget carts the
  optimizer prefers those containing **at least N "mains"** (mains = high
  relevance-weight items per the existing classification). If budget can't cover
  N mains, return the best effort with a note.
- **Drinks toggle.** Unchanged (already shipped).

**Acceptance:** Veg-only yields a cart with only veg items; group size visibly
shifts results toward N mains and shows ₹/head; all preserved through the live
bill verification.

### 4. Deploy (live shareable URL)

- Deploy via existing `Dockerfile` + `render.yaml` to Render; set `BASE_URL`,
  confirm `COUPON_DB` on the persistent disk, `SESSION_SECRET` generated.
- **Risk:** Swiggy OAuth on a public URL is unverified end-to-end (redirect_uri
  match + dynamic client registration). Mitigation/fallback: if live login on the
  deployed URL is flaky, record a short walkthrough (local login works) + capture
  screenshots so the showcase never hinges on a fragile live login.

**Acceptance:** A public URL loads the app; either live login works, or a
recorded walkthrough + screenshots stand in.

### 5. Showcase materials (brand-neutral)

- A concise one-page writeup (README section or `docs/`): the problem
  (best-value cart under budget = knapsack), the approach (exact DP **verified
  against a brute-force oracle**, live authoritative-bill verification,
  auto-discovered coupons, personalization), the architecture, and the
  reliability/ops story (rate limiting, observability, graceful degradation).
- Surface the 468-test suite and the correctness contract.

**Acceptance:** A reader unfamiliar with the repo understands what it does, why
the approach is sound, and what was engineered — in a few minutes.

### 6. Order placement (user-initiated, COD-aware)

A **Place Order** button on a recommended cart that lets the *user* place the
real order on their own Swiggy account. This is the one place the app performs an
irreversible, real-money action, so it is gated tightly.

**Safety model (non-negotiable):**
- `place_food_order` is invoked **only** by an explicit user click that passes
  through a confirmation dialog. Never automatically; never by the optimization
  pipeline; never by the assistant during testing.
- The confirmation dialog shows the restaurant, exact items, and the
  **authoritative `to_pay` total**, names the payment method (**Cash on
  Delivery**), and prominently warns: *COD orders cannot be cancelled or refunded
  once placed.* The user must explicitly confirm.
- **Price-drift guard:** before placing, the backend rebuilds the selected cart
  live and re-reads the bill; if `to_pay` no longer matches what the user was
  shown (beyond a small tolerance), it aborts and re-presents the updated total
  for re-confirmation rather than placing silently.
- The request must carry an explicit `confirmed: true` flag (set only by the
  modal's confirm action), so a stray/direct call cannot place an order.

**Flow:**
1. User clicks Place Order on an option → confirmation modal (items, total, COD
   warning).
2. On confirm → `POST /api/place-order {restaurantId, addressId, option,
   confirmed: true}`.
3. Backend rebuilds that exact cart (and re-applies its winning coupon), verifies
   `to_pay`, then calls `place_food_order`. The probe-flush rule does **not**
   apply here — the cart must persist to be ordered.
4. Return Swiggy's order confirmation (order id / status) and show it; surface any
   failure cleanly.

**Acceptance:** The button never fires without explicit confirmation; the COD
non-cancellable warning is shown before any order; a drifted price blocks the
placement and re-confirms. (Live end-to-end placement is the user's to test — the
assistant never places a real order.)

## Components / Files Touched

- `cart_optimizer/swiggy_client.py` — rate limiter, 429 detection, teardown.
- `cart_optimizer/adapters/swiggy.py` — veg classifier capture; empty-cart
  detection helper.
- `cart_optimizer/models.py` — `Item.is_veg` (optional tri-state: veg / non-veg /
  unknown).
- `webapp/server.py` — profile params on `/api/optimize`, veg filter, group-size
  preference, graceful error handling, fewer verify calls; `/api/place-order`
  endpoint (rebuild + verify + place).
- `cart_optimizer/run.py` — a build-and-place helper (no flush) used by
  `/api/place-order`.
- `webapp/static/index.html` — profile step (veg/non-veg, group size) in the
  flow; Place Order button + COD confirmation modal.
- `tests/` — `test_swiggy_client.py` (rate limiter + 429 detection), profiling
  tests, store-robustness messaging test, place-order gating test.
- Docs — this spec; showcase writeup.

## Data Flow (optimize request)

`request {budget, addressId, restaurantId, profile{vegOnly, groupSize, drinks}}`
→ fetch/parse menu (cached) → **apply profile filter** (veg-only drops non-veg;
drinks toggle as today) → calibrate to real per-item prices (probe) → offline
optimize within stretch ceiling → **prefer carts with ≥N mains** → live-verify
top candidate(s) for the authoritative bill (coupons auto-tried) → return top-2
options (within / stretch) or a typed message (busy / can't-build / no-fit).

**Place-order flow** (separate, user-initiated): confirm modal →
`POST /api/place-order {…, confirmed:true}` → rebuild selected cart (no flush) →
re-verify `to_pay` (drift guard) → `place_food_order` → return order
confirmation. See workstream 6.

## Error Handling

| Condition | Response |
|---|---|
| Swiggy rate-limited / transport poisoned | 200 `{found:false, message:"Swiggy is busy — try again in a moment."}` |
| No candidate could be built (store unsupported) | 200 `{found:false, message:"couldn't build a cart here"}` |
| Carts built but none within budget | 200 `{found:false, message:"No cart fits ₹X"}` |
| Place-order without `confirmed:true` | 400 — never reaches `place_food_order` |
| Price drifted since shown | 200 `{placed:false, reason:"price_changed", newTotal}` → re-confirm |
| Order placement failed upstream | 200 `{placed:false, message:"Couldn't place the order — try on Swiggy directly."}` |
| Bad request body | 422 (Pydantic validation) |
| Unexpected server error | 500 clean JSON (global handler, no traceback leak) |

## Testing Strategy

- Preserve all existing tests incl. the DP-vs-oracle equivalence (untouched).
- New unit tests: `_is_rate_limited` through ExceptionGroup/cause chains; rate
  limiter spacing; veg filter (veg-only excludes non-veg + unknown); group-size
  preference selects ≥N mains when affordable; store-robustness message mapping.
- Web layer: `/api/optimize` 422 on bad budget, 401 when logged out (first web
  tests).
- Order placement: `/api/place-order` rejects without `confirmed:true` and when
  logged out; the price-drift path returns re-confirm **without** calling
  `place_food_order` (the place call is mocked — never hit live in tests).
- Manual/live: one warm-cache optimize call-count check; veg-only and group-size
  visibly change a real cart.

## Risks & Mitigations

1. **Empty-cart root cause unknown** (workstream 2). Front-loaded; timeboxed.
   Graceful messaging ships regardless, so the demo degrades cleanly.
2. **Live OAuth on deployed URL** (workstream 4). Fallback: recorded walkthrough +
   screenshots. Local login is known-good.
3. **Veg metadata field name** varies in Swiggy payloads. Mitigation: inspect the
   live menu payload during implementation and map the confirmed field; default
   unknown → excluded under veg-only (fail safe).

## Sequencing (risk-ordered)

1. Reliability layer (unblocks all live testing).
2. Empty-cart investigation + graceful store handling.
3. Profiling (veg/non-veg, then group size).
4. Order placement (button + COD confirmation + price-drift guard).
5. Deploy + OAuth check (with fallback).
6. Showcase writeup + final polish.

## Definition of Done

- No 500s under rapid multi-store/budget testing; clean retry message under load.
- Unsupported stores show the correct message, not a misleading one.
- Veg-only and group-size demonstrably change carts, verified by the live bill.
- The Place Order button places a real order **only** after an explicit
  COD-warning confirmation, with a price-drift guard; the pipeline never
  auto-places.
- A public URL (or recorded walkthrough) demonstrates the full flow.
- A brand-neutral one-pager + green test suite present the rigor.

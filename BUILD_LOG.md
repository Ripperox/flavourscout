# Build Log — cart_optimizer

Purpose: crash-resumable record of progress. If a session dies, read this file
top-to-bottom; the last log entry says exactly where things stand and what to
do next.

## Project shape (decided in design discussion)

- **Goal (part 1):** exact coupon-aware cart optimizer. Given one restaurant
  menu + applicable offers + user + budget, return the provably best cart:
  max total preference, ties broken by lower final price, final price
  (subtotal − discount + delivery + platform fee + GST) ≤ budget.
- **Algorithm:** multiple-choice knapsack DP over *exact* spend levels, then a
  coupon layer that evaluates each coupon as a function of spend. Scoped
  coupons (`applies_to`) use a two-knapsack decomposition (in-scope DP ×
  rest DP, cross-joined). This captures the FLAT100 step function (adding an
  item can *lower* the final price by crossing a threshold).
- **Verification:** a brute-force oracle enumerates every cart × coupon and is
  asserted equal to the DP on ~120 random menus (property test). Both solvers
  share the same money math (`pricing.price_amounts`) so they cannot diverge
  on pricing rules — only on search strategy.
- **v1 scope:** items with choose-one variants; coupons flat / percent /
  free_delivery, optionally scoped via `applies_to`; delivery + platform fee +
  GST; availability flags + time windows. NOT in v1: add-ons, combos,
  quantities > 1.
- **Coupon query vocabulary** (validated at construction time, fail-fast):
  `subtotal`, `select_subtotal`, `user.member`, `user.first_order`,
  JSON literals `true/false/null`. `item_count` deliberately unsupported
  (would need an extra DP dimension).
- **Money:** item costs are int rupees; totals rounded to 2 dp.
- **Real-data track:** Swiggy MCP server is remote
  (`https://mcp.swiggy.com/food`, OAuth 2.1 + PKCE) — the user must connect it
  (`claude mcp add --transport http swiggy-food https://mcp.swiggy.com/food`).
  Adapter (`cart_optimizer/adapters/swiggy.py`) will be written against a
  captured live menu response. NOT done yet — blocked on connector.
- Root-level `knapsack.py` is the old prototype, superseded by this package.

## Module map

| File | Role |
|---|---|
| `cart_optimizer/safe_eval.py` | sandboxed AST evaluator for coupon query strings |
| `cart_optimizer/models.py` | Item/Variant/Coupon/Cart/Menu/User/PricingConfig + `Menu.from_dict` |
| `cart_optimizer/pricing.py` | shared money math: eligibility, discount, breakdown |
| `cart_optimizer/optimizer.py` | the exact DP solver (ships) |
| `cart_optimizer/brute_force.py` | enumeration oracle (tests only) |
| `cart_optimizer/mock_data.py` | spec-flavoured demo menu |
| `cart_optimizer/demo.py` | CLI demo: `python -m cart_optimizer.demo` |
| `tests/` | per-module tests + DP==brute-force property test |

## Environment

- Python: (see first log entry)
- pytest 9.0.3, pytest-randomly 4.1.0 in `./venv`
- Branch: `cart-optimizer-engine`

## Log

- [setup] venv ready, package skeleton (`cart_optimizer/`, `tests/`),
  `pytest.ini`, `requirements.txt`, `.gitignore` written.
- [round A — safe_eval] GREEN: 45 passed. Sandboxed AST evaluator with
  whitelist, JSON literals, short-circuit and/or, chained comparisons,
  parse-time vocabulary validation (`validate_expression`). Python 3.14.4.
- [round B — models] GREEN: 97 passed cumulative. Item/Variant/Coupon/Cart/
  Menu/User/PricingConfig with fail-fast validation (incl. coupon query
  vocabulary), wrap-around time windows, `Menu.from_dict` for the normalized
  JSON shape (variant map, int shorthand, bare-cost synthetic variant).
- [round C — pricing] GREEN: 13 passed (110 cumulative). Amount-level pricing
  (`price_amounts`) + cart wrapper, eligibility via safe_eval, GST on
  discounted item total, free_delivery waives the fee, 2dp rounding.
- [round D — brute_force] GREEN: 7 passed (117 cumulative). Oracle enumerates
  all carts × coupons via shared pricing; hand-verified cases incl. coupon
  threshold unlock, member free-delivery, scoped percent, tie-break.
- [round E — optimizer] GREEN: 11 passed (128 cumulative). Exact DP:
  multiple-choice knapsack over exact spend + per-spend coupon evaluation;
  scoped coupons via two-knapsack decomposition; spend cap derived from max
  possible coupon clawback. Step-function case verified (budget 120: solver
  adds a ₹60 item to unlock FLAT100 → ₹210 cart prices at ₹110).
- [round F — equivalence] GREEN: 120 random menus, DP == brute force on
  (preference, total), plus structural validity of both results. Extra
  one-off fuzz: 1000/1000 additional random scenarios matched.
- [round G — packaging] GREEN: full suite 250 passed in 0.15s. Added
  mock_data (7 items, 3 offers incl. unavailable + breakfast-window items),
  demo CLI (`python -m cart_optimizer.demo`), package exports, adapters stub
  with capture instructions, README. Demo verified: member/₹300 → FLAT100
  cart ₹218.80 @ pref 1.70 (vs ₹263.95 @ 1.30 couponless); guest/₹150 →
  ₹199 Margherita lands at ₹137.95 via FLAT100 (couponless same price only
  reaches pref 0.70) — the step-function win, end to end.

### Checkpoint: PART 1 (v1) ENGINE COMPLETE — 250 passed.
(Superseded by the v2 status at the bottom of this log; kept for history.)

## v2: full spec data model

Scope: add-on groups (`grp_` min/max + `opt_` options with own cost/pref),
combos (`cmb_` with cost, own preference, display composition, user-status
applicability), per-line quantities (opt-in via `max_quantity`, default 1 —
v1 behavior preserved). Design: a new `choices.py` enumerates every valid
order line per product (item config = variant × addon-selection × qty;
combo = qty); BOTH solvers consume the same per-product choice lists and
pick at most one line per product, so the DP stays a multiple-choice
knapsack and equivalence keeps verifying the search. Deliberately deferred:
cart-minimum combo applicability and `item_count` coupon queries (rejected
at validation, documented), mixed variants of the same item in one cart.

- [v2 plan] Swiggy MCP still not connected (ToolSearch: no swiggy tools) —
  real-data track remains blocked on user running `claude mcp add`.
- [v2 round 1 — models] GREEN: 286 passed (all 250 v1 tests untouched).
  AddonOption/AddonGroup (min/max bounds, unique ids across groups), Combo
  (user-status applicability only; cart-state rejected), ItemLine
  (variant+addons+quantity with full config validation; CartLine alias) and
  ComboLine, product-id based Cart, Menu.combos + orderable_combos +
  from_dict for addons/max_quantity/combos, coupon scopes accept cmb_ ids.
  Preference sums switched to line level (identical for v1 lines).
- [v2 round 2 — choices] GREEN. `choices.py`: product_lines() expands an
  item into variant × addon-selection × quantity lines (combinations honor
  group min/max), combo into quantity lines; menu_choices() filters by
  availability/time/user-applicability and groups per product. Explosion
  guard MAX_LINES_PER_PRODUCT=10_000.
- [v2 round 3 — solvers] GREEN. Rewrote BOTH solvers onto menu_choices: DP
  is now a multiple-choice knapsack over per-product line lists (variants,
  addons, qty, combos all ride the same machinery); brute force enumerates
  {skip}∪lines per product. Hand cases in test_solvers_v2 cover mandatory/
  optional addons, qty filling budget, qty/addon crossing a coupon
  threshold (step function generalized), combo-vs-items tradeoff, combo
  applicability, scoped coupon on a combo. One test expectation corrected
  (combo composes WITH a separately-added soda → that's the true optimum;
  oracle confirmed).
- [v2 round 4 — equivalence] GREEN: 383 passed. Extended random_scenario to
  generate addons (~55%), combos (~66%), quantities (~58%) with a
  brute-force-size trim (cap 60k carts); made assert_valid_result and the
  equivalence detail-string line-type-aware. Property test now 200 seeds.
  Off-line fuzz: **2000/2000 matched in 1.8s**. Enriched mock_data (pizza
  addons, 3x drink cap, two combos incl. first-order welcome) + line-aware
  demo renderer; exports + version bumped to 0.2.0.

## Status: v1 + v2 ENGINE COMPLETE ✅

Full spec data model implemented and proven exact. Branch
`cart-optimizer-engine`, uncommitted. Suite: `venv/bin/python -m pytest` →
383 passed (~0.3s). DP == brute-force oracle on 200 in-suite + 2000 off-line
random menus spanning the whole feature surface.

## Real-data track: Swiggy adapter DONE (menu path)

Swiggy MCP authenticated (the user ran `/mcp`). Captured live read-only data
for McDonald's (restaurantId 668678) → Chandivali address via `get_addresses`
(then user picked Home), `search_restaurants`, `get_restaurant_menu`,
`search_menu` (burger + fries), `fetch_food_coupons`. NO order/cart-mutating
tools were ever called.

- [real-data round 1 — adapter] GREEN: 400 passed. Fixtures
  `tests/fixtures/mcdonalds_{menu,search_addons}.json` (real, trimmed subsets;
  see fixtures/README). `cart_optimizer/adapters/swiggy.py`:
  - prefixes Swiggy bare-number ids into `itm_/var_/grp_/opt_`; `swiggy_id()`
    inverse for later cart-building.
  - `parse_menu` dedupes items across categories, rounds float prices to
    rupees, derives preference from rating (+bestseller bump), maps inStock →
    available, merges add-on detail from `search_menu` by item id.
  - `parse_addon_groups`: maxAddons → max_select, min_select=0 (included
    default already priced into the ₹0 choices), clamps max to option count.
  - Refuses to GUESS uncaptured shapes: item `variantsV2/variations` →
    SwiggyAdapterError; non-empty coupon payload → SwiggyAdapterError.
  - End-to-end verified: parsed real McDonald's menu (7 items) → optimizer
    picked McAloo Tikki + McChicken = ₹283.90 all-in @ pref 1.85 under ₹300.

### Real-data findings that shaped the design
- Swiggy ids are bare numbers; menu is paginated/compact (no variant/addon
  detail) and the same item id recurs across categories → dedupe required.
- This restaurant models sizes (Fries R/M/L) as SEPARATE items, so no
  `variantsV2/variations` ever appeared — variant path is guarded, not built.
- Prices carry paise (e.g. 171.57); DP needs int spend, model only ranks →
  round to rupees (authoritative bill comes from Swiggy at confirm time).
- `fetch_food_coupons` returned `{}` for McDonald's (coupons likely
  cart-dependent) → real coupon shape still uncaptured.

- [real-data round 2 — SWIGGYIT coupon] GREEN: 402 passed. User reported a
  Chandivali code SWIGGYIT (flat ₹80 off above ₹159). `fetch_food_coupons`
  returns `{}` for it across McDonald's/KFC/Burger King, with AND without the
  `couponCode` arg → the code is cart-gated (only surfaces once a ≥₹159 cart
  exists); the read-only endpoint can't reveal it. Modelled it from the user's
  stated rule as `Coupon(flat, 80, "subtotal >= 159")` and ran it on the real
  McDonald's menu: at ₹200 budget it unlocks the step function — plain cart =
  1 burger (pref 0.93), with SWIGGYIT = 2 burgers crossing ₹159 → −₹80 →
  ₹199.90 (pref 1.85). Pinned by `tests/test_real_coupon_scenario.py`.
  `parse_coupons` STILL has no real API shape (endpoint stays empty); capturing
  it needs the cart-build path below (mutating, pending user approval).

## Next steps (in order)

1. **Capture the real coupon API shape (needs approval):** the only way the
   MCP reveals a cart-gated code like SWIGGYIT is to build a real ≥₹159 cart
   (`update_food_cart`) and read it back (`apply_food_coupon`/`get_food_cart`).
   That MUTATES the user's live Swiggy cart, so do it only with explicit
   per-action approval, then flush. Save the payload as a fixture and shape
   `parse_coupons` against it (it currently raises on non-empty). Until then,
   user-described coupons work (see test_real_coupon_scenario).
2. **Capture a real variant shape:** a restaurant with `hasVariants:true`
   items (beverages elsewhere), then implement+test the variant path (it
   currently raises).
3. **Cart-build path:** map an optimized cart back to Swiggy `update_food_cart`
   calls via `swiggy_id()`, show the real bill for confirmation (read-only
   until the user explicitly approves an order). NEVER auto-call
   `place_food_order` (COD, non-cancellable).
4. Calibrate PricingConfig (delivery/platform/GST) against a real Swiggy bill.
5. Deferred engine scope if needed: `item_count` coupon queries, cart-minimum
   combo applicability, mixed variants of one item.
<!-- log-end -->

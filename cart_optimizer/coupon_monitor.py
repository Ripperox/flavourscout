"""Continuous coupon intelligence.

Assemble a candidate corpus and *prove* each code against the platform's real
bill, per branch, recording validity + discount in the shared ledger. This
replaces "trust whatever Swiggy suggests" with evidence.

It runs as **opportunistic background sweeps**: after a user's optimize at a
branch, the server kicks off a sweep there (see webapp), so the ledger stays
fresh from real traffic without a dedicated crawler. Every sweep builds a probe
cart, applies a code, reads the authoritative bill, and flushes — it NEVER places
an order, and it is paced by the client's global rate limiter.

Corpus = codes proven at any branch of the same brand (the big lever — coupons
are usually brand-wide) ∪ a seed list ∪ the cart's auto-suggested code. A code is
credited ONLY when it actually lowers ``to_pay`` *and* the bill shows it applied
(so a previous coupon lingering on the cart can't be mis-credited).

Note (v1 scope): each sweep probes one representative cart, so coupons scoped to
products absent from that cart read as misses here. Broadly-applicable coupons
are captured reliably; narrow per-product scope is a later refinement.
"""

from __future__ import annotations

import logging
import time

from .coupon_ledger import brand_key

log = logging.getLogger("cartoptimizer.coupons")

# Starting corpus. Grows automatically via brand-wide sharing as codes are proven.
SEED_CODES: tuple[str, ...] = (
    "SWIGGYIT", "FLAT100", "FLAT125", "FLAT75", "FLAVORFUL", "TRYNEW",
    "SAVE50", "NEW50", "WELCOME50", "ITSWIGGY", "PARTY",
)

RESWEEP_TTL = 6 * 3600        # re-validate a code at most ~every 6 hours
DEFAULT_SWEEP_BUDGET = 6      # codes tested per sweep (rate-limit friendly)


def build_corpus(ledger, brand: str, suggested: str | None = None) -> list[str]:
    """Ordered, de-duped candidate codes: brand-proven first (most likely to
    work), then the seed list, then the cart's suggested code."""
    out: list[str] = []

    def add(code: str | None) -> None:
        if code and code not in out:
            out.append(code)

    if hasattr(ledger, "brand_codes"):
        for code in ledger.brand_codes(brand):
            add(code)
    for code in SEED_CODES:
        add(code)
    add(suggested)
    return out


def codes_to_sweep(ledger, restaurant_id: str, corpus: list[str], *,
                   ttl: float = RESWEEP_TTL, budget: int = DEFAULT_SWEEP_BUDGET,
                   now: float | None = None) -> list[str]:
    """Pick codes worth (re)testing now: untested, or last tested longer ago than
    ``ttl``. Freshly-tested codes are skipped. Capped at ``budget``."""
    now = time.time() if now is None else now
    picks: list[str] = []
    for code in corpus:
        last = ledger.last_tested_at(restaurant_id, code) if hasattr(ledger, "last_tested_at") else 0.0
        if now - last >= ttl:
            picks.append(code)
        if len(picks) >= budget:
            break
    return picks


async def sweep_branch(client, restaurant_id: str, restaurant_name: str,
                       address_id: str, cart_items: list, ledger, *,
                       budget: int = DEFAULT_SWEEP_BUDGET, suggested: str | None = None) -> int:
    """Prove untested/stale coupon codes for a branch on a representative cart.

    Returns the count of NEW working codes found. Best-effort; always flushes the
    probe cart; never places an order."""
    from .adapters.swiggy import parse_cart_bill

    if not cart_items:
        return 0
    brand = brand_key(restaurant_name)
    if hasattr(ledger, "set_branch"):
        ledger.set_branch(restaurant_id, brand, restaurant_name)

    corpus = build_corpus(ledger, brand, suggested)
    picks = codes_to_sweep(ledger, restaurant_id, corpus, budget=budget)
    if not picks:
        return 0

    found = 0
    try:
        await client.call("flush_food_cart")
        await client.call("update_food_cart", restaurantId=restaurant_id,
                          restaurantName=restaurant_name, addressId=address_id,
                          cartItems=cart_items)
        base = parse_cart_bill(await client.call(
            "get_food_cart", addressId=address_id, restaurantName=restaurant_name))
        base_to_pay = base.to_pay
        for code in picks:
            try:
                await client.call("apply_food_coupon", couponCode=code, addressId=address_id)
                bill = parse_cart_bill(await client.call(
                    "get_food_cart", addressId=address_id, restaurantName=restaurant_name))
                discount = base_to_pay - bill.to_pay
                applied = (bill.coupon_code or "").strip().upper() == code.strip().upper()
                worked = discount > 0.5 and applied
                ledger.record(restaurant_id, code, discount if worked else 0)
                if worked:
                    found += 1
                    log.info("sweep rid=%s: %s works (-%.0f)", restaurant_id, code, discount)
            except Exception as e:  # noqa: BLE001 — a dead code just costs one call
                ledger.record(restaurant_id, code, 0)
                log.debug("sweep rid=%s: %s failed: %s", restaurant_id, code, e)
    finally:
        try:
            await client.call("flush_food_cart")
        except Exception:  # noqa: BLE001
            pass

    log.info("sweep rid=%s done: tested %d, %d new working", restaurant_id, len(picks), found)
    return found

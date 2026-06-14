"""Discovery-and-verify loop: turn the optimizer's estimates into a
Swiggy-confirmed best-value cart.

Why this exists: coupons in Swiggy are hidden until a qualifying cart exists,
the best coupon depends on the cart's contents, and our fee/coupon model is
only an estimate. So we cannot trust a single estimated "best cart". Instead:

1. ``propose_candidates`` generates a *diverse* set of plausible carts
   (anchored on different items) so they trigger different possible coupons.
2. Each candidate is confirmed through a ``CartVerifier`` — in production the
   live Swiggy cart (build → explicitly apply each candidate coupon → read
   bill → flush) and returns the authoritative ``to_pay``. NOTE (verified live
   2026-06-13): Swiggy does NOT auto-apply coupons — the cart only *suggests*
   one (``coupon_discount == 0``); a coupon must be applied explicitly, and it
   can be rejected by item restrictions (SWIGGYIT: "not applicable on
   pre-packaged & combo items"). So which coupon a cart can use is itself
   something only the live cart can tell us.
3. ``discover_best_cart`` keeps the highest-preference candidate whose REAL
   bill is within budget, ties broken by the lower real price.

The verifier is an injected boundary so the whole loop is unit-tested offline
with a fake; the live one lives in ``adapters.swiggy_session`` and is only run
with explicit user approval (it mutates the live cart).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Protocol, Sequence

from .adapters.swiggy import CartBill
from .models import Cart, ItemLine, Menu, PricingConfig, User
from .optimizer import best_cart

__all__ = [
    "CartVerifier",
    "VerifiedCart",
    "discover_best_cart",
    "propose_candidates",
]


class CartVerifier(Protocol):
    """Anything that can confirm a cart against Swiggy's real bill."""

    def verify(self, cart: Cart) -> CartBill: ...


@dataclass(frozen=True)
class VerifiedCart:
    cart: Cart
    bill: CartBill

    @property
    def preference(self) -> float:
        return sum(line.preference for line in self.cart.lines)


def _cart_key(cart: Cart):
    return frozenset(
        (line.product_id, getattr(line, "variant", None) and line.variant.id, line.quantity)
        for line in cart.lines
    )


def discover_best_cart(
    candidates: Sequence[Cart], verifier: CartVerifier, budget: float
) -> VerifiedCart | None:
    """Verify each candidate against the real bill, return the best within
    budget (max preference, then lowest real ``to_pay``), or None if none fit.

    Duplicate candidate carts are verified only once."""
    verified: list[VerifiedCart] = []
    seen: set = set()
    for cart in candidates:
        if not cart.lines:
            continue
        key = _cart_key(cart)
        if key in seen:
            continue
        seen.add(key)
        verified.append(VerifiedCart(cart, verifier.verify(cart)))

    feasible = [v for v in verified if v.bill.to_pay <= budget + 1e-9]
    if not feasible:
        return None
    return max(feasible, key=lambda v: (v.preference, -v.bill.to_pay))


def propose_candidates(
    menu: Menu,
    user: User,
    config: PricingConfig,
    budget: float,
    max_candidates: int = 5,
) -> list[Cart]:
    """A diverse set of candidate carts to probe different coupons.

    Strategy:
    1. The model's overall best cart (highest estimated preference).
    2. Carts anchored on each high-*preference* item — covers taste-driven picks.
    3. Carts anchored on each high-*cost* item — covers price-threshold coupons
       like SWIGGYIT (₹80 off above ₹159) that only apply on premium items; the
       cheap-preference-maximising cart may be coupon-ineligible.

    The anchor forces one item in, the complement is the best remaining cart.
    Estimates here only choose *which* carts to probe; the real bill comes from
    the verifier and is authoritative.
    """
    candidates: list[Cart] = []
    seen: set = set()

    def add(cart: Cart) -> None:
        if not cart.lines:
            return
        key = _cart_key(cart)
        if key not in seen:
            seen.add(key)
            candidates.append(cart)

    def anchor_on(item) -> None:
        if len(candidates) >= max_candidates:
            return
        if not item.is_orderable():
            return
        anchor = ItemLine(item, min(item.variants, key=lambda v: v.cost))
        if anchor.cost > budget:
            return
        rest_menu = dataclasses.replace(
            menu, items=tuple(i for i in menu.items if i.id != item.id)
        )
        complement = best_cart(rest_menu, user, config, budget - anchor.cost).cart
        add(Cart((anchor,) + complement.lines))

    # 1. Overall best estimate.
    add(best_cart(menu, user, config, budget).cart)

    # 2. Anchor on high-preference items.
    for item in sorted(menu.items, key=lambda i: i.preference, reverse=True):
        if len(candidates) >= max_candidates:
            break
        anchor_on(item)

    # 3. Anchor on high-cost items (different set — triggers threshold coupons).
    orderable = [i for i in menu.items if i.is_orderable()]
    for item in sorted(orderable, key=lambda i: max(v.cost for v in i.variants), reverse=True):
        if len(candidates) >= max_candidates:
            break
        anchor_on(item)

    return candidates[:max_candidates]

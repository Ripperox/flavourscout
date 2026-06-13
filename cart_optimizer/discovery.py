"""Discovery-and-verify loop: turn the optimizer's estimates into a
Swiggy-confirmed best-value cart.

Why this exists: coupons in Swiggy are hidden until a qualifying cart exists,
the best coupon depends on the cart's contents, and our fee/coupon model is
only an estimate. So we cannot trust a single estimated "best cart". Instead:

1. ``propose_candidates`` generates a *diverse* set of plausible carts
   (anchored on different items) so they trigger different possible coupons.
2. Each candidate is confirmed through a ``CartVerifier`` — in production the
   live Swiggy cart (build → read bill → flush), which auto-applies the best
   coupon and returns the authoritative ``to_pay``.
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

    Strategy (initial): the model's overall best cart, then carts *anchored*
    on each high-preference item (forced in, best complement chosen from the
    rest). Anchoring varies the item mix so candidates trigger different
    item-conditional coupons once verified. Estimates here only pick *which*
    carts to probe; the real bill comes from the verifier.
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

    add(best_cart(menu, user, config, budget).cart)

    for item in sorted(menu.items, key=lambda i: i.preference, reverse=True):
        if len(candidates) >= max_candidates:
            break
        if not item.is_orderable():
            continue
        anchor = ItemLine(item, min(item.variants, key=lambda v: v.cost))
        if anchor.cost > budget:
            continue
        rest_menu = dataclasses.replace(
            menu, items=tuple(i for i in menu.items if i.id != item.id)
        )
        complement = best_cart(rest_menu, user, config, budget - anchor.cost).cart
        add(Cart((anchor,) + complement.lines))

    return candidates[:max_candidates]

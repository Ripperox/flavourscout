"""Live CartVerifier: builds candidate carts on Swiggy, tries coupon codes,
returns the authoritative bill with the lowest to_pay.

Architecture: each MCP operation is an injected callable so the verifier is
fully unit-testable offline (swap real calls for mocks). The live wiring lives
in ``make_live_ops``, which callers pass real tool-call functions into.

Verification flow per candidate cart:
  1. flush → build cart → read base bill (no coupon)
  2. for each coupon code: flush → build → apply_coupon (skip if rejected)
     → read bill
  3. return the bill with the lowest to_pay

SAFETY: this mutates the live Swiggy cart. NEVER call place_food_order.
Only run with explicit per-action user approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ..models import Cart, ComboLine, Item, ItemLine
from .swiggy import CartBill, SwiggyAdapterError, parse_cart_bill, swiggy_id

__all__ = [
    "CouponRejected",
    "SwiggyOps",
    "SwiggySessionVerifier",
    "cart_to_swiggy_items",
]


class CouponRejected(Exception):
    """Raised when apply_coupon is called and Swiggy rejects the code."""


@dataclass
class SwiggyOps:
    """Injectable MCP operations — swap real vs mock without changing verifier.

    flush()                           -> None
    update(restaurant_id, address_id, cart_items) -> None
    apply_coupon(coupon_code, address_id)         -> Any   (raises CouponRejected on refusal)
    get_cart(address_id)              -> dict  (raw get_food_cart response)
    """

    flush: Callable[[], None]
    update: Callable[[str, str, list[dict]], None]
    apply_coupon: Callable[[str, str], Any]
    get_cart: Callable[[str], dict]


def _find_group_id(item: Item, opt_id: str) -> str | None:
    """Return the bare Swiggy group id for an option, by searching item.addons."""
    for group in item.addons:
        if any(o.id == opt_id for o in group.options):
            return swiggy_id(group.id)
    return None


def _decode_variant_selections(variant) -> list[dict[str, str]]:
    """Decode an encoded variant ID into Swiggy [{group_id, variation_id}] pairs.

    Encoded format (multi-group items like Starbucks):
        var_57291002:177629512|57291004:177629516
    Synthetic format (single-variant items like McDonald's):
        var_109348830  → no groups needed, return []
    """
    bare = swiggy_id(variant.id)   # strips "var_" prefix
    if ":" not in bare:
        return []   # synthetic single-variant — Swiggy needs no variants field
    pairs = []
    for segment in bare.split("|"):
        if ":" in segment:
            group_id, variation_id = segment.split(":", 1)
            pairs.append({"group_id": group_id, "variation_id": variation_id})
    return pairs


def cart_to_swiggy_items(cart: Cart) -> list[dict[str, Any]]:
    """Convert an optimized Cart into the cartItems list for update_food_cart.

    For single-variant items (McDonald's): {menu_item_id, quantity, addons?}
    For multi-variant items (Starbucks sizes): adds {variants: [{group_id, variation_id}]}
    """
    items: list[dict[str, Any]] = []
    for line in cart.lines:
        entry: dict[str, Any] = {
            "menu_item_id": swiggy_id(line.product_id),
            "quantity": line.quantity,
        }
        if isinstance(line, ItemLine):
            # Variant selections (size groups etc.)
            variant_pairs = _decode_variant_selections(line.variant)
            if variant_pairs:
                entry["variants"] = variant_pairs
            # Addon selections
            if line.addons:
                entry["addons"] = [
                    {
                        "choice_id": swiggy_id(opt.id),
                        **(
                            {"group_id": gid}
                            if (gid := _find_group_id(line.item, opt.id))
                            else {}
                        ),
                    }
                    for opt in line.addons
                ]
        items.append(entry)
    return items


class SwiggySessionVerifier:
    """Live CartVerifier. Builds each cart on Swiggy, tries every coupon,
    returns the authoritative CartBill with the lowest to_pay.

    Args:
        ops:           Injected MCP callables (real or mock).
        restaurant_id: Swiggy restaurant id (bare number, e.g. "668678").
        address_id:    Swiggy address id for delivery charge calculation.
        coupon_codes:  Coupon codes to try per cart (e.g. ["SWIGGYIT"]).
                       Empty list = just read the base bill.
    """

    def __init__(
        self,
        ops: SwiggyOps,
        restaurant_id: str,
        address_id: str,
        coupon_codes: list[str] | None = None,
    ) -> None:
        self.ops = ops
        self.restaurant_id = restaurant_id
        self.address_id = address_id
        self.coupon_codes = list(coupon_codes or [])

    def verify(self, cart: Cart) -> CartBill:
        """Build the cart on Swiggy, try each coupon, return the best bill."""
        cart_items = cart_to_swiggy_items(cart)
        bills: list[CartBill] = []

        # Base bill: no coupon applied.
        bills.append(self._build_and_read(cart_items))

        # Try each coupon on a fresh copy of the cart.
        for code in self.coupon_codes:
            try:
                self._build(cart_items)
                self.ops.apply_coupon(code, self.address_id)
                bills.append(parse_cart_bill(self.ops.get_cart(self.address_id)))
            except (CouponRejected, SwiggyAdapterError, Exception):
                pass  # rejected or unexpected error — skip, try next

        self.ops.flush()
        return min(bills, key=lambda b: b.to_pay)

    def cleanup(self) -> None:
        """Flush the Swiggy cart. Call after a full discovery run."""
        self.ops.flush()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _build(self, cart_items: list[dict]) -> None:
        self.ops.flush()
        self.ops.update(self.restaurant_id, self.address_id, cart_items)

    def _build_and_read(self, cart_items: list[dict]) -> CartBill:
        self._build(cart_items)
        return parse_cart_bill(self.ops.get_cart(self.address_id))

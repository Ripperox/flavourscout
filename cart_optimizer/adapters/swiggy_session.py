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


def _decode_variant_selections(variant) -> tuple[str, list[dict[str, str]]]:
    """Decode an encoded variant ID into (field, [{group_id, variation_id}]).

    ``field`` is which update_food_cart field to use:
      "variants"   — legacy variations format (Starbucks): var_57291002:177629512|...
      "variantsV2" — variantsV2 format (Burger King):      var_v2@75718135:220969284|...
    Synthetic single-variant ids (McDonald's, var_109348830) → ("variants", []).
    """
    bare = swiggy_id(variant.id)   # strips "var_" prefix
    field = "variants"
    if bare.startswith("v2@"):
        field = "variantsV2"
        bare = bare[len("v2@"):]
    if ":" not in bare:
        return field, []   # synthetic single-variant — no selection needed
    pairs = []
    for segment in bare.split("|"):
        if ":" in segment:
            group_id, variation_id = segment.split(":", 1)
            pairs.append({"group_id": group_id, "variation_id": variation_id})
    return field, pairs


def cart_to_swiggy_items(cart: Cart) -> list[dict[str, Any]]:
    """Convert an optimized Cart into the cartItems list for update_food_cart.

    For single-variant items (McDonald's): {menu_item_id, quantity, addons?}
    For variation items (Starbucks sizes):  adds {variants: [{group_id, variation_id}]}
    For variantsV2 items (Burger King):     adds {variantsV2: [{group_id, variation_id}]}
    """
    items: list[dict[str, Any]] = []
    for line in cart.lines:
        entry: dict[str, Any] = {
            "menu_item_id": swiggy_id(line.product_id),
            "quantity": line.quantity,
        }
        if isinstance(line, ItemLine):
            # Variant selections (size / format groups), correct field per format.
            field, variant_pairs = _decode_variant_selections(line.variant)
            if variant_pairs:
                entry[field] = variant_pairs
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


# Common Swiggy coupon codes to probe per cart. The list endpoint
# (fetch_food_coupons) is useless (returns {} even with a cart), so the only way
# to discover what a user can actually use is: (1) the coupon Swiggy AUTO-SUGGESTS
# in the built cart (always tried, free) plus (2) blind-trying known codes. This
# default is a seed; a real deployment maintains a per-user / per-area learned
# list so no good coupon is ever missed. Codes observed live: SWIGGYIT (McD),
# FLAT100 (BK), FLAT75 (Starbucks), FLAVORFUL (Taco Bell), DUOJOY/TRYNEW (suggested).
DEFAULT_COUPON_CANDIDATES: tuple[str, ...] = (
    "SWIGGYIT", "FLAT100", "FLAT125", "FLAT75", "FLAT150", "FLAVORFUL",
    "DUOJOY", "TRYNEW", "WELCOME", "SAVE50", "NEW100",
)


def _suggested_coupon(raw_cart_response) -> str | None:
    """The coupon Swiggy auto-suggests for a cart, even at ₹0 discount.

    parse_cart_bill() intentionally reports coupon_code=None when the discount is
    0 (suggested-but-not-applied). For DISCOVERY we still want that code, because
    applying it explicitly often unlocks the discount. Read it raw from offers."""
    try:
        offers = (raw_cart_response.get("data") or {}).get("offers") or {}
        code = offers.get("coupon_applied")
        return str(code) if code else None
    except AttributeError:
        return None


class SwiggySessionVerifier:
    """Live CartVerifier. Builds each cart on Swiggy ONCE, probes every coupon on
    that built cart (no rebuild between coupons → far fewer calls), and returns
    the authoritative CartBill with the lowest to_pay.

    Coupon discovery per cart = the auto-SUGGESTED coupon (always tried) ∪ the
    candidate list. So we never miss the coupon Swiggy itself recommends, and we
    also catch better ones it didn't surface.

    Args:
        ops:           Injected MCP callables (real or mock).
        restaurant_id: Swiggy restaurant id (bare number, e.g. "668678").
        address_id:    Swiggy address id for delivery charge calculation.
        coupon_codes:  Candidate codes to try (default DEFAULT_COUPON_CANDIDATES).
        rebuild_per_coupon: if True, flush+rebuild before each coupon (safest but
                       ~2x calls). Default False: apply coupons to the one built
                       cart (apply replaces the previous; rejections are skipped).
    """

    def __init__(
        self,
        ops: SwiggyOps,
        restaurant_id: str,
        address_id: str,
        coupon_codes: list[str] | None = None,
        rebuild_per_coupon: bool = False,
    ) -> None:
        self.ops = ops
        self.restaurant_id = restaurant_id
        self.address_id = address_id
        self.coupon_codes = (
            list(coupon_codes) if coupon_codes is not None
            else list(DEFAULT_COUPON_CANDIDATES)
        )
        self.rebuild_per_coupon = rebuild_per_coupon

    def verify(self, cart: Cart) -> CartBill:
        """Build once, probe suggested ∪ candidate coupons, return the best bill."""
        cart_items = cart_to_swiggy_items(cart)
        bills: list[CartBill] = []

        # Base bill (no coupon) + the coupon Swiggy auto-suggests for this cart.
        self._build(cart_items)
        raw = self.ops.get_cart(self.address_id)
        bills.append(parse_cart_bill(raw))
        suggested = _suggested_coupon(raw)

        # Try suggested first, then the candidate list (deduped, order-preserving).
        codes: list[str] = []
        for c in ([suggested] if suggested else []) + self.coupon_codes:
            if c and c not in codes:
                codes.append(c)

        for code in codes:
            try:
                if self.rebuild_per_coupon:
                    self._build(cart_items)
                self.ops.apply_coupon(code, self.address_id)
                bills.append(parse_cart_bill(self.ops.get_cart(self.address_id)))
            except (CouponRejected, SwiggyAdapterError, Exception):
                pass  # rejected or error — skip, try next

        self.ops.flush()
        return min(bills, key=lambda b: b.to_pay)

    def cleanup(self) -> None:
        """Flush the Swiggy cart. Call after a full discovery run."""
        self.ops.flush()

    # ── helpers ───────────────────────────────────────────────────────────────

    def _build(self, cart_items: list[dict]) -> None:
        self.ops.flush()
        self.ops.update(self.restaurant_id, self.address_id, cart_items)

"""Acceptance test: a real user-supplied coupon on the real captured menu.

SWIGGYIT (user-reported, Chandivali): flat ₹80 off on orders above ₹159.
Swiggy's read-only fetch_food_coupons does not surface this code without a
built cart, so it is modelled here from the rule the user gave — exactly how
the assistant would accept a coupon a user knows about. This pins the
coupon-aware step function against real McDonald's menu data so the behaviour
can't silently regress.
"""

import dataclasses
import json
from pathlib import Path

from cart_optimizer.adapters.swiggy import parse_menu
from cart_optimizer.models import Coupon, PricingConfig, User
from cart_optimizer.optimizer import best_cart

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = PricingConfig(delivery_fee=29, platform_fee=5, gst_rate=0.05)

SWIGGYIT = Coupon(
    id="off_SWIGGYIT",
    kind="flat",
    value=80,
    query="subtotal >= 159",
    description="₹80 off above ₹159",
)


def real_menu(with_coupon):
    menu = parse_menu(
        json.loads((FIXTURES / "mcdonalds_menu.json").read_text()),
        search_responses=[json.loads((FIXTURES / "mcdonalds_search_addons.json").read_text())],
    )
    return dataclasses.replace(menu, coupons=(SWIGGYIT,) if with_coupon else ())


def test_swiggyit_unlocks_a_better_cart_at_tight_budget():
    # ₹200: without the coupon only one burger fits; SWIGGYIT lets a second
    # burger cross the ₹159 threshold and the -₹80 brings it back under budget.
    plain = best_cart(real_menu(False), User(), CONFIG, budget=200)
    couponed = best_cart(real_menu(True), User(), CONFIG, budget=200)

    assert len(plain.cart.lines) == 1
    assert plain.coupon is None

    assert len(couponed.cart.lines) == 2
    assert couponed.coupon is not None and couponed.coupon.id == "off_SWIGGYIT"
    assert couponed.breakdown.discount == 80
    assert couponed.breakdown.total <= 200
    assert couponed.preference > plain.preference


def test_swiggyit_applies_whenever_subtotal_qualifies():
    couponed = best_cart(real_menu(True), User(), CONFIG, budget=300)
    assert couponed.coupon is not None and couponed.breakdown.discount == 80
    # final price = (subtotal - 80) + delivery + platform + 5% GST on the
    # discounted item total — the same math Swiggy would bill.
    sub = couponed.breakdown.subtotal
    expected = round((sub - 80) + 29 + 5 + 0.05 * (sub - 80), 2)
    assert couponed.breakdown.total == expected

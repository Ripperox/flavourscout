"""Tests for the discovery-and-verify loop.

The loop's job: take candidate carts, confirm each against Swiggy's REAL bill
(which auto-applies the best coupon), and return the best-value cart by the
authoritative price — never by our estimate. The live Swiggy calls sit behind
a CartVerifier boundary so the whole loop is testable offline with a fake.
"""

import json
from pathlib import Path

from cart_optimizer.adapters.swiggy import CartBill, parse_menu
from cart_optimizer.discovery import (
    VerifiedCart,
    discover_best_cart,
    propose_candidates,
)
from cart_optimizer.models import Cart, ItemLine, PricingConfig, User

FIXTURES = Path(__file__).parent / "fixtures"
CONFIG = PricingConfig(delivery_fee=29, platform_fee=5, gst_rate=0.05)


def real_menu():
    return parse_menu(json.loads((FIXTURES / "mcdonalds_menu.json").read_text()))


def line(menu, item_id):
    item = next(i for i in menu.items if i.id == item_id)
    return ItemLine(item, item.variants[0])


class FakeSwiggy:
    """Stand-in for Swiggy: prices a cart the way the real bill would, applying
    SWIGGYIT (₹80 off + free delivery) above ₹159. Records what it was asked to
    verify so tests can assert the loop never over-probes."""

    def __init__(self):
        self.verified: list[Cart] = []

    def verify(self, cart: Cart) -> CartBill:
        self.verified.append(cart)
        item_total = cart.subtotal
        if item_total >= 159:
            discount, free_delivery, delivery, code = 80, True, 0, "SWIGGYIT"
        else:
            discount, free_delivery, delivery, code = 0, False, 29, None
        taxes = round(0.05 * (item_total - discount), 2)
        to_pay = round(item_total - discount + delivery + 5 + taxes, 2)
        return CartBill(
            item_total=item_total,
            coupon_code=code,
            coupon_discount=discount,
            free_delivery=free_delivery,
            delivery_charge=delivery,
            taxes_and_charges=taxes,
            to_pay=to_pay,
            item_count=len(cart.lines),
            cod_available=True,
        )


def test_discover_picks_best_value_by_real_bill():
    menu = real_menu()
    one_burger = Cart((line(menu, "itm_109348830"),))                 # ₹79, no coupon
    two_burgers = Cart((line(menu, "itm_109348830"),
                        line(menu, "itm_109348844")))                 # ₹238, SWIGGYIT
    fake = FakeSwiggy()

    best = discover_best_cart([one_burger, two_burgers], fake, budget=200)

    assert isinstance(best, VerifiedCart)
    assert best.cart is two_burgers                  # higher preference, fits after coupon
    assert best.bill.coupon_code == "SWIGGYIT"
    assert best.bill.to_pay <= 200
    assert len(fake.verified) == 2                   # verified both, no more


def test_candidate_over_real_budget_is_rejected():
    menu = real_menu()
    # Maharaja (₹235) alone: no coupon below... it's >=159 so SWIGGYIT applies:
    # 235-80=155 +5 +tax 7.75 = 167.75, fits 170. A pricier pair would not.
    big = Cart((line(menu, "itm_109348828"), line(menu, "itm_109348844")))  # 235+159=394
    fake = FakeSwiggy()
    # 394-80=314 +5 +tax(15.7)=334.7 > 170 -> infeasible; nothing else offered
    assert discover_best_cart([big], fake, budget=170) is None


def test_discover_returns_none_for_no_candidates():
    assert discover_best_cart([], FakeSwiggy(), budget=300) is None


def test_propose_candidates_is_diverse_and_real():
    menu = real_menu()
    candidates = propose_candidates(menu, User(), CONFIG, budget=400, max_candidates=5)
    assert 2 <= len(candidates) <= 5
    # all non-empty and distinct by their item-id sets
    keys = {frozenset(l.product_id for l in c.lines) for c in candidates}
    assert len(keys) == len(candidates)
    assert all(c.lines for c in candidates)
    # diversity: candidates do not all share the exact same items
    assert len(keys) >= 2


def test_propose_then_discover_end_to_end():
    menu = real_menu()
    fake = FakeSwiggy()
    candidates = propose_candidates(menu, User(), CONFIG, budget=300, max_candidates=5)
    best = discover_best_cart(candidates, fake, budget=300)
    assert best is not None
    assert best.bill.to_pay <= 300
    assert len(fake.verified) == len(candidates)

"""Tests for the live SwiggySessionVerifier.

All MCP calls are mocked via SwiggyOps so nothing hits the network. The tests
verify the flush→build→apply→read logic and the best-bill selection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cart_optimizer.adapters.swiggy import CartBill, parse_menu
from cart_optimizer.adapters.swiggy_session import (
    CouponRejected,
    SwiggyOps,
    SwiggySessionVerifier,
    cart_to_swiggy_items,
)
from cart_optimizer.models import Cart, ItemLine

FIXTURES = Path(__file__).parent / "fixtures"
RESTAURANT_ID = "668678"
ADDRESS_ID = "addr_test"


def real_menu():
    return parse_menu(json.loads((FIXTURES / "mcdonalds_menu.json").read_text()))


def make_line(menu, item_id: str) -> ItemLine:
    item = next(i for i in menu.items if i.id == item_id)
    return ItemLine(item, item.variants[0])


def _bill(to_pay: float, coupon: str | None = None, discount: float = 0) -> dict:
    """Minimal get_food_cart response for parse_cart_bill."""
    return {
        "data": {
            "item_count": 1,
            "pricing": {
                "item_total": 200.0,
                "delivery_charge": 0 if coupon else 29.0,
                "taxes_and_charges": 10.0,
                "to_pay": to_pay,
            },
            "offers": {
                "coupon_applied": coupon,
                "coupon_discount": discount,
                "free_delivery_applied": bool(coupon),
            },
        },
        "availablePaymentMethods": ["Cash on Delivery"],
    }


# ── cart_to_swiggy_items ──────────────────────────────────────────────────────

def test_cart_to_swiggy_items_basic():
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    items = cart_to_swiggy_items(cart)
    assert len(items) == 1
    assert items[0]["menu_item_id"] == "109348830"
    assert items[0]["quantity"] == 1
    assert "addons" not in items[0]


def test_cart_to_swiggy_items_multi_line():
    menu = real_menu()
    cart = Cart((
        make_line(menu, "itm_109348830"),
        make_line(menu, "itm_109348844"),
    ))
    items = cart_to_swiggy_items(cart)
    assert len(items) == 2
    ids = {i["menu_item_id"] for i in items}
    assert ids == {"109348830", "109348844"}


# ── SwiggySessionVerifier ─────────────────────────────────────────────────────

class MockOps:
    """Records every call; caller sets responses via attributes."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.cart_responses: list[dict] = []  # popped in order
        self.coupon_error: Exception | None = None

    def flush(self):
        self.calls.append(("flush",))

    def update(self, restaurant_id, address_id, cart_items):
        self.calls.append(("update", restaurant_id, address_id, cart_items))

    def apply_coupon(self, code, address_id):
        self.calls.append(("apply_coupon", code, address_id))
        if self.coupon_error:
            raise self.coupon_error
        return {"status": "ok"}

    def get_cart(self, address_id):
        self.calls.append(("get_cart", address_id))
        return self.cart_responses.pop(0)

    def ops(self) -> SwiggyOps:
        return SwiggyOps(
            flush=self.flush,
            update=self.update,
            apply_coupon=self.apply_coupon,
            get_cart=self.get_cart,
        )


def make_verifier(mock: MockOps, coupon_codes=None) -> SwiggySessionVerifier:
    return SwiggySessionVerifier(
        ops=mock.ops(),
        restaurant_id=RESTAURANT_ID,
        address_id=ADDRESS_ID,
        coupon_codes=coupon_codes or [],
    )


def test_verify_no_coupons_returns_base_bill():
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    mock.cart_responses = [_bill(183.0)]

    bill = make_verifier(mock).verify(cart)

    assert bill.to_pay == 183.0
    assert bill.coupon_code is None
    # flush → update → get_cart → flush (cleanup)
    call_types = [c[0] for c in mock.calls]
    assert call_types == ["flush", "update", "get_cart", "flush"]


def test_verify_coupon_accepted_returns_discounted_bill():
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"), make_line(menu, "itm_109348844")))
    mock = MockOps()
    base = _bill(239.0)
    with_coupon = _bill(159.0, coupon="SWIGGYIT", discount=80)
    mock.cart_responses = [base, with_coupon]

    bill = make_verifier(mock, coupon_codes=["SWIGGYIT"]).verify(cart)

    assert bill.to_pay == 159.0
    assert bill.coupon_code == "SWIGGYIT"
    assert bill.coupon_discount == 80.0


def test_verify_coupon_rejected_falls_back_to_base():
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    mock.cart_responses = [_bill(183.0)]   # only base bill; coupon raises before get_cart
    mock.coupon_error = CouponRejected("Not applicable on pre-packaged items")

    bill = make_verifier(mock, coupon_codes=["SWIGGYIT"]).verify(cart)

    assert bill.to_pay == 183.0
    assert bill.coupon_code is None


def test_verify_picks_lowest_to_pay_across_coupons():
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"), make_line(menu, "itm_109348844")))
    mock = MockOps()
    base = _bill(300.0)
    coupon_a = _bill(250.0, coupon="FLAT50", discount=50)
    coupon_b = _bill(220.0, coupon="SWIGGYIT", discount=80)
    mock.cart_responses = [base, coupon_a, coupon_b]

    bill = make_verifier(mock, coupon_codes=["FLAT50", "SWIGGYIT"]).verify(cart)

    assert bill.to_pay == 220.0
    assert bill.coupon_code == "SWIGGYIT"


def test_verify_builds_once_and_applies_coupons_in_place():
    """Fast path: ONE build, then apply each coupon to the same cart (no rebuild)."""
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    base = _bill(239.0)
    a = _bill(200.0, coupon="FLAT50", discount=50)
    b = _bill(159.0, coupon="SWIGGYIT", discount=80)
    mock.cart_responses = [base, a, b]

    make_verifier(mock, coupon_codes=["FLAT50", "SWIGGYIT"]).verify(cart)

    call_types = [c[0] for c in mock.calls]
    # build once: flush, update, get_cart(base)
    # coupon 1:   apply_coupon, get_cart
    # coupon 2:   apply_coupon, get_cart
    # cleanup:    flush
    assert call_types == [
        "flush", "update", "get_cart",
        "apply_coupon", "get_cart",
        "apply_coupon", "get_cart",
        "flush",
    ]
    # Exactly one build (update) regardless of coupon count.
    assert sum(1 for c in mock.calls if c[0] == "update") == 1


def test_rebuild_per_coupon_flag_rebuilds():
    """Opt-in safety mode: flush+update before each coupon."""
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    mock.cart_responses = [_bill(239.0), _bill(159.0, coupon="SWIGGYIT", discount=80)]

    SwiggySessionVerifier(
        ops=mock.ops(), restaurant_id=RESTAURANT_ID, address_id=ADDRESS_ID,
        coupon_codes=["SWIGGYIT"], rebuild_per_coupon=True,
    ).verify(cart)

    assert sum(1 for c in mock.calls if c[0] == "update") == 2  # base + per-coupon


def test_suggested_coupon_is_auto_tried_even_if_not_in_list():
    """The coupon Swiggy auto-suggests (discount 0) must be applied explicitly."""
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    # Base bill suggests DUOJOY at ₹0 discount; applying it unlocks ₹80 off.
    base = _bill(300.0, coupon="DUOJOY", discount=0)
    applied = _bill(220.0, coupon="DUOJOY", discount=80)
    mock.cart_responses = [base, applied]

    # Empty candidate list — the suggested coupon must STILL be tried.
    bill = make_verifier(mock, coupon_codes=[]).verify(cart)

    assert bill.to_pay == 220.0
    assert bill.coupon_code == "DUOJOY"
    applied_codes = [c[1] for c in mock.calls if c[0] == "apply_coupon"]
    assert applied_codes == ["DUOJOY"]


def test_default_candidate_list_used_when_codes_omitted():
    verifier = SwiggySessionVerifier(MockOps().ops(), RESTAURANT_ID, ADDRESS_ID)
    assert "SWIGGYIT" in verifier.coupon_codes
    assert "FLAT100" in verifier.coupon_codes
    assert len(verifier.coupon_codes) >= 5


def test_verify_correct_restaurant_and_address_passed():
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    mock.cart_responses = [_bill(183.0)]

    make_verifier(mock).verify(cart)

    update_call = next(c for c in mock.calls if c[0] == "update")
    assert update_call[1] == RESTAURANT_ID
    assert update_call[2] == ADDRESS_ID


def test_cleanup_flushes_cart():
    mock = MockOps()
    verifier = make_verifier(mock)
    verifier.cleanup()
    assert mock.calls == [("flush",)]


def test_generic_exception_in_apply_is_skipped():
    """Any exception from apply_coupon (e.g. network error) is treated as rejection."""
    menu = real_menu()
    cart = Cart((make_line(menu, "itm_109348830"),))
    mock = MockOps()
    mock.cart_responses = [_bill(183.0)]
    mock.coupon_error = RuntimeError("timeout")

    bill = make_verifier(mock, coupon_codes=["CODE"]).verify(cart)

    assert bill.to_pay == 183.0

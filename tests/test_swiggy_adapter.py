"""Tests for the Swiggy MCP -> cart_optimizer adapter.

Driven by real captured responses in tests/fixtures/ (see that README). The
adapter's job: turn Swiggy's menu/search payloads into a validated
cart_optimizer.Menu the optimizer can solve, prefixing Swiggy's bare numeric
ids into the optimizer's typed-id scheme and degrading safely on shapes that
have not been captured (variants, non-empty coupons).
"""

import json
from pathlib import Path

import pytest

from cart_optimizer.adapters.swiggy import (
    SwiggyAdapterError,
    parse_addon_groups,
    parse_coupons,
    parse_menu,
    swiggy_id,
)
from cart_optimizer.models import Menu, MenuError
from cart_optimizer.optimizer import best_cart
from cart_optimizer.models import PricingConfig, User

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def menu_response():
    return load("mcdonalds_menu.json")


@pytest.fixture
def search_response():
    return load("mcdonalds_search_addons.json")


# --- id prefixing -------------------------------------------------------------

def test_swiggy_ids_are_prefixed_into_typed_scheme(menu_response):
    menu = parse_menu(menu_response)
    ids = {item.id for item in menu.items}
    assert "itm_109348830" in ids  # McAloo Tikki, bare id 109348830
    assert all(item.id.startswith("itm_") for item in menu.items)
    assert all(v.id.startswith("var_") for item in menu.items for v in item.variants)


def test_swiggy_id_inverse_recovers_bare_id():
    assert swiggy_id("itm_109348830") == "109348830"
    assert swiggy_id("opt_100543620") == "100543620"
    assert swiggy_id("grp_164859071") == "164859071"


# --- item shape ---------------------------------------------------------------

def test_items_deduped_across_categories(menu_response):
    # McChicken Burger Combo (109348897) appears in two categories.
    menu = parse_menu(menu_response)
    combos = [item for item in menu.items if item.id == "itm_109348897"]
    assert len(combos) == 1


def test_float_price_rounded_to_rupee(menu_response):
    menu = parse_menu(menu_response)
    mcpuff = next(i for i in menu.items if i.id == "itm_143869468")
    assert mcpuff.variants[0].cost == 172  # 171.57 -> 172


def test_each_item_has_one_standard_variant(menu_response):
    menu = parse_menu(menu_response)
    mcaloo = next(i for i in menu.items if i.id == "itm_109348830")
    assert len(mcaloo.variants) == 1
    assert mcaloo.variants[0].cost == 79


def test_preference_derived_from_rating_and_bestseller(menu_response):
    menu = parse_menu(menu_response)
    mcaloo = next(i for i in menu.items if i.id == "itm_109348830")  # 4.4 + bestseller
    mcveggie = next(i for i in menu.items if i.id == "itm_109348838")  # 4.5, not bestseller
    assert mcaloo.preference == pytest.approx(0.93)   # 4.4/5 = 0.88, +0.05 bestseller
    assert mcveggie.preference == pytest.approx(0.90)  # 4.5/5 = 0.90
    assert 0.0 <= mcaloo.preference <= 1.0


def test_missing_rating_gets_default_preference():
    item = {"id": "1", "name": "x", "price": 50, "inStock": 1}
    menu = parse_menu({"restaurant": {"name": "r"}, "categories": [
        {"title": "c", "items": [item]}]})
    assert menu.items[0].preference == pytest.approx(0.6)


def test_out_of_stock_item_marked_unavailable():
    items = [
        {"id": "1", "name": "in", "price": 50, "inStock": 1, "rating": "4.0"},
        {"id": "2", "name": "out", "price": 50, "inStock": 0, "rating": "4.0"},
    ]
    menu = parse_menu({"restaurant": {"name": "r"}, "categories": [
        {"title": "c", "items": items}]})
    in_stock = next(i for i in menu.items if i.id == "itm_1")
    out = next(i for i in menu.items if i.id == "itm_2")
    assert in_stock.available and not out.available


# --- add-on groups ------------------------------------------------------------

def test_addon_groups_parsed_from_search(search_response):
    item = search_response["items"][0]
    groups = parse_addon_groups(item["addons"])
    assert len(groups) == 3
    drink = next(g for g in groups if g.id == "grp_164859071")
    assert drink.max_select == 1 and drink.min_select == 0
    assert any(o.id == "opt_95615645" and o.cost == 0 for o in drink.options)  # Fanta
    assert any(o.id == "opt_117291768" and o.cost == 110 for o in drink.options)  # Mango


def test_addons_attached_to_item_when_search_supplied(menu_response, search_response):
    menu = parse_menu(menu_response, search_responses=[search_response])
    combo = next(i for i in menu.items if i.id == "itm_109348897")
    assert len(combo.addons) == 3
    group_ids = {g.id for g in combo.addons}
    assert "grp_164859071" in group_ids


def test_addons_absent_when_no_search_supplied(menu_response):
    # hasAddons is true for the combo, but with no detail we degrade to no addons.
    menu = parse_menu(menu_response)
    combo = next(i for i in menu.items if i.id == "itm_109348897")
    assert combo.addons == ()


def test_addon_max_select_clamped_to_option_count():
    addons = [{
        "groupId": "9", "groupName": "g",
        "choices": [{"id": "1", "name": "only", "price": 10}],
        "maxAddons": 5,  # more than available options
    }]
    groups = parse_addon_groups(addons)
    assert groups[0].max_select == 1


# --- coupons ------------------------------------------------------------------

def test_empty_coupons_response_yields_no_coupons():
    assert parse_coupons({}) == ()
    assert parse_coupons({"bestCoupons": []}) == ()


def test_nonempty_coupon_shape_refuses_to_guess():
    # We have not captured a real non-empty coupon payload; the adapter must
    # not silently fabricate a mapping.
    with pytest.raises(SwiggyAdapterError):
        parse_coupons({"bestCoupons": [{"code": "FLAT100", "mystery": "shape"}]})


# --- variants guard -----------------------------------------------------------

def test_unsupported_variant_shape_is_rejected_not_guessed():
    item = {"id": "1", "name": "x", "price": 50, "inStock": 1, "hasVariants": True,
            "variantsV2": {"some": "uncaptured shape"}}
    with pytest.raises(SwiggyAdapterError):
        parse_menu({"restaurant": {"name": "r"}, "categories": [
            {"title": "c", "items": [item]}]})


# --- end to end ---------------------------------------------------------------

def test_parsed_menu_is_valid_and_solvable(menu_response, search_response):
    menu = parse_menu(menu_response, search_responses=[search_response])
    assert isinstance(menu, Menu)
    assert menu.restaurant == "McDonald's"
    config = PricingConfig(delivery_fee=29, platform_fee=5, gst_rate=0.05)
    result = best_cart(menu, User(), config, budget=300)
    assert result.cart.lines
    assert result.breakdown.total <= 300


def test_blank_menu_response_raises():
    with pytest.raises((SwiggyAdapterError, MenuError)):
        parse_menu({})

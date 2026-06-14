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


# --- variants (variantsV2 + legacy variations parsing) ------------------------

def test_malformed_variantsV2_falls_through_to_no_detail():
    # A variantsV2 that isn't the captured shape yields no usable variations,
    # so (with hasVariants) it raises the clear "no variant detail" error.
    item = {"id": "1", "name": "x", "price": 50, "inStock": 1, "hasVariants": True,
            "variantsV2": {"some": "uncaptured shape"}}
    with pytest.raises(SwiggyAdapterError):
        parse_menu({"restaurant": {"name": "r"}, "categories": [
            {"title": "c", "items": [item]}]})


@pytest.fixture
def bk_variantsv2_search():
    return load("burgerking_variantsv2_search.json")


def _bk_menu_response():
    return {
        "restaurant": {"name": "Burger King"},
        "categories": [{"title": "Burgers", "items": [
            {"id": "101196423", "name": "Crispy Chicken Burger", "price": 99,
             "inStock": 1, "rating": "4.4", "hasVariants": True},
        ]}],
    }


def test_parse_variantsV2_creates_one_variant_per_option(bk_variantsv2_search):
    menu = parse_menu(_bk_menu_response(), search_responses=[bk_variantsv2_search])
    burger = menu.items[0]
    assert burger.id == "itm_101196423"
    # 4 variations: Burger Only / Reg Meal / Shake Meal / 4in1
    assert len(burger.variants) == 4
    # variantsV2 prices are ABSOLUTE, not increments
    costs = sorted(v.cost for v in burger.variants)
    assert costs == [99, 218, 297, 298]


def test_variantsV2_id_is_marked_v2_and_encodes_group(bk_variantsv2_search):
    menu = parse_menu(_bk_menu_response(), search_responses=[bk_variantsv2_search])
    burger = menu.items[0]
    burger_only = next(v for v in burger.variants if v.cost == 99)
    assert burger_only.id.startswith("var_v2@")
    assert "75718135:220969284" in burger_only.id   # group : Burger Only variation


def test_variantsV2_cart_payload_uses_variantsV2_field(bk_variantsv2_search):
    from cart_optimizer.adapters.swiggy_session import cart_to_swiggy_items
    from cart_optimizer.models import Cart, ItemLine

    menu = parse_menu(_bk_menu_response(), search_responses=[bk_variantsv2_search])
    burger = menu.items[0]
    burger_only = next(v for v in burger.variants if v.cost == 99)
    entry = cart_to_swiggy_items(Cart((ItemLine(burger, burger_only),)))[0]

    assert entry["menu_item_id"] == "101196423"
    assert "variantsV2" in entry          # NOT "variants"
    assert "variants" not in entry
    pairs = {(p["group_id"], p["variation_id"]) for p in entry["variantsV2"]}
    assert ("75718135", "220969284") in pairs


def test_variantsV2_optimizer_picks_cheapest_for_value(bk_variantsv2_search):
    # The optimizer should be able to pick "Burger Only" (₹99) over the meals.
    menu = parse_menu(_bk_menu_response(), search_responses=[bk_variantsv2_search])
    result = best_cart(menu, User(), PricingConfig(delivery_fee=30, platform_fee=5, gst_rate=0.05), budget=200)
    assert result.cart.lines
    line = result.cart.lines[0]
    assert line.variant.cost == 99   # cheapest variation chosen


def test_legacy_variations_still_use_variants_field():
    # Regression: Starbucks-style legacy variations must still emit "variants".
    from cart_optimizer.adapters.swiggy_session import cart_to_swiggy_items
    from cart_optimizer.models import Cart, ItemLine
    menu = parse_menu(
        {"restaurant": {"name": "Starbucks"}, "categories": [{"title": "c", "items": [
            {"id": "97388430", "name": "Caffe Latte", "price": 295, "inStock": 1,
             "rating": "4.4", "hasVariants": True}]}]},
        search_responses=[load("starbucks_latte_search.json")],
    )
    latte = menu.items[0]
    entry = cart_to_swiggy_items(Cart((ItemLine(latte, latte.variants[0]),)))[0]
    assert "variants" in entry and "variantsV2" not in entry


def test_hasVariants_without_search_data_raises():
    item = {"id": "1", "name": "x", "price": 50, "inStock": 1, "hasVariants": True}
    with pytest.raises(SwiggyAdapterError, match="variant detail"):
        parse_menu({"restaurant": {"name": "r"}, "categories": [
            {"title": "c", "items": [item]}]})


@pytest.fixture
def starbucks_latte_search():
    return load("starbucks_latte_search.json")


def _starbucks_menu_response():
    return {
        "restaurant": {"name": "Starbucks Coffee"},
        "categories": [{"title": "Hot Coffees", "items": [
            {"id": "97388430", "name": "Caffe Latte", "price": 295,
             "inStock": 1, "rating": "4.4", "hasVariants": True},
        ]}],
    }


def test_parse_variant_item_creates_one_variant_per_size(starbucks_latte_search):
    menu = parse_menu(_starbucks_menu_response(), search_responses=[starbucks_latte_search])
    assert len(menu.items) == 1
    latte = menu.items[0]
    assert latte.id == "itm_97388430"
    # 4 in-stock size options → 4 Variants
    assert len(latte.variants) == 4
    costs = sorted(v.cost for v in latte.variants)
    assert costs == [295, 330, 370, 410]


def test_variant_names_match_size_labels(starbucks_latte_search):
    menu = parse_menu(_starbucks_menu_response(), search_responses=[starbucks_latte_search])
    latte = menu.items[0]
    names = {v.name for v in latte.variants}
    assert names == {"HOT SHORT", "HOT TALL", "HOT GRANDE", "HOT VENTI"}


def test_variant_id_encodes_group_and_variation(starbucks_latte_search):
    menu = parse_menu(_starbucks_menu_response(), search_responses=[starbucks_latte_search])
    latte = menu.items[0]
    tall = next(v for v in latte.variants if v.name == "HOT TALL")
    # ID must encode primary size group AND secondary milk group default
    assert tall.id.startswith("var_")
    assert "57291002:177629512" in tall.id   # size group : TALL variation
    assert "57291004:177629516" in tall.id   # milk group : Regular milk default


def test_secondary_milk_group_becomes_optional_addon(starbucks_latte_search):
    menu = parse_menu(_starbucks_menu_response(), search_responses=[starbucks_latte_search])
    latte = menu.items[0]
    # milk group (57291004) → AddonGroup id grp_var57291004
    milk_group = next((g for g in latte.addons if "57291004" in g.id), None)
    assert milk_group is not None
    assert milk_group.min_select == 0   # optional — default already priced in
    assert milk_group.max_select == 1
    opt_names = {o.name for o in milk_group.options}
    assert "Almond" in opt_names and "Soy" in opt_names


def test_regular_addons_are_also_parsed(starbucks_latte_search):
    menu = parse_menu(_starbucks_menu_response(), search_responses=[starbucks_latte_search])
    latte = menu.items[0]
    sauce_group = next((g for g in latte.addons if "Syrup" in g.name or "Sauce" in g.name), None)
    assert sauce_group is not None
    opt_names = {o.name for o in sauce_group.options}
    assert "Caramel Sauce" in opt_names


# --- cart_to_swiggy_items variant decoding ------------------------------------

def test_cart_to_swiggy_items_emits_variant_pairs(starbucks_latte_search):
    from cart_optimizer.adapters.swiggy_session import cart_to_swiggy_items
    from cart_optimizer.models import Cart, ItemLine

    menu = parse_menu(_starbucks_menu_response(), search_responses=[starbucks_latte_search])
    latte = menu.items[0]
    tall = next(v for v in latte.variants if v.name == "HOT TALL")
    cart = Cart((ItemLine(latte, tall),))
    items = cart_to_swiggy_items(cart)

    assert len(items) == 1
    entry = items[0]
    assert entry["menu_item_id"] == "97388430"
    assert "variants" in entry
    pairs = {(p["group_id"], p["variation_id"]) for p in entry["variants"]}
    assert ("57291002", "177629512") in pairs   # HOT TALL
    assert ("57291004", "177629516") in pairs   # Regular milk default


def test_cart_to_swiggy_items_no_variants_for_single_variant_items():
    from cart_optimizer.adapters.swiggy_session import cart_to_swiggy_items
    import json
    from pathlib import Path
    menu = parse_menu(json.loads((Path(__file__).parent / "fixtures" / "mcdonalds_menu.json").read_text()))
    item = menu.items[0]
    from cart_optimizer.models import Cart, ItemLine
    cart = Cart((ItemLine(item, item.variants[0]),))
    items = cart_to_swiggy_items(cart)
    assert "variants" not in items[0]   # synthetic variant → no group pairs needed


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

"""Tests for per-product order-line enumeration (the choice lists both
solvers consume)."""

import pytest

from cart_optimizer.choices import menu_choices, product_lines
from cart_optimizer.models import (
    AddonGroup,
    AddonOption,
    Combo,
    Item,
    Menu,
    MenuError,
    User,
    Variant,
)


def mk_option(suffix, cost, pref=0.1):
    return AddonOption(id=f"opt_{suffix}", name=suffix, cost=cost, preference=pref)


def mk_item(suffix, cost, pref=0.5, **kwargs):
    return Item(
        id=f"itm_{suffix}",
        name=suffix,
        preference=pref,
        variants=(Variant(id=f"var_{suffix}", name="Standard", cost=cost),),
        **kwargs,
    )


def test_plain_item_yields_one_line():
    lines = product_lines(mk_item("plain", 100))
    assert len(lines) == 1
    assert lines[0].cost == 100 and lines[0].quantity == 1


def test_variants_times_quantity():
    item = Item(
        id="itm_p",
        name="p",
        preference=0.5,
        variants=(
            Variant(id="var_a", name="A", cost=100),
            Variant(id="var_b", name="B", cost=150),
        ),
        max_quantity=2,
    )
    lines = product_lines(item)
    assert len(lines) == 4  # 2 variants x qty {1,2}
    assert sorted(line.cost for line in lines) == [100, 150, 200, 300]


def test_addon_group_combinations_respect_min_max():
    a, b = mk_option("a", 10), mk_option("b", 20)
    item = mk_item(
        "x", 100,
        addons=(
            AddonGroup(id="grp_g", name="g", min_select=1, max_select=2, options=(a, b)),
        ),
    )
    lines = product_lines(item)
    # selections: {a}, {b}, {a,b} — never the empty set (min 1)
    assert sorted(line.cost for line in lines) == [110, 120, 130]


def test_two_groups_cross_product():
    dip = AddonGroup(
        id="grp_dip", name="dip", min_select=1, max_select=1,
        options=(mk_option("d1", 0), mk_option("d2", 20)),
    )
    top = AddonGroup(
        id="grp_top", name="top", min_select=0, max_select=1,
        options=(mk_option("t1", 30),),
    )
    item = mk_item("x", 100, addons=(dip, top))
    lines = product_lines(item)
    assert len(lines) == 4  # dips {d1,d2} x toppings {none, t1}
    assert all(
        any(option.id.startswith("opt_d") for option in line.addons) for line in lines
    )


def test_combo_lines_quantity():
    combo = Combo(id="cmb_m", name="m", cost=200, preference=0.9, max_quantity=2)
    lines = product_lines(combo)
    assert [line.cost for line in lines] == [200, 400]


def test_explosion_guard():
    options = tuple(mk_option(f"o{i}", 10) for i in range(16))
    group = AddonGroup(id="grp_big", name="big", min_select=0, max_select=8, options=options)
    item = mk_item("x", 100, addons=(group,))
    with pytest.raises(MenuError):
        product_lines(item)


def test_menu_choices_skips_exploding_product_not_whole_menu():
    # One pathological item must NOT abort the whole menu's choices.
    big_opts = tuple(mk_option(f"o{i}", 10) for i in range(16))
    big_group = AddonGroup(id="grp_big", name="big", min_select=0, max_select=8, options=big_opts)
    boom = mk_item("boom", 100, addons=(big_group,))
    fine = mk_item("fine", 80)
    menu = Menu(restaurant="r", items=(boom, fine))
    with pytest.warns(UserWarning):
        choices = menu_choices(menu, User())
    # the good item survives; the exploding one is skipped
    ids = {lines[0].product_id for lines in choices}
    assert "itm_fine" in ids
    assert "itm_boom" not in ids


def test_menu_choices_filters_and_groups_per_product():
    menu = Menu(
        restaurant="r",
        items=(
            mk_item("ok", 100),
            mk_item("gone", 100, available=False),
            mk_item("breakfast", 100, time_window=("07:00", "11:00")),
        ),
        combos=(
            Combo(id="cmb_open", name="open", cost=200, preference=0.8),
            Combo(
                id="cmb_members", name="m", cost=200, preference=0.8,
                applicability="user.member == true",
            ),
        ),
    )
    products = menu_choices(menu, User(), now="13:00")
    assert [lines[0].product_id for lines in products] == ["itm_ok", "cmb_open"]
    products = menu_choices(menu, User(member=True), now="09:00")
    assert [lines[0].product_id for lines in products] == [
        "itm_ok",
        "itm_breakfast",
        "cmb_open",
        "cmb_members",
    ]

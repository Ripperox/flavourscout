"""Enumerate every valid order line for each product (item or combo).

Both solvers consume these per-product choice lists and pick at most one
line per product, which keeps the optimizer a plain multiple-choice
knapsack even with variants × addon selections × quantities. Enumeration
correctness is unit-tested here once instead of being re-derived inside
each solver (the equivalence suite then verifies *search*, the one thing
the solvers don't share).
"""

from __future__ import annotations

import datetime as dt
import warnings
from itertools import combinations, product

from .models import Combo, ComboLine, Item, ItemLine, Menu, MenuError, User

__all__ = ["MAX_LINES_PER_PRODUCT", "menu_choices", "product_lines"]

# A single item with this many configurations would make even the DP crawl;
# fail loud instead of slow. Real menus sit far below (a few groups with a
# handful of options each).
MAX_LINES_PER_PRODUCT = 10_000


def product_lines(product_: Item | Combo) -> list[ItemLine | ComboLine]:
    if isinstance(product_, Combo):
        return [
            ComboLine(combo=product_, quantity=quantity)
            for quantity in range(1, product_.max_quantity + 1)
        ]
    return _item_lines(product_)


def _item_lines(item: Item) -> list[ItemLine]:
    group_selections = []
    for group in item.addons:
        selections = [
            selection
            for size in range(group.min_select, group.max_select + 1)
            for selection in combinations(group.options, size)
        ]
        group_selections.append(selections)
    lines: list[ItemLine] = []
    for variant in item.variants:
        for picks in product(*group_selections):
            addons = tuple(option for selection in picks for option in selection)
            for quantity in range(1, item.max_quantity + 1):
                lines.append(
                    ItemLine(item=item, variant=variant, addons=addons, quantity=quantity)
                )
                if len(lines) > MAX_LINES_PER_PRODUCT:
                    raise MenuError(
                        f"item {item.id}: more than {MAX_LINES_PER_PRODUCT} "
                        "configurations; trim addon groups or quantity cap"
                    )
    return lines


def menu_choices(
    menu: Menu, user: User, now: dt.time | str | None = None
) -> list[list[ItemLine | ComboLine]]:
    """Choice lists for every orderable product, in menu order (items first,
    then combos). Each inner list shares one product_id.

    A single product whose configuration space explodes past
    MAX_LINES_PER_PRODUCT is SKIPPED (with a warning) rather than aborting the
    whole menu — one pathological item (huge addon matrix) shouldn't sink the
    entire optimization. Both solvers call this, so they stay consistent."""
    products: list[list[ItemLine | ComboLine]] = []
    for item in menu.orderable_items(now):
        try:
            products.append(product_lines(item))
        except MenuError as exc:
            warnings.warn(f"skipped product in choices: {exc}", stacklevel=2)
    for combo in menu.orderable_combos(user):
        try:
            products.append(product_lines(combo))
        except MenuError as exc:
            warnings.warn(f"skipped product in choices: {exc}", stacklevel=2)
    return products

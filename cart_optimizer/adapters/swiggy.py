"""Adapter: Swiggy MCP Food responses -> cart_optimizer.Menu.

Shaped against real captured payloads (tests/fixtures/, McDonald's Mumbai).
What the real data dictated:

* Swiggy item/group/choice ids are bare numbers; we prefix them into the
  optimizer's typed-id scheme (``itm_``/``var_``/``grp_``/``opt_``) and keep
  the inverse (``swiggy_id``) for later cart-building back on Swiggy.
* ``get_restaurant_menu`` is a *compact* per-category listing (no add-on/variant
  detail) and the same item id can appear in several categories, so we dedupe.
  Add-on detail comes from ``search_menu`` responses, merged in by item id.
* Prices are floats (paise); the DP needs integer spend levels and this model
  only *ranks* candidates (Swiggy returns the authoritative bill), so prices
  are rounded to whole rupees.
* Swiggy has no per-item "taste" score exposed here, so preference is derived
  from the displayed rating (rating/5, with a small bestseller bump). A real
  deployment would swap in a personalization score.

Shapes we have NOT captured live are refused rather than guessed:
* item ``variantsV2``/``variations`` (this restaurant models sizes as separate
  items) -> ``SwiggyAdapterError``;
* a non-empty ``fetch_food_coupons`` payload -> ``SwiggyAdapterError``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..models import (
    AddonGroup,
    AddonOption,
    Item,
    Menu,
    MenuError,
    Variant,
)

__all__ = [
    "SwiggyAdapterError",
    "parse_menu",
    "parse_addon_groups",
    "parse_coupons",
    "swiggy_id",
]

DEFAULT_PREFERENCE = 0.6
BESTSELLER_BONUS = 0.05


class SwiggyAdapterError(ValueError):
    """A Swiggy payload could not be adapted (malformed, or an uncaptured shape)."""


def swiggy_id(typed_id: str) -> str:
    """Recover the bare Swiggy id from a prefixed optimizer id.

    ``itm_109348830`` -> ``109348830``. Used when translating an optimized
    cart back into Swiggy cart-build calls.
    """
    _, _, rest = typed_id.partition("_")
    return rest or typed_id


def _round_price(value: Any, what: str) -> int:
    try:
        rupees = round(float(value))
    except (TypeError, ValueError):
        raise SwiggyAdapterError(f"{what}: price {value!r} is not a number") from None
    if rupees < 0:
        raise SwiggyAdapterError(f"{what}: negative price {value!r}")
    return rupees


def _preference(item: Mapping[str, Any]) -> float:
    rating = item.get("rating")
    if rating in (None, ""):
        score = DEFAULT_PREFERENCE
    else:
        try:
            score = float(rating) / 5.0
        except (TypeError, ValueError):
            score = DEFAULT_PREFERENCE
    if item.get("isBestseller"):
        score += BESTSELLER_BONUS
    return round(max(0.0, min(1.0, score)), 2)


def parse_addon_groups(addons: Iterable[Mapping[str, Any]]) -> tuple[AddonGroup, ...]:
    """Map Swiggy add-on groups to AddonGroup.

    Swiggy gives ``maxAddons`` (our max_select) but no explicit minimum here;
    the included default is already priced into the item (the ₹0 choices), so
    every group is treated as optional (min_select=0). Empty groups are
    skipped. Raises SwiggyAdapterError on a malformed group.
    """
    groups: list[AddonGroup] = []
    for group in addons:
        choices = group.get("choices") or []
        options = []
        seen: set[str] = set()
        for choice in choices:
            try:
                raw_id = str(choice["id"])
            except (KeyError, TypeError):
                raise SwiggyAdapterError(f"add-on choice missing id: {choice!r}") from None
            if raw_id in seen:
                continue
            seen.add(raw_id)
            options.append(
                AddonOption(
                    id=f"opt_{raw_id}",
                    name=str(choice.get("name", raw_id)),
                    cost=_round_price(choice.get("price", 0), f"option {raw_id}"),
                    preference=0.0,  # no taste signal for add-ons
                )
            )
        if not options:
            continue
        try:
            raw_group_id = str(group["groupId"])
        except (KeyError, TypeError):
            raise SwiggyAdapterError(f"add-on group missing groupId: {group!r}") from None
        max_select = group.get("maxAddons", 1)
        if not isinstance(max_select, int) or isinstance(max_select, bool) or max_select < 1:
            max_select = 1
        max_select = min(max_select, len(options))
        try:
            groups.append(
                AddonGroup(
                    id=f"grp_{raw_group_id}",
                    name=str(group.get("groupName", raw_group_id)),
                    min_select=0,
                    max_select=max_select,
                    options=tuple(options),
                )
            )
        except MenuError as exc:
            raise SwiggyAdapterError(f"add-on group {raw_group_id}: {exc}") from exc
    return tuple(groups)


def _addons_by_item_id(
    search_responses: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    detail: dict[str, list[Mapping[str, Any]]] = {}
    for response in search_responses:
        for item in response.get("items", []):
            item_id = item.get("menu_item_id") or item.get("id")
            if item_id is not None and item.get("addons"):
                detail[str(item_id)] = item["addons"]
    return detail


def _parse_item(
    raw: Mapping[str, Any], addons_by_id: Mapping[str, list[Mapping[str, Any]]]
) -> Item:
    try:
        raw_id = str(raw["id"])
    except (KeyError, TypeError):
        raise SwiggyAdapterError(f"menu item missing id: {raw!r}") from None
    if raw.get("hasVariants") or raw.get("variantsV2") or raw.get("variations"):
        raise SwiggyAdapterError(
            f"item {raw_id}: variant shape (variantsV2/variations) not yet "
            "captured from live data; refusing to guess it"
        )
    cost = _round_price(raw.get("price"), f"item {raw_id}")
    addon_detail = addons_by_id.get(raw_id, [])
    try:
        return Item(
            id=f"itm_{raw_id}",
            name=str(raw.get("name", raw_id)),
            preference=_preference(raw),
            variants=(Variant(id=f"var_{raw_id}", name="Standard", cost=cost),),
            available=bool(raw.get("inStock", 1)),
            addons=parse_addon_groups(addon_detail),
        )
    except MenuError as exc:
        raise SwiggyAdapterError(f"item {raw_id}: {exc}") from exc


def parse_menu(
    menu_response: Mapping[str, Any],
    search_responses: Iterable[Mapping[str, Any]] = (),
) -> Menu:
    """Build a Menu from a ``get_restaurant_menu`` response, merging add-on
    detail from any ``search_menu`` responses. Items are deduped by id across
    categories (first occurrence wins)."""
    if not isinstance(menu_response, Mapping):
        raise SwiggyAdapterError("menu response must be a mapping")
    categories = menu_response.get("categories")
    if not categories:
        raise SwiggyAdapterError("menu response has no categories")
    restaurant = (menu_response.get("restaurant") or {}).get("name", "unknown")
    addons_by_id = _addons_by_item_id(search_responses)

    items: list[Item] = []
    seen: set[str] = set()
    for category in categories:
        for raw in category.get("items", []):
            raw_id = raw.get("id")
            if raw_id is None or str(raw_id) in seen:
                continue
            seen.add(str(raw_id))
            items.append(_parse_item(raw, addons_by_id))
    if not items:
        raise SwiggyAdapterError("menu response contained no items")
    try:
        return Menu(restaurant=str(restaurant), items=tuple(items), coupons=())
    except MenuError as exc:
        raise SwiggyAdapterError(str(exc)) from exc


def parse_coupons(coupons_response: Mapping[str, Any]) -> tuple:
    """Map a ``fetch_food_coupons`` response to Coupons.

    The live capture returned ``{}`` (no applicable coupons), so the non-empty
    shape is not yet known. We handle the empty case and refuse to guess the
    rest, rather than silently mis-modelling discounts.
    """
    if not coupons_response:
        return ()
    for key in ("bestCoupons", "moreOffers", "coupons", "paymentOffers"):
        if coupons_response.get(key):
            raise SwiggyAdapterError(
                "non-empty coupon payload not yet supported: capture a real "
                f"fetch_food_coupons response (key {key!r}) and shape parse_coupons "
                "against it before relying on coupon optimization with live data"
            )
    return ()

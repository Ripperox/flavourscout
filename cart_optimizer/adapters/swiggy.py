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

import warnings
from dataclasses import dataclass
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
    "CartBill",
    "parse_menu",
    "parse_addon_groups",
    "parse_coupons",
    "parse_cart_bill",
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


def _variations_by_item_id(
    search_responses: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Extract variation arrays from search_menu responses, keyed by item id."""
    detail: dict[str, list[Mapping[str, Any]]] = {}
    for response in search_responses:
        for item in response.get("items", []):
            item_id = item.get("menu_item_id") or item.get("id")
            if item_id is not None and item.get("variations"):
                detail[str(item_id)] = item["variations"]
    return detail


def _variantsv2_by_item_id(
    search_responses: Iterable[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    """Extract variantsV2 arrays from search_menu responses, keyed by item id."""
    detail: dict[str, list[Mapping[str, Any]]] = {}
    for response in search_responses:
        for item in response.get("items", []):
            item_id = item.get("menu_item_id") or item.get("id")
            if item_id is not None and item.get("variantsV2"):
                detail[str(item_id)] = item["variantsV2"]
    return detail


def _flatten_variantsv2(variantsv2: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Flatten Swiggy's nested ``variantsV2`` into the flat ``variations`` shape.

    variantsV2 groups variations under each group:
        [{"groupId": G, "name": ..., "variations": [{"name","price","id",...}]}]
    Legacy ``variations`` is flat with ``groupId`` on each entry. We normalize to
    the flat form (copying the parent groupId onto each variation) so a single
    parser handles both. The price field name also differs across captures
    (``price`` is paise-or-rupee depending on endpoint) — we keep it as-is and
    let the caller round."""
    flat: list[dict[str, Any]] = []
    for group in variantsv2:
        if not isinstance(group, Mapping):
            continue  # malformed / uncaptured shape — skip, caller handles emptiness
        gid = group.get("groupId") or group.get("group_id")
        for v in group.get("variations", []):
            if not isinstance(v, Mapping):
                continue
            entry = dict(v)
            entry["groupId"] = gid
            flat.append(entry)
    return flat


def _parse_variation_groups(
    variations: list[Mapping[str, Any]],
    base_cost: int,
    encoding: str = "v1",
) -> tuple[tuple[Variant, ...], tuple[AddonGroup, ...]]:
    """Parse Swiggy variations into Variants (primary group) + AddonGroups (secondary).

    Starbucks-style items have multiple variation groups (size + milk type).
    The FIRST group encountered becomes real Variants; subsequent groups become
    optional AddonGroups (min_select=0 — the default is already priced in).

    ``encoding`` is ``"v1"`` for legacy ``variations`` (cart-built via the
    ``variants`` field) or ``"v2"`` for ``variantsV2`` (cart-built via the
    ``variantsV2`` field). It is stamped into the variant id so
    cart_to_swiggy_items emits the right field:
        v1 → var_{g1}:{v1}|{g2_default}:{v2_default}|...
        v2 → var_v2@{g1}:{v1}|...
    """
    id_prefix = "v2@" if encoding == "v2" else ""
    # Group variations by groupId, preserving first-appearance order.
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for v in variations:
        gid = str(v.get("groupId", ""))
        if not gid:
            continue
        if gid not in groups:
            groups[gid] = []
        groups[gid].append(v)

    if not groups:
        raise SwiggyAdapterError("item has hasVariants=true but no variation groups")

    group_ids = list(groups.keys())
    primary_gid = group_ids[0]
    secondary_gids = group_ids[1:]

    # Default for each secondary group (variation with default=1, else first in-stock).
    secondary_defaults: list[tuple[str, str]] = []
    for gid in secondary_gids:
        opts = groups[gid]
        default = (
            next((v for v in opts if v.get("default") == 1), None)
            or next((v for v in opts if v.get("inStock", 1)), None)
            or opts[0]
        )
        secondary_defaults.append((gid, str(default["id"])))

    # One Variant per in-stock primary option; ID encodes all group selections.
    variants: list[Variant] = []
    for v in groups[primary_gid]:
        if not v.get("inStock", 1):
            continue
        vid = str(v.get("id", ""))
        if not vid:
            continue
        encoded = f"{id_prefix}{primary_gid}:{vid}"
        for sec_gid, sec_vid in secondary_defaults:
            encoded += f"|{sec_gid}:{sec_vid}"
        price_val = round(float(v.get("price") or 0))
        # v1 (legacy variations): price is an INCREMENT over base (Starbucks TALL = +35).
        # v2 (variantsV2): price is the ABSOLUTE item price (BK "Burger Only" = 99).
        if encoding == "v2":
            cost = price_val if price_val > 0 else base_cost
        else:
            cost = base_cost + price_val
        variants.append(Variant(
            id=f"var_{encoded}",
            name=str(v.get("name", vid)),
            cost=cost,
        ))

    if not variants:
        raise SwiggyAdapterError("all primary variant options are out of stock")

    # Secondary variation groups as optional AddonGroups (min_select=0).
    addon_groups: list[AddonGroup] = []
    for gid in secondary_gids:
        options = []
        for v in groups[gid]:
            opt_id = str(v.get("id", ""))
            if not opt_id:
                continue
            incremental = round(float(v.get("price") or 0))
            try:
                options.append(AddonOption(
                    id=f"opt_{opt_id}",
                    name=str(v.get("name", opt_id)),
                    cost=incremental,
                    preference=0.0,
                ))
            except MenuError:
                pass
        if len(options) >= 1:
            try:
                addon_groups.append(AddonGroup(
                    id=f"grp_var{gid}",
                    name=str(groups[gid][0].get("name", gid)).split()[0] + " options",
                    min_select=0,
                    max_select=1,
                    options=tuple(options),
                ))
            except MenuError:
                pass

    return tuple(variants), tuple(addon_groups)


def _parse_item(
    raw: Mapping[str, Any],
    addons_by_id: Mapping[str, list[Mapping[str, Any]]],
    variations_by_id: Mapping[str, list[Mapping[str, Any]]],
    variantsv2_by_id: Mapping[str, list[Mapping[str, Any]]] = {},
) -> Item:
    try:
        raw_id = str(raw["id"])
    except (KeyError, TypeError):
        raise SwiggyAdapterError(f"menu item missing id: {raw!r}") from None

    base_cost = _round_price(raw.get("price"), f"item {raw_id}")
    addon_detail = addons_by_id.get(raw_id, [])

    # Resolve variant detail: prefer variantsV2 (BK/newer), else legacy variations
    # (Starbucks). Detail comes from search_menu (merged in by id) or, for tests,
    # inline on the item itself.
    v2_detail = list(variantsv2_by_id.get(raw_id) or raw.get("variantsV2") or [])
    v1_detail = list(variations_by_id.get(raw_id) or raw.get("variations") or [])

    if v2_detail or v1_detail or raw.get("hasVariants"):
        if v2_detail:
            flat = _flatten_variantsv2(v2_detail)
            encoding = "v2"
        elif v1_detail:
            flat = v1_detail
            encoding = "v1"
        else:
            raise SwiggyAdapterError(
                f"item {raw_id}: hasVariants=true but no variant detail found — "
                "pass a search_menu response that includes this item to parse_menu()"
            )
        try:
            parsed_variants, variant_addon_groups = _parse_variation_groups(
                flat, base_cost, encoding=encoding
            )
        except (SwiggyAdapterError, MenuError) as exc:
            raise SwiggyAdapterError(f"item {raw_id}: {exc}") from exc
        regular_addons = parse_addon_groups(addon_detail)
        all_addons = variant_addon_groups + regular_addons
        try:
            return Item(
                id=f"itm_{raw_id}",
                name=str(raw.get("name", raw_id)),
                preference=_preference(raw),
                variants=parsed_variants,
                available=bool(raw.get("inStock", 1)),
                addons=all_addons,
            )
        except MenuError as exc:
            raise SwiggyAdapterError(f"item {raw_id}: {exc}") from exc

    # Standard single-variant item (e.g. McDonald's).
    try:
        return Item(
            id=f"itm_{raw_id}",
            name=str(raw.get("name", raw_id)),
            preference=_preference(raw),
            variants=(Variant(id=f"var_{raw_id}", name="Standard", cost=base_cost),),
            available=bool(raw.get("inStock", 1)),
            addons=parse_addon_groups(addon_detail),
        )
    except MenuError as exc:
        raise SwiggyAdapterError(f"item {raw_id}: {exc}") from exc


def parse_menu(
    menu_response: Mapping[str, Any],
    search_responses: Iterable[Mapping[str, Any]] = (),
    skip_unparseable: bool = False,
) -> Menu:
    """Build a Menu from a ``get_restaurant_menu`` response, merging add-on
    and variation detail from any ``search_menu`` responses.
    Items are deduped by id across categories (first occurrence wins).

    ``skip_unparseable=True`` skips items the adapter can't yet handle (e.g. a
    variant item with no captured ``search_menu`` detail) instead of raising,
    emitting a ``warnings.warn`` per skip so nothing is silently dropped. Used
    by the live runner, which can't guarantee detail for every menu item."""
    if not isinstance(menu_response, Mapping):
        raise SwiggyAdapterError("menu response must be a mapping")
    categories = menu_response.get("categories")
    if not categories:
        raise SwiggyAdapterError("menu response has no categories")
    restaurant = (menu_response.get("restaurant") or {}).get("name", "unknown")
    search_list = list(search_responses)
    addons_by_id = _addons_by_item_id(search_list)
    variations_by_id = _variations_by_item_id(search_list)
    variantsv2_by_id = _variantsv2_by_item_id(search_list)

    items: list[Item] = []
    seen: set[str] = set()
    skipped = 0
    for category in categories:
        for raw in category.get("items", []):
            raw_id = raw.get("id")
            if raw_id is None or str(raw_id) in seen:
                continue
            seen.add(str(raw_id))
            try:
                items.append(_parse_item(raw, addons_by_id, variations_by_id, variantsv2_by_id))
            except SwiggyAdapterError as exc:
                if not skip_unparseable:
                    raise
                skipped += 1
                warnings.warn(f"skipped menu item {raw_id}: {exc}", stacklevel=2)
    if not items:
        raise SwiggyAdapterError(
            "menu response contained no parseable items"
            + (f" ({skipped} skipped)" if skipped else "")
        )
    try:
        return Menu(restaurant=str(restaurant), items=tuple(items), coupons=())
    except MenuError as exc:
        raise SwiggyAdapterError(str(exc)) from exc


@dataclass(frozen=True)
class CartBill:
    """Swiggy's authoritative bill for a built cart (read from get_food_cart).

    ``to_pay`` is Swiggy's number and must be shown as-is — never recomputed
    from our approximate fee model. ``coupon_code`` is None unless a coupon is
    *actually* applied (a non-zero discount).

    NOTE (verified live 2026-06-13): a cart's ``offers.coupon_applied`` with
    ``coupon_discount == 0`` is only a SUGGESTION — Swiggy does not auto-apply
    it. The discount appears only after an explicit ``apply_food_coupon`` call,
    and that can be refused by item restrictions. So a populated CartBill here
    reflects a coupon that was explicitly applied, not an automatic best.
    """

    item_total: float
    coupon_code: str | None
    coupon_discount: float
    free_delivery: bool
    delivery_charge: float
    taxes_and_charges: float
    to_pay: float
    item_count: int
    cod_available: bool


def parse_cart_bill(cart_response: Mapping[str, Any]) -> CartBill:
    """Read the authoritative bill + applied coupon from a get_food_cart response.

    A ``coupon_applied`` with ``coupon_discount == 0`` means Swiggy merely
    *suggested* a coupon (not applied), so it is reported as no coupon.
    """
    if not isinstance(cart_response, Mapping):
        raise SwiggyAdapterError("cart response must be a mapping")
    data = cart_response.get("data")
    if not isinstance(data, Mapping):
        raise SwiggyAdapterError("cart response has no data block")
    pricing = data.get("pricing")
    if not isinstance(pricing, Mapping):
        raise SwiggyAdapterError("cart response has no pricing block")
    offers = data.get("offers") or {}

    discount = offers.get("coupon_discount", 0) or 0
    coupon_code = offers.get("coupon_applied")
    if not discount or not coupon_code:  # suggested-but-not-applied, or none
        coupon_code = None
        discount = 0

    payment_methods = cart_response.get("availablePaymentMethods") or []
    cod_available = any("cash" in str(m).lower() for m in payment_methods)

    return CartBill(
        item_total=float(pricing.get("item_total", 0)),
        coupon_code=coupon_code,
        coupon_discount=float(discount),
        free_delivery=bool(offers.get("free_delivery_applied", False)),
        delivery_charge=float(pricing.get("delivery_charge", 0)),
        taxes_and_charges=float(pricing.get("taxes_and_charges", 0)),
        to_pay=float(pricing["to_pay"]) if "to_pay" in pricing else _missing_to_pay(),
        item_count=int(data.get("item_count", len(data.get("items", [])))),
        cod_available=cod_available,
    )


def _missing_to_pay() -> float:
    raise SwiggyAdapterError("cart pricing has no to_pay (authoritative total)")


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

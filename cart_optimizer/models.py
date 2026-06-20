"""Domain model for the cart optimizer.

Normalized schema (see ``Menu.from_dict``): ids are strongly typed by prefix —
``itm_`` items, ``var_`` variants, ``off_`` offers/coupons. v1 models items
with choose-one variants plus three coupon shapes (flat / percent /
free_delivery, optionally scoped to a set of items via ``applies_to``).
Combos, add-ons and quantities > 1 are out of scope for v1.

Coupon ``query`` strings are validated at construction time against the
vocabulary the optimizer can reason about at spend level (ALLOWED_QUERY_NAMES);
anything else — e.g. ``item_count`` — would need an extra DP dimension and is
rejected up front rather than silently mis-optimized.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .safe_eval import UnsafeExpressionError, safe_eval, validate_expression

ITEM_PREFIX = "itm_"
VARIANT_PREFIX = "var_"
OFFER_PREFIX = "off_"
COMBO_PREFIX = "cmb_"
GROUP_PREFIX = "grp_"
OPTION_PREFIX = "opt_"

COUPON_KINDS = ("flat", "percent", "free_delivery")

# A single item's taste score is in [0, 1]. Bundles (meals/combos) may exceed 1
# because they're worth the SUM of their parts (main + side + drink); the cap is
# raised for Item/Combo so a meal can out-value an equivalent à-la-carte set.
MAX_PREFERENCE = 5.0

ALLOWED_QUERY_NAMES = frozenset({"subtotal", "select_subtotal", "user"})
ALLOWED_USER_FIELDS = frozenset({"member", "first_order"})


class MenuError(ValueError):
    """Raised when menu/offer/cart data is malformed."""


def _require_id(value: Any, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or len(value) <= len(prefix)
    ):
        raise MenuError(f"id {value!r} must be a string starting with {prefix!r}")
    return value


def _require_number(value: Any, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MenuError(f"{what} must be a number, got {value!r}")
    return float(value)


def _require_product_id(value: Any) -> str:
    """Items and combos share the product namespace (carts, coupon scopes)."""
    if isinstance(value, str) and value.startswith(COMBO_PREFIX):
        return _require_id(value, COMBO_PREFIX)
    return _require_id(value, ITEM_PREFIX)


def _require_quantity_cap(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MenuError(f"{what} must be an int >= 1, got {value!r}")
    return value


def _parse_hhmm(value: Any) -> dt.time:
    try:
        hours, minutes = str(value).split(":")
        return dt.time(int(hours), int(minutes))
    except (ValueError, TypeError) as exc:
        raise MenuError(f"bad time {value!r}, expected 'HH:MM'") from exc


@dataclass(frozen=True)
class Variant:
    id: str
    name: str
    cost: int  # whole rupees

    def __post_init__(self) -> None:
        _require_id(self.id, VARIANT_PREFIX)
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost < 0:
            raise MenuError(
                f"variant {self.id}: cost must be a non-negative int "
                f"(whole rupees), got {self.cost!r}"
            )


@dataclass(frozen=True)
class AddonOption:
    id: str
    name: str
    cost: int  # whole rupees
    preference: float  # [0, 1], adds to the line's preference when selected

    def __post_init__(self) -> None:
        _require_id(self.id, OPTION_PREFIX)
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost < 0:
            raise MenuError(
                f"option {self.id}: cost must be a non-negative int, got {self.cost!r}"
            )
        pref = _require_number(self.preference, f"option {self.id}: preference")
        if not 0.0 <= pref <= 1.0:
            raise MenuError(f"option {self.id}: preference must be in [0, 1]")


@dataclass(frozen=True)
class AddonGroup:
    id: str
    name: str
    min_select: int  # picks required from this group (0 = optional)
    max_select: int
    options: tuple[AddonOption, ...]

    def __post_init__(self) -> None:
        _require_id(self.id, GROUP_PREFIX)
        object.__setattr__(self, "options", tuple(self.options))
        if not self.options:
            raise MenuError(f"group {self.id}: needs at least one option")
        for option in self.options:
            if not isinstance(option, AddonOption):
                raise MenuError(f"group {self.id}: options must be AddonOption instances")
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise MenuError(f"group {self.id}: duplicate option ids")
        for bound in (self.min_select, self.max_select):
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise MenuError(f"group {self.id}: min/max must be ints")
        if not 0 <= self.min_select <= self.max_select:
            raise MenuError(f"group {self.id}: requires 0 <= min <= max")
        if self.max_select < 1 or self.max_select > len(self.options):
            raise MenuError(
                f"group {self.id}: max must be in [1, {len(self.options)}]"
            )


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    preference: float  # taste/popularity score in [0, 1]
    variants: tuple[Variant, ...]
    available: bool = True
    time_window: tuple[str, str] | None = None
    addons: tuple[AddonGroup, ...] = ()
    max_quantity: int = 1  # ordering several of one item is opt-in
    is_veg: bool | None = None  # True=veg, False=non-veg, None=unknown

    def __post_init__(self) -> None:
        _require_id(self.id, ITEM_PREFIX)
        pref = _require_number(self.preference, f"item {self.id}: preference")
        if not 0.0 <= pref <= MAX_PREFERENCE:
            raise MenuError(f"item {self.id}: preference must be in [0, {MAX_PREFERENCE}], got {pref}")
        if self.is_veg is not None and not isinstance(self.is_veg, bool):
            raise MenuError(f"item {self.id}: is_veg must be bool or None")
        object.__setattr__(self, "variants", tuple(self.variants))
        if not self.variants:
            raise MenuError(f"item {self.id}: needs at least one variant")
        for variant in self.variants:
            if not isinstance(variant, Variant):
                raise MenuError(f"item {self.id}: variants must be Variant instances")
        ids = [v.id for v in self.variants]
        if len(ids) != len(set(ids)):
            raise MenuError(f"item {self.id}: duplicate variant ids")
        object.__setattr__(self, "addons", tuple(self.addons))
        for group in self.addons:
            if not isinstance(group, AddonGroup):
                raise MenuError(f"item {self.id}: addons must be AddonGroup instances")
        group_ids = [group.id for group in self.addons]
        if len(group_ids) != len(set(group_ids)):
            raise MenuError(f"item {self.id}: duplicate addon group ids")
        option_ids = [
            option.id for group in self.addons for option in group.options
        ]
        if len(option_ids) != len(set(option_ids)):
            raise MenuError(f"item {self.id}: option ids must be unique across groups")
        _require_quantity_cap(self.max_quantity, f"item {self.id}: max_quantity")
        if self.time_window is not None:
            window = tuple(self.time_window)
            if len(window) != 2:
                raise MenuError(f"item {self.id}: time_window must be [start, end]")
            for value in window:
                _parse_hhmm(value)
            object.__setattr__(self, "time_window", window)

    def is_orderable(self, now: dt.time | str | None = None) -> bool:
        """Available, and (if a clock is given) inside the time window.

        ``now=None`` means "don't filter by time". Windows may wrap midnight
        (e.g. 22:00-02:00).
        """
        if not self.available:
            return False
        if self.time_window is None or now is None:
            return self.available
        if isinstance(now, str):
            now = _parse_hhmm(now)
        start, end = (_parse_hhmm(t) for t in self.time_window)
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end


@dataclass(frozen=True)
class Coupon:
    id: str
    kind: str  # one of COUPON_KINDS
    value: float = 0.0  # flat: rupees off; percent: 0-100
    cap: float | None = None  # max rupees off, percent only
    query: str | None = None  # eligibility condition; None/empty = always
    applies_to: frozenset[str] | None = None  # item ids; None = whole cart
    description: str = ""

    def __post_init__(self) -> None:
        _require_id(self.id, OFFER_PREFIX)
        if self.kind not in COUPON_KINDS:
            raise MenuError(
                f"coupon {self.id}: kind must be one of {COUPON_KINDS}, got {self.kind!r}"
            )
        value = _require_number(self.value, f"coupon {self.id}: value")
        if self.kind == "flat":
            if value <= 0:
                raise MenuError(f"coupon {self.id}: flat coupon needs value > 0")
            if self.cap is not None:
                raise MenuError(f"coupon {self.id}: cap is only for percent coupons")
        elif self.kind == "percent":
            if not 0 < value <= 100:
                raise MenuError(f"coupon {self.id}: percent must be in (0, 100]")
            if self.cap is not None and _require_number(
                self.cap, f"coupon {self.id}: cap"
            ) <= 0:
                raise MenuError(f"coupon {self.id}: cap must be > 0")
        else:  # free_delivery
            if value:
                raise MenuError(f"coupon {self.id}: free_delivery carries no value")
            if self.cap is not None:
                raise MenuError(f"coupon {self.id}: free_delivery carries no cap")
            if self.applies_to is not None:
                raise MenuError(f"coupon {self.id}: free_delivery cannot be scoped")
        if self.applies_to is not None:
            scope = frozenset(self.applies_to)
            if not scope:
                raise MenuError(f"coupon {self.id}: applies_to must not be empty")
            for product_id in scope:
                _require_product_id(product_id)  # items or combos
            object.__setattr__(self, "applies_to", scope)
        if self.query is not None:
            try:
                validate_expression(
                    self.query, ALLOWED_QUERY_NAMES, {"user": ALLOWED_USER_FIELDS}
                )
            except UnsafeExpressionError as exc:
                raise MenuError(f"coupon {self.id}: bad query: {exc}") from exc

    @property
    def is_scoped(self) -> bool:
        return self.applies_to is not None


@dataclass(frozen=True)
class User:
    member: bool = False
    first_order: bool = False

    def as_context(self) -> dict[str, bool]:
        return {"member": self.member, "first_order": self.first_order}


@dataclass(frozen=True)
class PricingConfig:
    delivery_fee: float = 0.0
    platform_fee: float = 0.0
    gst_rate: float = 0.0  # e.g. 0.05 for 5%

    def __post_init__(self) -> None:
        for field_name in ("delivery_fee", "platform_fee", "gst_rate"):
            if _require_number(getattr(self, field_name), field_name) < 0:
                raise MenuError(f"{field_name} must be >= 0")
        if self.gst_rate >= 1:
            raise MenuError("gst_rate is a fraction, e.g. 0.05 for 5%")


@dataclass(frozen=True)
class Combo:
    """Pre-bundled package selectable alongside items (``cmb_``).

    ``composition`` (item id -> quantity) describes what's inside, for
    display and for building the real cart on Swiggy — the optimizer prices
    the bundle by its own cost/preference. ``applicability`` may only
    reference user status (``user.member`` / ``user.first_order``);
    cart-state conditions (e.g. cart minimums) are deferred and rejected.
    """

    id: str
    name: str
    cost: int  # whole rupees, standalone bundle price
    preference: float  # [0, 1], rating of the bundle itself
    composition: Any = ()  # mapping accepted; stored as sorted (item_id, qty) pairs
    applicability: str | None = None
    available: bool = True
    max_quantity: int = 1

    def __post_init__(self) -> None:
        _require_id(self.id, COMBO_PREFIX)
        if isinstance(self.cost, bool) or not isinstance(self.cost, int) or self.cost < 0:
            raise MenuError(
                f"combo {self.id}: cost must be a non-negative int, got {self.cost!r}"
            )
        pref = _require_number(self.preference, f"combo {self.id}: preference")
        if not 0.0 <= pref <= MAX_PREFERENCE:
            raise MenuError(f"combo {self.id}: preference must be in [0, {MAX_PREFERENCE}]")
        pairs = (
            self.composition.items()
            if isinstance(self.composition, Mapping)
            else self.composition
        )
        normalized = []
        for item_id, quantity in pairs:
            _require_id(item_id, ITEM_PREFIX)
            normalized.append(
                (item_id, _require_quantity_cap(quantity, f"combo {self.id}: quantity of {item_id}"))
            )
        object.__setattr__(self, "composition", tuple(sorted(normalized)))
        if self.applicability is not None:
            try:
                validate_expression(
                    self.applicability, frozenset({"user"}), {"user": ALLOWED_USER_FIELDS}
                )
            except UnsafeExpressionError as exc:
                raise MenuError(
                    f"combo {self.id}: applicability may only reference user status: {exc}"
                ) from exc
        _require_quantity_cap(self.max_quantity, f"combo {self.id}: max_quantity")

    @property
    def composition_dict(self) -> dict[str, int]:
        return dict(self.composition)

    def is_orderable(self, user: User) -> bool:
        if not self.available:
            return False
        if self.applicability is None:
            return True
        return bool(
            safe_eval(self.applicability, {"user": user.as_context()})
        )


@dataclass(frozen=True)
class ItemLine:
    """One cart line: an item in a specific valid configuration.

    A configuration is a variant, a per-group-legal addon selection, and a
    quantity within the item's cap. v1 carts hold at most one configuration
    per item (mixing variants of the same item is deferred).
    """

    item: Item
    variant: Variant
    addons: tuple[AddonOption, ...] = ()
    quantity: int = 1

    def __post_init__(self) -> None:
        if self.variant.id not in {v.id for v in self.item.variants}:
            raise MenuError(
                f"variant {self.variant.id} does not belong to item {self.item.id}"
            )
        addons = tuple(sorted(self.addons, key=lambda option: option.id))
        object.__setattr__(self, "addons", addons)
        option_ids = [option.id for option in addons]
        if len(option_ids) != len(set(option_ids)):
            raise MenuError(f"item {self.item.id}: duplicate addon selection")
        owner = {
            option.id: group.id
            for group in self.item.addons
            for option in group.options
        }
        per_group: dict[str, int] = {}
        for option in addons:
            group_id = owner.get(option.id)
            if group_id is None:
                raise MenuError(
                    f"option {option.id} does not belong to item {self.item.id}"
                )
            per_group[group_id] = per_group.get(group_id, 0) + 1
        for group in self.item.addons:
            count = per_group.get(group.id, 0)
            if not group.min_select <= count <= group.max_select:
                raise MenuError(
                    f"item {self.item.id}: group {group.id} requires "
                    f"{group.min_select}-{group.max_select} picks, got {count}"
                )
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or not 1 <= self.quantity <= self.item.max_quantity
        ):
            raise MenuError(
                f"item {self.item.id}: quantity must be in [1, {self.item.max_quantity}]"
            )

    @property
    def product_id(self) -> str:
        return self.item.id

    @property
    def unit_cost(self) -> int:
        return self.variant.cost + sum(option.cost for option in self.addons)

    @property
    def cost(self) -> int:
        return self.quantity * self.unit_cost

    @property
    def preference(self) -> float:
        return self.quantity * (
            self.item.preference + sum(option.preference for option in self.addons)
        )


# Backward-compatible alias: v1 lines were plain (item, variant).
CartLine = ItemLine


@dataclass(frozen=True)
class ComboLine:
    combo: Combo
    quantity: int = 1

    def __post_init__(self) -> None:
        if (
            isinstance(self.quantity, bool)
            or not isinstance(self.quantity, int)
            or not 1 <= self.quantity <= self.combo.max_quantity
        ):
            raise MenuError(
                f"combo {self.combo.id}: quantity must be in [1, {self.combo.max_quantity}]"
            )

    @property
    def product_id(self) -> str:
        return self.combo.id

    @property
    def unit_cost(self) -> int:
        return self.combo.cost

    @property
    def cost(self) -> int:
        return self.quantity * self.combo.cost

    @property
    def preference(self) -> float:
        return self.quantity * self.combo.preference


@dataclass(frozen=True)
class Cart:
    lines: tuple[ItemLine | ComboLine, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "lines", tuple(self.lines))
        for line in self.lines:
            if not isinstance(line, (ItemLine, ComboLine)):
                raise MenuError("cart lines must be ItemLine or ComboLine")
        ids = [line.product_id for line in self.lines]
        if len(ids) != len(set(ids)):
            raise MenuError("cart holds at most one line per product")

    @property
    def subtotal(self) -> int:
        return sum(line.cost for line in self.lines)

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(line.product_id for line in self.lines)

    def select_subtotal(self, product_ids: Iterable[str] | None) -> int:
        """Spend on the given products only; None means the whole cart.
        A line's full cost (variant + addons, × quantity) counts toward
        its product's scope."""
        if product_ids is None:
            return self.subtotal
        scope = frozenset(product_ids)
        return sum(line.cost for line in self.lines if line.product_id in scope)


@dataclass(frozen=True)
class Menu:
    restaurant: str
    items: tuple[Item, ...]
    coupons: tuple[Coupon, ...] = ()
    combos: tuple[Combo, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "coupons", tuple(self.coupons))
        object.__setattr__(self, "combos", tuple(self.combos))
        product_ids = [item.id for item in self.items] + [c.id for c in self.combos]
        if len(product_ids) != len(set(product_ids)):
            raise MenuError("duplicate product ids in menu")
        coupon_ids = [coupon.id for coupon in self.coupons]
        if len(coupon_ids) != len(set(coupon_ids)):
            raise MenuError("duplicate coupon ids in menu")

    def orderable_items(self, now: dt.time | str | None = None) -> tuple[Item, ...]:
        return tuple(item for item in self.items if item.is_orderable(now))

    def orderable_combos(self, user: User) -> tuple[Combo, ...]:
        return tuple(combo for combo in self.combos if combo.is_orderable(user))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Menu":
        """Parse the normalized JSON-ish menu shape.

        Items may carry a ``variants`` map (each value either
        ``{"name": ..., "cost": int}`` or an int cost shorthand) or a bare
        ``cost``, which becomes one synthetic ``var_<item_id>`` variant.
        Unknown ids inside ``applies_to`` are allowed — they simply never
        match a cart item (lenient for real-world offer payloads).
        """
        if not isinstance(payload, Mapping):
            raise MenuError("menu payload must be a mapping")
        items = []
        for item_id, body in (payload.get("items") or {}).items():
            if not isinstance(body, Mapping):
                raise MenuError(f"item {item_id}: body must be a mapping")
            try:
                raw_variants = body.get("variants")
                if raw_variants:
                    variants = []
                    for variant_id, variant_body in raw_variants.items():
                        if isinstance(variant_body, Mapping):
                            variants.append(
                                Variant(
                                    id=variant_id,
                                    name=str(variant_body.get("name", variant_id)),
                                    cost=variant_body["cost"],
                                )
                            )
                        else:
                            variants.append(
                                Variant(id=variant_id, name=variant_id, cost=variant_body)
                            )
                else:
                    if "cost" not in body:
                        raise MenuError(f"item {item_id}: needs 'variants' or 'cost'")
                    variants = [
                        Variant(id=f"var_{item_id}", name="Standard", cost=body["cost"])
                    ]
                groups = []
                for group_id, group_body in (body.get("addons") or {}).items():
                    options = tuple(
                        AddonOption(
                            id=option_id,
                            name=str(option_body.get("name", option_id)),
                            cost=option_body["cost"],
                            preference=option_body.get("preference", 0.0),
                        )
                        for option_id, option_body in group_body["options"].items()
                    )
                    groups.append(
                        AddonGroup(
                            id=group_id,
                            name=str(group_body.get("name", group_id)),
                            min_select=group_body.get("min", 0),
                            max_select=group_body.get("max", len(options)),
                            options=options,
                        )
                    )
                window = body.get("time_window")
                items.append(
                    Item(
                        id=item_id,
                        name=str(body.get("name", item_id)),
                        preference=body["preference"],
                        variants=tuple(variants),
                        available=bool(body.get("available", True)),
                        time_window=tuple(window) if window else None,
                        addons=tuple(groups),
                        max_quantity=body.get("max_quantity", 1),
                        is_veg=body.get("is_veg"),
                    )
                )
            except KeyError as exc:
                raise MenuError(f"item {item_id}: missing field {exc}") from None
        combos = []
        for combo_id, body in (payload.get("combos") or {}).items():
            if not isinstance(body, Mapping):
                raise MenuError(f"combo {combo_id}: body must be a mapping")
            try:
                combos.append(
                    Combo(
                        id=combo_id,
                        name=str(body.get("name", combo_id)),
                        cost=body["cost"],
                        preference=body["preference"],
                        composition=body.get("composition") or {},
                        applicability=body.get("applicability"),
                        available=bool(body.get("available", True)),
                        max_quantity=body.get("max_quantity", 1),
                    )
                )
            except KeyError as exc:
                raise MenuError(f"combo {combo_id}: missing field {exc}") from None
        coupons = []
        for offer_id, body in (payload.get("offers") or {}).items():
            if not isinstance(body, Mapping):
                raise MenuError(f"offer {offer_id}: body must be a mapping")
            try:
                scope = body.get("applies_to")
                coupons.append(
                    Coupon(
                        id=offer_id,
                        kind=body["kind"],
                        value=body.get("value", 0.0),
                        cap=body.get("cap"),
                        query=body.get("query"),
                        applies_to=frozenset(scope) if scope else None,
                        description=str(body.get("description", "")),
                    )
                )
            except KeyError as exc:
                raise MenuError(f"offer {offer_id}: missing field {exc}") from None
        return cls(
            restaurant=str(payload.get("restaurant", "unknown")),
            items=tuple(items),
            coupons=tuple(coupons),
            combos=tuple(combos),
        )

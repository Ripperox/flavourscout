"""End-to-end runner: fetch menu → optimize → verify live → print best cart.

Usage:
    python3 -m cart_optimizer.run --budget 300 --restaurant 668678

Flow:
1. Load OAuth token from ~/.cart-optimizer/token.json
   (run `python3 swiggy_auth_dev.py` once first to log in).
2. Auto-pick your first saved address (or pass --address <id>).
3. Fetch the restaurant menu from Swiggy.
4. Propose 5 diverse candidate carts (cheap + premium mix).
5. Verify each live: flush → build → try coupons → read real bill.
6. Print the best cart with the authoritative Swiggy price.

SAFETY: never calls place_food_order. Cart is flushed after every probe.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .adapters.swiggy import SwiggyAdapterError, parse_cart_bill, parse_menu
from .adapters.swiggy_session import cart_to_swiggy_items
from .discovery import VerifiedCart, propose_candidates
from .models import Cart, PricingConfig, User
from .swiggy_client import SwiggyClient, SwiggyClientError

TOKEN_FILE = Path.home() / ".cart-optimizer" / "token.json"


# ── token helpers ─────────────────────────────────────────────────────────────

def load_token() -> dict:
    if not TOKEN_FILE.exists():
        sys.exit(
            f"No token found at {TOKEN_FILE}.\n"
            "Run:  python3 swiggy_auth_dev.py\n"
            "Then try again."
        )
    return json.loads(TOKEN_FILE.read_text())


async def refresh_token(token_data: dict) -> dict:
    import httpx
    resp = httpx.post(
        "https://mcp.swiggy.com/auth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": token_data["refresh_token"],
            "client_id": token_data.get("client_id", "cart-optimizer-dev"),
        },
    )
    if resp.status_code == 200:
        token_data = {**token_data, **resp.json()}
        TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    return token_data


# ── async cart verification ───────────────────────────────────────────────────

async def _verify_one(
    cart: Cart,
    client: SwiggyClient,
    restaurant_id: str,
    restaurant_name: str,
    address_id: str,
    coupons: list[str],
) -> "CartBill":  # noqa: F821
    """Verify one candidate cart against Swiggy's live bill.

    Build the cart ONCE, then probe the auto-SUGGESTED coupon ∪ the candidate
    list by applying each in place (no rebuild → far fewer calls). Returns the
    bill with the lowest to_pay across no-coupon and all coupon attempts.
    """
    from .adapters.swiggy import CartBill
    from .adapters.swiggy_session import _suggested_coupon

    cart_items = cart_to_swiggy_items(cart)
    bills: list[CartBill] = []

    async def get_raw() -> dict:
        return await client.call(
            "get_food_cart", addressId=address_id, restaurantName=restaurant_name
        )

    # Build once → base bill + the coupon Swiggy auto-suggests for this cart.
    await client.call("flush_food_cart")
    await client.call(
        "update_food_cart",
        restaurantId=restaurant_id,
        restaurantName=restaurant_name,
        addressId=address_id,
        cartItems=cart_items,
    )
    raw = await get_raw()
    bills.append(parse_cart_bill(raw))
    suggested = _suggested_coupon(raw)

    # suggested first, then candidates (deduped, order-preserving).
    codes: list[str] = []
    for c in ([suggested] if suggested else []) + list(coupons):
        if c and c not in codes:
            codes.append(c)

    for code in codes:
        try:
            await client.call("apply_food_coupon", couponCode=code, addressId=address_id)
            bills.append(parse_cart_bill(await get_raw()))
        except (SwiggyClientError, SwiggyAdapterError, Exception):
            pass  # rejected or error — skip

    await client.call("flush_food_cart")
    return min(bills, key=lambda b: b.to_pay)


# ── display helpers ───────────────────────────────────────────────────────────

def _line_name(line) -> str:
    item = getattr(line, "item", None)
    if item:
        return item.name
    combo = getattr(line, "combo", None)
    if combo:
        return combo.name
    return line.product_id


def _print_bill(bill) -> None:
    print(f"  Total:     ₹{bill.to_pay:.0f}")
    print(f"  Items:     ₹{bill.item_total:.0f}")
    if bill.coupon_code:
        print(f"  Coupon:    {bill.coupon_code}  -₹{bill.coupon_discount:.0f}")
    if bill.free_delivery:
        print("  Delivery:  FREE")
    else:
        print(f"  Delivery:  ₹{bill.delivery_charge:.0f}")
    print(f"  Taxes:     ₹{bill.taxes_and_charges:.0f}")


# ── menu fetching (pagination + variant/addon enrichment) ─────────────────────

async def _fetch_full_menu(client, restaurant_id: str, address_id: str) -> dict:
    """Page through get_restaurant_menu and merge all categories into one dict.

    A single call returns only the first page of categories (and trims items
    within large categories). We follow ``hasMore`` to collect everything."""
    merged: dict = {}
    all_categories: list = []
    page = 1
    while True:
        resp = await client.call(
            "get_restaurant_menu",
            restaurantId=restaurant_id,
            addressId=address_id,
            page=page,
            pageSize=8,
        )
        if not merged:
            merged = {"restaurant": resp.get("restaurant", {}), "categories": []}
        all_categories.extend(resp.get("categories", []))
        if not resp.get("hasMore") or page >= 6:   # cap at 6 pages (48 categories)
            break
        page += 1
    merged["categories"] = all_categories
    return merged


async def _enrich_menu_detail(client, raw_menu, restaurant_id, address_id) -> list[dict]:
    """For items flagged hasVariants/hasAddons, fetch search_menu detail so the
    adapter can parse their variations/addons. Searches by each such item's name
    (deduped). Returns the list of search_menu responses to feed parse_menu."""
    names: list[str] = []
    seen: set[str] = set()
    for cat in raw_menu.get("categories", []):
        for item in cat.get("items", []):
            if (item.get("hasVariants") or item.get("hasAddons")):
                name = str(item.get("name", "")).strip()
                key = name.lower()
                if name and key not in seen:
                    seen.add(key)
                    names.append(name)

    if not names:
        return []

    print(f"  enriching {len(names)} variant/addon items via search...")
    responses: list[dict] = []
    for name in names[:40]:   # cap searches to bound call volume
        try:
            resp = await client.call(
                "search_menu",
                query=name,
                addressId=address_id,
                restaurantIdOfAddedItem=restaurant_id,
            )
            responses.append(resp)
        except Exception:
            pass
    if len(names) > 40:
        print(f"  (note: {len(names) - 40} variant/addon items not enriched — search cap)")
    return responses


# ── main runner ───────────────────────────────────────────────────────────────

async def run(
    budget: float,
    restaurant_id: str,
    address_id: str | None,
    coupons: list[str],
) -> None:
    token_data = load_token()

    async with SwiggyClient(token_data["access_token"]) as client:

        # 1. Address
        if address_id is None:
            print("Fetching your addresses...")
            addrs = await client.call("get_addresses")
            addresses = (
                addrs.get("addresses")
                or addrs.get("data", {}).get("addresses", [])
            )
            if not addresses:
                sys.exit("No saved addresses on this Swiggy account.")
            addr = addresses[0]
            address_id = str(addr.get("id") or addr.get("address_id"))
            label = (
                addr.get("flatNo")
                or addr.get("tag")
                or addr.get("address")
                or address_id
            )
            print(f"Using address: {label}  (id={address_id})\n")

        # 2. Menu — paginate all categories, enrich variant/addon detail via search.
        print(f"Fetching menu for restaurant {restaurant_id}...")
        raw_menu = await _fetch_full_menu(client, restaurant_id, address_id)
        search_responses = await _enrich_menu_detail(
            client, raw_menu, restaurant_id, address_id
        )
        menu = parse_menu(raw_menu, search_responses=search_responses, skip_unparseable=True)
        print(f"Menu: {menu.restaurant} — {len(menu.items)} items\n")

        # 3. Candidates
        config = PricingConfig(delivery_fee=30, platform_fee=5, gst_rate=0.05)
        user = User()
        candidates = propose_candidates(menu, user, config, budget, max_candidates=5)
        print(f"Proposed {len(candidates)} candidate carts to probe.\n")

        # 4. Live verification (fully async — no nested event-loop issues)
        print("Verifying each candidate live on Swiggy...")
        print("(Cart is flushed after each probe — no order is placed)\n")

        verified: list[VerifiedCart] = []
        for i, cart in enumerate(candidates, 1):
            names = ", ".join(_line_name(l) for l in cart.lines)
            print(f"  [{i}/{len(candidates)}] {names}")
            try:
                bill = await _verify_one(
                    cart, client, restaurant_id, menu.restaurant, address_id, coupons
                )
                suffix = (
                    f"  (coupon: {bill.coupon_code}  -₹{bill.coupon_discount:.0f})"
                    if bill.coupon_code else ""
                )
                print(f"         → ₹{bill.to_pay:.0f}{suffix}")
                if bill.to_pay <= budget:
                    verified.append(VerifiedCart(cart, bill))
                else:
                    print("         → over budget, skipped")
            except Exception as e:
                print(f"         → error: {e}")

        # 5. Result
        print()
        if not verified:
            print(f"No cart found within ₹{budget:.0f} budget.")
            return

        best = max(verified, key=lambda v: (v.preference, -v.bill.to_pay))
        print("=" * 52)
        print(f"BEST CART  (preference {best.preference:.2f})")
        print("=" * 52)
        for line in best.cart.lines:
            print(f"  • {line.quantity}x {_line_name(line)}  ₹{line.cost}")
        print()
        _print_bill(best.bill)
        print()
        print("To order: open Swiggy and place manually.")
        print("NEVER auto-order — COD orders are non-cancellable.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find the best-value Swiggy cart within your budget."
    )
    parser.add_argument("--budget", type=float, required=True, help="Max spend in ₹")
    parser.add_argument("--restaurant", required=True, help="Swiggy restaurant id (e.g. 668678)")
    parser.add_argument("--address", default=None, help="Swiggy address id (default: first saved)")
    parser.add_argument(
        "--coupons", nargs="*", default=None,
        help="Coupon codes to try per cart (default: built-in candidate list; "
             "the cart's auto-suggested coupon is ALWAYS tried on top)"
    )
    args = parser.parse_args()
    from .adapters.swiggy_session import DEFAULT_COUPON_CANDIDATES
    coupons = args.coupons if args.coupons is not None else list(DEFAULT_COUPON_CANDIDATES)
    asyncio.run(run(
        budget=args.budget,
        restaurant_id=args.restaurant,
        address_id=args.address,
        coupons=coupons,
    ))


if __name__ == "__main__":
    main()

"""FastAPI backend for the cart optimizer web UI.

Each visitor logs into their own Swiggy (OAuth/PKCE); we run the
optimize→live-verify pipeline with their token and share discovered coupons
across all users via a SQLite per-branch ledger.

Run locally:
    uvicorn webapp.server:app --reload --port 8000

Env:
    SESSION_SECRET   cookie-signing secret (set in prod; random if unset)
    BASE_URL         public base url, e.g. https://x.onrender.com (else derived)
    SWIGGY_CLIENT_ID pre-registered OAuth client id (else dynamic registration)
    COUPON_DB        sqlite path for the shared ledger (default ./data/coupons.db)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from cart_optimizer.coupon_ledger import SqliteCouponLedger
from cart_optimizer.models import Cart, ItemLine, PricingConfig, User
from cart_optimizer.adapters.swiggy import parse_menu, parse_cart_bill
from cart_optimizer.adapters.swiggy_session import DEFAULT_COUPON_CANDIDATES, cart_to_swiggy_items
from cart_optimizer.discovery import (
    VerifiedCart, apply_real_prices, propose_candidates,
)
from cart_optimizer.run import (
    _fetch_full_menu, _enrich_menu_detail, _verify_one, discover_prices,
)
from cart_optimizer.swiggy_client import SwiggyClient, SwiggyClientError, _is_rate_limited
from cart_optimizer import coupon_monitor
from cart_optimizer.optimizer import best_cart
from . import oauth
from .demo_data import demo_menu, DEMO_RESTAURANT

STATIC_DIR = Path(__file__).parent / "static"
COUPON_DB = os.getenv("COUPON_DB", "./data/coupons.db")
SESSION_FILE = Path(os.getenv("SESSION_FILE", "./data/sessions.json"))
CONFIG = PricingConfig(delivery_fee=30, platform_fee=5, gst_rate=0.05)
BUDGET_BUFFER = 0.15   # option 2 may exceed budget by up to 15% if it's far better value
TOKEN_REFRESH_SKEW = 60   # refresh an access token this many seconds before it expires

# Chains we've validated end-to-end; "restaurants our service provides".
SUPPORTED_BRANDS = ["McDonald's", "Burger King", "Starbucks", "Taco Bell", "KFC", "Domino's"]

# Observability: a single logger for the web layer. basicConfig is a no-op if the
# host (e.g. uvicorn) already installed root handlers, so this just guarantees our
# records reach a handler when run standalone.
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("cartoptimizer.web")

app = FastAPI(title="BudgetBite")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", secrets.token_hex(32)))

# Shared coupon ledger — one DB for ALL users of this backend.
ledger = SqliteCouponLedger(COUPON_DB)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    """Last-resort handler: log the real error, return a clean JSON 500 to the
    client (never leak a traceback). HTTPException / validation errors keep their
    own handlers — this only catches genuinely unexpected failures."""
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": "internal server error"}, status_code=500)


# Server-side session store: cookie holds only an opaque sid; tokens stay here.
# Persisted to disk so logins survive a server restart (single-instance friendly).
def _load_sessions() -> dict[str, dict]:
    try:
        return json.loads(SESSION_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_sessions() -> None:
    try:
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_FILE.write_text(json.dumps(_SESSIONS))
    except OSError:
        pass


_SESSIONS: dict[str, dict] = _load_sessions()


# ── helpers ───────────────────────────────────────────────────────────────────

def _redirect_uri(request: Request) -> str:
    base = os.getenv("BASE_URL") or str(request.base_url).rstrip("/")
    return f"{base}/callback"


def _session(request: Request) -> dict | None:
    sid = request.session.get("sid")
    return _SESSIONS.get(sid) if sid else None


def _store_tokens(sess: dict, tokens: dict) -> None:
    """Record a token response and when its access_token expires."""
    sess.update(tokens)
    try:
        ttl = float(tokens.get("expires_in") or 3600)
    except (TypeError, ValueError):
        ttl = 3600.0
    sess["expires_at"] = time.time() + ttl


def _token(request: Request) -> str:
    """Return a usable access token for this session, proactively refreshing it
    via the stored refresh_token when it's at/near expiry. Without a refresh
    token we hand back the current one and let the upstream 401 surface."""
    sess = _session(request)
    if not sess or "access_token" not in sess:
        raise HTTPException(status_code=401, detail="not logged in")
    expires_at = sess.get("expires_at", 0)
    if (expires_at and expires_at - time.time() < TOKEN_REFRESH_SKEW
            and sess.get("refresh_token")):
        try:
            tokens = oauth.refresh(sess["refresh_token"], sess.get("client_id", ""))
            _store_tokens(sess, tokens)
            _save_sessions()
            log.info("refreshed access token")
        except Exception:  # noqa: BLE001 — refresh is best-effort; fall back to old token
            log.warning("token refresh failed; using existing token", exc_info=True)
    return sess["access_token"]


# ── auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
def login(request: Request):
    redirect_uri = _redirect_uri(request)
    client_id = oauth.resolve_client_id(redirect_uri)
    auth_url, verifier, state = oauth.start_login(redirect_uri, client_id)
    sid = secrets.token_urlsafe(24)
    request.session["sid"] = sid
    _SESSIONS[sid] = {"pkce": verifier, "state": state, "client_id": client_id}
    _save_sessions()
    return RedirectResponse(auth_url)


@app.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None):
    sess = _session(request)
    if error:
        return HTMLResponse(f"<h3>Login failed: {error}</h3><a href='/'>back</a>", status_code=400)
    if not sess or not code or state != sess.get("state"):
        return HTMLResponse("<h3>Invalid login state.</h3><a href='/'>back</a>", status_code=400)
    try:
        tokens = oauth.exchange_code(
            code, _redirect_uri(request), sess["client_id"], sess["pkce"]
        )
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(f"<h3>Token exchange failed: {e}</h3><a href='/'>back</a>",
                            status_code=400)
    sess.pop("pkce", None)
    _store_tokens(sess, tokens)
    _save_sessions()
    log.info("login complete (client_id=%s)", sess.get("client_id"))
    return RedirectResponse("/")


@app.post("/logout")
def logout(request: Request):
    sid = request.session.pop("sid", None)
    if sid:
        _SESSIONS.pop(sid, None)
        _save_sessions()
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    return {"logged_in": bool(_session(request) and "access_token" in _session(request))}


# ── data routes ───────────────────────────────────────────────────────────────

def _as_dict(data):
    """Tolerate a stray JSON string that slipped through."""
    if isinstance(data, str):
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


@app.get("/api/addresses")
async def addresses(request: Request):
    token = _token(request)
    async with SwiggyClient(token) as client:
        data = _as_dict(await client.call("get_addresses"))
    addrs = data.get("addresses") or (data.get("data") or {}).get("addresses", [])
    out = []
    for a in addrs:
        if not isinstance(a, dict):
            continue
        out.append({
            "id": str(a.get("id") or a.get("address_id")),
            "label": a.get("addressTag") or a.get("flatNo") or a.get("addressLine") or "Address",
            "line": a.get("addressLine", ""),
        })
    return out


def _restaurant_row(r: dict) -> dict:
    return {"id": str(r["id"]), "name": r.get("name"),
            "area": r.get("areaName"), "rating": r.get("avgRating"),
            "offer": r.get("offer"), "etaMins": r.get("deliveryTimeMinutes"),
            "distanceKm": r.get("distanceKm")}


@app.get("/api/restaurants")
async def restaurants(request: Request, q: str, addressId: str):
    token = _token(request)
    async with SwiggyClient(token) as client:
        data = _as_dict(await client.call("search_restaurants", query=q, addressId=addressId))
    out = []
    for r in data.get("restaurants", []):
        if not isinstance(r, dict) or str(r.get("availabilityStatus", "OPEN")).upper() != "OPEN":
            continue
        out.append(_restaurant_row(r))
    return out[:12]


@app.get("/api/nearby")
async def nearby(request: Request, addressId: str):
    """Closest OPEN restaurants our service supports, for this delivery address.

    Swiggy search is address-based (no lat/lng tool), so 'near you' = nearest
    branches of our supported chains deliverable to the chosen address, sorted
    by Swiggy's distanceKm."""
    token = _token(request)
    seen: dict[str, dict] = {}
    sem = asyncio.Semaphore(4)
    async with SwiggyClient(token) as client:
        async def one(brand: str):
            async with sem:
                try:
                    return _as_dict(await client.call(
                        "search_restaurants", query=brand, addressId=addressId))
                except Exception:  # noqa: BLE001
                    return {}
        for data in await asyncio.gather(*[one(b) for b in SUPPORTED_BRANDS]):
            for r in data.get("restaurants", []):
                if not isinstance(r, dict):
                    continue
                if str(r.get("availabilityStatus", "OPEN")).upper() != "OPEN":
                    continue
                rid = str(r.get("id"))
                if rid not in seen:                 # keep the closest instance
                    seen[rid] = _restaurant_row(r)
    rows = sorted(seen.values(),
                  key=lambda x: (x.get("distanceKm") is None, x.get("distanceKm") or 1e9))
    return rows[:12]


class OptimizeRequest(BaseModel):
    """Validated body for /api/optimize. FastAPI returns a 422 automatically when
    a field is missing or out of range, so the handler only sees sane input."""
    restaurantId: str = Field(min_length=1)
    addressId: str = Field(min_length=1)
    budget: float = Field(gt=0, le=100_000)
    restaurantName: str = ""
    drinks: bool = False
    vegOnly: bool = False
    groupSize: int = Field(default=1, ge=1, le=20)
    demo: bool = False


@app.post("/api/optimize")
async def optimize(request: Request, body: OptimizeRequest):
    if body.demo:                       # no-login public demo on the bundled menu
        return _demo_optimize(body)
    token = _token(request)
    rid = body.restaurantId
    addr = body.addressId
    budget = float(body.budget)
    rname = body.restaurantName
    want_drinks = body.drinks
    veg_only = body.vegOnly
    group_size = body.groupSize
    log.info("optimize start rid=%s name=%r budget=%.0f drinks=%s veg=%s group=%d",
             rid, rname, budget, want_drinks, veg_only, group_size)

    verified: list[VerifiedCart] = []
    built = 0            # candidates that produced an authoritative bill (cart truly built)
    rate_limited = False  # a probe failed specifically because Swiggy rate-limited us
    try:
        async with SwiggyClient(token) as client:
            menu = await _get_menu_cached(client, rid, addr)
            if not rname:
                rname = menu.restaurant

            # Drinks toggle (default OFF): if the user doesn't want a drink, strip
            # standalone drinks AND meals/combos (which bundle a drink) so the cart
            # is pure food. ON: keep them — a meal is valued as a bundle (worth its
            # parts) so the optimizer prefers it over à-la-carte.
            if not want_drinks:
                menu = _food_only(menu)

            # Veg-only filter (fail safe): keep only items confirmed veg. Swiggy
            # marks veg items isVeg=true but often OMITS the flag on non-veg, so we
            # exclude unknowns too — never serve a veg user something unverified.
            if veg_only:
                menu = _veg_only(menu)
                if not menu.items:
                    who = rname or "this restaurant"
                    return JSONResponse({"found": False, "restaurant": rname,
                                         "message": f"No vegetarian items found at {who}."})

            # Calibrate to REAL prices: read each item's actual final_price from a
            # probe cart (Swiggy item-level discounts make our list prices too high)
            # so the optimizer fills the real budget instead of stopping early.
            real = await discover_prices(client, rid, rname, addr, menu, budget)
            if real:
                menu = apply_real_prices(menu, real)

            # We show TWO carts: option 1 strictly within budget, option 2 a small
            # "worth the stretch" buffer. Verify up to the stretch ceiling and pick
            # both from the same pool. (Candidate count kept small to limit calls.)
            stretch = budget * (1 + BUDGET_BUFFER)
            candidates = propose_candidates(menu, User(), CONFIG, stretch, max_candidates=3)

            # Coupon strategy (minimal calls): every cart tries its auto-SUGGESTED +
            # this branch's known coupons; a small discovery list runs only on the
            # first cart at a never-seen branch.
            learned = bool(ledger.known(rid))
            seen_keys: set = set()

            async def verify_candidates(carts, allow_discovery):
                nonlocal built, rate_limited
                out: list[VerifiedCart] = []
                for i, cart in enumerate(carts):
                    key = tuple(sorted((l.product_id, l.quantity) for l in cart.lines))
                    if not cart.lines or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    discovery = allow_discovery and i == 0 and not learned
                    coupons = list(DEFAULT_COUPON_CANDIDATES) if discovery else []
                    try:
                        bill = await _verify_one(cart, client, rid, rname, addr, coupons, ledger=ledger)
                    except Exception as e:  # noqa: BLE001 — one bad cart shouldn't sink the request
                        rate_limited = rate_limited or _is_rate_limited(e)
                        log.warning("verify failed (rid=%s cart=%s): %s", rid, key, e)
                        continue
                    built += 1
                    if bill.to_pay <= stretch:        # keep everything up to the stretch ceiling
                        out.append(VerifiedCart(cart, bill))
                return out

            verified = await verify_candidates(candidates, allow_discovery=True)

            # Greedy top-up toward the stretch ceiling: add the best affordable main
            # and re-verify while it fits + improves. The pool keeps every
            # intermediate cart, so we can still pick the best within-budget one.
            if verified:
                best = max(verified, key=lambda v: (v.preference, -v.bill.to_pay))
                addable = sorted(
                    [i for i in menu.orderable_items()
                     if min(v.cost for v in i.variants) <= stretch],
                    key=lambda i: i.preference, reverse=True)
                for _ in range(2):
                    headroom = stretch - best.bill.to_pay
                    if headroom < 40:
                        break
                    in_cart = {l.product_id for l in best.cart.lines}
                    added = False
                    for item in addable:
                        if item.id in in_cart:
                            continue
                        line = ItemLine(item, min(item.variants, key=lambda v: v.cost))
                        if line.cost > headroom * 1.4:
                            continue
                        try:
                            cart = Cart(best.cart.lines + (line,))
                        except Exception:  # noqa: BLE001 — e.g. dup product; just skip this item
                            continue
                        key = tuple(sorted((l.product_id, l.quantity) for l in cart.lines))
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                        try:
                            bill = await _verify_one(cart, client, rid, rname, addr, [], ledger=ledger)
                        except Exception as e:  # noqa: BLE001 — top-up probe failed; keep best
                            rate_limited = rate_limited or _is_rate_limited(e)
                            log.warning("top-up verify failed (rid=%s cart=%s): %s", rid, key, e)
                            continue
                        if bill.to_pay <= stretch and sum(l.preference for l in cart.lines) > best.preference:
                            best = VerifiedCart(cart, bill)
                            verified.append(best)
                            added = True
                            break
                    if not added:
                        break
    except SwiggyClientError as e:
        log.warning("optimize rid=%s aborted (Swiggy error): %s", rid, e)
        return JSONResponse(_busy_response(rname))
    except Exception as e:  # noqa: BLE001
        if _is_rate_limited(e):
            log.warning("optimize rid=%s aborted (rate-limited)", rid)
            return JSONResponse(_busy_response(rname))
        raise  # genuinely unexpected → global handler → clean 500

    log.info("optimize rid=%s done: %d built, %d within-stretch, %d within-budget",
             rid, built, len(verified), sum(1 for v in verified if v.bill.to_pay <= budget + 0.5))

    within = [v for v in verified if v.bill.to_pay <= budget + 0.5]
    if not within:
        if rate_limited and built == 0:
            return JSONResponse(_busy_response(rname))
        if built == 0:   # carts never built (e.g. items not addable at this store)
            who = rname or "this restaurant"
            return JSONResponse({"found": False, "restaurant": rname,
                                 "message": f"We couldn't build a cart at {who} — "
                                            "it may not be fully supported yet."})
        return JSONResponse({"found": False, "restaurant": rname,
                             "message": f"No cart fits ₹{budget:.0f}."})

    # Group size: prefer carts that field at least one "main" per person (mains =
    # higher-preference items, not sides), then by total preference / lower price.
    def _rank(v):
        return (_main_count(v.cart) >= group_size, v.preference, -v.bill.to_pay)

    option1 = max(within, key=_rank)
    chosen = [option1]
    options = [_option(option1, budget, "within")]

    # Option 2: the best cart in the buffer zone, but only if it clearly beats
    # option 1's value for that small overage (else two near-identical carts).
    stretch_best = max(verified, key=_rank)
    if (stretch_best.bill.to_pay > budget + 0.5
            and stretch_best.preference > option1.preference + 1e-6):
        options.append(_option(stretch_best, budget, "stretch"))
        chosen.append(stretch_best)

    # Stash the exact carts behind these options so Place Order can rebuild them.
    sid = request.session.get("sid")
    if sid:
        _PENDING[sid] = {
            "rid": rid, "rname": rname, "addr": addr,
            "options": [{"items": cart_to_swiggy_items(v.cart),
                         "coupon": v.bill.coupon_code,
                         "to_pay": v.bill.to_pay} for v in chosen],
        }

    # Opportunistic coupon intelligence: in the background, prove more codes for
    # this branch on the representative cart — enriches the shared ledger so the
    # next user (here or at any branch of this brand) gets them first.
    _kick_coupon_sweep(token, rid, rname, addr, cart_to_swiggy_items(option1.cart))

    return {"found": True, "restaurant": rname,
            "budget": budget, "group_size": group_size,
            "per_head": round(budget / group_size),
            "options": options,
            "branch_known_coupons": ledger.known(rid)}


def _busy_response(restaurant: str) -> dict:
    """Clean, retryable payload when Swiggy rate-limits / the transport fails —
    shown to the user instead of a 500."""
    return {"found": False, "restaurant": restaurant,
            "message": "Swiggy is busy right now — please try again in a moment."}


MAIN_PREFERENCE_THRESHOLD = 0.6  # items at/above this read as a "main", not a side


def _main_count(cart) -> int:
    """How many lines are 'mains' (vs sides/drinks) — used to satisfy group size."""
    return sum(1 for l in cart.lines if l.preference >= MAIN_PREFERENCE_THRESHOLD)


def _line_veg(line):
    item = getattr(line, "item", None)
    return getattr(item, "is_veg", None) if item is not None else None


def _demo_optimize(body: OptimizeRequest) -> dict:
    """No-login demo: run the real optimizer + profiling on the bundled menu.
    Totals are ESTIMATED from the internal fee model (no live bill, no ordering)."""
    menu = demo_menu()
    if not body.drinks:
        menu = _food_only(menu)
    if body.vegOnly:
        menu = _veg_only(menu)
        if not menu.items:
            return {"found": False, "restaurant": DEMO_RESTAURANT, "demo": True,
                    "message": "No vegetarian items in the demo menu."}

    budget = float(body.budget)
    stretch = budget * (1 + BUDGET_BUFFER)
    opt1 = _demo_option(menu, budget, budget, "within")
    if not opt1:
        return {"found": False, "restaurant": DEMO_RESTAURANT, "demo": True,
                "message": f"No cart fits ₹{budget:.0f} in the demo menu."}
    options = [opt1]
    stretch_opt = _demo_option(menu, stretch, budget, "stretch")
    if (stretch_opt and stretch_opt["bill"]["to_pay"] > budget + 0.5
            and stretch_opt["preference"] > opt1["preference"] + 1e-6):
        options.append(stretch_opt)

    return {"found": True, "restaurant": DEMO_RESTAURANT, "demo": True, "estimated": True,
            "budget": budget, "group_size": body.groupSize,
            "per_head": round(budget / body.groupSize),
            "options": options, "branch_known_coupons": []}


def _demo_option(menu, solve_budget: float, display_budget: float, kind: str) -> dict | None:
    result = best_cart(menu, User(), CONFIG, solve_budget)
    if not result.cart.lines:
        return None
    b = result.breakdown
    code = result.coupon.id.replace("off_", "").upper() if result.coupon else None
    return {
        "kind": kind,
        "over": max(0, round(b.total - display_budget)),
        "preference": round(result.preference, 2),
        "items": [{"name": _line_name(l), "qty": l.quantity, "veg": _line_veg(l)}
                  for l in result.cart.lines],
        "bill": {
            "to_pay": round(b.total, 2),
            "item_total": round(b.subtotal, 2),
            "coupon": code,
            "coupon_discount": round(b.discount, 2),
            "free_delivery": b.delivery_fee == 0,
            "taxes": round(b.tax + b.platform_fee, 2),
            "cod": True,
        },
    }


def _option(v: VerifiedCart, budget: float, kind: str) -> dict:
    return {
        "kind": kind,
        "over": max(0, round(v.bill.to_pay - budget)),
        "preference": round(v.preference, 2),
        "items": [{"name": _line_name(l), "qty": l.quantity, "veg": _line_veg(l)}
                  for l in v.cart.lines],
        "bill": {
            "to_pay": v.bill.to_pay,
            "item_total": v.bill.item_total,
            "coupon": v.bill.coupon_code,
            "coupon_discount": v.bill.coupon_discount,
            "free_delivery": v.bill.free_delivery,
            "taxes": v.bill.taxes_and_charges,
            "cod": v.bill.cod_available,
        },
    }


@app.get("/api/coupons/{restaurant_id}")
def branch_coupons(restaurant_id: str):
    """Shared, crowd-sourced coupons known to work at this branch."""
    return {"restaurant_id": restaurant_id, "coupons": ledger.known(restaurant_id)}


@app.get("/api/coupon-stats")
def coupon_stats():
    """Aggregate coupon-intelligence stats (for the showcase surface)."""
    try:
        return ledger.stats()
    except Exception:  # noqa: BLE001
        return {"branches": 0, "working_coupons": 0, "codes_tracked": 0, "best_discount": 0}


# ── order placement (user-initiated, COD-aware) ────────────────────────────────

class PlaceOrderRequest(BaseModel):
    optionIndex: int = Field(default=0, ge=0)
    confirmed: bool = False                 # set only by the confirmation modal
    expectedTotal: float | None = None      # the total the user saw, for the drift guard


def _order_info(result) -> dict:
    """Defensively pull an order id + status from place_food_order's response
    (its exact shape isn't verified here — placement is the user's to test)."""
    data = result if isinstance(result, dict) else {}
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    return {
        "order_id": inner.get("order_id") or inner.get("orderId") or data.get("order_id"),
        "status": str(inner.get("status") or data.get("status") or "PLACED"),
    }


@app.post("/api/place-order")
async def place_order(request: Request, body: PlaceOrderRequest):
    """Place the real order for a previously-shown option. SAFETY: only ever runs
    on an explicit, confirmed user action; rebuilds + re-verifies the bill (drift
    guard) before calling place_food_order; honours Swiggy's <₹1000 beta cap."""
    token = _token(request)
    if not body.confirmed:
        raise HTTPException(status_code=400, detail="order not confirmed")
    sid = request.session.get("sid")
    pending = _PENDING.get(sid) if sid else None
    if not pending or body.optionIndex >= len(pending["options"]):
        raise HTTPException(status_code=409, detail="no cart ready — run a search first")

    opt = pending["options"][body.optionIndex]
    rid, rname, addr = pending["rid"], pending["rname"], pending["addr"]
    log.info("place-order start rid=%s option=%d", rid, body.optionIndex)

    try:
        async with SwiggyClient(token) as client:
            # Rebuild the exact cart (fresh slate first), re-apply its winning coupon.
            await client.call("flush_food_cart")
            await client.call("update_food_cart", restaurantId=rid, restaurantName=rname,
                              addressId=addr, cartItems=opt["items"])
            if opt.get("coupon"):
                try:
                    await client.call("apply_food_coupon", couponCode=opt["coupon"], addressId=addr)
                except Exception as e:  # noqa: BLE001 — coupon may no longer apply; place anyway
                    log.warning("place-order coupon %s failed: %s", opt["coupon"], e)
            bill = parse_cart_bill(await client.call("get_food_cart", addressId=addr,
                                                     restaurantName=rname))

            # Swiggy MCP beta hard-caps placement under ₹1000.
            if bill.to_pay >= 1000:
                await client.call("flush_food_cart")
                return JSONResponse({"placed": False, "reason": "too_large", "newTotal": bill.to_pay,
                    "message": "Swiggy's beta only places orders under ₹1000 — "
                               "use the Swiggy app for larger orders."})

            # Price-drift guard: if the authoritative total moved, re-confirm.
            expected = body.expectedTotal if body.expectedTotal is not None else opt.get("to_pay")
            if expected is not None and abs(bill.to_pay - float(expected)) > 1.0:
                await client.call("flush_food_cart")
                return JSONResponse({"placed": False, "reason": "price_changed", "newTotal": bill.to_pay,
                    "message": f"The total changed to ₹{bill.to_pay:.0f}. Review and confirm again."})

            # Place. (The assistant never reaches here — user-confirmed clicks only.)
            result = await client.call("place_food_order", addressId=addr)
    except SwiggyClientError as e:
        log.warning("place-order rid=%s failed: %s", rid, e)
        return JSONResponse({"placed": False, "reason": "error",
            "message": "Couldn't place the order just now — try again or use the Swiggy app."})
    except Exception as e:  # noqa: BLE001
        if _is_rate_limited(e):
            return JSONResponse(_busy_response(rname) | {"placed": False})
        raise

    info = _order_info(result)
    if info["status"].upper() == "PENDING_PAYMENT":   # UPI flow — NOT placed yet
        return JSONResponse({"placed": False, "reason": "payment_pending", "order": info,
            "message": "Payment is pending — finish it in the Swiggy app to complete the order."})

    _PENDING.pop(sid, None)  # consume so it can't be double-placed
    log.info("place-order rid=%s placed: %s", rid, info)
    return {"placed": True, "to_pay": bill.to_pay, "order": info}


def _line_name(line) -> str:
    item = getattr(line, "item", None)
    if item:
        return item.name
    combo = getattr(line, "combo", None)
    return combo.name if combo else line.product_id


# Parsed-menu cache keyed by (restaurant_id, address_id). The menu (and its
# enrichment) doesn't change between optimize requests, so caching it removes the
# pagination + enrichment calls on repeat budgets/visits — a big latency cut.
_MENU_CACHE: dict[tuple[str, str], tuple[float, object]] = {}
_MENU_TTL = 600  # seconds

# Per-session stash of the exact carts behind the options we last showed, so the
# Place Order button can rebuild precisely what the user saw (keyed by session id).
_PENDING: dict[str, dict] = {}

# Background coupon sweeps (opportunistic coupon intelligence). One per branch at a
# time; we keep task references so they aren't garbage-collected mid-flight.
_SWEEP_TASKS: set = set()
_SWEEPING: set[str] = set()


def _kick_coupon_sweep(token: str, rid: str, rname: str, addr: str, cart_items: list) -> None:
    """Fire-and-forget: prove untested/stale coupons for this branch on a
    representative cart, enriching the shared ledger. Never blocks the response,
    never places an order, paced by the global rate limiter."""
    if not cart_items or rid in _SWEEPING:
        return
    _SWEEPING.add(rid)

    async def _run():
        try:
            async with SwiggyClient(token) as client:
                await coupon_monitor.sweep_branch(client, rid, rname, addr, cart_items, ledger)
        except Exception as e:  # noqa: BLE001 — best-effort background work
            log.warning("coupon sweep rid=%s failed: %s", rid, e)
        finally:
            _SWEEPING.discard(rid)

    try:
        task = asyncio.create_task(_run())
        _SWEEP_TASKS.add(task)
        task.add_done_callback(_SWEEP_TASKS.discard)
    except RuntimeError:        # no running loop (e.g. called from a sync test) — skip
        _SWEEPING.discard(rid)


def _food_only(menu):
    """Drop drink items and meal/combo bundles (they carry a drink) — used when the
    user hasn't opted into drinks. Reuses the adapter's name-based classification."""
    import dataclasses
    from cart_optimizer.adapters.swiggy import contains_drink, is_beverage_led
    bev = is_beverage_led(getattr(menu, "cuisines", []) or [])
    kept = tuple(i for i in menu.items if not contains_drink(i.name, "", bev))
    combos = tuple(c for c in menu.combos if not contains_drink(c.name, "", bev))
    if not kept:
        return menu   # never strip everything (e.g. a cafe) — fall back to full menu
    return dataclasses.replace(menu, items=kept, combos=combos)


def _veg_only(menu):
    """Keep only items confirmed veg (is_veg is True). Fail safe: items with
    unknown/missing veg metadata are excluded (Swiggy often omits the flag on
    non-veg items). Combos carry no veg metadata, so they're dropped under
    veg-only. May return an empty item list — the caller handles that."""
    import dataclasses
    kept = tuple(i for i in menu.items if i.is_veg is True)
    return dataclasses.replace(menu, items=kept, combos=())


async def _get_menu_cached(client, rid: str, addr: str):
    key = (rid, addr)
    hit = _MENU_CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    raw_menu = await _fetch_full_menu(client, rid, addr)
    search = await _enrich_menu_detail(client, raw_menu, rid, addr)
    menu = parse_menu(raw_menu, search_responses=search, skip_unparseable=True)
    _MENU_CACHE[key] = (time.time() + _MENU_TTL, menu)
    return menu


# ── static UI (mounted last so /api/* wins) ───────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

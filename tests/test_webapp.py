"""Web-layer tests: veg/group-size profiling, request validation, auth gating."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cart_optimizer.adapters.swiggy import classify_veg, parse_menu
from cart_optimizer.models import Cart, Item, ItemLine, Menu, Variant
import webapp.server as srv

FIX = Path(__file__).parent / "fixtures" / "mcdonalds_menu.json"


def _item(suffix, pref, veg, cost=100):
    return Item(
        id=f"itm_{suffix}", name=suffix, preference=pref,
        variants=(Variant(id=f"var_{suffix}", name="Standard", cost=cost),),
        is_veg=veg,
    )


# ── veg classification + filter ────────────────────────────────────────────────

def test_classify_veg_variants():
    assert classify_veg({"isVeg": True}) is True
    assert classify_veg({"isVeg": False}) is False
    assert classify_veg({"isVeg": "2"}) is False
    assert classify_veg({"isVeg": 1}) is True
    assert classify_veg({}) is None


def test_parse_menu_sets_is_veg_from_fixture():
    menu = parse_menu(json.loads(FIX.read_text()), skip_unparseable=True)
    veg = {i.name for i in menu.items if i.is_veg is True}
    assert "McVeggie Burger" in veg
    # Swiggy omits the flag on non-veg items in this payload → unknown, not False.
    mcchicken = next(i for i in menu.items if i.name == "McChicken Burger")
    assert mcchicken.is_veg is None


def test_veg_only_keeps_confirmed_veg_excludes_unknown_and_nonveg():
    menu = Menu(restaurant="r", items=(
        _item("paneer", 0.9, True),
        _item("chicken", 0.9, False),
        _item("mystery", 0.9, None),   # unknown — excluded (fail safe)
    ))
    assert {i.name for i in srv._veg_only(menu).items} == {"paneer"}


def test_veg_only_may_return_empty():
    menu = Menu(restaurant="r", items=(_item("chicken", 0.9, False),))
    assert srv._veg_only(menu).items == ()


# ── group size ─────────────────────────────────────────────────────────────────

def test_main_count_counts_mains_not_sides():
    main = _item("burger", 0.9, True)
    side = _item("fries", 0.3, True)
    cart = Cart((ItemLine(main, main.variants[0]), ItemLine(side, side.variants[0])))
    assert srv._main_count(cart) == 1


# ── endpoints (validation + auth) ──────────────────────────────────────────────

@pytest.fixture
def client():
    return TestClient(srv.app)


def test_me_unauthenticated(client):
    r = client.get("/api/me")
    assert r.status_code == 200 and r.json() == {"logged_in": False}


def test_optimize_requires_login(client):
    r = client.post("/api/optimize",
                    json={"restaurantId": "1", "addressId": "1", "budget": 400})
    assert r.status_code == 401


def test_optimize_rejects_bad_budget(client):
    r = client.post("/api/optimize",
                    json={"restaurantId": "1", "addressId": "1", "budget": 0})
    assert r.status_code == 422


def test_optimize_rejects_bad_group_size(client):
    r = client.post("/api/optimize", json={
        "restaurantId": "1", "addressId": "1", "budget": 400, "groupSize": 0})
    assert r.status_code == 422


# ── place-order gating (never reaches place_food_order in these paths) ──────────

def test_place_order_requires_login(client):
    r = client.post("/api/place-order", json={"optionIndex": 0, "confirmed": True})
    assert r.status_code == 401


def test_place_order_requires_confirmed_flag(monkeypatch, client):
    # Bypass auth so we exercise the confirmed-flag gate specifically.
    monkeypatch.setattr(srv, "_token", lambda request: "tok")
    r = client.post("/api/place-order", json={"optionIndex": 0, "confirmed": False})
    assert r.status_code == 400


def test_place_order_without_pending_cart(monkeypatch, client):
    monkeypatch.setattr(srv, "_token", lambda request: "tok")
    r = client.post("/api/place-order", json={"optionIndex": 0, "confirmed": True})
    assert r.status_code == 409  # nothing stashed for this session


# ── public demo mode (no login) ─────────────────────────────────────────────────

def test_demo_optimize_no_login(client):
    r = client.post("/api/optimize", json={
        "restaurantId": "demo", "addressId": "demo", "budget": 400, "demo": True})
    assert r.status_code == 200
    body = r.json()
    assert body["found"] and body["estimated"] and body["demo"]
    assert body["options"] and body["options"][0]["bill"]["to_pay"] <= 400 + 0.5


def test_demo_veg_only_excludes_nonveg(client):
    r = client.post("/api/optimize", json={
        "restaurantId": "demo", "addressId": "demo", "budget": 600,
        "demo": True, "vegOnly": True})
    body = r.json()
    assert body["found"]
    names = " ".join(it["name"] for o in body["options"] for it in o["items"]).lower()
    assert "chicken" not in names      # non-veg filtered out

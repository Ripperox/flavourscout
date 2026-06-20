"""Tests for continuous coupon intelligence: corpus, sweep scheduling, and the
ledger's brand-wide sharing + staleness."""

import asyncio

from cart_optimizer.coupon_ledger import InMemoryCouponLedger, brand_key
from cart_optimizer import coupon_monitor as cm


# ── brand normalization ─────────────────────────────────────────────────────────

def test_brand_key_normalizes():
    assert brand_key("Faasos - Wraps, Rolls & Shawarma (Ad)") == "faasos"
    assert brand_key("McDonald's") == "mcdonald's"
    assert brand_key("Burger King") == "burger king"
    assert brand_key("KFC @ Saki Vihar") == "kfc"


# ── ledger: brand-wide sharing + staleness ──────────────────────────────────────

def test_brand_codes_shared_across_branches():
    led = InMemoryCouponLedger()
    led.set_branch("1", "kfc", "KFC A")
    led.set_branch("2", "kfc", "KFC B")
    led.set_branch("9", "dominos", "Domino's")
    led.record("1", "FLAT75", 75)      # proven at one KFC branch
    led.record("9", "PIZZA50", 50)     # a Domino's code
    assert "FLAT75" in led.brand_codes("kfc")     # shared to the brand
    assert "FLAT75" not in led.brand_codes("dominos")   # no cross-brand leak


def test_record_stamps_last_tested_and_stats():
    led = InMemoryCouponLedger()
    led.record("1", "FLAT75", 75)
    assert led.last_tested_at("1", "FLAT75") > 0
    s = led.stats()
    assert s["working_coupons"] == 1 and s["best_discount"] == 75


# ── corpus + scheduling ─────────────────────────────────────────────────────────

def test_build_corpus_brand_first_then_seed_then_suggested():
    led = InMemoryCouponLedger()
    led.set_branch("1", "kfc", "KFC")
    led.record("1", "BRANDWIN", 60)
    corpus = cm.build_corpus(led, "kfc", suggested="SUGGESTED")
    assert corpus[0] == "BRANDWIN"            # brand-proven first
    assert "SWIGGYIT" in corpus               # seed included
    assert corpus[-1] == "SUGGESTED"          # suggested last
    assert len(corpus) == len(set(corpus))    # de-duped


def test_codes_to_sweep_skips_fresh_includes_stale(monkeypatch):
    led = InMemoryCouponLedger()
    led.record("1", "FRESH", 10)              # last_tested ~ now
    now = led.last_tested_at("1", "FRESH")
    picks = cm.codes_to_sweep(led, "1", ["FRESH", "NEVER"], ttl=3600, now=now + 5)
    assert picks == ["NEVER"]                  # FRESH skipped, NEVER (untested) included


def test_codes_to_sweep_respects_budget():
    led = InMemoryCouponLedger()
    picks = cm.codes_to_sweep(led, "1", ["A", "B", "C", "D"], budget=2)
    assert picks == ["A", "B"]


# ── the sweep itself (fake async client) ────────────────────────────────────────

class FakeClient:
    """Scripts get_food_cart bills; raises on invalid coupons like the real API."""

    def __init__(self, base_to_pay, working):
        self.base = base_to_pay
        self.working = working            # {code: discount}
        self.calls = []
        self._applied = None

    async def call(self, tool, **kw):
        self.calls.append((tool, kw))
        if tool == "flush_food_cart":
            self._applied = None
            return {}
        if tool == "update_food_cart":
            return {}
        if tool == "apply_food_coupon":
            code = kw["couponCode"]
            if code not in self.working:
                raise RuntimeError("invalid coupon")
            self._applied = code
            return {}
        if tool == "get_food_cart":
            if self._applied:
                disc = self.working[self._applied]
                return {"data": {"pricing": {"to_pay": self.base - disc, "item_total": self.base},
                                 "offers": {"coupon_applied": self._applied, "coupon_discount": disc},
                                 "items": [{}]},
                        "availablePaymentMethods": ["cash"]}
            return {"data": {"pricing": {"to_pay": self.base, "item_total": self.base},
                             "offers": {}, "items": [{}]},
                    "availablePaymentMethods": ["cash"]}
        return {}


def test_sweep_records_working_and_dead_codes_and_never_orders():
    led = InMemoryCouponLedger()
    client = FakeClient(base_to_pay=300, working={"FLAT75": 75})
    cart_items = [{"menu_item_id": "1", "quantity": 1}]

    found = asyncio.run(cm.sweep_branch(
        client, "rid1", "KFC - Saki Vihar", "addr1", cart_items, led, budget=6))

    assert found == 1
    assert "FLAT75" in led.known("rid1")                 # proven code remembered
    assert led.stats()["working_coupons"] == 1
    tools = [t for t, _ in client.calls]
    assert "place_food_order" not in tools               # NEVER places an order
    assert tools.count("flush_food_cart") >= 2           # flushed at start and end
    assert led._branches["rid1"]["brand"] == "kfc"       # branch→brand recorded


def test_sweep_noop_without_cart_items():
    led = InMemoryCouponLedger()
    client = FakeClient(base_to_pay=300, working={})
    found = asyncio.run(cm.sweep_branch(client, "rid1", "KFC", "addr1", [], led))
    assert found == 0 and client.calls == []

# 🍽️ FlavourScout

**Tell it your budget. It hands you the best-value food order you can get for that money — coupons and all.**

### 🌐 [**Try it live → flavourscout.onrender.com**](https://flavourscout.onrender.com/)

You pick a restaurant and a number (say ₹400). FlavourScout figures out the
*highest-value cart that lands just under your budget* — the real total, after the
best working coupon, delivery, and taxes. No more juggling items in your head or
hunting for codes that turn out to be expired.

> 🧪 **No sign-in needed:** open the live link (or run it locally) and click
> **Connect Swiggy** — while real Swiggy login is being set up, it opens a live
> demo that runs the real engine on a sample menu.
> *(First load can take ~30s — the free host wakes from sleep.)*

---

## ✨ What you get

- 🎯 **The best cart for your budget** — not just *a* cart that fits, the one with
  the most value, landing just under your limit.
- 🎟️ **Coupons found & proven for you** — it discovers working codes and checks
  them against the *real* bill. No typing codes, no "this coupon isn't valid 😞".
- 🥗 **Made your way** — veg-only, "for N people", and an optional drink (added the
  smart way — usually by turning a burger into a meal, not bolting on a soda).
- 🧾 **Honest totals** — every price is the platform's *real* checkout total, not a guess.
- 🛵 **Order in one tap** — when you're happy, place it (Cash on Delivery) after a
  clear confirmation. It never orders behind your back.

---

## 🚀 Run it in 60 seconds

```bash
git clone https://github.com/Ripperox/flavourscout.git
cd flavourscout

python3 -m venv venv
venv/bin/pip install -r requirements.txt

venv/bin/uvicorn webapp.server:app --port 8000
```

Open **http://localhost:8000** → click **"Try the demo — no login"**, set a budget,
and watch it build the cart. ✨

*(To use it on your own Swiggy account you need Swiggy's MCP/builder access — the
demo needs none.)*

### 🐳 Or with Docker

```bash
docker build -t flavourscout .
docker run -p 8000:8000 -e SESSION_SECRET=$(openssl rand -hex 16) flavourscout
```

Open http://localhost:8000. (Add `-v flavourscout-data:/data` to keep the coupon
ledger + logins across restarts.)

---

## 🧠 The clever bit (in plain English)

Finding the best cart sounds easy — until coupons enter the picture. A coupon like
*"₹100 off above ₹199"* means **adding an item can make your order cheaper**. That
breaks normal "fill the basket" logic.

FlavourScout solves it *exactly* (it's a coupon-aware knapsack problem), so the
answer is genuinely optimal — not a good guess. And to be sure the math is right,
every result is cross-checked against a brute-force solver in the tests. ✅

It also keeps a **shared coupon brain**: a code proven to work at one outlet is
tried first at every branch of that chain, and re-checked over time so expired
ones drop off.

---

## 🔒 Safety, by design

- It **never places an order on its own** — only when *you* tap *Place Order* and
  confirm. The confirmation spells out that Cash-on-Delivery can't be cancelled.
- While searching, it only *reads* prices and clears any test cart afterwards.
- Coupons are discovered for you — you're never asked to paste codes.

---

## 🛠️ Under the hood (for the curious)

A FastAPI backend + a single-page UI, with the optimizer as a clean, tested core.

| Area | Where |
|---|---|
| Exact coupon-aware optimizer (the core IP) | `cart_optimizer/optimizer.py` |
| Brute-force oracle (proves the optimizer in tests) | `cart_optimizer/brute_force.py` |
| Shared money math (one source of truth) | `cart_optimizer/pricing.py` |
| Continuous coupon discovery + per-branch ledger | `cart_optimizer/coupon_monitor.py`, `coupon_ledger.py` |
| Resilient live client (rate-limit + retry aware) | `cart_optimizer/swiggy_client.py` |
| Web app (optimize, profiling, demo, ordering, auth) | `webapp/server.py` |
| The UI | `webapp/static/index.html` |

**Tested:** 500 tests, including the optimizer-vs-oracle equivalence and the web
layer's validation, profiling, and order-safety gating.

```bash
venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest          # 500 passing
```

Deploying (Docker / Render, env vars, the OAuth note): see **[`DEPLOY.md`](DEPLOY.md)**.

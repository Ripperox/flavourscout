# Deploying BudgetBite

A FastAPI backend + static UI. Each visitor logs into their own Swiggy account
(OAuth 2.1 + PKCE); the backend runs the optimize → live-verify pipeline,
personalizes (veg / group size / drinks), continuously proves coupons per branch
(shared ledger), and can place the order on explicit confirmation.

> **Safety:** the optimizer never places orders. `place_food_order` runs *only*
> from an explicit, confirmed user click (Cash on Delivery, with a
> "can't be cancelled" warning). All coupon/price probing flushes the cart after
> every probe.

## Run locally

```bash
python3 -m venv venv && venv/bin/pip install -r requirements-dev.txt
venv/bin/python -m pytest                                  # 498 tests
venv/bin/uvicorn webapp.server:app --reload --port 8000    # http://localhost:8000
```

The local OAuth redirect URI is `http://localhost:8000/callback` (derived from
the request automatically).

## Deploy to Render (free tier, persistent disk)

The repo is on GitHub (`Cart-Optimization/knapsack-logic`, default branch `main`).

1. **Render → New → Blueprint**, point it at the repo. It reads `render.yaml` and
   creates the Docker web service **budgetbite** + a 1 GB disk at `/data`.
2. First deploy gives a URL like `https://budgetbite-xxxx.onrender.com`.
3. Set the **`BASE_URL`** env var to that exact https URL and redeploy (so the
   OAuth `redirect_uri` matches). This is the **only** var you must set by hand.
4. Open the URL → **Login with Swiggy**.

Auto-configured by `render.yaml`: `SESSION_SECRET` (generated), `COUPON_DB` and
`SESSION_FILE` (both on the persistent disk, so the coupon ledger **and** logins
survive restarts). `SWIGGY_CLIENT_ID` is optional (see OAuth note).

### Any other Docker host (Fly / Railway / …)
Point it at the `Dockerfile`, set `BASE_URL` to the public URL, and mount a volume
at `/data` (or accept that the ledger + sessions reset on redeploy).

## ⚠️ The one thing that needs your live test: Swiggy OAuth

The OAuth flow is built to Swiggy's published metadata, but a real token exchange
on a **public URL** hasn't been confirmed end-to-end (local login works). On first
deploy, verify:

1. **redirect_uri match** — must exactly equal `${BASE_URL}/callback`. Mismatch →
   error on callback. Fix `BASE_URL` and redeploy.
2. **Dynamic client registration** — the app tries RFC 7591 registration at
   `https://mcp.swiggy.com/auth/register`. If Swiggy rejects it, register a client
   out-of-band (or reuse the one Claude's `/mcp` uses) and set `SWIGGY_CLIENT_ID`.

If live login is flaky, fall back to a recorded walkthrough of the local app for
the showcase — everything after login is already validated against live Swiggy.

## What's shared vs per-user
- **Per-user:** the OAuth token (server-side session, keyed by an opaque `sid`
  cookie; tokens never reach the browser).
- **Shared:** the coupon ledger (`/data/coupons.db`) — a code proven at one branch
  is tried first for everyone there, and **brand-wide** across that chain.

> Single-instance by design (in-process caches + background coupon sweeps). The
> free tier's one instance is the right fit; don't scale to multiple instances
> without moving sessions/ledger to a shared store.

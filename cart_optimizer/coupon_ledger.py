"""Per-branch coupon ledger.

A Swiggy ``restaurantId`` identifies a specific *branch* (the Saki Vihar
McDonald's has a different id from every other McDonald's). Coupons are issued at
the branch level, so a code that worked for one user at a branch will, ~90% of
the time, work again at that same branch. This ledger remembers which codes have
actually produced a discount at each branch, so the next run probes those first
(and rarely misses a good coupon).

Design:
- ``known(restaurant_id)`` → codes that have yielded a real discount here,
  best-first (highest discount seen). These are tried before the generic
  candidate list.
- ``record(restaurant_id, code, discount)`` → log an application result.
  discount > 0 reinforces the code; discount == 0 (failed/no-help) is tracked
  too so persistently-dead codes can be pruned.

The live verifier ALWAYS re-validates against Swiggy, so a stale (expired) code
in the ledger is harmless — applying it just fails and costs one call. ``known``
already drops codes whose recent attempts only ever fail.

``CouponLedger`` is a Protocol so the verifier is testable with an in-memory
fake; ``JsonCouponLedger`` persists to disk for the real app.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Protocol

__all__ = [
    "CouponLedger",
    "InMemoryCouponLedger",
    "JsonCouponLedger",
    "SqliteCouponLedger",
    "PRUNE_AFTER_MISSES",
    "brand_key",
]


def brand_key(restaurant_name: str) -> str:
    """Normalize a restaurant name to a brand, so a coupon proven at one branch
    can be tried at every branch of the same chain. e.g.
    "Faasos - Wraps, Rolls & Shawarma (Ad)" -> "faasos";  "McDonald's" -> "mcdonald's"."""
    name = str(restaurant_name or "").lower().strip()
    for sep in (" - ", " – ", " | ", " @ "):   # drop the location/descriptor tail
        if sep in name:
            name = name.split(sep, 1)[0]
    for cut in ("(ad)", "(", "[", ","):         # drop trailing tags/descriptors
        if cut in name:
            name = name.split(cut, 1)[0]
    return " ".join(name.split())               # collapse whitespace

# A code with zero hits and at least this many misses is considered dead and is
# no longer returned by known() (it stays recorded so we don't keep re-adding it).
PRUNE_AFTER_MISSES = 3


class CouponLedger(Protocol):
    def known(self, restaurant_id: str) -> list[str]: ...
    def record(self, restaurant_id: str, code: str, discount: float) -> None: ...


def _rank(entries: dict[str, dict]) -> list[str]:
    """Codes worth trying, best-first: any with a hit (by best_discount, then
    hits), excluding never-worked codes that have missed too many times."""
    alive = [
        (code, e)
        for code, e in entries.items()
        if e.get("hits", 0) > 0 or e.get("misses", 0) < PRUNE_AFTER_MISSES
    ]
    alive.sort(
        key=lambda ce: (ce[1].get("hits", 0) > 0,
                        ce[1].get("best_discount", 0.0),
                        ce[1].get("hits", 0)),
        reverse=True,
    )
    return [code for code, _ in alive]


class InMemoryCouponLedger:
    """Non-persistent ledger (tests, or a single run)."""

    def __init__(self, data: dict[str, dict[str, dict]] | None = None) -> None:
        # {restaurant_id: {code: {"hits": int, "misses": int, "best_discount": float,
        #                         "last_tested": float}}}
        self._data: dict[str, dict[str, dict]] = data or {}
        # {restaurant_id: {"brand": str, "name": str}}  — for cross-branch sharing.
        self._branches: dict[str, dict] = {}

    def known(self, restaurant_id: str) -> list[str]:
        return _rank(self._data.get(str(restaurant_id), {}))

    def record(self, restaurant_id: str, code: str, discount: float) -> None:
        if not code:
            return
        branch = self._data.setdefault(str(restaurant_id), {})
        entry = branch.setdefault(
            code, {"hits": 0, "misses": 0, "best_discount": 0.0, "last_tested": 0.0})
        if discount and discount > 0:
            entry["hits"] += 1
            entry["best_discount"] = max(entry["best_discount"], float(discount))
        else:
            entry["misses"] += 1
        entry["last_tested"] = time.time()

    # ── coupon-intelligence extensions ────────────────────────────────────────
    def set_branch(self, restaurant_id: str, brand: str, name: str = "") -> None:
        self._branches[str(restaurant_id)] = {"brand": brand, "name": name}

    def brand_codes(self, brand: str) -> list[str]:
        """Every code ever proven (hits > 0) at ANY branch of this brand."""
        codes: set[str] = set()
        for rid, meta in self._branches.items():
            if meta.get("brand") != brand:
                continue
            for code, e in self._data.get(rid, {}).items():
                if e.get("hits", 0) > 0:
                    codes.add(code)
        return sorted(codes)

    def last_tested_at(self, restaurant_id: str, code: str) -> float:
        return self._data.get(str(restaurant_id), {}).get(code, {}).get("last_tested", 0.0)

    def stats(self) -> dict:
        branches = set(self._data) | set(self._branches)
        working = {(rid, c) for rid, cs in self._data.items()
                   for c, e in cs.items() if e.get("hits", 0) > 0}
        best = max((e.get("best_discount", 0.0)
                    for cs in self._data.values() for e in cs.values()), default=0.0)
        return {"branches": len(branches),
                "working_coupons": len(working),
                "codes_tracked": sum(len(cs) for cs in self._data.values()),
                "best_discount": round(best)}

    @property
    def data(self) -> dict[str, dict[str, dict]]:
        return self._data


class JsonCouponLedger(InMemoryCouponLedger):
    """Ledger persisted to a JSON file. Loads on construction, writes on every
    record (small file; simplicity over write-batching). Corrupt/missing file
    starts empty."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        data: dict = {}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError):
                data = {}
        super().__init__(data)

    def record(self, restaurant_id: str, code: str, discount: float) -> None:
        super().record(restaurant_id, code, discount)
        self._flush()

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2, sort_keys=True))


class SqliteCouponLedger:
    """SQLite-backed ledger SHARED across every user of one backend.

    This is the multi-user version: a coupon discovered by ANY user at a branch
    immediately helps EVERY other user ordering from that same branch. Same
    CouponLedger interface as the in-memory/JSON variants, so the verifier is
    unchanged. Safe for concurrent web requests (one connection, a write lock,
    and SQLite's own locking). Swap to Postgres later by reimplementing these two
    methods against the same protocol.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS coupons (
                restaurant_id TEXT NOT NULL,
                code          TEXT NOT NULL,
                hits          INTEGER NOT NULL DEFAULT 0,
                misses        INTEGER NOT NULL DEFAULT 0,
                best_discount REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY (restaurant_id, code)
            )
            """
        )
        # Branch → brand map, so a code proven at one branch is tried brand-wide.
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS branches (
                restaurant_id TEXT PRIMARY KEY,
                brand         TEXT,
                name          TEXT
            )
            """
        )
        # Migration: add last_tested to pre-existing coupons tables.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(coupons)").fetchall()}
        if "last_tested" not in cols:
            self._conn.execute("ALTER TABLE coupons ADD COLUMN last_tested REAL NOT NULL DEFAULT 0")
        self._conn.commit()

    def known(self, restaurant_id: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT code FROM coupons
                WHERE restaurant_id = ?
                  AND (hits > 0 OR misses < ?)
                ORDER BY (hits > 0) DESC, best_discount DESC, hits DESC
                """,
                (str(restaurant_id), PRUNE_AFTER_MISSES),
            ).fetchall()
        return [r[0] for r in rows]

    def record(self, restaurant_id: str, code: str, discount: float) -> None:
        if not code:
            return
        rid = str(restaurant_id)
        hit = bool(discount and discount > 0)
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO coupons (restaurant_id, code, hits, misses, best_discount, last_tested)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(restaurant_id, code) DO UPDATE SET
                    hits          = hits   + ?,
                    misses        = misses + ?,
                    best_discount = MAX(best_discount, ?),
                    last_tested   = ?
                """,
                (rid, code, 1 if hit else 0, 0 if hit else 1, float(discount) if hit else 0.0, now,
                 1 if hit else 0, 0 if hit else 1, float(discount) if hit else 0.0, now),
            )
            self._conn.commit()

    # ── coupon-intelligence extensions ────────────────────────────────────────
    def set_branch(self, restaurant_id: str, brand: str, name: str = "") -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO branches (restaurant_id, brand, name) VALUES (?, ?, ?)
                ON CONFLICT(restaurant_id) DO UPDATE SET brand = ?, name = ?
                """,
                (str(restaurant_id), brand, name, brand, name),
            )
            self._conn.commit()

    def brand_codes(self, brand: str) -> list[str]:
        """Every code ever proven (hits > 0) at ANY branch of this brand."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT c.code FROM coupons c
                JOIN branches b ON b.restaurant_id = c.restaurant_id
                WHERE b.brand = ? AND c.hits > 0
                ORDER BY c.best_discount DESC
                """,
                (brand,),
            ).fetchall()
        return [r[0] for r in rows]

    def last_tested_at(self, restaurant_id: str, code: str) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_tested FROM coupons WHERE restaurant_id = ? AND code = ?",
                (str(restaurant_id), code),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def stats(self) -> dict:
        with self._lock:
            branches = self._conn.execute(
                "SELECT COUNT(DISTINCT restaurant_id) FROM "
                "(SELECT restaurant_id FROM coupons UNION SELECT restaurant_id FROM branches)"
            ).fetchone()[0]
            working = self._conn.execute(
                "SELECT COUNT(*) FROM coupons WHERE hits > 0").fetchone()[0]
            tracked = self._conn.execute("SELECT COUNT(*) FROM coupons").fetchone()[0]
            best = self._conn.execute(
                "SELECT COALESCE(MAX(best_discount), 0) FROM coupons").fetchone()[0]
        return {"branches": branches, "working_coupons": working,
                "codes_tracked": tracked, "best_discount": round(best)}

    def all_branches(self) -> dict[str, list[str]]:
        """Every branch → its known codes (for an admin/coupons view in the UI)."""
        with self._lock:
            rids = [r[0] for r in self._conn.execute(
                "SELECT DISTINCT restaurant_id FROM coupons").fetchall()]
        return {rid: self.known(rid) for rid in rids}

# Test fixtures — captured live Swiggy MCP responses

Captured 2026-06-13 from the `swiggy-food` MCP server (`https://mcp.swiggy.com/food`)
for **McDonald's, Saki Vihar Powai, Mumbai** (restaurantId `668678`), delivering
to a Chandivali address. Trimmed to a representative subset of **real** items —
ids, prices, and flags are verbatim from the API; no values were invented.

| File | Source tool | Captures |
|---|---|---|
| `mcdonalds_menu.json` | `get_restaurant_menu` | compact category/item shape: float prices, `inStock`, `rating`, `hasVariants`/`hasAddons`, same item id repeating across categories |
| `mcdonalds_search_addons.json` | `search_menu` | detailed item with real add-on groups (`groupId`/`choices`/`maxAddons`) |

Notes on the real data that drove the adapter design:
- Swiggy item/group/choice ids are **bare numbers**; the adapter prefixes them
  (`itm_`/`grp_`/`opt_`) to fit the optimizer's typed-id scheme.
- This restaurant models sizes (Fries R/M/L) as **separate items**, so no item
  here carries a `variantsV2`/`variations` block — the variant path is guarded,
  not guessed.
- `fetch_food_coupons` returned `{}` (no blanket coupons at capture time), so the
  coupon shape is not yet captured; `parse_coupons` handles empty and refuses to
  guess a non-empty shape.

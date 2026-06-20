"""Bundled sample menu for the no-login public demo.

The demo runs the REAL optimizer + profiling + coupon-aware logic on fixed data,
so anyone can try BudgetBite without a Swiggy login. Totals are *estimated* from
the internal fee model — the live app reads the platform's authoritative bill
instead. Kept separate from cart_optimizer.mock_data (which tests pin)."""

from cart_optimizer.models import Menu

DEMO_RESTAURANT = "Pizza Palace (demo)"

DEMO_MENU_DICT = {
    "restaurant": DEMO_RESTAURANT,
    "items": {
        "itm_margherita": {
            "name": "Margherita Pizza", "preference": 0.9, "is_veg": True,
            "variants": {"var_marg_reg": {"name": "Regular", "cost": 199},
                         "var_marg_lrg": {"name": "Large", "cost": 299}},
            "addons": {"grp_top": {"name": "Extra toppings", "min": 0, "max": 2, "options": {
                "opt_cheese": {"name": "Cheese Burst", "cost": 60, "preference": 0.12},
                "opt_jalapeno": {"name": "Jalapeños", "cost": 30, "preference": 0.08}}}},
        },
        "itm_farmhouse": {
            "name": "Farmhouse Pizza", "preference": 0.85, "is_veg": True,
            "variants": {"var_farm_reg": {"name": "Regular", "cost": 249},
                         "var_farm_lrg": {"name": "Large", "cost": 349}},
        },
        "itm_pepperoni": {
            "name": "Chicken Pepperoni Pizza", "preference": 0.92, "is_veg": False,
            "variants": {"var_pep_reg": {"name": "Regular", "cost": 279},
                         "var_pep_lrg": {"name": "Large", "cost": 379}},
        },
        "itm_wings": {"name": "Chicken Wings (6 pc)", "preference": 0.7,
                      "cost": 199, "is_veg": False},
        "itm_garlic_bread": {"name": "Garlic Bread", "preference": 0.45,
                             "cost": 120, "is_veg": True},
        "itm_choco_lava": {"name": "Choco Lava Cake", "preference": 0.4,
                           "cost": 99, "is_veg": True},
        "itm_pepsi": {"name": "Pepsi", "preference": 0.3, "cost": 57,
                      "max_quantity": 4, "is_veg": True},
    },
    "combos": {
        "cmb_pizza_meal": {
            "name": "Margherita + Pepsi Combo", "cost": 239, "preference": 0.95,
            "composition": {"itm_margherita": 1, "itm_pepsi": 1},
            "description": "Margherita + Pepsi at a bundle price",
        },
    },
    "offers": {
        "off_flat100": {"kind": "flat", "value": 100, "query": "subtotal >= 199",
                        "description": "₹100 off above ₹199"},
        "off_pizza30": {"kind": "percent", "value": 30, "cap": 120,
                        "query": "select_subtotal >= 199",
                        "applies_to": ["itm_margherita", "itm_farmhouse", "itm_pepperoni"],
                        "description": "30% off pizzas (max ₹120)"},
        "off_freedel": {"kind": "free_delivery", "query": "subtotal >= 99",
                        "description": "Free delivery above ₹99"},
    },
}


def demo_menu() -> Menu:
    return Menu.from_dict(DEMO_MENU_DICT)

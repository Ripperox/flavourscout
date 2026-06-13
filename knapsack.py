# 1. The Mock Swiggy Data
MENU_ITEMS = {
    "itm_8f3k9d": {
        "name": "Chicken Pepperoni Pizza",
        "preference": 0.85, # Value
        "available": True,
        "variants": {"var_a19f": {"cost": 288}} # Weight
    },
    "itm_aa1": {
        "name": "Garlic Bread",
        "preference": 0.60,
        "available": True,
        "variants": {"var_base": {"cost": 120}}
    },
    "itm_bb2": {
        "name": "Thums Up",
        "preference": 0.40,
        "available": True,
        "variants": {"var_can": {"cost": 60}}
    },
    "itm_cc3": {
        "name": "Choco Lava Cake",
        "preference": 0.70,
        "available": True,
        "variants": {"var_base": {"cost": 110}}
    }
}

# 2. Your Tabulation Function (Slightly modified to return the full DP table)
def solve_knapsack_tabulation(W, weight, value, n):
    dp = [[0 for _ in range(W + 1)] for _ in range(n)]
    
    for cap in range(weight[0], W + 1):
        dp[0][cap] = value[0]
        
    for ind in range(1, n):
        for cap in range(W + 1):
            not_pick = dp[ind - 1][cap]
            pick = float('-inf')
            if weight[ind] <= cap:
                pick = value[ind] + dp[ind - 1][cap - weight[ind]]
            dp[ind][cap] = max(pick, not_pick)
            
    return dp # Returning the whole table so we can backtrack

# 3. The Swiggy Adapter
def build_optimized_cart(budget, menu):
    item_ids = []
    names = []
    weights = [] # Costs
    values = []  # Preferences
    
    # Preprocessor: Flatten the dictionary into lists
    for item_id, data in menu.items():
        if data['available']:
            item_ids.append(item_id)
            names.append(data['name'])
            values.append(data['preference'])
            
            # Get cost of the first variant
            first_variant = list(data['variants'].values())[0]
            weights.append(first_variant['cost'])
            
    n = len(weights)
    
    # Run the DP Engine
    dp_table = solve_knapsack_tabulation(budget, weights, values, n)
    max_preference = dp_table[n-1][budget]
    
    # Backtracker: Find out WHICH items were selected
    selected_items = []
    total_cost = 0
    current_capacity = budget
    
    for ind in range(n - 1, 0, -1):
        # If the value came from the row above, we didn't pick it
        if dp_table[ind][current_capacity] != dp_table[ind - 1][current_capacity]:
            # We picked it!
            selected_items.append(names[ind])
            total_cost += weights[ind]
            current_capacity -= weights[ind]
            
    # Check the 0th item base case
    if dp_table[0][current_capacity] > 0:
        selected_items.append(names[0])
        total_cost += weights[0]
        
    return {
        "max_preference_score": round(max_preference, 2),
        "total_cost": total_cost,
        "items_in_cart": selected_items
    }

# --- TEST IT ---
USER_BUDGET = 300

print(f"--- OPTIMIZING FOR BUDGET: ₹{USER_BUDGET} ---")
result = build_optimized_cart(USER_BUDGET, MENU_ITEMS)

print(f"Best Preference Score: {result['max_preference_score']}")
print(f"Total Cart Cost: ₹{result['total_cost']}")
print("Items Selected:")
for item in result['items_in_cart']:
    print(f" - {item}")
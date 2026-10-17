"""Ingredients, purchases, stock, recipes, dish costing and profit."""
from datetime import timedelta

from . import db
from .utils import now_iso, today

UNITS = {"kg": "كغ", "g": "غ", "l": "لتر", "ml": "مل", "piece": "حبة", "pack": "علبة"}


# ---------------------------------------------------------------- ingredients & stock
def unit_costs():
    """Average cost per unit (in cents) from all purchases; falls back to the default cost."""
    bought = {r["ingredient_id"]: r for r in db.query(
        "SELECT ingredient_id, SUM(cost_cents) AS cost, SUM(qty) AS qty FROM purchases GROUP BY ingredient_id")}
    out = {}
    for ing in db.query("SELECT id, default_cost FROM ingredients"):
        b = bought.get(ing["id"])
        out[ing["id"]] = (b["cost"] / b["qty"]) if b and b["qty"] else float(ing["default_cost"] or 0)
    return out


def stock_levels():
    return {r["ingredient_id"]: float(r["qty"] or 0) for r in db.query(
        "SELECT ingredient_id, SUM(qty) AS qty FROM stock_moves GROUP BY ingredient_id")}


def ingredients():
    costs, stock = unit_costs(), stock_levels()
    rows = []
    for ing in db.query("SELECT * FROM ingredients ORDER BY name"):
        qty = stock.get(ing["id"], 0.0)
        cost = costs.get(ing["id"], 0.0)
        rows.append({**ing, "stock": qty, "unit_cost": cost, "value": max(qty, 0) * cost,
                     "unit_label": UNITS.get(ing["unit"], ing["unit"]),
                     "low": ing["low_stock"] > 0 and qty <= ing["low_stock"]})
    return rows


def add_ingredient(name, unit, default_cost_cents, low_stock, notes=""):
    new_id = db.insert("ingredients", {"name": name, "unit": unit if unit in UNITS else "kg",
                                       "default_cost": default_cost_cents, "low_stock": low_stock,
                                       "notes": notes, "created_at": now_iso()})
    db.commit()
    return new_id


def delete_ingredient(ingredient_id):
    for table in ("recipe_items", "stock_moves", "purchases"):
        db.execute(f"DELETE FROM {table} WHERE ingredient_id = ?", (ingredient_id,))
    db.execute("DELETE FROM ingredients WHERE id = ?", (ingredient_id,))
    db.commit()


def add_purchase(purchase_date, ingredient_id, qty, cost_cents, supplier="", note="", user_id=None):
    stamp = now_iso()
    pid = db.insert("purchases", {"purchase_date": purchase_date, "ingredient_id": ingredient_id, "qty": qty,
                                  "cost_cents": cost_cents, "supplier": supplier, "note": note,
                                  "created_by": user_id, "created_at": stamp})
    db.insert("stock_moves", {"ingredient_id": ingredient_id, "qty": qty, "reason": "purchase",
                              "purchase_id": pid, "note": supplier, "created_at": stamp})
    db.commit()
    return pid


def delete_purchase(purchase_id):
    db.execute("DELETE FROM stock_moves WHERE purchase_id = ?", (purchase_id,))
    db.execute("DELETE FROM purchases WHERE id = ?", (purchase_id,))
    db.commit()


def recent_purchases(limit=30):
    return db.query("SELECT p.*, i.name AS ingredient, i.unit FROM purchases p JOIN ingredients i ON i.id = p.ingredient_id "
                    "ORDER BY p.purchase_date DESC, p.id DESC LIMIT ?", (limit,))


def adjust_stock(ingredient_id, counted, note="جرد"):
    """Stocktake: record the difference between the counted and the expected quantity."""
    current = stock_levels().get(ingredient_id, 0.0)
    diff = round(counted - current, 4)
    if diff:
        db.insert("stock_moves", {"ingredient_id": ingredient_id, "qty": diff, "reason": "adjust",
                                  "note": note, "created_at": now_iso()})
        db.commit()
    return diff


# ---------------------------------------------------------------- recipes & dish costs
def recipe(menu_item_id):
    costs = unit_costs()
    rows = db.query("SELECT r.*, i.name, i.unit FROM recipe_items r JOIN ingredients i ON i.id = r.ingredient_id "
                    "WHERE r.menu_item_id = ? ORDER BY i.name", (menu_item_id,))
    return [{**r, "unit_label": UNITS.get(r["unit"], r["unit"]), "cost": r["qty"] * costs.get(r["ingredient_id"], 0)}
            for r in rows]


def set_recipe_line(menu_item_id, ingredient_id, qty):
    db.execute("DELETE FROM recipe_items WHERE menu_item_id = ? AND ingredient_id = ?", (menu_item_id, ingredient_id))
    if qty > 0:
        db.insert("recipe_items", {"menu_item_id": menu_item_id, "ingredient_id": ingredient_id, "qty": qty})
    db.commit()


def dish_costs(target_food_cost_pct):
    """Cost, profit and suggested price for every dish."""
    costs = unit_costs()
    lines = {}
    for r in db.query("SELECT menu_item_id, ingredient_id, qty FROM recipe_items"):
        lines.setdefault(r["menu_item_id"], []).append(r["qty"] * costs.get(r["ingredient_id"], 0))
    out = []
    for m in db.query("SELECT * FROM menu_items ORDER BY sort_order, id"):
        ing_cost = sum(lines.get(m["id"], []))
        total = ing_cost + (m["extra_cost_cents"] or 0)
        price = m["price_cents"]
        profit = price - total
        out.append({
            **m,
            "has_recipe": m["id"] in lines,
            "ingredient_cost": ing_cost,
            "total_cost": total,
            "profit": profit,
            "margin": (profit / price * 100) if price else 0,
            "cost_pct": (total / price * 100) if price else 0,
            "suggested": (total / (target_food_cost_pct / 100)) if target_food_cost_pct and total else 0,
        })
    return out


# ---------------------------------------------------------------- order stock deduction
def sync_order_stock(order_id):
    """Make stock moves for an order match its state: deducted once it is delivered, otherwise none."""
    db.execute("DELETE FROM stock_moves WHERE order_id = ?", (order_id,))
    order = db.query("SELECT id, status, number FROM orders WHERE id = ?", (order_id,), one=True)
    if order and order["status"] == "done":
        need = {}
        for it in db.query("SELECT menu_item_id, qty FROM order_items WHERE order_id = ? AND menu_item_id IS NOT NULL",
                           (order_id,)):
            for r in db.query("SELECT ingredient_id, qty FROM recipe_items WHERE menu_item_id = ?", (it["menu_item_id"],)):
                need[r["ingredient_id"]] = need.get(r["ingredient_id"], 0) + r["qty"] * it["qty"]
        stamp = now_iso()
        for ingredient_id, qty in need.items():
            db.insert("stock_moves", {"ingredient_id": ingredient_id, "qty": -qty, "reason": "order",
                                      "order_id": order_id, "note": f"طلب #{order['number']}", "created_at": stamp})
    db.commit()


def ingredients_needed(day):
    """Ingredients required for the day's orders that are not yet delivered, against current stock."""
    stock = stock_levels()
    need = {}
    rows = db.query(
        "SELECT oi.menu_item_id, oi.qty FROM order_items oi JOIN orders o ON o.id = oi.order_id "
        "WHERE o.order_date = ? AND o.status IN ('new', 'confirmed', 'preparing', 'ready') "
        "AND oi.menu_item_id IS NOT NULL", (day,))
    for it in rows:
        for r in db.query("SELECT ingredient_id, qty FROM recipe_items WHERE menu_item_id = ?", (it["menu_item_id"],)):
            need[r["ingredient_id"]] = need.get(r["ingredient_id"], 0) + r["qty"] * it["qty"]
    if not need:
        return []
    names = {i["id"]: i for i in db.query("SELECT id, name, unit FROM ingredients")}
    out = []
    for ing_id, qty in need.items():
        ing = names.get(ing_id)
        if not ing:
            continue
        have = stock.get(ing_id, 0.0)
        out.append({"name": ing["name"], "unit_label": UNITS.get(ing["unit"], ing["unit"]),
                    "need": qty, "have": have, "short": max(qty - have, 0)})
    return sorted(out, key=lambda r: (-r["short"], r["name"]))


# ---------------------------------------------------------------- profit
def profit_report(days, target_food_cost_pct):
    end = today()
    start = end - timedelta(days=days - 1)
    dish = {d["id"]: d for d in dish_costs(target_food_cost_pct)}
    orders = db.query("SELECT id, delivery_fee_cents, fulfillment FROM orders WHERE order_date >= ? AND order_date <= ? "
                      "AND status != 'cancelled'", (start.isoformat(), end.isoformat()))
    ids = [o["id"] for o in orders]
    revenue = cost = uncosted = 0
    per_dish = {}
    if ids:
        marks = ", ".join("?" for _ in ids)
        for it in db.query(f"SELECT * FROM order_items WHERE order_id IN ({marks})", ids):
            line = it["unit_price_cents"] * it["qty"]
            revenue += line
            d = dish.get(it["menu_item_id"])
            row = per_dish.setdefault(it["name"], {"name": it["name"], "qty": 0, "revenue": 0, "cost": 0, "known": True})
            row["qty"] += it["qty"]
            row["revenue"] += line
            if d and d["has_recipe"]:
                c = d["total_cost"] * it["qty"]
                cost += c
                row["cost"] += c
            else:
                uncosted += line
                row["known"] = False
    fees = sum(o["delivery_fee_cents"] for o in orders if o["fulfillment"] == "delivery")
    spent = db.scalar("SELECT SUM(cost_cents) AS n FROM purchases WHERE purchase_date >= ? AND purchase_date <= ?",
                      (start.isoformat(), end.isoformat())) or 0
    for r in per_dish.values():
        r["profit"] = r["revenue"] - r["cost"]
    return {
        "start": start, "end": end, "days": days, "orders": len(orders),
        "revenue": revenue, "fees": fees, "cost": cost, "gross_profit": revenue - cost,
        # Margin only over dishes whose cost is known, so missing recipes don't inflate it
        "margin": ((revenue - uncosted - cost) / (revenue - uncosted) * 100) if revenue - uncosted else 0,
        "uncosted": uncosted, "purchases": spent,
        "dishes": sorted(per_dish.values(), key=lambda r: -r["revenue"]),
    }

"""Business logic: settings, menu, customers, orders, prep and reports."""
import secrets
from datetime import timedelta

from flask import g

from . import db
from .utils import (ACTIVE_STATUSES, FULFILLMENT, day_label, fmt_time, money,
                    normalize_phone, now_iso, now_local, parse_date,
                    parse_int, parse_money, pretty_phone, today, valid_time,
                    whatsapp_link)

# ---------------------------------------------------------------- settings
DEFAULT_SETTINGS = {
    "kitchen_name": "مطبخ العائلة",
    "tagline": "أكل بيت طازج، يُطبخ يوميًا بحب",
    "kitchen_phone": "",
    "pickup_address": "",
    "accepting_orders": "1",
    "announcement": "",
    "delivery_enabled": "1",
    "delivery_fee_cents": "500",
    "delivery_note": "",
    "min_order_cents": "0",
    "time_slots": "12:00,13:00,14:00,17:00,18:00,19:00,20:00",
    "days_ahead": "2",
    "lead_minutes": "90",
    "target_food_cost": "35",
    "logo_type": "",
    "logo_version": "",
}


def get_settings():
    if "settings" not in g:
        values = dict(DEFAULT_SETTINGS)
        # The logo image itself is large; it is loaded only by the logo route
        for row in db.query("SELECT key, value FROM settings WHERE key != 'logo_data'"):
            values[row["key"]] = row["value"]
        g.settings = values
    return g.settings


def save_settings(values):
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS and key != "logo_data":
            continue
        if db.execute("UPDATE settings SET value = ? WHERE key = ?", (value, key)) == 0:
            db.insert("settings", {"key": key, "value": value})
    db.commit()
    g.pop("settings", None)


def setting_int(key):
    return parse_int(get_settings().get(key), parse_int(DEFAULT_SETTINGS[key]))


def time_slots():
    raw = get_settings().get("time_slots", "")
    slots = sorted({s.strip() for s in raw.split(",") if valid_time(s.strip())})
    return slots


def order_days():
    """Dates customers may order for: today plus `days_ahead` days."""
    t = today()
    return [t + timedelta(days=i) for i in range(setting_int("days_ahead") + 1)]


def open_slots(for_date):
    """Slots still bookable online for a given date (respects lead time)."""
    slots = time_slots()
    if for_date != today():
        return slots
    cutoff = now_local() + timedelta(minutes=setting_int("lead_minutes"))
    limit = cutoff.strftime("%H:%M") if cutoff.date() == today() else "24:00"
    return [s for s in slots if s >= limit]


# ---------------------------------------------------------------- menu
def menu_items(online_only=False):
    sql = "SELECT * FROM menu_items"
    if online_only:
        sql += " WHERE show_online = 1"
    return db.query(sql + " ORDER BY sort_order, id")


def menu_by_id():
    return {m["id"]: m for m in menu_items()}


# ---------------------------------------------------------------- customers
def upsert_customer(phone, name, address):
    key = normalize_phone(phone)
    if not key:
        return None
    row = db.query("SELECT * FROM customers WHERE phone = ?", (key,), one=True)
    stamp = now_iso()
    if row:
        db.execute(
            "UPDATE customers SET name = ?, address = ?, updated_at = ? WHERE id = ?",
            (name or row["name"], address or row["address"], stamp, row["id"]),
        )
        return row["id"]
    return db.insert("customers", {"phone": key, "name": name, "address": address,
                                   "created_at": stamp, "updated_at": stamp})


def search_customers(q, limit=6):
    """Match by phone digits (preferred) or by name. Includes the last order."""
    q = (q or "").strip()
    digits = normalize_phone(q) if any(c.isdigit() for c in q) else ""
    if digits and len(digits) >= 3:
        rows = db.query("SELECT * FROM customers WHERE phone LIKE ? ORDER BY updated_at DESC LIMIT ?",
                        (f"%{digits}%", limit))
    elif len(q) >= 2:
        rows = db.query("SELECT * FROM customers WHERE name LIKE ? ORDER BY updated_at DESC LIMIT ?",
                        (f"%{q}%", limit))
    else:
        return []
    out = []
    for c in rows:
        count = db.scalar("SELECT COUNT(*) AS n FROM orders WHERE customer_id = ? AND status != 'cancelled'",
                          (c["id"],))
        last = db.query("SELECT * FROM orders WHERE customer_id = ? ORDER BY order_date DESC, id DESC LIMIT 1",
                        (c["id"],), one=True)
        last_items = order_items(last["id"]) if last else []
        out.append({
            "phone": pretty_phone(c["phone"]),
            "phone_key": c["phone"],
            "name": c["name"],
            "address": c["address"],
            "orders": count or 0,
            "last_date": day_label(last["order_date"]) if last else "",
            "last_fulfillment": last["fulfillment"] if last else "pickup",
            "last_items": [{"menu_item_id": i["menu_item_id"], "name": i["name"],
                            "price": i["unit_price_cents"] / 100, "qty": i["qty"]} for i in last_items],
        })
    return out


# ---------------------------------------------------------------- orders
class OrderError(ValueError):
    pass


def order_items(order_id):
    return db.query("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))


def order_total(order, items):
    subtotal = sum(i["unit_price_cents"] * i["qty"] for i in items)
    fee = order["delivery_fee_cents"] if order["fulfillment"] == "delivery" else 0
    return subtotal + fee


def parse_items(form, allow_unavailable=False, allow_custom=False):
    """Read `qty_<menu id>` fields (and custom rows for staff) into item dicts."""
    menu = menu_by_id()
    items = []
    for key, value in form.items():
        if not key.startswith("qty_"):
            continue
        item_id = parse_int(key[4:], None)
        qty = parse_int(value, 0, 0, 99)
        if not qty or item_id not in menu:
            continue
        m = menu[item_id]
        if not m["available"] and not allow_unavailable:
            raise OrderError(f"عذرًا، «{m['name']}» نفد اليوم.")
        items.append({"menu_item_id": m["id"], "name": m["name"],
                      "unit_price_cents": m["price_cents"], "qty": qty})
    items.sort(key=lambda i: (menu[i["menu_item_id"]]["sort_order"], i["menu_item_id"]))
    if allow_custom:
        names = form.getlist("custom_name")
        prices = form.getlist("custom_price")
        qtys = form.getlist("custom_qty")
        for name, price, qty in zip(names, prices, qtys):
            name = (name or "").strip()[:120]
            qty = parse_int(qty, 0, 0, 99)
            if name and qty:
                items.append({"menu_item_id": None, "name": name,
                              "unit_price_cents": parse_money(price) or 0, "qty": qty})
    return items


def _next_number(order_date):
    n = db.scalar("SELECT MAX(number) AS n FROM orders WHERE order_date = ?", (order_date,))
    return (n or 0) + 1


def save_order(fields, items, order_id=None, user_id=None):
    """Create or update an order with its items. Returns the order id."""
    if not items:
        raise OrderError("أضف صنفًا واحدًا على الأقل.")
    if not fields.get("customer_name") and not normalize_phone(fields.get("phone")):
        raise OrderError("اكتب اسم الزبون أو رقم جواله.")
    if fields.get("fulfillment") == "delivery" and not fields.get("address"):
        raise OrderError("اكتب عنوان التوصيل.")

    fields = dict(fields)
    fields["customer_id"] = upsert_customer(fields.get("phone"), fields.get("customer_name", ""),
                                            fields.get("address", ""))
    fields["updated_at"] = now_iso()

    for attempt in range(5):
        try:
            if order_id is None:
                values = dict(fields)
                values.update({
                    "number": _next_number(fields["order_date"]),
                    "public_token": secrets.token_urlsafe(9),
                    "created_by": user_id,
                    "created_at": fields["updated_at"],
                })
                values.setdefault("status", "new")
                new_id = db.insert("orders", values)
            else:
                current = db.query("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
                if not current:
                    raise OrderError("الطلب غير موجود.")
                values = dict(fields)
                if values["order_date"] != current["order_date"]:
                    values["number"] = _next_number(values["order_date"])
                cols = ", ".join(f"{k} = ?" for k in values)
                db.execute(f"UPDATE orders SET {cols} WHERE id = ?", (*values.values(), order_id))
                db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
                new_id = order_id
            for item in items:
                db.insert("order_items", {**item, "order_id": new_id})
            db.commit()
            from .inventory import sync_order_stock
            sync_order_stock(new_id)
            return new_id
        except Exception as exc:  # retry when two orders grab the same number
            db.rollback()
            if not db.is_integrity_error(exc) or attempt == 4:
                raise
    raise OrderError("تعذّر حفظ الطلب، حاول مرة أخرى.")


def set_status(order_id, status):
    from .inventory import sync_order_stock
    db.execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), order_id))
    db.commit()
    sync_order_stock(order_id)


def toggle_paid(order_id):
    db.execute("UPDATE orders SET paid = 1 - paid, updated_at = ? WHERE id = ?", (now_iso(), order_id))
    db.commit()


def delete_order(order_id):
    db.execute("DELETE FROM stock_moves WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM reviews WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
    db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    db.commit()


def get_order(order_id=None, token=None):
    if token is not None:
        o = db.query("SELECT * FROM orders WHERE public_token = ?", (token,), one=True)
    else:
        o = db.query("SELECT * FROM orders WHERE id = ?", (order_id,), one=True)
    return enrich(o) if o else None


def enrich(o, items=None):
    o = dict(o)
    o["items"] = items if items is not None else order_items(o["id"])
    o["total_cents"] = order_total(o, o["items"])
    o["phone_display"] = pretty_phone(o["phone"])
    o["wa_link"] = whatsapp_link(o["phone"], whatsapp_message(o)) if o["phone"] else ""
    return o


def orders_for_day(day, statuses=None):
    rows = db.query("SELECT * FROM orders WHERE order_date = ? ORDER BY "
                    "CASE WHEN pickup_time = '' THEN 1 ELSE 0 END, pickup_time, number", (day,))
    if statuses:
        rows = [r for r in rows if r["status"] in statuses]
    return _with_items(rows)


def _with_items(rows):
    if not rows:
        return []
    ids = [r["id"] for r in rows]
    marks = ", ".join("?" for _ in ids)
    by_order = {}
    for it in db.query(f"SELECT * FROM order_items WHERE order_id IN ({marks}) ORDER BY id", ids):
        by_order.setdefault(it["order_id"], []).append(it)
    return [enrich(r, by_order.get(r["id"], [])) for r in rows]


def whatsapp_message(o):
    name = o.get("customer_name") or ""
    total = money(order_total(o, o["items"]))
    kitchen = get_settings()["kitchen_name"]
    how = FULFILLMENT.get(o["fulfillment"], "")
    if o["status"] in ("preparing", "ready"):
        where = "جاهز وفي الطريق إليك" if o["fulfillment"] == "delivery" else "جاهز للاستلام"
        paid = " (مدفوع)" if o["paid"] else ""
        return f"مرحبًا {name}، طلبك رقم {o['number']} من {kitchen} {where}.\nالمجموع: {total}{paid}\nبالعافية"
    lines = "\n".join(f"- {i['qty']}× {i['name']}" for i in o["items"])
    when = day_label(o["order_date"])
    if o["pickup_time"]:
        when += " الساعة " + fmt_time(o["pickup_time"])
    return (f"مرحبًا {name}، تم تأكيد طلبك رقم {o['number']} من {kitchen}:\n{lines}\n"
            f"المجموع: {total}\nالموعد: {when} ({how})\nشكرًا لك")


def upcoming_days(exclude_day, days=7):
    """Active order counts per date from today onward (excluding the day being viewed)."""
    start = today()
    end = start + timedelta(days=days)
    rows = db.query("SELECT order_date, COUNT(*) AS n FROM orders WHERE order_date >= ? AND order_date <= ? "
                    "AND status IN ('new', 'confirmed', 'preparing', 'ready') GROUP BY order_date ORDER BY order_date",
                    (start.isoformat(), end.isoformat()))
    return [(r["order_date"], r["n"]) for r in rows if r["order_date"] != exclude_day]


def new_web_orders_since(last_id):
    return db.scalar("SELECT COUNT(*) AS n FROM orders WHERE id > ? AND source = 'web'", (last_id,)) or 0


def latest_order_id():
    return db.scalar("SELECT MAX(id) AS n FROM orders") or 0


# ---------------------------------------------------------------- prep & reports
def prep_summary(day):
    orders = [o for o in orders_for_day(day) if o["status"] != "cancelled"]
    agg = {}
    for o in orders:
        finished = o["status"] in ("ready", "done")
        for it in o["items"]:
            a = agg.setdefault(it["name"], {"name": it["name"], "total": 0, "ready": 0})
            a["total"] += it["qty"]
            if finished:
                a["ready"] += it["qty"]
    rows = sorted(agg.values(), key=lambda a: -a["total"])
    active = [o for o in orders if o["status"] in ACTIVE_STATUSES]
    return rows, active


def report(days):
    end = today()
    start = end - timedelta(days=days - 1)
    rows = db.query("SELECT * FROM orders WHERE order_date >= ? AND order_date <= ? AND status != 'cancelled'",
                    (start.isoformat(), end.isoformat()))
    orders = _with_items(rows)
    per_day = {}
    for i in range(days):
        per_day[(start + timedelta(days=i)).isoformat()] = 0
    items, sources = {}, {}
    for o in orders:
        per_day[o["order_date"]] = per_day.get(o["order_date"], 0) + o["total_cents"]
        sources[o["source"]] = sources.get(o["source"], 0) + 1
        for it in o["items"]:
            a = items.setdefault(it["name"], {"name": it["name"], "qty": 0, "revenue": 0})
            a["qty"] += it["qty"]
            a["revenue"] += it["qty"] * it["unit_price_cents"]
    sales = sum(o["total_cents"] for o in orders)
    unpaid = [o for o in orders if not o["paid"]]
    return {
        "start": start, "end": end, "days": days,
        "sales": sales, "count": len(orders),
        "average": sales // len(orders) if orders else 0,
        "unpaid_total": sum(o["total_cents"] for o in unpaid),
        "unpaid": sorted(unpaid, key=lambda o: o["order_date"], reverse=True)[:10],
        "per_day": [{"date": parse_date(d), "cents": c} for d, c in sorted(per_day.items())],
        "top_items": sorted(items.values(), key=lambda a: -a["revenue"])[:8],
        "sources": sorted(sources.items(), key=lambda kv: -kv[1]),
    }


# ---------------------------------------------------------------- categories
def categories():
    rows = db.query("SELECT * FROM categories ORDER BY sort_order, id")
    counts = {r["category"]: r["n"] for r in db.query("SELECT category, COUNT(*) AS n FROM menu_items GROUP BY category")}
    return [{**c, "dishes": counts.get(c["name"], 0)} for c in rows]


def add_category(name):
    name = name.strip()[:60]
    if not name:
        raise ValueError("اكتب اسم التصنيف.")
    if db.query("SELECT id FROM categories WHERE name = ?", (name,), one=True):
        raise ValueError("هذا التصنيف موجود من قبل.")
    n = db.scalar("SELECT MAX(sort_order) AS n FROM categories") or 0
    db.insert("categories", {"name": name, "sort_order": n + 1})
    db.commit()


def rename_category(cat_id, new_name):
    new_name = new_name.strip()[:60]
    cat = db.query("SELECT * FROM categories WHERE id = ?", (cat_id,), one=True)
    if not cat or not new_name or new_name == cat["name"]:
        return
    if db.query("SELECT id FROM categories WHERE name = ? AND id != ?", (new_name, cat_id), one=True):
        raise ValueError("يوجد تصنيف آخر بهذا الاسم.")
    db.execute("UPDATE categories SET name = ? WHERE id = ?", (new_name, cat_id))
    db.execute("UPDATE menu_items SET category = ? WHERE category = ?", (new_name, cat["name"]))
    db.commit()


def delete_category(cat_id):
    """Delete a category; its dishes stay on the menu without a category."""
    cat = db.query("SELECT * FROM categories WHERE id = ?", (cat_id,), one=True)
    if cat:
        db.execute("UPDATE menu_items SET category = '' WHERE category = ?", (cat["name"],))
        db.execute("DELETE FROM categories WHERE id = ?", (cat_id,))
        db.commit()


def move_category(cat_id, direction):
    rows = db.query("SELECT id FROM categories ORDER BY sort_order, id")
    ids = [r["id"] for r in rows]
    if cat_id not in ids:
        return
    i = ids.index(cat_id)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        for pos, cid in enumerate(ids, start=1):
            db.execute("UPDATE categories SET sort_order = ? WHERE id = ?", (pos, cid))
        db.commit()


def category_order():
    return {r["name"]: r["sort_order"] for r in db.query("SELECT name, sort_order FROM categories")}


# ---------------------------------------------------------------- logo
LOGO_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


def save_logo(data, mimetype):
    import base64
    save_settings({"logo_data": base64.b64encode(data).decode("ascii"), "logo_type": mimetype,
                   "logo_version": now_iso().replace(":", "").replace("-", "")[:15]})


def remove_logo():
    db.execute("DELETE FROM settings WHERE key IN ('logo_data', 'logo_type', 'logo_version')")
    db.commit()
    g.pop("settings", None)


def load_logo():
    import base64
    row = db.query("SELECT value FROM settings WHERE key = 'logo_data'", one=True)
    if not row:
        return None, None
    return base64.b64decode(row["value"]), get_settings().get("logo_type") or "image/png"


# ---------------------------------------------------------------- reviews
REVIEW_STATUSES = {"pending": "بانتظار المراجعة", "published": "منشور", "hidden": "مخفي"}


def review_for_order(order_id):
    return db.query("SELECT * FROM reviews WHERE order_id = ?", (order_id,), one=True)


def add_review(order, rating, comment):
    if review_for_order(order["id"]):
        raise ValueError("سبق أن قيّمت هذا الطلب، شكرًا لك.")
    if order["status"] != "done":
        raise ValueError("يمكنك التقييم بعد استلام الطلب.")
    db.insert("reviews", {"order_id": order["id"], "customer_name": order["customer_name"],
                          "rating": max(1, min(5, rating)), "comment": comment.strip()[:600],
                          "status": "pending", "created_at": now_iso()})
    db.commit()


def reviews(status=None):
    sql = ("SELECT r.*, o.number, o.order_date, o.phone FROM reviews r LEFT JOIN orders o ON o.id = r.order_id")
    params = ()
    if status:
        sql += " WHERE r.status = ?"
        params = (status,)
    return db.query(sql + " ORDER BY r.created_at DESC", params)


def set_review_status(review_id, status):
    if status in REVIEW_STATUSES:
        db.execute("UPDATE reviews SET status = ? WHERE id = ?", (status, review_id))
        db.commit()


def delete_review(review_id):
    db.execute("DELETE FROM reviews WHERE id = ?", (review_id,))
    db.commit()


def review_stats(published_only=False):
    where = " WHERE status = 'published'" if published_only else ""
    row = db.query(f"SELECT COUNT(*) AS n, AVG(rating) AS avg FROM reviews{where}", one=True)
    return {"count": row["n"] or 0, "average": float(row["avg"] or 0)}

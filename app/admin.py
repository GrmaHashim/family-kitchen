"""Family dashboard: orders, phone-order entry, prep list, menu, reports, settings."""
from datetime import timedelta
from functools import wraps

from flask import (Blueprint, abort, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db, services
from .utils import (ACTIVE_STATUSES, NEXT_STATUS, SOURCES, STATUSES,
                    normalize_phone, now_iso, parse_date, parse_int,
                    parse_money, today, valid_time)

bp = Blueprint("admin", __name__, url_prefix="/admin")


# ---------------------------------------------------------------- auth
@bp.before_app_request
def load_user():
    uid = session.get("user_id")
    g.user = db.query("SELECT * FROM users WHERE id = ? AND is_active = 1", (uid,), one=True) if uid else None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/admin/api/"):
                return jsonify(error="login required"), 401
            return redirect(url_for("admin.login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def _has_users():
    return bool(db.scalar("SELECT COUNT(*) AS n FROM users"))


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """First-run page: create the first account. Disabled once any user exists."""
    if _has_users():
        return redirect(url_for("admin.login"))
    error = None
    if request.method == "POST":
        error = _create_user_from_form(request.form)
        if not error:
            user = db.query("SELECT * FROM users ORDER BY id LIMIT 1", one=True)
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            flash("تم إنشاء حسابك. أهلًا بك!")
            return redirect(url_for("admin.menu"))
    return render_template("admin/setup.html", error=error)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not _has_users():
        return redirect(url_for("admin.setup"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        user = db.query("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/admin") else url_for("admin.orders"))
        error = "اسم المستخدم أو كلمة المرور غير صحيحة."
    return render_template("admin/login.html", error=error)


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("admin.login"))


def _create_user_from_form(form):
    username = form.get("username", "").strip().lower()
    name = form.get("display_name", "").strip() or username
    password = form.get("password", "")
    if not username or not username.replace("_", "").replace(".", "").isalnum():
        return "اسم المستخدم بالأحرف الإنجليزية والأرقام فقط."
    if len(password) < 8:
        return "كلمة المرور 8 أحرف على الأقل."
    if db.query("SELECT id FROM users WHERE username = ?", (username,), one=True):
        return "اسم المستخدم مستخدم من قبل."
    db.insert("users", {"username": username, "display_name": name,
                        "password_hash": generate_password_hash(password),
                        "is_active": 1, "created_at": now_iso()})
    db.commit()
    return None


def _user_names():
    return {u["id"]: u["display_name"] for u in db.query("SELECT id, display_name FROM users")}


def _day_arg():
    return parse_date(request.args.get("date"), today())


# ---------------------------------------------------------------- orders
@bp.get("/")
@login_required
def orders():
    day = _day_arg()
    flt = request.args.get("status", "active")
    all_orders = services.orders_for_day(day.isoformat())
    counts = {}
    for o in all_orders:
        counts[o["status"]] = counts.get(o["status"], 0) + 1
    if flt == "active":
        shown = [o for o in all_orders if o["status"] in ACTIVE_STATUSES]
    elif flt in STATUSES:
        shown = [o for o in all_orders if o["status"] == flt]
    else:
        flt, shown = "all", all_orders
    live = [o for o in all_orders if o["status"] != "cancelled"]
    return render_template(
        "admin/orders.html", day=day, flt=flt, orders=shown, counts=counts,
        total_count=len(all_orders), active_count=sum(counts.get(s, 0) for s in ACTIVE_STATUSES),
        live_count=len(live), sales=sum(o["total_cents"] for o in live),
        unpaid=sum(o["total_cents"] for o in live if not o["paid"]),
        users=_user_names(), latest_id=services.latest_order_id(),
        confirm=request.args.get("confirm"), confirm_id=parse_int(request.args.get("id"), None),
        timedelta=timedelta,
    )


def _back(order=None):
    target = request.form.get("back") or ""
    if target.startswith("/admin"):
        return redirect(target)
    if order:
        return redirect(url_for("admin.orders", date=order["order_date"]))
    return redirect(url_for("admin.orders"))


@bp.post("/orders/<int:order_id>/status")
@login_required
def change_status(order_id):
    o = services.get_order(order_id) or abort(404)
    to = request.form.get("to", "next")
    if to == "next":
        to = NEXT_STATUS.get(o["status"])
    if to not in STATUSES:
        abort(400)
    services.set_status(order_id, to)
    return _back(o)


@bp.post("/orders/<int:order_id>/paid")
@login_required
def toggle_paid(order_id):
    o = services.get_order(order_id) or abort(404)
    services.toggle_paid(order_id)
    return _back(o)


@bp.post("/orders/<int:order_id>/delete")
@login_required
def delete_order(order_id):
    o = services.get_order(order_id) or abort(404)
    services.delete_order(order_id)
    flash(f"حُذف الطلب #{o['number']}.")
    return redirect(url_for("admin.orders", date=o["order_date"]))


def _form_fields(form):
    fulfillment = form.get("fulfillment") if form.get("fulfillment") in ("pickup", "delivery") else "pickup"
    source = form.get("source") if form.get("source") in SOURCES else "phone"
    pickup_time = form.get("pickup_time", "")
    return {
        "order_date": parse_date(form.get("order_date"), today()).isoformat(),
        "customer_name": form.get("customer_name", "").strip()[:80],
        "phone": normalize_phone(form.get("phone", "")),
        "address": form.get("address", "").strip()[:200] if fulfillment == "delivery" else "",
        "fulfillment": fulfillment,
        "source": source,
        "pickup_time": pickup_time if valid_time(pickup_time) else "",
        "delivery_fee_cents": (parse_money(form.get("delivery_fee")) or 0) if fulfillment == "delivery" else 0,
        "notes": form.get("notes", "").strip()[:500],
        "paid": 1 if form.get("paid") else 0,
        "status": form.get("status") if form.get("status") in STATUSES else "new",
    }


def _blank_order():
    day = _day_arg()
    return {"id": None, "order_date": max(day, today()).isoformat(), "customer_name": "", "phone": "",
            "address": "", "fulfillment": "pickup", "source": "phone", "pickup_time": "",
            "delivery_fee_cents": services.setting_int("delivery_fee_cents"), "notes": "", "paid": 0,
            "status": "new", "items": []}


@bp.route("/orders/new", methods=["GET", "POST"])
@login_required
def new_order():
    return _order_form(None)


@bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@login_required
def edit_order(order_id):
    return _order_form(order_id)


def _order_form(order_id):
    existing = services.get_order(order_id) if order_id else None
    if order_id and not existing:
        abort(404)
    error = None
    if request.method == "POST":
        fields = _form_fields(request.form)
        try:
            items = services.parse_items(request.form, allow_unavailable=True, allow_custom=True)
            saved_id = services.save_order(fields, items, order_id=order_id, user_id=g.user["id"])
            saved = services.get_order(saved_id)
            flash(f"تم {'تحديث' if order_id else 'تسجيل'} الطلب #{saved['number']}.")
            return redirect(url_for("admin.orders", date=saved["order_date"]))
        except services.OrderError as e:
            error = str(e)
        order = {**(existing or _blank_order()), **fields, "id": order_id,
                 "items": _items_from_form(request.form)}
    else:
        order = existing or _blank_order()
    qty = {i["menu_item_id"]: i["qty"] for i in order["items"] if i.get("menu_item_id")}
    custom = [i for i in order["items"] if not i.get("menu_item_id")]
    return render_template("admin/order_form.html", o=order, qty=qty, custom=custom,
                           menu=services.menu_items(), slots=services.time_slots(), error=error,
                           default_fee=services.setting_int("delivery_fee_cents"))


def _items_from_form(form):
    """Rebuild items from a rejected form so the user does not lose them."""
    try:
        return services.parse_items(form, allow_unavailable=True, allow_custom=True)
    except services.OrderError:
        return []


@bp.get("/api/customers")
@login_required
def api_customers():
    return jsonify(services.search_customers(request.args.get("q", "")))


@bp.get("/api/pulse")
@login_required
def api_pulse():
    since = parse_int(request.args.get("since"), 0)
    return jsonify(new_web_orders=services.new_web_orders_since(since), latest_id=services.latest_order_id())


# ---------------------------------------------------------------- prep
@bp.get("/prep")
@login_required
def prep():
    day = _day_arg()
    rows, active = services.prep_summary(day.isoformat())
    return render_template("admin/prep.html", day=day, rows=rows, active=active, timedelta=timedelta)


# ---------------------------------------------------------------- menu
@bp.get("/menu")
@login_required
def menu():
    return render_template("admin/menu.html", items=services.menu_items(),
                           confirm_id=parse_int(request.args.get("confirm"), None))


@bp.post("/menu")
@login_required
def menu_add():
    name = request.form.get("name", "").strip()[:120]
    if not name:
        flash("اكتب اسم الصنف.", "error")
        return redirect(url_for("admin.menu"))
    sort = (db.scalar("SELECT MAX(sort_order) AS n FROM menu_items") or 0) + 1
    db.insert("menu_items", {
        "name": name,
        "category": request.form.get("category", "").strip()[:60],
        "description": request.form.get("description", "").strip()[:200],
        "price_cents": parse_money(request.form.get("price")) or 0,
        "available": 1, "show_online": 1, "sort_order": sort, "created_at": now_iso(),
    })
    db.commit()
    flash(f"أُضيف «{name}».")
    return redirect(url_for("admin.menu"))


@bp.post("/menu/<int:item_id>")
@login_required
def menu_update(item_id):
    item = db.query("SELECT * FROM menu_items WHERE id = ?", (item_id,), one=True) or abort(404)
    action = request.form.get("action", "save")
    if action == "toggle":
        db.execute("UPDATE menu_items SET available = 1 - available WHERE id = ?", (item_id,))
    elif action == "online":
        db.execute("UPDATE menu_items SET show_online = 1 - show_online WHERE id = ?", (item_id,))
    elif action in ("up", "down"):
        items = services.menu_items()
        idx = next(i for i, m in enumerate(items) if m["id"] == item_id)
        swap = idx - 1 if action == "up" else idx + 1
        if 0 <= swap < len(items):
            items[idx], items[swap] = items[swap], items[idx]
            for pos, m in enumerate(items, start=1):
                db.execute("UPDATE menu_items SET sort_order = ? WHERE id = ?", (pos, m["id"]))
    elif action == "delete":
        db.execute("UPDATE order_items SET menu_item_id = NULL WHERE menu_item_id = ?", (item_id,))
        db.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
        flash(f"حُذف «{item['name']}».")
    else:
        name = request.form.get("name", "").strip()[:120] or item["name"]
        price = parse_money(request.form.get("price"))
        db.execute("UPDATE menu_items SET name = ?, category = ?, description = ?, price_cents = ? WHERE id = ?",
                   (name, request.form.get("category", "").strip()[:60],
                    request.form.get("description", "").strip()[:200],
                    item["price_cents"] if price is None else price, item_id))
        flash(f"حُفظ «{name}».")
    db.commit()
    return redirect(url_for("admin.menu") + f"#item-{item_id}")


# ---------------------------------------------------------------- reports
@bp.get("/reports")
@login_required
def reports():
    days = 30 if request.args.get("days") == "30" else 7
    return render_template("admin/reports.html", r=services.report(days))


# ---------------------------------------------------------------- settings & users
@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    error = None
    if request.method == "POST":
        f = request.form
        slots = sorted({s.strip() for s in f.get("time_slots", "").replace("،", ",").split(",") if valid_time(s.strip())})
        services.save_settings({
            "kitchen_name": f.get("kitchen_name", "").strip()[:60] or "مطبخ العائلة",
            "tagline": f.get("tagline", "").strip()[:120],
            "kitchen_phone": f.get("kitchen_phone", "").strip()[:30],
            "pickup_address": f.get("pickup_address", "").strip()[:200],
            "announcement": f.get("announcement", "").strip()[:200],
            "accepting_orders": "1" if f.get("accepting_orders") else "0",
            "delivery_enabled": "1" if f.get("delivery_enabled") else "0",
            "delivery_fee_cents": str(parse_money(f.get("delivery_fee")) or 0),
            "delivery_note": f.get("delivery_note", "").strip()[:200],
            "min_order_cents": str(parse_money(f.get("min_order")) or 0),
            "time_slots": ",".join(slots),
            "days_ahead": str(parse_int(f.get("days_ahead"), 2, 0, 7)),
            "lead_minutes": str(parse_int(f.get("lead_minutes"), 90, 0, 1440)),
        })
        flash("حُفظت الإعدادات.")
        return redirect(url_for("admin.settings"))
    users = db.query("SELECT id, username, display_name, is_active FROM users ORDER BY id")
    return render_template("admin/settings.html", users=users, error=error)


@bp.post("/users")
@login_required
def add_user():
    error = _create_user_from_form(request.form)
    flash(error or "أُضيف الحساب. أرسل اسم المستخدم وكلمة المرور لصاحبه.", "error" if error else "info")
    return redirect(url_for("admin.settings") + "#users")


@bp.post("/users/<int:user_id>/toggle")
@login_required
def toggle_user(user_id):
    if user_id == g.user["id"]:
        flash("لا يمكنك إيقاف حسابك أنت.", "error")
    else:
        db.execute("UPDATE users SET is_active = 1 - is_active WHERE id = ?", (user_id,))
        db.commit()
    return redirect(url_for("admin.settings") + "#users")


@bp.post("/password")
@login_required
def change_password():
    f = request.form
    if not check_password_hash(g.user["password_hash"], f.get("current", "")):
        flash("كلمة المرور الحالية غير صحيحة.", "error")
    elif len(f.get("new", "")) < 8:
        flash("كلمة المرور الجديدة 8 أحرف على الأقل.", "error")
    else:
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                   (generate_password_hash(f["new"]), g.user["id"]))
        db.commit()
        flash("تم تغيير كلمة المرور.")
    return redirect(url_for("admin.settings") + "#account")

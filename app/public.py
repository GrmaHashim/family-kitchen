"""Customer-facing pages: menu + order form, and order tracking."""
from flask import Blueprint, abort, redirect, render_template, request, url_for

from . import services
from .utils import normalize_phone, parse_date, today

bp = Blueprint("public", __name__)


def _categories(items):
    groups = {}
    for m in items:
        groups.setdefault(m["category"] or "القائمة", []).append(m)
    return list(groups.items())


@bp.get("/")
def menu():
    return _render_menu({}, None)


def _render_menu(form, error, status=200):
    items = services.menu_items(online_only=True)
    days = services.order_days()
    slots = {d.isoformat(): services.open_slots(d) for d in days}
    return render_template(
        "public/menu.html",
        groups=_categories(items),
        days=days,
        slots=slots,
        default_day=next((d for d, s in slots.items() if s), days[0].isoformat() if days else ""),
        form=form,
        error=error,
        min_order=services.setting_int("min_order_cents"),
        delivery_fee=services.setting_int("delivery_fee_cents"),
    ), status


@bp.post("/order")
def place_order():
    form = request.form
    S = services.get_settings()

    if form.get("website"):  # honeypot field: real customers never fill it
        return redirect(url_for("public.menu"))
    if S["accepting_orders"] != "1":
        return _render_menu(form, "عذرًا، استقبال الطلبات متوقف حاليًا.", 409)

    try:
        items = services.parse_items(form)
    except services.OrderError as e:
        return _render_menu(form, str(e), 422)
    online_ids = {m["id"] for m in services.menu_items(online_only=True)}
    items = [i for i in items if i["menu_item_id"] in online_ids]

    name = form.get("customer_name", "").strip()[:80]
    phone = form.get("phone", "").strip()[:30]
    fulfillment = form.get("fulfillment", "pickup")
    if fulfillment not in ("pickup", "delivery") or (fulfillment == "delivery" and S["delivery_enabled"] != "1"):
        fulfillment = "pickup"
    address = form.get("address", "").strip()[:200] if fulfillment == "delivery" else ""
    order_date = parse_date(form.get("order_date"), today())
    pickup_time = form.get("pickup_time", "")

    error = None
    subtotal = sum(i["unit_price_cents"] * i["qty"] for i in items)
    if not items:
        error = "اختر صنفًا واحدًا على الأقل من القائمة."
    elif not name:
        error = "اكتب اسمك."
    elif len(normalize_phone(phone)) < 10:
        error = "اكتب رقم جوال صحيح من 10 أرقام لنتواصل معك."
    elif fulfillment == "delivery" and not address:
        error = "اكتب عنوان التوصيل."
    elif order_date not in services.order_days():
        error = "اختر يومًا من الأيام المتاحة."
    elif pickup_time not in services.open_slots(order_date):
        error = "هذا الموعد لم يعد متاحًا، اختر موعدًا آخر."
    elif subtotal < services.setting_int("min_order_cents"):
        error = "قيمة الطلب أقل من الحد الأدنى."
    if error:
        return _render_menu(form, error, 422)

    fee = services.setting_int("delivery_fee_cents") if fulfillment == "delivery" else 0
    try:
        order_id = services.save_order({
            "order_date": order_date.isoformat(),
            "customer_name": name,
            "phone": normalize_phone(phone),
            "address": address,
            "fulfillment": fulfillment,
            "source": "web",
            "pickup_time": pickup_time,
            "status": "new",
            "delivery_fee_cents": fee,
            "notes": form.get("notes", "").strip()[:500],
            "paid": 0,
        }, items)
    except services.OrderError as e:
        return _render_menu(form, str(e), 422)

    order = services.get_order(order_id)
    return redirect(url_for("public.track", token=order["public_token"], new=1))


@bp.get("/o/<token>")
def track(token):
    order = services.get_order(token=token)
    if not order:
        abort(404)
    steps = ["new", "confirmed", "preparing", "ready", "done"]
    return render_template("public/track.html", o=order, steps=steps,
                           just_placed=request.args.get("new") == "1")

"""Dashboard pages added in v2: categories, logo, reviews, inventory, purchases, costs."""
from flask import abort, flash, g, redirect, render_template, request, url_for

from . import db, inventory, services
from .admin import bp, login_required
from .utils import parse_date, parse_int, parse_money, today

MAX_LOGO_BYTES = 600 * 1024


def _float(text, default=0.0):
    try:
        value = float(str(text).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# ---------------------------------------------------------------- categories
@bp.post("/categories")
@login_required
def category_add():
    try:
        services.add_category(request.form.get("name", ""))
        flash("أُضيف التصنيف.")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.settings") + "#categories")


@bp.post("/categories/<int:cat_id>")
@login_required
def category_update(cat_id):
    action = request.form.get("action", "rename")
    try:
        if action == "rename":
            services.rename_category(cat_id, request.form.get("name", ""))
            flash("حُفظ التصنيف.")
        elif action in ("up", "down"):
            services.move_category(cat_id, action)
        elif action == "delete":
            services.delete_category(cat_id)
            flash("حُذف التصنيف. أصنافه باقية في القائمة بدون تصنيف.")
    except ValueError as e:
        flash(str(e), "error")
    return redirect(url_for("admin.settings") + "#categories")


# ---------------------------------------------------------------- logo
@bp.post("/logo")
@login_required
def logo_upload():
    f = request.files.get("logo")
    if not f or not f.filename:
        flash("اختر صورة الشعار أولًا.", "error")
    elif f.mimetype not in services.LOGO_TYPES:
        flash("الصيغ المقبولة: PNG أو JPG أو WEBP.", "error")
    else:
        data = f.read(MAX_LOGO_BYTES + 1)
        if len(data) > MAX_LOGO_BYTES:
            flash("حجم الصورة كبير. اختر صورة أصغر من 600 كيلوبايت.", "error")
        else:
            services.save_logo(data, f.mimetype)
            flash("حُفظ الشعار.")
    return redirect(url_for("admin.settings") + "#brand")


@bp.post("/logo/delete")
@login_required
def logo_delete():
    services.remove_logo()
    flash("أُزيل الشعار.")
    return redirect(url_for("admin.settings") + "#brand")


# ---------------------------------------------------------------- reviews
@bp.get("/reviews")
@login_required
def reviews():
    flt = request.args.get("status", "pending")
    if flt not in services.REVIEW_STATUSES and flt != "all":
        flt = "pending"
    counts = {s: len(services.reviews(s)) for s in services.REVIEW_STATUSES}
    return render_template("admin/reviews.html", items=services.reviews(None if flt == "all" else flt),
                           flt=flt, counts=counts, stats=services.review_stats(),
                           REVIEW_STATUSES=services.REVIEW_STATUSES)


@bp.post("/reviews/<int:review_id>")
@login_required
def review_update(review_id):
    action = request.form.get("action")
    if action == "delete":
        services.delete_review(review_id)
        flash("حُذف التقييم.")
    else:
        services.set_review_status(review_id, action)
    return redirect(url_for("admin.reviews", status=request.form.get("back", "pending")))


# ---------------------------------------------------------------- inventory & purchases
@bp.get("/inventory")
@login_required
def inventory_page():
    items = inventory.ingredients()
    return render_template("admin/inventory.html", items=items, purchases=inventory.recent_purchases(),
                           UNITS=inventory.UNITS, today_iso=today().isoformat(),
                           low_count=sum(1 for i in items if i["low"]),
                           stock_value=sum(i["value"] for i in items),
                           edit_id=parse_int(request.args.get("edit"), None),
                           confirm_id=parse_int(request.args.get("confirm"), None))


@bp.post("/ingredients")
@login_required
def ingredient_add():
    name = request.form.get("name", "").strip()[:80]
    if not name:
        flash("اكتب اسم المكوّن.", "error")
        return redirect(url_for("admin.inventory_page"))
    new_id = inventory.add_ingredient(name, request.form.get("unit", "kg"),
                                      parse_money(request.form.get("default_cost")) or 0,
                                      _float(request.form.get("low_stock")))
    opening = _float(request.form.get("opening_stock"))
    if opening:
        inventory.adjust_stock(new_id, opening, "رصيد افتتاحي")
    flash(f"أُضيف «{name}».")
    return redirect(url_for("admin.inventory_page") + f"#ing-{new_id}")


@bp.post("/ingredients/<int:ing_id>")
@login_required
def ingredient_update(ing_id):
    ing = db.query("SELECT * FROM ingredients WHERE id = ?", (ing_id,), one=True) or abort(404)
    action = request.form.get("action", "save")
    if action == "delete":
        inventory.delete_ingredient(ing_id)
        flash(f"حُذف «{ing['name']}» مع وصفاته ومشترياته.")
        return redirect(url_for("admin.inventory_page"))
    if action == "count":
        diff = inventory.adjust_stock(ing_id, _float(request.form.get("counted")))
        flash(f"سُجّل الجرد لـ«{ing['name']}» (الفرق {diff:+g}).")
    else:
        unit = request.form.get("unit", ing["unit"])
        db.execute("UPDATE ingredients SET name = ?, unit = ?, default_cost = ?, low_stock = ? WHERE id = ?",
                   (request.form.get("name", "").strip()[:80] or ing["name"],
                    unit if unit in inventory.UNITS else ing["unit"],
                    parse_money(request.form.get("default_cost")) or 0,
                    _float(request.form.get("low_stock")), ing_id))
        db.commit()
        flash(f"حُفظ «{ing['name']}».")
    return redirect(url_for("admin.inventory_page") + f"#ing-{ing_id}")


@bp.post("/purchases")
@login_required
def purchase_add():
    ing_id = parse_int(request.form.get("ingredient_id"), None)
    qty = _float(request.form.get("qty"))
    cost = parse_money(request.form.get("cost"))
    if not db.query("SELECT id FROM ingredients WHERE id = ?", (ing_id,), one=True):
        flash("اختر المكوّن.", "error")
    elif qty <= 0:
        flash("اكتب الكمية المشتراة.", "error")
    elif cost is None:
        flash("اكتب المبلغ المدفوع.", "error")
    else:
        inventory.add_purchase(parse_date(request.form.get("purchase_date"), today()).isoformat(), ing_id, qty, cost,
                               request.form.get("supplier", "").strip()[:80],
                               request.form.get("note", "").strip()[:200], g.user["id"])
        flash("سُجّلت المشتريات وأُضيفت إلى المخزون.")
    return redirect(url_for("admin.inventory_page") + "#purchases")


@bp.post("/purchases/<int:purchase_id>/delete")
@login_required
def purchase_delete(purchase_id):
    inventory.delete_purchase(purchase_id)
    flash("حُذفت عملية الشراء وخُصمت كميتها من المخزون.")
    return redirect(url_for("admin.inventory_page") + "#purchases")


# ---------------------------------------------------------------- costs, recipes, profit
@bp.get("/costs")
@login_required
def costs():
    days = 30 if request.args.get("days") == "30" else 7
    target = services.setting_int("target_food_cost")
    return render_template("admin/costs.html", dishes=inventory.dish_costs(target),
                           p=inventory.profit_report(days, target), target=target)


@bp.post("/costs/target")
@login_required
def costs_target():
    services.save_settings({"target_food_cost": str(parse_int(request.form.get("target"), 35, 5, 95))})
    flash("حُفظت نسبة التكلفة المستهدفة.")
    return redirect(url_for("admin.costs"))


@bp.route("/costs/<int:item_id>", methods=["GET", "POST"])
@login_required
def recipe_edit(item_id):
    item = db.query("SELECT * FROM menu_items WHERE id = ?", (item_id,), one=True) or abort(404)
    if request.method == "POST":
        action = request.form.get("action")
        if action == "line":
            ing_id = parse_int(request.form.get("ingredient_id"), None)
            if db.query("SELECT id FROM ingredients WHERE id = ?", (ing_id,), one=True):
                inventory.set_recipe_line(item_id, ing_id, _float(request.form.get("qty")))
        elif action == "remove":
            inventory.set_recipe_line(item_id, parse_int(request.form.get("ingredient_id"), 0), 0)
        elif action == "extra":
            db.execute("UPDATE menu_items SET extra_cost_cents = ? WHERE id = ?",
                       (parse_money(request.form.get("extra_cost")) or 0, item_id))
            db.commit()
        elif action == "price":
            price = parse_money(request.form.get("price"))
            if price is not None:
                db.execute("UPDATE menu_items SET price_cents = ? WHERE id = ?", (price, item_id))
                db.commit()
                flash("حُدّث سعر البيع.")
        return redirect(url_for("admin.recipe_edit", item_id=item_id))
    target = services.setting_int("target_food_cost")
    dish = next(d for d in inventory.dish_costs(target) if d["id"] == item_id)
    lines = inventory.recipe(item_id)
    used = {l["ingredient_id"] for l in lines}
    return render_template("admin/recipe.html", dish=dish, lines=lines, target=target,
                           choices=[i for i in inventory.ingredients() if i["id"] not in used])

"""End-to-end tests. Run with: python -m unittest discover tests   (or: pytest)"""
import io
import os
import re
import tempfile
import unittest
from datetime import timedelta

from app import create_app, db, services, utils


class KitchenTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test",
            "DATABASE_URL": "sqlite:///" + self.db_path,
            "CSRF_ENABLED": False,
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            # Every slot of the day so tests never depend on the clock
            services.save_settings({"time_slots": ",".join(f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)),
                                    "lead_minutes": "0", "days_ahead": "2"})
            self.menu_ids = []
            for i, (name, price) in enumerate([("كبسة دجاج", 1800), ("سمبوسة", 1200), ("كركدي", 600)], 1):
                self.menu_ids.append(db.insert("menu_items", {
                    "name": name, "price_cents": price, "available": 1, "show_online": 1,
                    "sort_order": i, "category": "", "description": "", "created_at": utils.now_iso()}))
            db.commit()
            self.tomorrow = (utils.today() + timedelta(days=1)).isoformat()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass

    # helpers
    def setup_account(self):
        return self.client.post("/admin/setup", data={"display_name": "محمد", "username": "mohamed",
                                                       "password": "secret123"})

    def today_slot(self):
        with self.app.app_context():
            slots = services.open_slots(utils.today())
        if not slots:
            self.skipTest("no ordering slots left today")
        return slots[-1]

    def web_order(self, **overrides):
        data = {"customer_name": "سارة", "phone": "403-555-0177", "fulfillment": "pickup",
                "order_date": self.tomorrow, "pickup_time": "13:00", f"qty_{self.menu_ids[0]}": "2"}
        data.update(overrides)
        return self.client.post("/order", data=data)


class PublicSiteTests(KitchenTestCase):
    def test_menu_page_lists_dishes(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("كبسة دجاج", html)
        self.assertIn("$18.00", html)

    def test_customer_can_place_and_track_order(self):
        r = self.web_order()
        self.assertEqual(r.status_code, 302)
        self.assertIn("/o/", r.headers["Location"])
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("#1", page)
        self.assertIn("$36.00", page)
        with self.app.app_context():
            o = db.query("SELECT * FROM orders", one=True)
            self.assertEqual(o["source"], "web")
            self.assertEqual(o["phone"], "4035550177")

    def test_delivery_adds_fee_and_needs_address(self):
        r = self.web_order(fulfillment="delivery")
        self.assertEqual(r.status_code, 422)
        self.assertIn("عنوان التوصيل", r.get_data(as_text=True))
        r = self.web_order(fulfillment="delivery", address="Saddle Ridge")
        page = self.client.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn("$41.00", page)  # 2 x 18 + 5 delivery

    def test_validation_keeps_entered_values(self):
        r = self.web_order(phone="123")
        html = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 422)
        self.assertIn("رقم جوال صحيح", html)
        self.assertIn('value="سارة"', html)

    def test_sold_out_item_rejected(self):
        with self.app.app_context():
            db.execute("UPDATE menu_items SET available = 0 WHERE id = ?", (self.menu_ids[0],))
            db.commit()
        r = self.web_order()
        self.assertEqual(r.status_code, 422)
        self.assertIn("نفد", r.get_data(as_text=True))

    def test_prices_come_from_server_not_client(self):
        r = self.web_order(**{f"qty_{self.menu_ids[0]}": "1", "price": "0"})
        self.assertIn("$18.00", self.client.get(r.headers["Location"]).get_data(as_text=True))

    def test_closed_kitchen_refuses_orders(self):
        with self.app.app_context():
            services.save_settings({"accepting_orders": "0"})
        self.assertIn("متوقف", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.web_order().status_code, 409)

    def test_honeypot_blocks_bots(self):
        self.web_order(website="spam")
        with self.app.app_context():
            self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM orders"), 0)

    def test_order_numbers_restart_each_day(self):
        self.web_order()
        self.web_order(phone="4035550100")
        today = utils_today(self.app)
        self.web_order(order_date=today, pickup_time=self.today_slot())
        with self.app.app_context():
            rows = db.query("SELECT order_date, number FROM orders ORDER BY id")
        self.assertEqual([r["number"] for r in rows], [1, 2, 1])


class DashboardTests(KitchenTestCase):
    def test_setup_then_login_required(self):
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.setup_account()
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/admin/setup").status_code, 302)  # setup closes after first user
        self.client.post("/admin/logout")
        r = self.client.post("/admin/login", data={"username": "mohamed", "password": "wrong-pass"})
        self.assertIn("غير صحيحة", r.get_data(as_text=True))
        r = self.client.post("/admin/login", data={"username": "mohamed", "password": "secret123"})
        self.assertEqual(r.status_code, 302)

    def test_phone_order_with_custom_item(self):
        self.setup_account()
        r = self.client.post("/admin/orders/new", data={
            "source": "phone", "phone": "587 555 0199", "customer_name": "خالد",
            "fulfillment": "pickup", "order_date": self.tomorrow, "pickup_time": "18:00",
            f"qty_{self.menu_ids[1]}": "3",
            "custom_name": ["صينية محشي", ""], "custom_price": ["35", ""], "custom_qty": ["1", ""],
            "status": "new",
        })
        self.assertEqual(r.status_code, 302)
        with self.app.app_context():
            o = services.get_order(db.scalar("SELECT MAX(id) AS n FROM orders"))
        self.assertEqual(o["total_cents"], 3 * 1200 + 3500)
        self.assertEqual(o["source"], "phone")
        self.assertIsNotNone(o["created_by"])
        page = self.client.get(f"/admin/?date={self.tomorrow}").get_data(as_text=True)
        self.assertIn("صينية محشي", page)
        self.assertIn("سجّله: محمد", page)

    def test_dashboard_points_to_orders_on_other_days(self):
        self.web_order()  # tomorrow
        self.setup_account()
        html = self.client.get("/admin/").get_data(as_text=True)
        self.assertIn("طلبات في أيام أخرى", html)
        self.assertIn(f"date={self.tomorrow}", html)

    def test_customer_lookup_and_repeat(self):
        self.web_order()
        self.setup_account()
        data = self.client.get("/admin/api/customers?q=0177").get_json()
        self.assertEqual(data[0]["name"], "سارة")
        self.assertEqual(data[0]["orders"], 1)
        self.assertEqual(data[0]["last_items"][0]["qty"], 2)

    def test_status_flow_and_payment(self):
        self.web_order()
        self.setup_account()
        with self.app.app_context():
            oid = db.scalar("SELECT id AS n FROM orders")
        for expected in ["confirmed", "preparing", "ready", "done"]:
            self.client.post(f"/admin/orders/{oid}/status", data={"to": "next"})
            with self.app.app_context():
                self.assertEqual(services.get_order(oid)["status"], expected)
        self.client.post(f"/admin/orders/{oid}/paid")
        with self.app.app_context():
            self.assertEqual(services.get_order(oid)["paid"], 1)

    def test_whatsapp_link(self):
        self.web_order()
        with self.app.app_context():
            o = services.get_order(db.scalar("SELECT id AS n FROM orders"))
        self.assertTrue(o["wa_link"].startswith("https://wa.me/14035550177?text="))

    def test_prep_summary_counts_dishes(self):
        self.web_order()
        self.web_order(phone="4035550100", **{f"qty_{self.menu_ids[0]}": "1", f"qty_{self.menu_ids[1]}": "4"})
        self.setup_account()
        html = self.client.get(f"/admin/prep?date={self.tomorrow}").get_data(as_text=True)
        self.assertRegex(html, r'pbig">3</span>\s*<span class="pname">كبسة دجاج')
        self.assertRegex(html, r'pbig">4</span>\s*<span class="pname">سمبوسة')

    def test_edit_and_cancel_order(self):
        self.web_order()
        self.setup_account()
        with self.app.app_context():
            oid = db.scalar("SELECT id AS n FROM orders")
        self.client.post(f"/admin/orders/{oid}/edit", data={
            "source": "web", "phone": "4035550177", "customer_name": "سارة أحمد", "fulfillment": "pickup",
            "order_date": self.tomorrow, "pickup_time": "14:00", f"qty_{self.menu_ids[2]}": "2", "status": "confirmed"})
        with self.app.app_context():
            o = services.get_order(oid)
        self.assertEqual((o["customer_name"], o["status"], o["total_cents"]), ("سارة أحمد", "confirmed", 1200))
        self.client.post(f"/admin/orders/{oid}/status", data={"to": "cancelled"})
        with self.app.app_context():
            self.assertEqual(services.get_order(oid)["status"], "cancelled")

    def test_menu_management(self):
        self.setup_account()
        self.client.post("/admin/menu", data={"name": "مندي لحم", "price": "25", "category": "أطباق رئيسية"})
        with self.app.app_context():
            item = db.query("SELECT * FROM menu_items WHERE name = ?", ("مندي لحم",), one=True)
        self.assertEqual(item["price_cents"], 2500)
        self.client.post(f"/admin/menu/{item['id']}", data={"action": "toggle"})
        self.client.post(f"/admin/menu/{item['id']}", data={"action": "save", "name": "مندي لحم", "price": "27.5",
                                                             "category": "", "description": ""})
        with self.app.app_context():
            item = db.query("SELECT * FROM menu_items WHERE id = ?", (item["id"],), one=True)
        self.assertEqual((item["available"], item["price_cents"]), (0, 2750))

    def test_reports_and_settings_pages_render(self):
        self.web_order(order_date=utils_today(self.app), pickup_time=self.today_slot())
        self.setup_account()
        html = self.client.get("/admin/reports").get_data(as_text=True)
        self.assertIn("$36.00", html)
        self.assertEqual(self.client.get("/admin/reports?days=30").status_code, 200)
        self.assertEqual(self.client.get("/admin/settings").status_code, 200)
        self.client.post("/admin/settings", data={"kitchen_name": "مطبخ أم أحمد", "time_slots": "12:00، 18:00,bad",
                                                  "delivery_fee": "6", "days_ahead": "1", "lead_minutes": "60"})
        with self.app.app_context():
            s = services.get_settings()
        self.assertEqual((s["kitchen_name"], s["time_slots"], s["delivery_fee_cents"]), ("مطبخ أم أحمد", "12:00,18:00", "600"))

    def test_family_accounts(self):
        self.setup_account()
        self.client.post("/admin/users", data={"display_name": "أم أحمد", "username": "umahmed", "password": "kitchen123"})
        self.client.post("/admin/logout")
        r = self.client.post("/admin/login", data={"username": "umahmed", "password": "kitchen123"})
        self.assertEqual(r.status_code, 302)


class V2FeatureTests(KitchenTestCase):
    def setUp(self):
        super().setUp()
        self.setup_account()

    def order_id(self):
        with self.app.app_context():
            return db.scalar("SELECT MAX(id) AS n FROM orders")

    def test_categories_managed_from_settings(self):
        self.client.post("/admin/categories", data={"name": "أطباق رئيسية"})
        self.client.post("/admin/categories", data={"name": "مشروبات"})
        with self.app.app_context():
            cats = {c["name"]: c["id"] for c in services.categories()}
            db.execute("UPDATE menu_items SET category = 'مشروبات' WHERE id = ?", (self.menu_ids[2],))
            db.execute("UPDATE menu_items SET category = 'أطباق رئيسية' WHERE id != ?", (self.menu_ids[2],))
            db.commit()
        # Move drinks first, then check the customer page order
        self.client.post(f"/admin/categories/{cats['مشروبات']}", data={"action": "up"})
        html = self.client.get("/").get_data(as_text=True)
        self.assertLess(html.index("مشروبات"), html.index("أطباق رئيسية"))
        # Rename applies to dishes
        self.client.post(f"/admin/categories/{cats['مشروبات']}", data={"action": "rename", "name": "عصائر"})
        with self.app.app_context():
            self.assertEqual(db.query("SELECT category FROM menu_items WHERE id = ?", (self.menu_ids[2],), one=True)["category"], "عصائر")
        # Delete keeps the dish without a category
        self.client.post(f"/admin/categories/{cats['مشروبات']}", data={"action": "delete"})
        with self.app.app_context():
            self.assertEqual(db.query("SELECT category FROM menu_items WHERE id = ?", (self.menu_ids[2],), one=True)["category"], "")
        self.assertIn("كركدي", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get("/admin/settings").status_code, 200)
        self.assertEqual(self.client.get("/admin/menu").status_code, 200)

    def test_logo_upload_and_serve(self):
        png = (b"\x89PNG\r\n\x1a\n" + b"0" * 100)
        r = self.client.post("/admin/logo", data={"logo": (io.BytesIO(png), "logo.png", "image/png")},
                             content_type="multipart/form-data")
        self.assertEqual(r.status_code, 302)
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="logo lg"', html)
        src = re.search(r'src="(/brand/logo\?v=[^"]+)"', html).group(1)
        img = self.client.get(src)
        self.assertEqual((img.status_code, img.mimetype, img.data), (200, "image/png", png))
        # Non-images are refused
        self.client.post("/admin/logo", data={"logo": (io.BytesIO(b"<svg/>"), "x.svg", "image/svg+xml")},
                         content_type="multipart/form-data")
        self.assertEqual(self.client.get(src).data, png)
        self.client.post("/admin/logo/delete")
        self.assertEqual(self.client.get("/brand/logo").status_code, 404)

    def test_review_flow_with_moderation(self):
        r = self.web_order()
        token = r.headers["Location"].split("/o/")[1].split("?")[0]
        oid = self.order_id()
        # Not deliverable yet: no form, and posting is ignored
        self.assertNotIn("كيف كان طلبك", self.client.get(f"/o/{token}").get_data(as_text=True))
        self.client.post(f"/o/{token}/review", data={"rating": "5"})
        with self.app.app_context():
            self.assertEqual(db.scalar("SELECT COUNT(*) AS n FROM reviews"), 0)
        self.client.post(f"/admin/orders/{oid}/status", data={"to": "done"})
        self.assertIn("كيف كان طلبك", self.client.get(f"/o/{token}").get_data(as_text=True))
        self.client.post(f"/o/{token}/review", data={"rating": "abc"})
        self.client.post(f"/o/{token}/review", data={"rating": "4", "comment": "الكبسة ممتازة"})
        self.client.post(f"/o/{token}/review", data={"rating": "1", "comment": "مكرر"})
        with self.app.app_context():
            rows = services.reviews()
        self.assertEqual([(r["rating"], r["status"]) for r in rows], [(4, "pending")])
        self.assertNotIn("الكبسة ممتازة", self.client.get("/").get_data(as_text=True))
        self.assertIn('class="badge">1<', self.client.get("/admin/reviews").get_data(as_text=True))
        self.client.post(f"/admin/reviews/{rows[0]['id']}", data={"action": "published"})
        self.assertIn("الكبسة ممتازة", self.client.get("/").get_data(as_text=True))

    def test_inventory_purchases_recipes_and_deduction(self):
        self.client.post("/admin/ingredients", data={"name": "أرز", "unit": "kg", "opening_stock": "2", "low_stock": "1"})
        self.client.post("/admin/ingredients", data={"name": "دجاج", "unit": "kg", "default_cost": "8"})
        with self.app.app_context():
            ing = {i["name"]: i["id"] for i in db.query("SELECT id, name FROM ingredients")}
        # Buy 10 kg rice for $30 -> $3/kg; stock 12
        self.client.post("/admin/purchases", data={"ingredient_id": ing["أرز"], "qty": "10", "cost": "30"})
        # Recipe: kabsa = 0.25 kg rice + 0.5 kg chicken, plus $0.50 packaging
        kabsa = self.menu_ids[0]
        self.client.post(f"/admin/costs/{kabsa}", data={"action": "line", "ingredient_id": ing["أرز"], "qty": "0.25"})
        self.client.post(f"/admin/costs/{kabsa}", data={"action": "line", "ingredient_id": ing["دجاج"], "qty": "0.5"})
        self.client.post(f"/admin/costs/{kabsa}", data={"action": "extra", "extra_cost": "0.5"})
        with self.app.app_context():
            from app import inventory
            d = next(x for x in inventory.dish_costs(35) if x["id"] == kabsa)
        self.assertAlmostEqual(d["total_cost"], 75 + 400 + 50)  # cents
        self.assertAlmostEqual(d["profit"], 1800 - 525)
        self.assertAlmostEqual(d["suggested"], 525 / 0.35)
        # Order 2 kabsa; delivered -> stock deducted; reverted -> restored
        self.web_order()
        oid = self.order_id()
        prep = self.client.get(f"/admin/prep?date={self.tomorrow}").get_data(as_text=True)
        self.assertIn("المكوّنات المطلوبة", prep)
        self.client.post(f"/admin/orders/{oid}/status", data={"to": "done"})
        self.client.post(f"/admin/orders/{oid}/status", data={"to": "done"})  # twice: no double deduction
        with self.app.app_context():
            from app import inventory
            stock = inventory.stock_levels()
        self.assertAlmostEqual(stock[ing["أرز"]], 12 - 0.5)
        self.assertAlmostEqual(stock[ing["دجاج"]], -1.0)
        self.client.post(f"/admin/orders/{oid}/status", data={"to": "cancelled"})
        with self.app.app_context():
            from app import inventory
            self.assertAlmostEqual(inventory.stock_levels()[ing["أرز"]], 12)
        # Stocktake sets the real count
        self.client.post(f"/admin/ingredients/{ing['أرز']}", data={"action": "count", "counted": "11"})
        with self.app.app_context():
            from app import inventory
            self.assertAlmostEqual(inventory.stock_levels()[ing["أرز"]], 11)
        html = self.client.get("/admin/inventory").get_data(as_text=True)
        self.assertIn("أرز", html)
        self.assertEqual(self.client.get(f"/admin/inventory?edit={ing['أرز']}").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/costs/{kabsa}").status_code, 200)

    def test_profit_report(self):
        with self.app.app_context():
            from app import inventory
            rice = inventory.add_ingredient("أرز", "kg", 400, 0)
            inventory.set_recipe_line(self.menu_ids[0], rice, 0.5)  # $2 per kabsa
        self.web_order(order_date=utils_today(self.app), pickup_time=self.today_slot(),
                       **{f"qty_{self.menu_ids[0]}": "2", f"qty_{self.menu_ids[1]}": "1"})
        with self.app.app_context():
            from app import inventory
            p = inventory.profit_report(7, 35)
        self.assertEqual((p["revenue"], p["cost"], p["uncosted"]), (3600 + 1200, 400, 1200))
        self.assertAlmostEqual(p["margin"], (3600 - 400) / 3600 * 100)
        html = self.client.get("/admin/costs").get_data(as_text=True)
        self.assertIn("بدون وصفة", html)
        self.assertEqual(self.client.get("/admin/costs?days=30").status_code, 200)


class SecurityTests(unittest.TestCase):
    def test_csrf_blocks_forged_posts(self):
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        app = create_app({"TESTING": True, "SECRET_KEY": "t", "DATABASE_URL": "sqlite:///" + path})
        client = app.test_client()
        self.assertEqual(client.post("/order", data={"customer_name": "x"}).status_code, 400)
        html = client.get("/admin/setup").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
        r = client.post("/admin/setup", data={"_csrf": token, "display_name": "a", "username": "abc", "password": "12345678"})
        self.assertEqual(r.status_code, 302)
        os.remove(path)


def utils_today(app):
    with app.app_context():
        return utils.today().isoformat()


if __name__ == "__main__":
    unittest.main()

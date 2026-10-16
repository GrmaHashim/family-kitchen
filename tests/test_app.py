"""End-to-end tests. Run with: python -m unittest discover tests   (or: pytest)"""
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

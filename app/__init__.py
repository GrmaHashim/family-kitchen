"""Family Kitchen: a small order-management system for a home kitchen."""
import hmac
import os
import secrets

import click
from flask import Flask, abort, request, session
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash

from . import db, services, utils


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE_URL=os.environ.get("DATABASE_URL", ""),
        KITCHEN_TZ=os.environ.get("KITCHEN_TZ", "America/Edmonton"),
        CSRF_ENABLED=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    if app.config["SECRET_KEY"] == "dev-only-change-me" and os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("Set SECRET_KEY before running in production.")

    # Trust X-Forwarded-* headers from the hosting platform's proxy
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    os.makedirs(app.instance_path, exist_ok=True)
    app.teardown_appcontext(db.close_db)

    with app.app_context():
        db.init_schema()

    _register_csrf(app)
    _register_template_helpers(app)
    _register_cli(app)

    from .admin import bp as admin_bp
    from .public import bp as public_bp
    app.register_blueprint(public_bp)
    app.register_blueprint(admin_bp)

    @app.errorhandler(404)
    def not_found(_e):
        from flask import render_template
        return render_template("error.html", title="الصفحة غير موجودة",
                               message="تأكد من الرابط أو ارجع إلى الصفحة الرئيسية."), 404

    @app.errorhandler(400)
    def bad_request(_e):
        from flask import render_template
        return render_template("error.html", title="انتهت صلاحية الصفحة",
                               message="حدّث الصفحة ثم حاول مرة أخرى."), 400

    return app


# ---------------------------------------------------------------- CSRF
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


def _register_csrf(app):
    @app.before_request
    def check_csrf():
        if not app.config["CSRF_ENABLED"] or request.method in ("GET", "HEAD", "OPTIONS"):
            return
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400)


# ---------------------------------------------------------------- templates
def _register_template_helpers(app):
    app.jinja_env.filters.update(
        money=utils.money,
        time=utils.fmt_time,
        date_ar=utils.fmt_date,
        short_date=utils.short_date,
        day_label=utils.day_label,
        phone=utils.pretty_phone,
    )

    @app.context_processor
    def inject():
        return {
            "csrf_token": csrf_token,
            "S": services.get_settings(),
            "STATUSES": utils.STATUSES,
            "NEXT_STATUS": utils.NEXT_STATUS,
            "NEXT_ACTION": utils.NEXT_ACTION,
            "SOURCES": utils.SOURCES,
            "FULFILLMENT": utils.FULFILLMENT,
            "today_date": utils.today(),
            "pending_reviews": lambda: db.scalar("SELECT COUNT(*) AS n FROM reviews WHERE status = 'pending'") or 0,
        }


# ---------------------------------------------------------------- CLI
SAMPLE_MENU = [
    ("كبسة دجاج", "أطباق رئيسية", "أرز بسمتي بالبهارات مع نصف دجاجة", 1800),
    ("ملوخية باللحم", "أطباق رئيسية", "تُقدَّم مع أرز أبيض", 2000),
    ("سمبوسة لحم (12 حبة)", "مقبلات", "مقلية طازجة أو مجمّدة للقلي في البيت", 1200),
    ("فول بالزيت والجبنة", "فطور", "مع خبز بلدي", 1000),
    ("بسبوسة (صينية صغيرة)", "حلويات", "بالقشطة والمكسرات", 1500),
    ("عصير كركدي (لتر)", "مشروبات", "بارد ومحلّى قليلًا", 600),
]


def _register_cli(app):
    @app.cli.command("create-user")
    @click.argument("username")
    @click.option("--name", "display_name", default=None, help="Display name shown on orders")
    @click.password_option()
    def create_user(username, display_name, password):
        """Create a family member account for the dashboard."""
        username = username.strip().lower()
        if db.query("SELECT id FROM users WHERE username = ?", (username,), one=True):
            raise click.ClickException(f"User '{username}' already exists.")
        db.insert("users", {
            "username": username,
            "display_name": display_name or username,
            "password_hash": generate_password_hash(password),
            "is_active": 1,
            "created_at": utils.now_iso(),
        })
        db.commit()
        click.echo(f"Created user '{username}'.")

    @app.cli.command("seed-menu")
    def seed_menu():
        """Add a few sample dishes (only when the menu is empty)."""
        if db.scalar("SELECT COUNT(*) AS n FROM menu_items"):
            raise click.ClickException("Menu is not empty; nothing was added.")
        for i, (name, category, desc, price) in enumerate(SAMPLE_MENU, start=1):
            db.insert("menu_items", {"name": name, "category": category, "description": desc,
                                     "price_cents": price, "available": 1, "show_online": 1,
                                     "sort_order": i, "created_at": utils.now_iso()})
        db.commit()
        click.echo(f"Added {len(SAMPLE_MENU)} sample dishes.")

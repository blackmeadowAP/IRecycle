import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import (
    Flask,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from werkzeug.utils import secure_filename

APP_DIR = Path(__file__).resolve().parent
DATABASE = APP_DIR / "database.db"
UPLOAD_DIR = APP_DIR / "static" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "gif"}
DEMO_SEED_VERSION = 2

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "ya-othody-demo-key")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def order_collected_tons(db, order_id):
    row = db.execute(
        """
        SELECT COALESCE(SUM(quantity_tons), 0) AS total
        FROM supplier_deliveries
        WHERE order_id = ? AND status IN ('сдано', 'принято')
        """,
        (order_id,),
    ).fetchone()
    return float(row["total"])


def refresh_order_status(db, order_id):
    order = db.execute(
        "SELECT * FROM factory_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if order is None:
        return

    collected = order_collected_tons(db, order_id)
    status = order["status"]

    if status == "сбор" and collected >= order["quantity_tons"]:
        db.execute(
            "UPDATE factory_orders SET status = 'на_складе' WHERE id = ?",
            (order_id,),
        )
    db.commit()


def _table_exists(db, name):
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _column_exists(db, table, column):
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS warehouses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS supplier_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact TEXT,
            capacity_tons REAL NOT NULL,
            location_hint TEXT,
            latitude REAL,
            longitude REAL
        );

        CREATE TABLE IF NOT EXISTS factory_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_name TEXT NOT NULL,
            material TEXT NOT NULL DEFAULT 'Солома',
            quantity_tons REAL NOT NULL,
            delivery_date TEXT NOT NULL,
            prepay_amount REAL NOT NULL,
            reward_supplier_per_ton REAL NOT NULL,
            reward_logistics REAL NOT NULL,
            factory_address TEXT,
            factory_lat REAL NOT NULL,
            factory_lng REAL NOT NULL,
            warehouse_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'сбор',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
        );

        CREATE TABLE IF NOT EXISTS supplier_deliveries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            supplier_name TEXT NOT NULL,
            quantity_tons REAL NOT NULL,
            photo_filename TEXT,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            delivered_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            status TEXT NOT NULL DEFAULT 'сдано',
            FOREIGN KEY (order_id) REFERENCES factory_orders(id)
        );

        CREATE TABLE IF NOT EXISTS order_route_stops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            stop_order INTEGER NOT NULL,
            tons_at_stop REAL NOT NULL DEFAULT 0,
            FOREIGN KEY (order_id) REFERENCES factory_orders(id),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
        );

        CREATE TABLE IF NOT EXISTS supplier_offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            warehouse_id INTEGER NOT NULL,
            supplier_name TEXT NOT NULL,
            quantity_tons REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'активна',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY (order_id) REFERENCES factory_orders(id),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )

    if not _column_exists(db, "supplier_deliveries", "warehouse_id"):
        db.execute(
            "ALTER TABLE supplier_deliveries ADD COLUMN warehouse_id INTEGER "
            "REFERENCES warehouses(id)"
        )

    db.commit()
    db.close()


def _meta_get(db, key, default=None):
    if not _table_exists(db, "app_meta"):
        return default
    row = db.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _meta_set(db, key, value):
    db.execute(
        """
        INSERT INTO app_meta (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def seed_base_catalog(db):
    count = db.execute("SELECT COUNT(*) AS c FROM warehouses").fetchone()["c"]
    if count == 0:
        warehouses = [
            (
                "Склад «Тульский хаб»",
                "Тульская обл., Узловский р-н, с. Дубрава, ул. Полевая, 12",
                53.978,
                38.172,
            ),
            (
                "Склад «Рязанский узел»",
                "Рязанская обл., Скопинский р-н, п. Заречный, д. 5",
                54.002,
                39.548,
            ),
            (
                "Склад «Калужский терминал»",
                "Калужская обл., Жуковский р-н, д. Берёзовка, 3",
                54.936,
                36.712,
            ),
        ]
        for w in warehouses:
            db.execute(
                "INSERT INTO warehouses (name, address, latitude, longitude) VALUES (?, ?, ?, ?)",
                w,
            )

    supplier_count = db.execute(
        "SELECT COUNT(*) AS c FROM supplier_profiles"
    ).fetchone()["c"]
    if supplier_count == 0:
        suppliers = [
            ("ИП Сидоров А.В.", "+7 900 111-22-33", 8, "Тула, с. Красное", 54.12, 37.55),
            ("ИП Козлова М.И.", "+7 900 444-55-66", 12, "Рязань, п. Солнечный", 54.62, 39.71),
            ("ИП Петров Д.С.", "+7 900 777-88-99", 6, "Калуга, д. Луговое", 54.53, 36.25),
            ("ИП Никитина Е.П.", "+7 900 000-11-22", 5, "Тула, д. Яблоневка", 54.08, 37.82),
            ("ИП Волков С.Н.", "+7 900 333-44-55", 10, "Рязань, с. Зелёное", 54.45, 39.90),
        ]
        for s in suppliers:
            db.execute(
                """
                INSERT INTO supplier_profiles
                (name, contact, capacity_tons, location_hint, latitude, longitude)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                s,
            )


def seed_demo_data():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    seed_base_catalog(db)

    current = _meta_get(db, "demo_seed_version")
    if current == str(DEMO_SEED_VERSION):
        db.commit()
        db.close()
        return

    db.execute("DELETE FROM supplier_deliveries")
    db.execute("DELETE FROM supplier_offers")
    db.execute("DELETE FROM order_route_stops")
    db.execute("DELETE FROM factory_orders")

    warehouses = db.execute("SELECT * FROM warehouses ORDER BY id").fetchall()
    if len(warehouses) < 3:
        db.execute("DELETE FROM warehouses")
        db.commit()
        seed_base_catalog(db)
        warehouses = db.execute("SELECT * FROM warehouses ORDER BY id").fetchall()

    wh_tula, wh_ryazan, wh_kaluga = warehouses[0], warehouses[1], warehouses[2]
    today = datetime.now().date()
    d14 = (today + timedelta(days=14)).isoformat()
    d21 = (today + timedelta(days=21)).isoformat()
    d7 = (today + timedelta(days=7)).isoformat()

    # Заказ №1 — сбор соломы, частично закрыт ИП
    db.execute(
        """
        INSERT INTO factory_orders (
            factory_name, material, quantity_tons, delivery_date,
            prepay_amount, reward_supplier_per_ton, reward_logistics,
            factory_address, factory_lat, factory_lng, warehouse_id, status
        ) VALUES (?, 'Солома', 45, ?, 225000, 2800, 62000,
            'Владимирская обл., Муром, промзона «Север», уч. 14', 55.579, 42.052, ?, 'сбор')
        """,
        ("Завод «БиоТопливо Муром»", d14, wh_tula["id"]),
    )
    order1_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    route1 = [
        (order1_id, wh_tula["id"], 1, 12.0),
        (order1_id, wh_ryazan["id"], 2, 18.0),
        (order1_id, wh_kaluga["id"], 3, 15.0),
    ]
    for stop in route1:
        db.execute(
            """
            INSERT INTO order_route_stops (order_id, warehouse_id, stop_order, tons_at_stop)
            VALUES (?, ?, ?, ?)
            """,
            stop,
        )

    offers1 = [
        (order1_id, wh_tula["id"], "ИП Сидоров А.В.", 8.0, "активна"),
        (order1_id, wh_ryazan["id"], "ИП Козлова М.И.", 12.0, "активна"),
        (order1_id, wh_kaluga["id"], "ИП Петров Д.С.", 6.0, "активна"),
        (order1_id, wh_tula["id"], "ИП Никитина Е.П.", 5.0, "доставлено"),
    ]
    for offer in offers1:
        db.execute(
            """
            INSERT INTO supplier_offers (order_id, warehouse_id, supplier_name, quantity_tons, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            offer,
        )

    db.execute(
        """
        INSERT INTO supplier_deliveries
        (order_id, supplier_name, quantity_tons, latitude, longitude, warehouse_id, status)
        VALUES (?, 'ИП Никитина Е.П.', 5.0, ?, ?, ?, 'принято')
        """,
        (order1_id, wh_tula["latitude"], wh_tula["longitude"], wh_tula["id"]),
    )
    db.execute(
        """
        INSERT INTO supplier_deliveries
        (order_id, supplier_name, quantity_tons, latitude, longitude, warehouse_id, status)
        VALUES (?, 'ИП Сидоров А.В.', 4.5, ?, ?, ?, 'сдано')
        """,
        (order1_id, wh_tula["latitude"] + 0.002, wh_tula["longitude"] - 0.001, wh_tula["id"]),
    )

    # Заказ №2 — готов к забору логистом (на складах собран объём)
    db.execute(
        """
        INSERT INTO factory_orders (
            factory_name, material, quantity_tons, delivery_date,
            prepay_amount, reward_supplier_per_ton, reward_logistics,
            factory_address, factory_lat, factory_lng, warehouse_id, status
        ) VALUES (?, 'Солома', 32, ?, 180000, 2600, 54000,
            'Тульская обл., Новомосковск, ул. Промышленная, 8', 54.010, 38.284, ?, 'на_складе')
        """,
        ("Завод «ЭкоПеллет»", d21, wh_ryazan["id"]),
    )
    order2_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    for stop in [
        (order2_id, wh_ryazan["id"], 1, 14.0),
        (order2_id, wh_tula["id"], 2, 10.0),
        (order2_id, wh_kaluga["id"], 3, 8.0),
    ]:
        db.execute(
            """
            INSERT INTO order_route_stops (order_id, warehouse_id, stop_order, tons_at_stop)
            VALUES (?, ?, ?, ?)
            """,
            stop,
        )

    for offer in [
        (order2_id, wh_ryazan["id"], "ИП Козлова М.И.", 10.0, "доставлено"),
        (order2_id, wh_tula["id"], "ИП Волков С.Н.", 8.0, "доставлено"),
        (order2_id, wh_kaluga["id"], "ИП Петров Д.С.", 6.0, "доставлено"),
        (order2_id, wh_ryazan["id"], "ИП Сидоров А.В.", 8.0, "доставлено"),
    ]:
        db.execute(
            """
            INSERT INTO supplier_offers (order_id, warehouse_id, supplier_name, quantity_tons, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            offer,
        )

    for delivery in [
        (order2_id, "ИП Козлова М.И.", 10.0, wh_ryazan),
        (order2_id, "ИП Волков С.Н.", 8.0, wh_tula),
        (order2_id, "ИП Петров Д.С.", 6.0, wh_kaluga),
        (order2_id, "ИП Сидоров А.В.", 8.0, wh_ryazan),
    ]:
        db.execute(
            """
            INSERT INTO supplier_deliveries
            (order_id, supplier_name, quantity_tons, latitude, longitude, warehouse_id, status)
            VALUES (?, ?, ?, ?, ?, ?, 'принято')
            """,
            (
                delivery[0],
                delivery[1],
                delivery[2],
                delivery[3]["latitude"],
                delivery[3]["longitude"],
                delivery[3]["id"],
            ),
        )

    # Заказ №3 — завершённый рейс логиста (демо истории)
    db.execute(
        """
        INSERT INTO factory_orders (
            factory_name, material, quantity_tons, delivery_date,
            prepay_amount, reward_supplier_per_ton, reward_logistics,
            factory_address, factory_lat, factory_lng, warehouse_id, status
        ) VALUES (?, 'Солома', 28, ?, 140000, 2400, 48000,
            'Калужская обл., Обнинск, ул. Промышленная, 2', 55.096, 36.610, ?, 'завершён')
        """,
        ("Завод «ТеплоЭнерго»", d7, wh_kaluga["id"]),
    )
    order3_id = db.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db.execute(
        """
        INSERT INTO order_route_stops (order_id, warehouse_id, stop_order, tons_at_stop)
        VALUES (?, ?, 1, 28.0)
        """,
        (order3_id, wh_kaluga["id"]),
    )

    _meta_set(db, "demo_seed_version", DEMO_SEED_VERSION)
    db.commit()
    db.close()


init_db()
seed_demo_data()


@app.context_processor
def inject_globals():
    return {"now_year": datetime.now().year}


def _route_stops(db, order_id):
    return db.execute(
        """
        SELECT s.*, w.name AS warehouse_name, w.address AS warehouse_address,
               w.latitude, w.longitude
        FROM order_route_stops s
        JOIN warehouses w ON w.id = s.warehouse_id
        WHERE s.order_id = ?
        ORDER BY s.stop_order ASC
        """,
        (order_id,),
    ).fetchall()


def _yandex_route_url(stops, factory_lat, factory_lng):
    points = [f"{s['latitude']},{s['longitude']}" for s in stops]
    points.append(f"{factory_lat},{factory_lng}")
    return "https://yandex.ru/maps/?rtext=" + "~".join(points) + "&rtt=auto"


@app.route("/")
def home():
    db = get_db()
    stats = {
        "orders": db.execute("SELECT COUNT(*) AS c FROM factory_orders").fetchone()["c"],
        "deliveries": db.execute(
            "SELECT COUNT(*) AS c FROM supplier_deliveries"
        ).fetchone()["c"],
        "suppliers": db.execute(
            "SELECT COUNT(*) AS c FROM supplier_profiles"
        ).fetchone()["c"],
        "warehouses": db.execute("SELECT COUNT(*) AS c FROM warehouses").fetchone()["c"],
        "offers": db.execute(
            "SELECT COUNT(*) AS c FROM supplier_offers WHERE status = 'активна'"
        ).fetchone()["c"],
    }
    demo_order = db.execute(
        """
        SELECT o.*, w.name AS warehouse_name
        FROM factory_orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        WHERE o.status = 'на_складе'
        ORDER BY o.id DESC LIMIT 1
        """
    ).fetchone()
    route_preview = _route_stops(db, demo_order["id"]) if demo_order else []
    return render_template(
        "home.html",
        stats=stats,
        demo_order=demo_order,
        route_preview=route_preview,
    )


@app.route("/factory", methods=["GET", "POST"])
def factory():
    db = get_db()

    if request.method == "POST":
        try:
            quantity = float(request.form.get("quantity_tons", 0))
            prepay = float(request.form.get("prepay_amount", 0))
            reward_supplier = float(request.form.get("reward_supplier_per_ton", 0))
            reward_logistics = float(request.form.get("reward_logistics", 0))
            factory_lat = float(request.form.get("factory_lat", 0))
            factory_lng = float(request.form.get("factory_lng", 0))
        except (TypeError, ValueError):
            return render_template(
                "factory.html",
                error="Проверьте числовые поля формы.",
                warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
                orders=_factory_orders(db),
                suppliers=_supplier_summary(db),
            )

        factory_name = (request.form.get("factory_name") or "").strip()
        delivery_date = (request.form.get("delivery_date") or "").strip()
        warehouse_id = request.form.get("warehouse_id")

        if not factory_name or not delivery_date or not warehouse_id:
            return render_template(
                "factory.html",
                error="Заполните название завода, дату и склад.",
                warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
                orders=_factory_orders(db),
                suppliers=_supplier_summary(db),
            )

        if quantity <= 0:
            return render_template(
                "factory.html",
                error="Количество должно быть больше нуля.",
                warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
                orders=_factory_orders(db),
                suppliers=_supplier_summary(db),
            )

        cursor = db.execute(
            """
            INSERT INTO factory_orders (
                factory_name, material, quantity_tons, delivery_date,
                prepay_amount, reward_supplier_per_ton, reward_logistics,
                factory_address, factory_lat, factory_lng, warehouse_id, status
            ) VALUES (?, 'Солома', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'сбор')
            """,
            (
                factory_name,
                quantity,
                delivery_date,
                prepay,
                reward_supplier,
                reward_logistics,
                (request.form.get("factory_address") or "").strip(),
                factory_lat,
                factory_lng,
                int(warehouse_id),
            ),
        )
        order_id = cursor.lastrowid
        wh = db.execute(
            "SELECT * FROM warehouses WHERE id = ?", (int(warehouse_id),)
        ).fetchone()
        db.execute(
            """
            INSERT INTO order_route_stops (order_id, warehouse_id, stop_order, tons_at_stop)
            VALUES (?, ?, 1, 0)
            """,
            (order_id, wh["id"]),
        )
        db.commit()
        return redirect(url_for("factory", ok=1))

    return render_template(
        "factory.html",
        ok=request.args.get("ok"),
        warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
        orders=_factory_orders(db),
        suppliers=_supplier_summary(db),
    )


def _factory_orders(db):
    orders = db.execute(
        """
        SELECT o.*, w.name AS warehouse_name, w.address AS warehouse_address
        FROM factory_orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        ORDER BY o.id DESC
        """
    ).fetchall()
    result = []
    for o in orders:
        collected = order_collected_tons(db, o["id"])
        stops = _route_stops(db, o["id"])
        result.append({**dict(o), "collected_tons": collected, "route_stops": stops})
    return result


def _supplier_summary(db):
    profiles = db.execute("SELECT * FROM supplier_profiles ORDER BY name").fetchall()
    delivered = db.execute(
        """
        SELECT supplier_name, COALESCE(SUM(quantity_tons), 0) AS delivered
        FROM supplier_deliveries
        GROUP BY supplier_name
        """
    ).fetchall()
    delivered_map = {r["supplier_name"]: r["delivered"] for r in delivered}
    rows = []
    for p in profiles:
        d = float(delivered_map.get(p["name"], 0))
        rows.append(
            {
                **dict(p),
                "delivered_tons": d,
                "available_tons": max(0, p["capacity_tons"] - d),
            }
        )
    return rows


@app.route("/supplier", methods=["GET", "POST"])
def supplier():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "deliver")

        if action == "offer":
            try:
                order_id = int(request.form.get("order_id"))
                warehouse_id = int(request.form.get("warehouse_id"))
                quantity = float(request.form.get("quantity_tons"))
            except (TypeError, ValueError):
                return _supplier_render(db, error="Проверьте поля заявки.")

            supplier_name = (request.form.get("supplier_name") or "").strip()
            if not supplier_name or quantity <= 0:
                return _supplier_render(db, error="Укажите ИП и объём больше нуля.")

            order = db.execute(
                "SELECT * FROM factory_orders WHERE id = ? AND status = 'сбор'",
                (order_id,),
            ).fetchone()
            if order is None:
                return _supplier_render(db, error="Заявка не найдена или закрыта.")

            db.execute(
                """
                INSERT INTO supplier_offers
                (order_id, warehouse_id, supplier_name, quantity_tons, status)
                VALUES (?, ?, ?, ?, 'активна')
                """,
                (order_id, warehouse_id, supplier_name, quantity),
            )
            db.commit()
            return redirect(url_for("supplier", ok="offer"))

        order_id = request.form.get("order_id")
        supplier_name = (request.form.get("supplier_name") or "").strip()
        quantity_raw = request.form.get("quantity_tons")
        lat = request.form.get("latitude")
        lng = request.form.get("longitude")
        warehouse_id = request.form.get("warehouse_id")
        photo = request.files.get("photo")

        try:
            quantity = float(quantity_raw)
            latitude = float(lat)
            longitude = float(lng)
            order_id = int(order_id)
            warehouse_id = int(warehouse_id)
        except (TypeError, ValueError):
            return _supplier_render(
                db, error="Заполните все поля и укажите геолокацию."
            )

        if not supplier_name or quantity <= 0:
            return _supplier_render(db, error="Укажите имя ИП и объём больше нуля.")

        order = db.execute(
            "SELECT * FROM factory_orders WHERE id = ? AND status = 'сбор'",
            (order_id,),
        ).fetchone()
        if order is None:
            return _supplier_render(
                db, error="Заявка не найдена или уже закрыта для поставок."
            )

        filename = None
        if photo and photo.filename:
            if not allowed_file(photo.filename):
                return _supplier_render(
                    db, error="Фото: допустимы PNG, JPG, WEBP, GIF."
                )
            ext = photo.filename.rsplit(".", 1)[1].lower()
            filename = secure_filename(
                f"delivery_{order_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
            )
            photo.save(UPLOAD_DIR / filename)

        db.execute(
            """
            INSERT INTO supplier_deliveries
            (order_id, supplier_name, quantity_tons, photo_filename,
             latitude, longitude, warehouse_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id,
                supplier_name,
                quantity,
                filename,
                latitude,
                longitude,
                warehouse_id,
            ),
        )
        db.execute(
            """
            UPDATE supplier_offers SET status = 'доставлено'
            WHERE order_id = ? AND supplier_name = ? AND status = 'активна'
            """,
            (order_id, supplier_name),
        )
        db.commit()
        refresh_order_status(db, order_id)
        return redirect(url_for("supplier", ok="deliver"))

    return _supplier_render(db)


def _supplier_render(db, error=None, ok=None):
    if ok is None:
        ok = request.args.get("ok")
    return render_template(
        "supplier.html",
        ok=ok,
        error=error,
        orders=_open_orders_for_suppliers(db),
        warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
        offers=db.execute(
            """
            SELECT f.*, o.factory_name, o.material, o.reward_supplier_per_ton,
                   w.name AS warehouse_name, w.address AS warehouse_address
            FROM supplier_offers f
            JOIN factory_orders o ON o.id = f.order_id
            JOIN warehouses w ON w.id = f.warehouse_id
            ORDER BY f.id DESC
            """
        ).fetchall(),
        recent_deliveries=db.execute(
            """
            SELECT d.*, o.factory_name, o.material, w.name AS warehouse_name
            FROM supplier_deliveries d
            JOIN factory_orders o ON o.id = d.order_id
            LEFT JOIN warehouses w ON w.id = d.warehouse_id
            ORDER BY d.id DESC LIMIT 12
            """
        ).fetchall(),
    )


def _open_orders_for_suppliers(db):
    rows = db.execute(
        """
        SELECT o.*, w.name AS warehouse_name, w.address AS warehouse_address,
               w.latitude AS wh_lat, w.longitude AS wh_lng
        FROM factory_orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        WHERE o.status = 'сбор'
        ORDER BY o.delivery_date ASC
        """
    ).fetchall()
    result = []
    for o in rows:
        collected = order_collected_tons(db, o["id"])
        remaining = max(0, o["quantity_tons"] - collected)
        stops = _route_stops(db, o["id"])
        result.append(
            {
                **dict(o),
                "collected_tons": collected,
                "remaining_tons": remaining,
                "route_stops": stops,
            }
        )
    return result


@app.route("/warehouse")
def warehouse():
    db = get_db()
    orders = db.execute(
        """
        SELECT o.*, w.name AS warehouse_name, w.address AS warehouse_address
        FROM factory_orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        ORDER BY o.id DESC
        """
    ).fetchall()
    orders_data = []
    for o in orders:
        collected = order_collected_tons(db, o["id"])
        deliveries = db.execute(
            """
            SELECT d.*, w.name AS warehouse_name
            FROM supplier_deliveries d
            LEFT JOIN warehouses w ON w.id = d.warehouse_id
            WHERE d.order_id = ?
            ORDER BY d.delivered_at DESC
            """,
            (o["id"],),
        ).fetchall()
        orders_data.append(
            {
                **dict(o),
                "collected_tons": collected,
                "deliveries": [dict(d) for d in deliveries],
            }
        )

    return render_template(
        "warehouse.html",
        warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
        orders=orders_data,
    )


@app.route("/warehouse/accept/<int:delivery_id>", methods=["POST"])
def warehouse_accept(delivery_id):
    db = get_db()
    db.execute(
        "UPDATE supplier_deliveries SET status = 'принято' WHERE id = ?",
        (delivery_id,),
    )
    db.commit()
    return redirect(url_for("warehouse", ok=1))


@app.route("/logistics", methods=["GET", "POST"])
def logistics():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")
        order_id = request.form.get("order_id")
        try:
            order_id = int(order_id)
        except (TypeError, ValueError):
            return redirect(url_for("logistics"))

        order = db.execute(
            "SELECT * FROM factory_orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            return redirect(url_for("logistics"))

        if action == "take" and order["status"] == "на_складе":
            db.execute(
                "UPDATE factory_orders SET status = 'в_дороге' WHERE id = ?",
                (order_id,),
            )
            db.commit()
        elif action == "complete" and order["status"] == "в_дороге":
            db.execute(
                "UPDATE factory_orders SET status = 'завершён' WHERE id = ?",
                (order_id,),
            )
            db.commit()

        return redirect(url_for("logistics", ok=1))

    orders = db.execute(
        """
        SELECT o.*, w.name AS warehouse_name, w.address AS warehouse_address,
               w.latitude AS wh_lat, w.longitude AS wh_lng
        FROM factory_orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        WHERE o.status IN ('на_складе', 'в_дороге', 'сбор')
        ORDER BY
            CASE o.status
                WHEN 'на_складе' THEN 0
                WHEN 'в_дороге' THEN 1
                ELSE 2
            END,
            o.delivery_date ASC
        """
    ).fetchall()
    orders_data = []
    for o in orders:
        collected = order_collected_tons(db, o["id"])
        stops = _route_stops(db, o["id"])
        orders_data.append(
            {
                **dict(o),
                "collected_tons": collected,
                "route_stops": stops,
                "route_url": _yandex_route_url(
                    stops, o["factory_lat"], o["factory_lng"]
                ),
            }
        )

    completed = db.execute(
        """
        SELECT o.*, w.name AS warehouse_name
        FROM factory_orders o
        JOIN warehouses w ON w.id = o.warehouse_id
        WHERE o.status = 'завершён'
        ORDER BY o.id DESC LIMIT 5
        """
    ).fetchall()
    completed_data = []
    for o in completed:
        stops = _route_stops(db, o["id"])
        completed_data.append(
            {
                **dict(o),
                "route_stops": stops,
                "route_url": _yandex_route_url(
                    stops, o["factory_lat"], o["factory_lng"]
                ),
            }
        )

    return render_template(
        "logistics.html",
        ok=request.args.get("ok"),
        warehouses=db.execute("SELECT * FROM warehouses").fetchall(),
        orders=orders_data,
        completed=completed_data,
    )


# --- Legacy routes (пилорама MVP) ---
@app.route("/pilorama")
def pilorama_legacy():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard_legacy():
    db = get_db()
    if not _table_exists(db, "zayavki"):
        return redirect(url_for("home"))
    rows = db.execute(
        "SELECT * FROM zayavki WHERE status = ? ORDER BY id DESC", ("Активна",)
    ).fetchall()
    return render_template("dashboard.html", zayavki=rows)


@app.route("/api/zayavki", methods=["POST"])
def create_zayavka_legacy():
    if not _table_exists(get_db(), "zayavki"):
        return jsonify({"ok": False, "error": "Legacy API disabled."}), 410
    data = request.get_json(silent=True) or {}
    nazvanie = (data.get("nazvanie_piloramy") or "").strip()
    try:
        obem = float(data.get("obem_opilok"))
        latitude = float(data.get("latitude"))
        longitude = float(data.get("longitude"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Некорректные данные."}), 400
    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO zayavki (nazvanie_piloramy, obem_opilok, latitude, longitude, status)
        VALUES (?, ?, ?, ?, 'Активна')
        """,
        (nazvanie, obem, latitude, longitude),
    )
    db.commit()
    return jsonify({"ok": True, "id": cursor.lastrowid})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)

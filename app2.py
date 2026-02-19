from flask import Flask, render_template, request, redirect, send_file, jsonify
from openpyxl import load_workbook, Workbook
import os
import psycopg2
from psycopg2 import pool
from urllib.parse import urlparse

app = Flask(__name__)

@app.before_first_request
def startup():
    global db_pool
    if db_pool is None:
        print("INIT DB POOL...")
        init_pool()
        init_db()

UPLOAD_FOLDER = "uploads"

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

# ---------------- DB POOL ---------------- #

db_pool = None

def init_pool():
    global db_pool
    database_url = os.environ.get("DATABASE_URL")

    if not database_url:
        raise Exception("DATABASE_URL not set")

    result = urlparse(database_url)

    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 10,
        database=result.path[1:],
        user=result.username,
        password=result.password,
        host=result.hostname,
        port=result.port,
        sslmode="require"
    )


def get_connection():
    global db_pool
    if db_pool is None:
        init_pool()
    return db_pool.getconn()



def release_connection(conn):
    db_pool.putconn(conn)


def init_db():
    conn = get_connection()
    try:
        c = conn.cursor()

        c.execute("""
        CREATE TABLE IF NOT EXISTS warehouses (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            warehouse TEXT,
            location TEXT,
            model TEXT,
            description TEXT,
            inv_qty INTEGER,
            act_qty INTEGER DEFAULT 0
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS scans (
            id SERIAL PRIMARY KEY,
            full_barcode TEXT,
            warehouse TEXT,
            UNIQUE(full_barcode, warehouse)
        )
        """)

        conn.commit()
    except Exception as e:
        conn.rollback()
        print("INIT DB ERROR:", e)
    finally:
        release_connection(conn)


# ✅ wake DB (เบาๆ ไม่สร้าง connection ค้าง)
@app.before_request
def before_request():
    try:
        conn = get_connection()
        release_connection(conn)
    except Exception as e:
        print("DB WAKE ERROR:", e)


@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------- FUNCTIONS ---------------- #

def get_warehouses():
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM warehouses ORDER BY name")
        rows = c.fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        print("GET WAREHOUSE ERROR:", e)
        return []
    finally:
        release_connection(conn)


def get_products(warehouse):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, location, model, description, inv_qty, act_qty
            FROM products
            WHERE warehouse=%s
        """, (warehouse,))
        return c.fetchall()
    except Exception as e:
        print("GET PRODUCT ERROR:", e)
        return []
    finally:
        release_connection(conn)


# ---------------- ROUTES ---------------- #

@app.route("/")
def index():
    warehouses = get_warehouses()

    if not warehouses:
        return "OK"

    return redirect(f"/warehouse/{warehouses[0]}")


@app.route("/add_warehouse", methods=["POST"])
def add_warehouse():
    data = request.get_json()
    name = data.get("name")

    if not name:
        return jsonify({"success": False})

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO warehouses (name) VALUES (%s)", (name,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        conn.rollback()
        print("ADD ERROR:", e)
        return jsonify({"success": False})
    finally:
        release_connection(conn)


@app.route("/warehouse/<warehouse>")
def warehouse_page(warehouse):
    try:
        warehouses = get_warehouses()
        products = get_products(warehouse)

        return render_template(
            "warehouse.html",
            warehouses=warehouses,
            warehouse=warehouse,
            products=products
        )
    except Exception as e:
        print("PAGE ERROR:", e)
        return "ERROR PAGE"


# -------- IMPORT EXCEL -------- #

@app.route("/import/<warehouse>", methods=["POST"])
def import_excel(warehouse):
    conn = get_connection()
    try:
        file = request.files["file"]
        path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(path)

        wb = load_workbook(path)
        ws = wb.active

        c = conn.cursor()

        c.execute("DELETE FROM products WHERE warehouse=%s", (warehouse,))
        c.execute("DELETE FROM scans WHERE warehouse=%s", (warehouse,))

        for row in ws.iter_rows(min_row=2, values_only=True):
            location, model, description, inv_qty = row[:4]

            c.execute("""
                INSERT INTO products
                (warehouse, location, model, description, inv_qty, act_qty)
                VALUES (%s,%s,%s,%s,%s,0)
            """, (warehouse, location, model, description, inv_qty))

        conn.commit()
        return redirect(f"/warehouse/{warehouse}")

    except Exception as e:
        conn.rollback()
        print("IMPORT ERROR:", e)
        return "IMPORT FAIL"
    finally:
        release_connection(conn)


# -------- SCAN -------- #

@app.route("/scan", methods=["POST"])
def scan():
    conn = get_connection()
    try:
        barcode = request.form.get("barcode")
        warehouse = request.form.get("warehouse")

        if not barcode:
            return jsonify({"status": "not_found"})

        model = barcode[:9].upper()

        cur = conn.cursor()

        cur.execute("""
            SELECT id, act_qty
            FROM products
            WHERE model=%s AND warehouse=%s
        """, (model, warehouse))

        row = cur.fetchone()

        if not row:
            return jsonify({"status": "not_found"})

        product_id, act = row

        # ✅ ไม่ใช้ exception แล้ว
        cur.execute("""
            INSERT INTO scans (full_barcode, warehouse)
            VALUES (%s,%s)
            ON CONFLICT DO NOTHING
        """, (barcode, warehouse))

        if cur.rowcount == 0:
            return jsonify({"status": "duplicate"})

        new_act = act + 1

        cur.execute("""
            UPDATE products
            SET act_qty=%s
            WHERE id=%s
        """, (new_act, product_id))

        conn.commit()
        return jsonify({"status": "success"})

    except Exception as e:
        conn.rollback()
        print("SCAN ERROR:", e)
        return jsonify({"status": "error"})
    finally:
        release_connection(conn)


# -------- DELETE -------- #

@app.route("/delete_selected", methods=["POST"])
def delete_selected():
    conn = get_connection()
    try:
        data = request.get_json()
        ids = data.get("ids", [])

        if not ids:
            return "No data"

        ids = [int(i) for i in ids]

        c = conn.cursor()
        c.execute(
            "DELETE FROM products WHERE id = ANY(%s::int[])",
            (ids,)
        )

        conn.commit()
        return "OK"

    except Exception as e:
        conn.rollback()
        print("DELETE ERROR:", e)
        return "ERROR"
    finally:
        release_connection(conn)


# -------- EXPORT -------- #

@app.route("/export/<warehouse>")
def export_excel(warehouse):
    conn = get_connection()
    try:
        c = conn.cursor()

        c.execute("""
            SELECT location, model, description, inv_qty, act_qty
            FROM products WHERE warehouse=%s
        """, (warehouse,))

        rows = c.fetchall()

        wb = Workbook()
        ws = wb.active

        ws.append(["Location", "Model Code", "Description", "Inv.Qty", "Act.Qty"])

        for row in rows:
            ws.append(row)

        file_path = os.path.join(UPLOAD_FOLDER, f"{warehouse}_result.xlsx")
        wb.save(file_path)

        return send_file(file_path, as_attachment=True)

    except Exception as e:
        conn.rollback()
        print("EXPORT ERROR:", e)
        return "EXPORT FAIL"
    finally:
        release_connection(conn)


# ---------------- RUN ---------------- #

if __name__ == "__main__":
    init_pool()   # ✅ สำคัญมาก
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

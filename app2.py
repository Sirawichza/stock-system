from flask import Flask, render_template, request, redirect, send_file, jsonify
import sqlite3
from openpyxl import load_workbook, Workbook
import os
import psycopg2
from urllib.parse import urlparse

app = Flask(__name__)

DB_NAME = "database.db"
UPLOAD_FOLDER = "uploads"

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


# ---------------- DB ---------------- #

def get_connection():
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        # ใช้ PostgreSQL บน Render
        result = urlparse(database_url)
        conn = psycopg2.connect(
            database=result.path[1:],
            user=result.username,
            password=result.password,
            host=result.hostname,
            port=result.port
        )
        return conn
    else:
        # ใช้ SQLite ตอนรันในเครื่อง
        return sqlite3.connect(DB_NAME)


def init_db():
    conn = get_connection()
    c = conn.cursor()

    # PostgreSQL ไม่มี AUTOINCREMENT ต้องใช้ SERIAL
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
    conn.close()


def get_warehouses():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT name FROM warehouses ORDER BY name")
    rows = c.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_products(warehouse):
    conn = get_connection()
    c = conn.cursor()
    c.execute("""
        SELECT id, location, model, description, inv_qty, act_qty
        FROM products
        WHERE warehouse=%s
    """, (warehouse,))
    rows = c.fetchall()
    conn.close()
    return rows


# ---------------- ROUTES ---------------- #

@app.route("/")
def index():
    warehouses = get_warehouses()

    if not warehouses:
        return render_template(
            "warehouse.html",
            warehouses=[],
            warehouse="",
            products=[]
        )

    return redirect(f"/warehouse/{warehouses[0]}")


@app.route("/add_warehouse", methods=["POST"])
def add_warehouse():
    data = request.get_json()
    name = data.get("name")

    if not name:
        return jsonify({"success": False})

    conn = get_connection()
    c = conn.cursor()

    try:
        c.execute("INSERT INTO warehouses (name) VALUES (%s)", (name,))
        conn.commit()
    except:
        conn.close()
        return jsonify({"success": False})

    conn.close()
    return jsonify({"success": True})


@app.route("/warehouse/<warehouse>")
def warehouse_page(warehouse):

    warehouses = get_warehouses()
    products = get_products(warehouse)

    return render_template(
        "warehouse.html",
        warehouses=warehouses,
        warehouse=warehouse,
        products=products
    )


# -------- IMPORT EXCEL -------- #

@app.route("/import/<warehouse>", methods=["POST"])
def import_excel(warehouse):

    file = request.files["file"]
    path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(path)

    wb = load_workbook(path)
    ws = wb.active

    conn = get_connection()
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
    conn.close()

    return redirect(f"/warehouse/{warehouse}")


# -------- SCAN -------- #

@app.route("/scan", methods=["POST"])
def scan():

    barcode = request.form.get("barcode")
    warehouse = request.form.get("warehouse")

    if not barcode:
        return jsonify({"status": "not_found"})

    model = barcode[:9].upper()

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT id, act_qty
        FROM products
        WHERE model=%s AND warehouse=%s
    """, (model, warehouse))

    row = cur.fetchone()

    if not row:
        conn.close()
        return jsonify({"status": "not_found"})

    product_id, act = row

    try:
        cur.execute("""
            INSERT INTO scans (full_barcode, warehouse)
            VALUES (%s,%s)
        """, (barcode, warehouse))
    except Exception:
        conn.close()
        return jsonify({"status": "duplicate"})

    new_act = act + 1

    cur.execute("""
        UPDATE products
        SET act_qty=%s
        WHERE id=%s
    """, (new_act, product_id))

    conn.commit()
    conn.close()

    return jsonify({
        "status": "success",
        "model": model
    })


# -------- DELETE SELECTED -------- #

@app.route("/delete_selected", methods=["POST"])
def delete_selected():

    data = request.get_json()
    ids = data.get("ids", [])

    if not ids:
        return "No data"

    conn = get_connection()
    c = conn.cursor()

    for id in ids:
        c.execute("DELETE FROM products WHERE id=%s", (id,))

    conn.commit()
    conn.close()

    return "OK"


# -------- EXPORT -------- #

@app.route("/export/<warehouse>")
def export_excel(warehouse):

    conn = get_connection()
    c = conn.cursor()

    c.execute("""
        SELECT location, model, description, inv_qty, act_qty
        FROM products WHERE warehouse=%s
    """, (warehouse,))

    rows = c.fetchall()
    conn.close()

    wb = Workbook()
    ws = wb.active

    ws.append(["Location", "Model Code",
               "Description", "Inv.Qty", "Act.Qty"])

    for row in rows:
        ws.append(row)

    file_path = os.path.join(
        UPLOAD_FOLDER,
        f"{warehouse}_result.xlsx"
    )

    wb.save(file_path)

    return send_file(file_path, as_attachment=True)




# ---------------- RUN ---------------- #

# ---------------- RUN ---------------- #

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)



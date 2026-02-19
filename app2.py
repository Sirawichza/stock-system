from flask import Flask, render_template, request, redirect, send_file, jsonify
from openpyxl import load_workbook, Workbook
import os
import psycopg2
import gc

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ================= DB (FIX จบจริง) ================= #

def get_connection():
    try:
        conn = psycopg2.connect(
            os.environ.get("DATABASE_URL"),
            sslmode="require",
            connect_timeout=10
        )
        conn.autocommit = False
        return conn
    except Exception as e:
        print("DB CONNECT ERROR:", e)
        return None


# ================= INIT DB ================= #

def init_db():
    conn = get_connection()
    if not conn:
        return

    try:
        with conn.cursor() as c:
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
    finally:
        conn.close()


# ================= NO CACHE ================= #

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-store"
    gc.collect()
    return response


# ================= FUNCTIONS ================= #

def get_warehouses():
    conn = get_connection()
    if not conn:
        return []

    try:
        with conn.cursor() as c:
            c.execute("SELECT name FROM warehouses ORDER BY name")
            rows = c.fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        print("GET WAREHOUSE ERROR:", e)
        return []
    finally:
        conn.close()


def get_products(warehouse):
    conn = get_connection()
    if not conn:
        return []

    try:
        with conn.cursor() as c:
            c.execute("""
                SELECT id, location, model, description, inv_qty, act_qty
                FROM products
                WHERE warehouse=%s
            """, (warehouse,))
            rows = c.fetchall()
        return rows
    except Exception as e:
        print("GET PRODUCT ERROR:", e)
        return []
    finally:
        conn.close()


# ================= ROUTES ================= #

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
    if not conn:
        return jsonify({"success": False})

    try:
        with conn.cursor() as c:
            c.execute("INSERT INTO warehouses (name) VALUES (%s)", (name,))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        print("ADD ERROR:", e)
        return jsonify({"success": False})
    finally:
        conn.close()


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


# ================= IMPORT ================= #

@app.route("/import/<warehouse>", methods=["POST"])
def import_excel(warehouse):
    file = request.files["file"]
    path = os.path.join(UPLOAD_FOLDER, file.filename)
    file.save(path)

    wb = load_workbook(path)
    ws = wb.active

    conn = get_connection()
    if not conn:
        return "DB ERROR"

    try:
        with conn.cursor() as c:
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
        print("IMPORT ERROR:", e)
        return "IMPORT FAIL"
    finally:
        conn.close()


# ================= SCAN ================= #

@app.route("/scan", methods=["POST"])
def scan():
    barcode = request.form.get("barcode")
    warehouse = request.form.get("warehouse")

    if not barcode:
        return jsonify({"status": "not_found"})

    model = barcode[:9].upper()

    conn = get_connection()
    if not conn:
        return jsonify({"status": "error"})

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, act_qty
                FROM products
                WHERE model=%s AND warehouse=%s
            """, (model, warehouse))

            row = cur.fetchone()

            if not row:
                return jsonify({"status": "not_found"})

            product_id, act = row

            try:
                cur.execute("""
                    INSERT INTO scans (full_barcode, warehouse)
                    VALUES (%s,%s)
                """, (barcode, warehouse))
            except:
                return jsonify({"status": "duplicate"})

            cur.execute("""
                UPDATE products
                SET act_qty=%s
                WHERE id=%s
            """, (act + 1, product_id))

        conn.commit()
        return jsonify({"status": "success"})

    except Exception as e:
        print("SCAN ERROR:", e)
        return jsonify({"status": "error"})
    finally:
        conn.close()


# ================= DELETE ================= #

@app.route("/delete_selected", methods=["POST"])
def delete_selected():
    data = request.get_json()
    ids = [int(i) for i in data.get("ids", [])]

    if not ids:
        return "No data"

    conn = get_connection()
    if not conn:
        return "ERROR"

    try:
        with conn.cursor() as c:
            c.execute("DELETE FROM products WHERE id = ANY(%s)", (ids,))
        conn.commit()
        return "OK"
    except Exception as e:
        print("DELETE ERROR:", e)
        return "ERROR"
    finally:
        conn.close()


# ================= EXPORT ================= #

@app.route("/export/<warehouse>")
def export_excel(warehouse):
    conn = get_connection()
    if not conn:
        return "DB ERROR"

    try:
        with conn.cursor() as c:
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
        print("EXPORT ERROR:", e)
        return "EXPORT FAIL"
    finally:
        conn.close()


# ================= RUN ================= #

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

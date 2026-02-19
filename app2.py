from flask import Flask, render_template, request, redirect, send_file, jsonify
from openpyxl import load_workbook, Workbook
import os
import psycopg2
from psycopg2 import pool
from urllib.parse import urlparse
db_pool = None
db_initialized = False

app = Flask(__name__)



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
    global db_pool, db_initialized

    try:
        if db_pool is None:
            print("🔵 INIT POOL")
            init_pool()

        if not db_initialized:
            print("🟢 INIT DB")
            init_db()
            db_initialized = True

        conn = db_pool.getconn()
        return conn

    except Exception as e:
        print("❌ GET CONNECTION ERROR:", e)
        raise


def release_connection(conn):
    db_pool.putconn(conn)


def init_db():
    global db_pool

    print("🟢 INIT DB")

    conn = None
    cur = None

    try:
        # ✅ ใช้ connection จาก pool โดยตรง (สำคัญมาก)
        conn = db_pool.getconn()
        cur = conn.cursor()

        # =========================
        # 🔽 ใส่ TABLE เดิมของคุณตรงนี้
        # (ห้ามเรียก get_connection())
        # =========================

        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            location TEXT,
            model TEXT,
            description TEXT,
            inv_qty INTEGER DEFAULT 0,
            act_qty INTEGER DEFAULT 0
        )
        """)

        # 👉 ถ้ามี table อื่น ใส่ต่อได้เลย เช่น:
        # cur.execute("""CREATE TABLE IF NOT EXISTS ...""")

        conn.commit()

    except Exception as e:
        print("❌ INIT DB ERROR:", e)

    finally:
        # ✅ ปิด cursor
        if cur:
            cur.close()

        # ✅ คืน connection กลับ pool
        if conn:
            db_pool.putconn(conn)



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
@app.route('/scan', methods=['POST'])
def scan():
    try:
        data = request.get_json()

        model_code = data.get('model_code')
        location   = data.get('location')

        if not model_code:
            return jsonify({"status": "error", "message": "ไม่มี model_code"})

        if not location:
            return jsonify({"status": "error", "message": "กรุณาเลือก location"})

        conn = get_connection()
        cur = conn.cursor()

        # ✅ เช็คซ้ำ
        cur.execute("""
            SELECT 1 FROM stock
            WHERE model_code = %s AND location = %s
        """, (model_code, location))

        if cur.fetchone():
            return jsonify({
                "status": "duplicate",
                "message": "❌ บาร์โค้ดนี้มีแล้วใน location นี้"
            })

        # ✅ insert
        cur.execute("""
            INSERT INTO stock (model_code, location)
            VALUES (%s, %s)
        """, (model_code, location))

        # ✅ บวก Act.Qty
        cur.execute("""
            UPDATE products
            SET act_qty = act_qty + 1
            WHERE model = %s AND location = %s
        """, (model_code, location))

        conn.commit()

        return jsonify({
            "status": "success",
            "message": "✅ บันทึกสำเร็จ"
        })

    except Exception as e:
        print("SCAN ERROR:", e)
        return jsonify({
            "status": "error",
            "message": str(e)
        })






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
   
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

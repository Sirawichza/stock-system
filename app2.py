from flask import Flask, render_template, request, jsonify, send_file
import psycopg2
from psycopg2 import pool
import os
from openpyxl import Workbook

app = Flask(__name__)

UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ================= DB POOL =================
db_pool = None

def init_pool():
    global db_pool
    db_pool = psycopg2.pool.SimpleConnectionPool(
        1, 10,
        host=os.environ.get("DB_HOST"),
        database=os.environ.get("DB_NAME"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASS"),
        port=os.environ.get("DB_PORT", 5432)
    )

def get_connection():
    return db_pool.getconn()

def release_connection(conn):
    db_pool.putconn(conn)

# ================= INIT DB =================
def init_db():
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        # ตารางหลัก
        cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY,
            location TEXT,
            model TEXT,
            description TEXT,
            inv_qty INT DEFAULT 0,
            act_qty INT DEFAULT 0
        )
        """)

        # ตารางกันสแกนซ้ำ
        cur.execute("""
        CREATE TABLE IF NOT EXISTS stock (
            id SERIAL PRIMARY KEY,
            model_code TEXT,
            location TEXT
        )
        """)

        conn.commit()

    except Exception as e:
        print("INIT DB ERROR:", e)

    finally:
        if conn:
            release_connection(conn)

# ================= ROUTE =================
@app.route("/warehouse/<warehouse>")
def warehouse(warehouse):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT location, model, description, inv_qty, act_qty
            FROM products
            WHERE warehouse=%s
        """, (warehouse,))

        rows = cur.fetchall()

        return render_template("warehouse.html", rows=rows, warehouse=warehouse)

    finally:
        if conn:
            release_connection(conn)

# ================= SCAN =================
@app.route("/scan", methods=["POST"])
def scan():
    conn = None
    try:
        data = request.get_json()

        model_code = data.get("model_code")
        location = data.get("location")

        if not model_code or not location:
            return jsonify({"status": "error", "message": "ข้อมูลไม่ครบ"})

        conn = get_connection()
        cur = conn.cursor()

        # 🔴 เช็คซ้ำ
        cur.execute("""
            SELECT 1 FROM stock
            WHERE model_code=%s AND location=%s
        """, (model_code, location))

        if cur.fetchone():
            return jsonify({
                "status": "duplicate",
                "message": "❌ สแกนซ้ำ"
            })

        # 🔴 เช็คว่ามีสินค้าไหม
        cur.execute("""
            SELECT act_qty FROM products
            WHERE model=%s AND location=%s
        """, (model_code, location))

        row = cur.fetchone()

        if not row:
            return jsonify({
                "status": "error",
                "message": "❌ ไม่มีบาร์โค้ดใน location นี้"
            })

        # 🔴 เพิ่มค่า
        cur.execute("""
            UPDATE products
            SET act_qty = act_qty + 1
            WHERE model=%s AND location=%s
        """, (model_code, location))

        # 🔴 บันทึกสแกน
        cur.execute("""
            INSERT INTO stock (model_code, location)
            VALUES (%s, %s)
        """, (model_code, location))

        conn.commit()

        return jsonify({
            "status": "success",
            "message": "OK"
        })

    except Exception as e:
        print("SCAN ERROR:", e)
        return jsonify({"status": "error", "message": str(e)})

    finally:
        if conn:
            release_connection(conn)

# ================= EXPORT =================
@app.route("/export/<warehouse>")
def export_excel(warehouse):
    conn = None
    try:
        conn = get_connection()
        cur = conn.cursor()

        cur.execute("""
            SELECT location, model, description, inv_qty, act_qty
            FROM products
            WHERE warehouse=%s
        """, (warehouse,))

        rows = cur.fetchall()

        wb = Workbook()
        ws = wb.active
        ws.append(["Location", "Model", "Description", "Inv.Qty", "Act.Qty"])

        for r in rows:
            ws.append(r)

        file_path = os.path.join(UPLOAD_FOLDER, f"{warehouse}.xlsx")
        wb.save(file_path)

        return send_file(file_path, as_attachment=True)

    finally:
        if conn:
            release_connection(conn)

# ================= RUN =================
init_pool()
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

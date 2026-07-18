from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import psycopg2, psycopg2.extras, time, os, cloudinary, cloudinary.uploader

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Cloudinary config (set these as env vars on Render)
cloudinary.config(
    cloud_name=os.environ.get("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.environ.get("CLOUDINARY_API_KEY", ""),
    api_secret=os.environ.get("CLOUDINARY_API_SECRET", ""),
)

def get_db():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()

    # Users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            device_id TEXT PRIMARY KEY,
            nickname TEXT NOT NULL,
            radius_km INTEGER NOT NULL DEFAULT 5,
            email TEXT,
            created_at DOUBLE PRECISION NOT NULL,
            last_seen DOUBLE PRECISION NOT NULL
        )
    """)

    # Items table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id SERIAL PRIMARY KEY,
            device_id TEXT NOT NULL REFERENCES users(device_id),
            post_type TEXT NOT NULL DEFAULT 'give',  -- 'give' or 'take'
            title TEXT NOT NULL,
            description TEXT,
            category TEXT NOT NULL,
            image_url TEXT,
            lat DOUBLE PRECISION NOT NULL,
            lon DOUBLE PRECISION NOT NULL,
            status TEXT NOT NULL DEFAULT 'available',  -- 'available', 'taken'
            created_at DOUBLE PRECISION NOT NULL,
            reminded_at DOUBLE PRECISION
        )
    """)
    
    # Add post_type column if it doesnt exist (for existing tables)
    cur.execute("""
        ALTER TABLE items ADD COLUMN IF NOT EXISTS post_type TEXT NOT NULL DEFAULT 'give'
    """)

    # Requests table (someone wants an item)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id SERIAL PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id),
            device_id TEXT NOT NULL REFERENCES users(device_id),
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(item_id, device_id)
        )
    """)

    # Image reports table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS image_reports (
            id SERIAL PRIMARY KEY,
            item_id INTEGER NOT NULL REFERENCES items(id),
            reporter_device_id TEXT NOT NULL,
            reason TEXT,
            created_at DOUBLE PRECISION NOT NULL
        )
    """)

    conn.commit()
    cur.close()
    conn.close()

init_db()

# ── Models ──────────────────────────────────────────────

class UserSetup(BaseModel):
    device_id: str
    nickname: str
    radius_km: int
    email: Optional[str] = None

class UserSettings(BaseModel):
    device_id: str
    radius_km: Optional[int] = None
    email: Optional[str] = None

class ItemRequest(BaseModel):
    item_id: int
    device_id: str

class ItemStatusUpdate(BaseModel):
    device_id: str
    status: str  # 'available' or 'taken'

# ── Static files ─────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def root():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/manifest.json")
def manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")

@app.get("/icon.png")
def icon():
    return FileResponse("icon.png", media_type="image/png")

@app.get("/sw.js")
def service_worker():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/privacy")
def privacy():
    return FileResponse("privacy_policy.html", media_type="text/html")

# ── Users ────────────────────────────────────────────────

@app.post("/user/setup")
def setup_user(user: UserSetup):
    conn = get_db()
    cur = conn.cursor()
    now = time.time()
    cur.execute("""
        INSERT INTO users (device_id, nickname, radius_km, email, created_at, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(device_id) DO UPDATE SET
            nickname = EXCLUDED.nickname,
            radius_km = EXCLUDED.radius_km,
            email = COALESCE(EXCLUDED.email, users.email),
            last_seen = EXCLUDED.last_seen
    """, (user.device_id, user.nickname, user.radius_km, user.email, now, now))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.get("/user/{device_id}")
def get_user(device_id: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM users WHERE device_id = %s", (device_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return dict(row)

@app.patch("/user/settings")
def update_settings(settings: UserSettings):
    conn = get_db()
    cur = conn.cursor()
    now = time.time()
    if settings.radius_km:
        cur.execute("UPDATE users SET radius_km = %s, last_seen = %s WHERE device_id = %s",
                    (settings.radius_km, now, settings.device_id))
    if settings.email:
        cur.execute("UPDATE users SET email = %s, last_seen = %s WHERE device_id = %s",
                    (settings.email, now, settings.device_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

# ── Items ────────────────────────────────────────────────

@app.post("/item")
async def post_item(
    device_id: str = Form(...),
    post_type: str = Form("give"),
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    image: Optional[UploadFile] = File(None)
):
    image_url = None
    if image and image.filename:
        data = await image.read()
        result = cloudinary.uploader.upload(data, folder="ineed")
        image_url = result.get("secure_url")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO items (device_id, post_type, title, description, category, image_url, lat, lon, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (device_id, post_type, title, description, category, image_url, lat, lon, time.time()))
    item_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True, "item_id": item_id}

@app.get("/items")
def get_items(
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    device_id: Optional[str] = None
):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT i.*, u.nickname,
               COUNT(r.id) as request_count
        FROM items i
        JOIN users u ON i.device_id = u.device_id
        LEFT JOIN requests r ON r.item_id = i.id
        WHERE i.lat BETWEEN %s AND %s
          AND i.lon BETWEEN %s AND %s
          AND i.status = 'available'
        GROUP BY i.id, u.nickname
        ORDER BY i.created_at DESC
    """, (lat_min, lat_max, lon_min, lon_max))
    items = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return items

@app.get("/my-items/{device_id}")
def get_my_items(device_id: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT i.*, COUNT(r.id) as request_count
        FROM items i
        LEFT JOIN requests r ON r.item_id = i.id
        WHERE i.device_id = %s
        GROUP BY i.id
        ORDER BY i.created_at DESC
    """, (device_id,))
    items = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return items

@app.get("/item/{item_id}/requests")
def get_item_requests(item_id: int, device_id: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Verify the item belongs to this user
    cur.execute("SELECT device_id FROM items WHERE id = %s", (item_id,))
    row = cur.fetchone()
    if not row or row["device_id"] != device_id:
        raise HTTPException(status_code=403, detail="Not your item")
    cur.execute("""
        SELECT r.*, u.nickname
        FROM requests r
        JOIN users u ON r.device_id = u.device_id
        WHERE r.item_id = %s
        ORDER BY r.created_at ASC
    """, (item_id,))
    requests = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return requests

@app.patch("/item/{item_id}")
async def edit_item(
    item_id: int,
    device_id: str = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    remove_image: str = Form("false"),
    image: Optional[UploadFile] = File(None)
):
    conn = get_db()
    cur = conn.cursor()

    image_url = None
    if image and image.filename:
        data = await image.read()
        result = cloudinary.uploader.upload(data, folder="ineed")
        image_url = result.get("secure_url")

    if image_url:
        cur.execute("""
            UPDATE items SET title=%s, description=%s, category=%s, image_url=%s
            WHERE id=%s AND device_id=%s
        """, (title, description, category, image_url, item_id, device_id))
    elif remove_image == 'true':
        cur.execute("""
            UPDATE items SET title=%s, description=%s, category=%s, image_url=NULL
            WHERE id=%s AND device_id=%s
        """, (title, description, category, item_id, device_id))
    else:
        cur.execute("""
            UPDATE items SET title=%s, description=%s, category=%s
            WHERE id=%s AND device_id=%s
        """, (title, description, category, item_id, device_id))

    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.patch("/item/{item_id}/status")
def update_item_status(item_id: int, update: ItemStatusUpdate):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE items SET status = %s
        WHERE id = %s AND device_id = %s
    """, (update.status, item_id, update.device_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.delete("/item/{item_id}")
def delete_item(item_id: int, device_id: str):
    conn = get_db()
    cur = conn.cursor()
    # Verify ownership before deleting anything
    cur.execute("SELECT device_id FROM items WHERE id = %s", (item_id,))
    row = cur.fetchone()
    if not row or row[0] != device_id:
        cur.close()
        conn.close()
        raise HTTPException(status_code=403, detail="Not your item")
    # Remove dependent rows first so the foreign key constraints don't block deletion
    cur.execute("DELETE FROM requests WHERE item_id = %s", (item_id,))
    cur.execute("DELETE FROM image_reports WHERE item_id = %s", (item_id,))
    cur.execute("DELETE FROM items WHERE id = %s", (item_id,))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

# ── Push notification tokens ────────────────────────────────

class PushToken(BaseModel):
    device_id: str
    token: str

@app.post("/push-token")
def save_push_token(pt: PushToken):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS push_token TEXT
    """)
    cur.execute("UPDATE users SET push_token = %s WHERE device_id = %s", (pt.token, pt.device_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

# ── Image Reports ───────────────────────────────────────────

class ImageReport(BaseModel):
    item_id: int
    reporter_device_id: str
    reason: Optional[str] = None

@app.post("/report-image")
def report_image(report: ImageReport):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO image_reports (item_id, reporter_device_id, reason, created_at)
        VALUES (%s, %s, %s, %s)
    """, (report.item_id, report.reporter_device_id, report.reason, time.time()))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

# ── Requests ─────────────────────────────────────────────

@app.post("/request")
def request_item(req: ItemRequest):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO requests (item_id, device_id, created_at)
            VALUES (%s, %s, %s)
        """, (req.item_id, req.device_id, time.time()))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        cur.close()
        conn.close()
        return {"ok": False, "detail": "Already requested"}

    # Get item info + requester nickname for notification
    cur.execute("""
        SELECT i.title, i.device_id as giver_device_id, u.nickname as requester_name
        FROM items i
        JOIN users u ON u.device_id = %s
        WHERE i.id = %s
    """, (req.device_id, req.item_id))
    row = cur.fetchone()
    cur.close()
    conn.close()

    notification_data = None
    if row:
        notification_data = {
            "giver_device_id": row["giver_device_id"],
            "item_title": row["title"],
            "requester_name": row["requester_name"]
        }
    return {"ok": True, "notification": notification_data}

@app.get("/my-requests/{device_id}")
def get_my_requests(device_id: str):
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        "SELECT item_id FROM requests WHERE device_id = %s",
        (device_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [row["item_id"] for row in rows]

@app.delete("/request/{item_id}")
def cancel_request(item_id: int, device_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM requests WHERE item_id = %s AND device_id = %s", (item_id, device_id))
    conn.commit()
    cur.close()
    conn.close()
    return {"ok": True}

@app.get("/my-item-requests/{device_id}")
def get_my_item_requests(device_id: str):
    """All interest requests on items owned by this device — used by the app
    to detect new interest and trigger notifications / the unread indicator."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT r.id, r.item_id, r.created_at, i.title AS item_title,
               i.post_type, u.nickname AS requester_name
        FROM requests r
        JOIN items i ON i.id = r.item_id
        JOIN users u ON u.device_id = r.device_id
        WHERE i.device_id = %s
        ORDER BY r.created_at DESC
    """, (device_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


@app.delete("/account/{device_id}")
def delete_account(device_id: str):
    """Fully delete a user's account and all associated data.
    Order matters because of foreign keys:
      requests -> items, users ; items -> users ; image_reports -> items."""
    conn = get_db()
    cur = conn.cursor()
    try:
        # 1. This user's own requests (interest they expressed on others' items)
        cur.execute("DELETE FROM requests WHERE device_id = %s", (device_id,))
        # 2. Others' requests pointing at THIS user's items
        cur.execute("""
            DELETE FROM requests
            WHERE item_id IN (SELECT id FROM items WHERE device_id = %s)
        """, (device_id,))
        # 3. Image reports on this user's items
        cur.execute("""
            DELETE FROM image_reports
            WHERE item_id IN (SELECT id FROM items WHERE device_id = %s)
        """, (device_id,))
        # 4. Reports this user filed on any item (no FK, but tidy up)
        cur.execute("DELETE FROM image_reports WHERE reporter_device_id = %s", (device_id,))
        # 5. This user's items
        cur.execute("DELETE FROM items WHERE device_id = %s", (device_id,))
        # 6. The user row itself
        cur.execute("DELETE FROM users WHERE device_id = %s", (device_id,))
        conn.commit()
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        raise HTTPException(status_code=500, detail=f"Failed to delete account: {e}")
    cur.close()
    conn.close()
    return {"ok": True}

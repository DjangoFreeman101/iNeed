from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import psycopg2, psycopg2.extras, time, os, cloudinary, cloudinary.uploader, requests, re, math, bcrypt, random, string, secrets, threading

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
            language TEXT NOT NULL DEFAULT 'il',
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

    # Add phone column to users (for existing tables)
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT
    """)

    # Preferred UI language per user, used to localize outgoing emails.
    # Values: 'il' (Hebrew), 'en', 'cs', 'ru'. Defaults to Hebrew.
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'il'
    """)

    # Add status column to requests: 'pending' or 'approved' (for existing tables)
    cur.execute("""
        ALTER TABLE requests ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending'
    """)

    # Taker swiped "לקחתי" on their awaiting-list request
    cur.execute("""
        ALTER TABLE requests ADD COLUMN IF NOT EXISTS taken BOOLEAN NOT NULL DEFAULT FALSE
    """)
    cur.execute("""
        ALTER TABLE requests ADD COLUMN IF NOT EXISTS taken_at DOUBLE PRECISION
    """)

    # Timestamp for when an item's owner swiped it as exchanged
    cur.execute("""
        ALTER TABLE items ADD COLUMN IF NOT EXISTS exchanged_at DOUBLE PRECISION
    """)

    # Give/take matches — a give-item and a take-item that matched on
    # category + normalized title, within each other's radius. Notified once.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS matches (
            id SERIAL PRIMARY KEY,
            give_item_id INTEGER NOT NULL REFERENCES items(id),
            take_item_id INTEGER NOT NULL REFERENCES items(id),
            created_at DOUBLE PRECISION NOT NULL,
            UNIQUE(give_item_id, take_item_id)
        )
    """)

    # Password hash for account login (bcrypt — never store plaintext).
    cur.execute("""
        ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT
    """)

    # Email verification codes (sign-up) — one active code per email.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS email_verifications (
            email TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL,
            verified_at DOUBLE PRECISION
        )
    """)

    # Password reset codes ("forgot password") — one active code per email.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            email TEXT PRIMARY KEY,
            code TEXT NOT NULL,
            created_at DOUBLE PRECISION NOT NULL
        )
    """)

    # Item condition (only meaningful for "give" posts) — new/like-new/used/bad.
    # Nullable so existing rows stay blank rather than needing a backfill.
    cur.execute("""
        ALTER TABLE items ADD COLUMN IF NOT EXISTS condition TEXT
    """)

    # Post-approval exchange reminders — one row per party per approved request.
    # Stage 1 goes out 30 min after approval; stage 2 only if they clicked
    # "I'll give/take", 24h after that click. Then the row is done for good.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS exchange_reminders (
            id SERIAL PRIMARY KEY,
            request_id INTEGER NOT NULL REFERENCES requests(id),
            device_id TEXT NOT NULL,
            role TEXT NOT NULL,               -- 'give' or 'take'
            token TEXT NOT NULL UNIQUE,
            approved_at DOUBLE PRECISION NOT NULL,
            stage1_sent_at DOUBLE PRECISION,
            intent_at DOUBLE PRECISION,       -- clicked "I'll give/take"
            stage2_sent_at DOUBLE PRECISION,
            done BOOLEAN NOT NULL DEFAULT FALSE,
            UNIQUE(request_id, device_id)
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
    phone: Optional[str] = None

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

# ── Give/Take matching ──────────────────────────────────
# A "match" is a give-item and a take-item where: same category, same item
# name after normalizing (strip redundant whitespace + lowercase), and each
# item falls within the OTHER item's owner's radius (mutual). Checked whenever
# an item is created/edited, or a user's radius changes. Each pair is only
# ever notified once (enforced by the UNIQUE constraint on matches).

def normalize_title(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))

def check_and_create_matches(item_id):
    """Given an item that just changed, look for opposite-type items that now
    match it, and record any new mutual matches. Safe to call repeatedly —
    already-matched pairs are skipped via the UNIQUE constraint."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT i.id, i.post_type, i.title, i.category, i.lat, i.lon, i.status,
               i.device_id, u.radius_km
        FROM items i JOIN users u ON u.device_id = i.device_id
        WHERE i.id = %s
    """, (item_id,))
    item = cur.fetchone()
    if not item or item["status"] != "available":
        cur.close(); conn.close()
        return []

    other_type = "take" if item["post_type"] == "give" else "give"
    norm_name = normalize_title(item["title"])

    cur.execute("""
        SELECT i.id, i.lat, i.lon, i.device_id, u.radius_km
        FROM items i JOIN users u ON u.device_id = i.device_id
        WHERE i.post_type = %s
          AND i.status = 'available'
          AND i.category = %s
          AND i.device_id != %s
    """, (other_type, item["category"], item["device_id"]))
    candidates = cur.fetchall()

    new_matches = []
    for c in candidates:
        # Compare normalized in Python (accounts for extra/irregular whitespace).
        cur.execute("SELECT title FROM items WHERE id = %s", (c["id"],))
        cand_title = cur.fetchone()["title"]
        if normalize_title(cand_title) != norm_name:
            continue

        dist = haversine_km(item["lat"], item["lon"], c["lat"], c["lon"])
        if dist > item["radius_km"] or dist > c["radius_km"]:
            continue  # must be within BOTH radii

        give_id = item["id"] if item["post_type"] == "give" else c["id"]
        take_id = c["id"] if item["post_type"] == "give" else item["id"]

        try:
            cur.execute("""
                INSERT INTO matches (give_item_id, take_item_id, created_at)
                VALUES (%s, %s, %s)
            """, (give_id, take_id, time.time()))
            conn.commit()
            new_matches.append({"give_item_id": give_id, "take_item_id": take_id})
        except psycopg2.errors.UniqueViolation:
            conn.rollback()  # already matched before — skip silently

    cur.close()
    conn.close()
    return new_matches

def recheck_matches_for_user(device_id):
    """Called when a user's radius changes — re-checks every one of their
    active items against the world, since a wider/narrower radius can turn
    existing items into new matches."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM items WHERE device_id = %s AND status = 'available'", (device_id,))
    item_ids = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    for iid in item_ids:
        check_and_create_matches(iid)

@app.get("/my-matches/{device_id}")
def get_my_matches(device_id: str):
    """All matches involving items owned by this device — each row describes
    the OTHER side's item + owner, so the client can drop a pin/banner for it."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT m.id AS match_id, m.created_at,
               mine.id AS my_item_id, mine.title AS my_item_title, mine.post_type AS my_post_type,
               other.id AS other_item_id, other.title AS other_item_title,
               other.category AS other_item_category, other.description AS other_item_description,
               other.image_url AS other_item_image_url, other.lat AS other_item_lat,
               other.lon AS other_item_lon, other.post_type AS other_post_type,
               other.device_id AS other_device_id,
               ou.nickname AS other_nickname
        FROM matches m
        JOIN items mine ON mine.id = m.give_item_id
        JOIN items other ON other.id = m.take_item_id
        JOIN users ou ON ou.device_id = other.device_id
        WHERE mine.device_id = %s

        UNION ALL

        SELECT m.id AS match_id, m.created_at,
               mine.id AS my_item_id, mine.title AS my_item_title, mine.post_type AS my_post_type,
               other.id AS other_item_id, other.title AS other_item_title,
               other.category AS other_item_category, other.description AS other_item_description,
               other.image_url AS other_item_image_url, other.lat AS other_item_lat,
               other.lon AS other_item_lon, other.post_type AS other_post_type,
               other.device_id AS other_device_id,
               ou.nickname AS other_nickname
        FROM matches m
        JOIN items mine ON mine.id = m.take_item_id
        JOIN items other ON other.id = m.give_item_id
        JOIN users ou ON ou.device_id = other.device_id
        WHERE mine.device_id = %s

        ORDER BY created_at DESC
    """, (device_id, device_id))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows

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

@app.get("/email-banner.png")
def email_banner():
    """Banner shown at the top of every outgoing email. Must be publicly
    reachable — mail clients fetch it over HTTP, they can't read local files."""
    return FileResponse("email-banner.png", media_type="image/png")

@app.get("/header-logo.png")
def header_logo():
    return FileResponse("header-logo.png", media_type="image/png")

@app.get("/sw.js")
def service_worker():
    return FileResponse("sw.js", media_type="application/javascript")

@app.get("/privacy")
def privacy():
    return FileResponse("privacy_policy.html", media_type="text/html")

@app.get("/delete-account")
def delete_account_page():
    return FileResponse("delete-account.html", media_type="text/html")

# ── Users ────────────────────────────────────────────────

@app.post("/user/setup")
def setup_user(user: UserSetup):
    # Server-side phone validation: exactly 10 digits, starting with '05'.
    phone = (user.phone or "").strip()
    if not (phone.isdigit() and len(phone) == 10 and phone.startswith("05")):
        raise HTTPException(status_code=400, detail="Phone must be 10 digits starting with 05")

    conn = get_db()
    cur = conn.cursor()

    # Reject if this phone already belongs to a DIFFERENT device. (Excluding
    # this device_id lets an existing user re-submit their own unchanged info
    # without tripping the check on themselves.) Nicknames may duplicate freely.
    cur.execute("SELECT device_id FROM users WHERE phone = %s AND device_id != %s", (phone, user.device_id))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Phone number already registered")

    if user.email:
        email_norm = user.email.strip().lower()
        cur.execute("SELECT device_id FROM users WHERE LOWER(email) = %s AND device_id != %s", (email_norm, user.device_id))
        if cur.fetchone():
            cur.close(); conn.close()
            raise HTTPException(status_code=400, detail="Email already registered")

    now = time.time()
    cur.execute("""
        INSERT INTO users (device_id, nickname, radius_km, email, phone, created_at, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT(device_id) DO UPDATE SET
            nickname = EXCLUDED.nickname,
            radius_km = EXCLUDED.radius_km,
            email = COALESCE(EXCLUDED.email, users.email),
            phone = EXCLUDED.phone,
            last_seen = EXCLUDED.last_seen
    """, (user.device_id, user.nickname, user.radius_km, user.email, phone, now, now))
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
    radius_changed = bool(settings.radius_km)
    if settings.radius_km:
        cur.execute("UPDATE users SET radius_km = %s, last_seen = %s WHERE device_id = %s",
                    (settings.radius_km, now, settings.device_id))
    if settings.email:
        cur.execute("UPDATE users SET email = %s, last_seen = %s WHERE device_id = %s",
                    (settings.email, now, settings.device_id))
    conn.commit()
    cur.close()
    conn.close()
    if radius_changed:
        recheck_matches_for_user(settings.device_id)
    return {"ok": True}

# ── Items ────────────────────────────────────────────────

CONDITION_OPTIONS = {"new", "like_new", "used", "bad"}

@app.post("/item")
async def post_item(
    device_id: str = Form(...),
    post_type: str = Form("give"),
    title: str = Form(...),
    description: str = Form(""),
    category: str = Form(...),
    condition: Optional[str] = Form(None),
    lat: float = Form(...),
    lon: float = Form(...),
    image: Optional[UploadFile] = File(None)
):
    # Condition is only meaningful (and required) for "give" posts — a "take"
    # post has no physical item yet, so there's nothing to describe the state of.
    if post_type == "give":
        if not condition or condition not in CONDITION_OPTIONS:
            raise HTTPException(status_code=400, detail="Condition is required for give items")
    else:
        condition = None

    image_url = None
    if image and image.filename:
        data = await image.read()
        result = cloudinary.uploader.upload(data, folder="ineed")
        image_url = result.get("secure_url")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO items (device_id, post_type, title, description, category, condition, image_url, lat, lon, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (device_id, post_type, title, description, category, condition, image_url, lat, lon, time.time()))
    item_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    check_and_create_matches(item_id)
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
        SELECT i.*, COUNT(r.id) as request_count,
               COUNT(r.id) FILTER (WHERE r.status = 'approved') as approved_count
        FROM items i
        LEFT JOIN requests r ON r.item_id = i.id
        WHERE i.device_id = %s
          AND i.status != 'exchanged'
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
        SELECT r.*, u.nickname, u.phone AS requester_phone
        FROM requests r
        JOIN users u ON r.device_id = u.device_id
        WHERE r.item_id = %s
        ORDER BY r.created_at ASC
    """, (item_id,))
    requests = [dict(r) for r in cur.fetchall()]
    # Only expose the phone once the giver has approved this specific request.
    for req in requests:
        if req.get("status") != "approved":
            req["requester_phone"] = None
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
    check_and_create_matches(item_id)
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
    cur.execute("""
        DELETE FROM exchange_reminders
        WHERE request_id IN (SELECT id FROM requests WHERE item_id = %s)
    """, (item_id,))
    cur.execute("DELETE FROM requests WHERE item_id = %s", (item_id,))
    cur.execute("DELETE FROM image_reports WHERE item_id = %s", (item_id,))
    cur.execute("DELETE FROM matches WHERE give_item_id = %s OR take_item_id = %s", (item_id, item_id))
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
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Store the report
    cur.execute("""
        INSERT INTO image_reports (item_id, reporter_device_id, reason, created_at)
        VALUES (%s, %s, %s, %s)
    """, (report.item_id, report.reporter_device_id, report.reason, time.time()))
    conn.commit()

    # Pull the item's details for the email
    cur.execute("""
        SELECT i.id, i.title, i.description, i.category, i.post_type, i.image_url,
               i.lat, i.lon, u.nickname, u.phone
        FROM items i
        JOIN users u ON u.device_id = i.device_id
        WHERE i.id = %s
    """, (report.item_id,))
    item = cur.fetchone()
    cur.close()
    conn.close()

    # Send the moderation email (best-effort; never blocks the user's report)
    try:
        send_report_email(dict(item) if item else None, report)
    except Exception as e:
        print("Report email failed:", e)

    return {"ok": True}


def send_report_email(item, report):
    """Email a moderation report with the listing details and image embedded.
    Uses Resend's HTTP API. Requires RESEND_API_KEY and REPORT_EMAIL_TO env vars;
    if either is missing, silently skips (the report is still stored in the DB)."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    to_addr = os.environ.get("REPORT_EMAIL_TO", "")
    from_addr = os.environ.get("REPORT_EMAIL_FROM", "onboarding@resend.dev")
    if not api_key or not to_addr:
        print("Report email skipped: RESEND_API_KEY or REPORT_EMAIL_TO not set")
        return

    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    if item:
        img_html = f'<img src="{item.get("image_url")}" style="max-width:400px;border-radius:8px;"/>' if item.get("image_url") else "<i>(no image on this listing)</i>"
        maps = f'https://www.openstreetmap.org/?mlat={item.get("lat")}&mlon={item.get("lon")}#map=17/{item.get("lat")}/{item.get("lon")}' if item.get("lat") else ""
        body = f"""
        <h2>Image report — iNeed</h2>
        <p><b>Reported at:</b> {when}</p>
        <p><b>Reason:</b> {report.reason or '(none given)'}</p>
        <p><b>Reporter device:</b> {report.reporter_device_id}</p>
        <hr/>
        <h3>Listing details</h3>
        <p><b>Item ID:</b> {item.get('id')}</p>
        <p><b>Title:</b> {item.get('title')}</p>
        <p><b>Description:</b> {item.get('description') or '(none)'}</p>
        <p><b>Category:</b> {item.get('category')}</p>
        <p><b>Type:</b> {item.get('post_type')}</p>
        <p><b>Posted by:</b> {item.get('nickname')} ({item.get('phone') or 'no phone'})</p>
        {f'<p><b>Location:</b> <a href="{maps}">{item.get("lat")}, {item.get("lon")}</a></p>' if maps else ''}
        <h3>Reported image</h3>
        {img_html}
        """
    else:
        body = f"""
        <h2>Image report — iNeed</h2>
        <p><b>Reported at:</b> {when}</p>
        <p><b>Reason:</b> {report.reason or '(none given)'}</p>
        <p><b>Reporter device:</b> {report.reporter_device_id}</p>
        <p style="color:#c00;"><b>Note:</b> item #{report.item_id} was not found (may have been deleted).</p>
        """

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "from": from_addr,
            "to": [to_addr],
            "subject": f"[iNeed] Image reported — item #{report.item_id}",
            "html": email_shell(body, rtl=False),
        },
        timeout=10,
    )
    if resp.status_code >= 300:
        print("Resend error:", resp.status_code, resp.text)

# ── Auth: helpers ────────────────────────────────────────

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def gen_code() -> str:
    return "".join(random.choices(string.digits, k=6))

def email_shell(inner_html, rtl=True):
    """Wraps message content in the standard iNeed email frame: banner on top,
    content in a centered card. The banner is sized to sit comfortably above
    body text rather than dominate the message."""
    direction = "rtl" if rtl else "ltr"
    banner_url = f"{app_base_url()}/email-banner.png"
    return f"""
    <div style="background:#f4f4f5;padding:24px 12px;">
      <div dir="{direction}" style="max-width:560px;margin:0 auto;background:#fff;
                  border-radius:12px;overflow:hidden;font-family:Arial,sans-serif;">
        <div style="text-align:center;background:#F4433620;padding:16px 12px 8px;">
          <img src="{banner_url}" alt="iNeed" width="380"
               style="display:inline-block;width:100%;max-width:380px;height:auto;
                      border:0;border-radius:8px;"/>
        </div>
        <div style="padding:24px;color:#222;line-height:1.6;">
          {inner_html}
        </div>
      </div>
    </div>
    """

def normalize_lang(lang):
    """Map the app's language codes to the stored set. The frontend uses 'he'
    for Hebrew; we store that as 'il'. Everything else passes through if known,
    otherwise falls back to Hebrew ('il')."""
    lang = (lang or "").strip().lower()
    if lang in ("he", "il", "iw"):
        return "il"
    if lang in ("en", "cs", "ru"):
        return lang
    return "il"

def send_simple_email(to_addr, subject, html, rtl=True):
    """Generic Resend sender for verification/reset codes. Best-effort: returns
    False (and logs) rather than raising, so callers can turn that into a
    clean HTTP error. Content is wrapped in the standard banner frame."""
    api_key = os.environ.get("RESEND_API_KEY", "")
    from_addr = os.environ.get("REPORT_EMAIL_FROM", "onboarding@resend.dev")
    if not api_key or not to_addr:
        print("Email skipped: RESEND_API_KEY or recipient missing")
        return False
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"from": from_addr, "to": [to_addr], "subject": subject,
              "html": email_shell(html, rtl=rtl)},
        timeout=10,
    )
    if resp.status_code >= 300:
        print("Resend error:", resp.status_code, resp.text)
        return False
    return True

PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.djangofreeman.ineed"

# Localized copy for the "someone is interested in your item" email.
# is_take=True means the poster was looking for something and another user
# has it ("X has the item you wanted"); is_take=False is the classic
# "X wants the item you're giving away".
INTEREST_EMAIL = {
    "il": {
        "subject_give": "מישהו מעוניין בפריט שלך ב-iNeed",
        "subject_take": "מישהו מצא את מה שחיפשת ב-iNeed",
        "body_give": "{name} מעוניין/ת בפריט שלך „{item}“.",
        "body_take": "ל{name} יש „{item}“ שחיפשת.",
        "cta": "פתח/י את iNeed",
        "rtl": True,
    },
    "en": {
        "subject_give": "Someone is interested in your item on iNeed",
        "subject_take": "Someone found what you were looking for on iNeed",
        "body_give": "{name} is interested in your item \u201c{item}\u201d.",
        "body_take": "{name} has \u201c{item}\u201d that you were looking for.",
        "cta": "Open iNeed",
        "rtl": False,
    },
    "cs": {
        "subject_give": "Někdo má zájem o vaši položku na iNeed",
        "subject_take": "Někdo našel to, co jste hledali, na iNeed",
        "body_give": "{name} má zájem o vaši položku \u201e{item}\u201c.",
        "body_take": "{name} má \u201e{item}\u201c, kterou jste hledali.",
        "cta": "Otevřít iNeed",
        "rtl": False,
    },
    "ru": {
        "subject_give": "Кто-то заинтересовался вашей вещью в iNeed",
        "subject_take": "Кто-то нашёл то, что вы искали, в iNeed",
        "body_give": "{name} заинтересован(а) в вашей вещи \u00ab{item}\u00bb.",
        "body_take": "У {name} есть \u00ab{item}\u00bb, которую вы искали.",
        "cta": "Открыть iNeed",
        "rtl": False,
    },
}

def send_interest_email(to_addr, language, requester_name, item_title, is_take):
    """Notify an item's poster that another user expressed interest. Localized
    to the poster's stored language. Best-effort: never raises."""
    if not to_addr:
        return False
    copy = INTEREST_EMAIL.get(normalize_lang(language), INTEREST_EMAIL["il"])
    subject = copy["subject_take"] if is_take else copy["subject_give"]
    body_tpl = copy["body_take"] if is_take else copy["body_give"]
    line = body_tpl.format(name=requester_name or "", item=item_title or "")
    align = "right" if copy["rtl"] else "left"
    html = f"""
      <p style="font-size:17px;font-weight:700;margin:0 0 12px;text-align:{align};">{line}</p>
      <div style="text-align:center;margin:24px 0 8px;">
        <a href="{PLAY_STORE_URL}"
           style="display:inline-block;background:#F44336;color:#fff;text-decoration:none;
                  padding:12px 28px;border-radius:10px;font-weight:700;font-size:16px;">
          {copy['cta']}
        </a>
      </div>
    """
    try:
        return send_simple_email(to_addr, subject, html, rtl=copy["rtl"])
    except Exception as e:
        print("Interest email failed:", e)
        return False

# ── Auth: sign-up (per-screen checks + final creation) ───

class PhoneCheck(BaseModel):
    phone: str

@app.post("/check-phone")
def check_phone(body: PhoneCheck):
    phone = (body.phone or "").strip()
    if not (phone.isdigit() and len(phone) == 10 and phone.startswith("05")):
        raise HTTPException(status_code=400, detail="Phone must be 10 digits starting with 05")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT device_id FROM users WHERE phone = %s", (phone,))
    taken = cur.fetchone() is not None
    cur.close(); conn.close()
    if taken:
        raise HTTPException(status_code=400, detail="Phone number already registered")
    return {"ok": True}

class EmailCodeRequest(BaseModel):
    email: str

@app.post("/send-email-code")
def send_email_code(body: EmailCodeRequest):
    email = (body.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT device_id FROM users WHERE LOWER(email) = %s", (email,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    code = gen_code()
    cur.execute("""
        INSERT INTO email_verifications (email, code, created_at, verified_at)
        VALUES (%s, %s, %s, NULL)
        ON CONFLICT (email) DO UPDATE SET code = EXCLUDED.code, created_at = EXCLUDED.created_at, verified_at = NULL
    """, (email, code, time.time()))
    conn.commit()
    cur.close(); conn.close()

    sent = send_simple_email(
        email,
        "קוד האימות שלך ל-iNeed",
        f"<p>קוד האימות שלך הוא:</p><h1 style='letter-spacing:4px;'>{code}</h1><p>הקוד בתוקף ל-15 דקות.</p>"
    )
    if not sent:
        raise HTTPException(status_code=500, detail="Failed to send verification email")
    return {"ok": True}

class VerifyEmailCode(BaseModel):
    email: str
    code: str

@app.post("/verify-email-code")
def verify_email_code(body: VerifyEmailCode):
    email = (body.email or "").strip().lower()
    code = (body.code or "").strip()
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT code, created_at FROM email_verifications WHERE email = %s", (email,))
    row = cur.fetchone()
    if not row or row["code"] != code or (time.time() - row["created_at"]) > 900:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    cur.execute("UPDATE email_verifications SET verified_at = %s WHERE email = %s", (time.time(), email))
    conn.commit()
    cur.close(); conn.close()
    return {"ok": True}

class SignupBody(BaseModel):
    device_id: str
    nickname: str
    phone: str
    email: str
    password: str
    radius_km: int
    language: str = "il"

@app.post("/signup")
def signup(body: SignupBody):
    phone = (body.phone or "").strip()
    email = (body.email or "").strip().lower()
    if not (phone.isdigit() and len(phone) == 10 and phone.startswith("05")):
        raise HTTPException(status_code=400, detail="Phone must be 10 digits starting with 05")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    if not body.password or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Re-check uniqueness (the per-screen checks already covered this, but
    # time may have passed since — someone else could have taken it meanwhile).
    cur.execute("SELECT device_id FROM users WHERE phone = %s", (phone,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Phone number already registered")
    cur.execute("SELECT device_id FROM users WHERE LOWER(email) = %s", (email,))
    if cur.fetchone():
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Email already registered")

    # Confirm this email actually completed verification recently.
    cur.execute("SELECT verified_at FROM email_verifications WHERE email = %s", (email,))
    ver = cur.fetchone()
    if not ver or not ver["verified_at"] or (time.time() - ver["verified_at"]) > 3600:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Email not verified")

    now = time.time()
    pw_hash = hash_password(body.password)
    lang = normalize_lang(body.language)
    cur.execute("""
        INSERT INTO users (device_id, nickname, radius_km, email, phone, password_hash, language, created_at, last_seen)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (device_id) DO UPDATE SET
            nickname = EXCLUDED.nickname, radius_km = EXCLUDED.radius_km,
            email = EXCLUDED.email, phone = EXCLUDED.phone,
            password_hash = EXCLUDED.password_hash, language = EXCLUDED.language,
            last_seen = EXCLUDED.last_seen
    """, (body.device_id, body.nickname, body.radius_km, email, phone, pw_hash, lang, now, now))
    conn.commit()
    cur.close(); conn.close()
    return {"ok": True}

class LanguageBody(BaseModel):
    device_id: str
    language: str

@app.post("/user/language")
def update_language(body: LanguageBody):
    """Persist the user's UI language so emails can be localized. Called
    fire-and-forget by the app whenever the language changes; best-effort."""
    lang = normalize_lang(body.language)
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET language = %s WHERE device_id = %s",
                    (lang, body.device_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("Language update failed:", e)
    finally:
        cur.close(); conn.close()
    return {"ok": True}

# ── Auth: login / forgot password ────────────────────────

class LoginBody(BaseModel):
    identifier: str  # email OR phone number
    password: str

@app.post("/login")
def login(body: LoginBody):
    identifier = (body.identifier or "").strip()
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Digits-only and 10 chars starting with 05 -> treat as a phone number.
    # Anything else -> treat as an email (case-insensitive).
    if identifier.isdigit() and len(identifier) == 10 and identifier.startswith("05"):
        cur.execute("SELECT device_id, password_hash FROM users WHERE phone = %s", (identifier,))
    else:
        cur.execute("SELECT device_id, password_hash FROM users WHERE LOWER(email) = %s", (identifier.lower(),))

    row = cur.fetchone()
    cur.close(); conn.close()
    if not row or not row["password_hash"] or not check_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=400, detail="Invalid credentials")
    return {"ok": True, "device_id": row["device_id"]}

class ForgotPasswordBody(BaseModel):
    email: str

@app.post("/forgot-password")
def forgot_password(body: ForgotPasswordBody):
    email = (body.email or "").strip().lower()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT device_id FROM users WHERE LOWER(email) = %s", (email,))
    exists = cur.fetchone() is not None
    code = None
    if exists:
        code = gen_code()
        cur.execute("""
            INSERT INTO password_resets (email, code, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (email) DO UPDATE SET code = EXCLUDED.code, created_at = EXCLUDED.created_at
        """, (email, code, time.time()))
        conn.commit()
    cur.close(); conn.close()

    if exists:
        send_simple_email(
            email,
            "איפוס סיסמה — iNeed",
            f"<p>קוד לאיפוס הסיסמה שלך:</p><h1 style='letter-spacing:4px;'>{code}</h1><p>הקוד בתוקף ל-30 דקות.</p>"
        )
    # Always return ok whether or not the email is registered — avoids
    # leaking to a caller which emails exist in the system.
    return {"ok": True}

class ResetPasswordBody(BaseModel):
    email: str
    code: str
    new_password: str

@app.post("/reset-password")
def reset_password(body: ResetPasswordBody):
    email = (body.email or "").strip().lower()
    code = (body.code or "").strip()
    if not body.new_password or len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT code, created_at FROM password_resets WHERE email = %s", (email,))
    row = cur.fetchone()
    if not row or row["code"] != code or (time.time() - row["created_at"]) > 1800:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    pw_hash = hash_password(body.new_password)
    cur.execute("UPDATE users SET password_hash = %s WHERE LOWER(email) = %s", (pw_hash, email))
    cur.execute("DELETE FROM password_resets WHERE email = %s", (email,))
    conn.commit()
    cur.close(); conn.close()
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
        SELECT i.title, i.device_id as giver_device_id, i.post_type,
               u.nickname as requester_name,
               g.email as giver_email, g.language as giver_language
        FROM items i
        JOIN users u ON u.device_id = %s
        JOIN users g ON g.device_id = i.device_id
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
        # Email the poster (giver or taker) that someone's interested.
        # Best-effort — never let a failed email break the request.
        try:
            is_take = (row.get("post_type") == "take")
            send_interest_email(
                row.get("giver_email"),
                row.get("giver_language"),
                row.get("requester_name"),
                row.get("title"),
                is_take,
            )
        except Exception as e:
            print("Interest email dispatch failed:", e)
    return {"ok": True, "notification": notification_data}

@app.post("/request/{request_id}/approve")
def approve_request(request_id: int, device_id: str):
    """The giver (item owner) approves a taker's request. Verifies the caller
    owns the item the request is on, then flips status to 'approved'."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # Fetch the request together with the owning item's device_id
    cur.execute("""
        SELECT r.id, i.device_id AS owner_device_id
        FROM requests r
        JOIN items i ON i.id = r.item_id
        WHERE r.id = %s
    """, (request_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Request not found")
    if row["owner_device_id"] != device_id:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Not your item")

    cur.execute("UPDATE requests SET status = 'approved' WHERE id = %s", (request_id,))
    conn.commit()
    cur.close()
    conn.close()
    create_exchange_reminders(request_id)
    return {"ok": True}

# ── Exchange reminders (post-approval, email-based) ──────
# Push can't reliably carry a 30min/24h delay on Android (exact-alarm permission
# plus OEM battery killers), so these go out by email instead. Each party gets a
# single-use token; clicking a link runs the same flow as acting in the app.

REMINDER_STAGE1_DELAY = 30 * 60        # 30 minutes after approval
REMINDER_STAGE2_DELAY = 24 * 60 * 60   # 24 hours after "I'll give/take"

def app_base_url():
    return os.environ.get("APP_BASE_URL", "https://ineed-0otk.onrender.com").rstrip("/")

PLAY_STORE_URL = "https://play.google.com/store/apps/details?id=com.djangofreeman.ineed"

def create_exchange_reminders(request_id):
    """Called when a giver approves a taker. Arms one reminder row for each
    side. Idempotent — re-approving won't duplicate or reset the clock."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT r.device_id AS taker_device_id, i.device_id AS giver_device_id
        FROM requests r JOIN items i ON i.id = r.item_id
        WHERE r.id = %s
    """, (request_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        return

    now = time.time()
    for device_id, role in ((row["giver_device_id"], "give"), (row["taker_device_id"], "take")):
        try:
            cur.execute("""
                INSERT INTO exchange_reminders (request_id, device_id, role, token, approved_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (request_id, device_id) DO NOTHING
            """, (request_id, device_id, role, secrets.token_urlsafe(24), now))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print("Reminder create failed:", e)
    cur.close()
    conn.close()

def finish_reminders(cur, request_id, device_id):
    """Stop reminding this person about this request — they've confirmed,
    whether from the email link or inside the app."""
    cur.execute("""
        UPDATE exchange_reminders SET done = TRUE
        WHERE request_id = %s AND device_id = %s
    """, (request_id, device_id))

def send_reminder_email(rem, stage):
    """stage 1 = 'did you meet?' with both buttons. stage 2 = 24h nudge."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT i.title, i.image_url,
               me.email AS my_email, me.nickname AS my_nickname,
               other.nickname AS other_nickname
        FROM requests r
        JOIN items i ON i.id = r.item_id
        JOIN users me ON me.device_id = %s
        JOIN users other ON other.device_id = CASE
            WHEN %s = 'give' THEN r.device_id ELSE i.device_id END
        WHERE r.id = %s
    """, (rem["device_id"], rem["role"], rem["request_id"]))
    info = cur.fetchone()
    cur.close()
    conn.close()
    if not info or not info["my_email"]:
        return False

    is_give = rem["role"] == "give"
    verb_past = "מסרת" if is_give else "לקחת"
    verb_future = "אמסור" if is_give else "אקח"
    base = app_base_url()
    confirm_url = f"{base}/r/{rem['token']}/confirm"
    intent_url = f"{base}/r/{rem['token']}/intent"

    if stage == 1:
        lead = f"האם {verb_past} את \"{info['title']}\"?"
    else:
        lead = (f"תזכורת: סימנת ש{verb_future} את \"{info['title']}\" "
                f"עם {info['other_nickname']}. כבר קרה?")

    btn = ("display:inline-block;padding:12px 22px;border-radius:8px;"
           "text-decoration:none;font-weight:bold;margin:6px 4px;")
    buttons = f"""
      <a href="{confirm_url}" style="{btn}background:#27ae60;color:#fff;">כן, {verb_past}</a>
      <a href="{intent_url}" style="{btn}background:#eee;color:#333;">עוד לא — {verb_future} בקרוב</a>
    """ if stage == 1 else f"""
      <a href="{confirm_url}" style="{btn}background:#27ae60;color:#fff;">כן, {verb_past}</a>
    """

    body = f"""
      <p>שלום {info['my_nickname'] or ''},</p>
      <p>{lead}</p>
      {f'<p><img src="{info["image_url"]}" alt="" style="max-width:280px;border-radius:8px;"/></p>' if info["image_url"] else ''}
      <p>{buttons}</p>
      <hr style="border:none;border-top:1px solid #eee;margin:20px 0;"/>
      <p><a href="{PLAY_STORE_URL}" style="{btn}background:#e74c3c;color:#fff;">פתח את האפליקציה</a></p>
      <p style="font-size:12px;color:#777;">אפשר לסמן גם ישירות באפליקציה.</p>
    """
    subject = (f"[iNeed] האם {verb_past} את \"{info['title']}\"?" if stage == 1
               else f"[iNeed] תזכורת: {info['title']}")
    return send_simple_email(info["my_email"], subject, body)

def process_due_reminders():
    """One sweep: send whatever is due right now."""
    now = time.time()
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Stage 1 — 30 min after approval, if they haven't already confirmed.
    cur.execute("""
        SELECT * FROM exchange_reminders
        WHERE done = FALSE AND stage1_sent_at IS NULL AND approved_at <= %s
    """, (now - REMINDER_STAGE1_DELAY,))
    stage1 = [dict(r) for r in cur.fetchall()]

    # Stage 2 — 24h after they said "I'll give/take". Final message either way.
    cur.execute("""
        SELECT * FROM exchange_reminders
        WHERE done = FALSE AND intent_at IS NOT NULL
          AND stage2_sent_at IS NULL AND intent_at <= %s
    """, (now - REMINDER_STAGE2_DELAY,))
    stage2 = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()

    for rem in stage1:
        try:
            send_reminder_email(rem, 1)
        except Exception as e:
            print("Stage1 reminder failed:", e)
        c = get_db(); cc = c.cursor()
        cc.execute("UPDATE exchange_reminders SET stage1_sent_at = %s WHERE id = %s", (now, rem["id"]))
        c.commit(); cc.close(); c.close()

    for rem in stage2:
        try:
            send_reminder_email(rem, 2)
        except Exception as e:
            print("Stage2 reminder failed:", e)
        # Nothing further is ever sent for this pair after the 24h nudge.
        c = get_db(); cc = c.cursor()
        cc.execute("UPDATE exchange_reminders SET stage2_sent_at = %s, done = TRUE WHERE id = %s",
                   (now, rem["id"]))
        c.commit(); cc.close(); c.close()

def reminder_loop():
    while True:
        try:
            process_due_reminders()
        except Exception as e:
            print("Reminder loop error:", e)
        time.sleep(120)

def _reminder_page(msg):
    return HTMLResponse(f"""
    <html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"></head>
    <body dir="rtl" style="font-family:Arial,sans-serif;text-align:center;padding:60px 20px;">
      <h1 style="color:#e74c3c;">iNeed</h1>
      <p style="font-size:18px;">{msg}</p>
    </body></html>
    """)

@app.get("/r/{token}/intent")
def reminder_intent(token: str):
    """'Not yet — I'll give/take it soon.' Arms the 24h follow-up."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM exchange_reminders WHERE token = %s", (token,))
    rem = cur.fetchone()
    if not rem:
        cur.close(); conn.close()
        return _reminder_page("הקישור אינו תקין.")
    if rem["done"]:
        cur.close(); conn.close()
        return _reminder_page("כבר סימנת שההעברה בוצעה.")
    cur.execute("UPDATE exchange_reminders SET intent_at = %s WHERE id = %s", (time.time(), rem["id"]))
    conn.commit()
    cur.close()
    conn.close()
    return _reminder_page("תודה! נזכיר לך שוב מחר.")

@app.get("/r/{token}/confirm")
def reminder_confirm(token: str):
    """'Yes, I gave/took it.' Runs exactly the same flow as confirming in the
    app: the owner marks the item exchanged, the requester marks it taken."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM exchange_reminders WHERE token = %s", (token,))
    rem = cur.fetchone()
    if not rem:
        cur.close(); conn.close()
        return _reminder_page("הקישור אינו תקין.")
    if rem["done"]:
        cur.close(); conn.close()
        return _reminder_page("כבר סימנת שההעברה בוצעה.")

    cur.execute("SELECT item_id FROM requests WHERE id = %s", (rem["request_id"],))
    req = cur.fetchone()
    cur.close()
    conn.close()
    if not req:
        return _reminder_page("הבקשה לא נמצאה.")

    try:
        if rem["role"] == "give":
            mark_item_exchanged(req["item_id"], rem["device_id"])
        else:
            mark_request_taken(rem["request_id"], rem["device_id"])
    except HTTPException as e:
        # Most likely already marked in-app — treat as success, just stop nagging.
        print("Reminder confirm skipped:", e.detail)

    conn = get_db()
    cur = conn.cursor()
    finish_reminders(cur, rem["request_id"], rem["device_id"])
    conn.commit()
    cur.close()
    conn.close()
    return _reminder_page("תודה! ההעברה נרשמה והפריט עבר להיסטוריה.")

@app.get("/my-outgoing-requests/{device_id}")
def get_my_outgoing_requests(device_id: str):
    """The requests THIS user made on other people's items — with status and,
    once approved, the giver's phone. Used by the taker to detect approvals,
    show the notification/banner, and reveal the giver's number."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT r.id, r.item_id, r.status, r.created_at,
               i.title AS item_title, i.post_type, i.description AS item_description,
               i.category AS item_category, i.image_url AS item_image_url,
               owner.nickname AS giver_name, owner.phone AS giver_phone
        FROM requests r
        JOIN items i ON i.id = r.item_id
        JOIN users owner ON owner.device_id = i.device_id
        WHERE r.device_id = %s
          AND r.taken = FALSE
          AND i.status != 'exchanged'
        ORDER BY r.created_at DESC
    """, (device_id,))
    rows = [dict(r) for r in cur.fetchall()]
    # Only reveal the giver's phone once approved.
    for r in rows:
        if r.get("status") != "approved":
            r["giver_phone"] = None
    cur.close()
    conn.close()
    return rows

def send_transaction_email(to_addr, nickname, item, role, when_ts):
    """Personal record of an exchange the user just confirmed. role is 'gave' or
    'took'. Deliberately worded as *this user's* record — the other side may not
    have confirmed yet, so this is not a mutual certification."""
    if not to_addr:
        return False

    when = time.strftime("%d/%m/%Y %H:%M", time.localtime(when_ts))
    action = "מסרת" if role == "gave" else "לקחת"
    title = item.get("title") or ""
    category = item.get("category") or ""
    description = item.get("description") or ""
    image_url = item.get("image_url")
    condition = item.get("condition")
    condition_he = {"new": "חדש", "like_new": "כמו חדש",
                    "used": "משומש", "bad": "מצב גרוע"}.get(condition or "", "")

    rows = [f"<p><b>פריט:</b> {title}</p>"]
    if category:
        rows.append(f"<p><b>קטגוריה:</b> {category}</p>")
    if condition_he:
        rows.append(f"<p><b>מצב הפריט:</b> {condition_he}</p>")
    if description:
        rows.append(f"<p><b>תיאור:</b> {description}</p>")
    rows.append(f"<p><b>תאריך:</b> {when}</p>")
    if image_url:
        rows.append(f'<p><img src="{image_url}" alt="" style="max-width:320px;border-radius:8px;"/></p>')

    body = f"""
      <p>שלום {nickname or ''},</p>
      <p>סימנת שה{action} את הפריט הבא:</p>
      {''.join(rows)}
      <hr style="border:none;border-top:1px solid #eee;margin:20px 0;"/>
      <p style="font-size:12px;color:#777;">
        זהו תיעוד אישי של הסימון שביצעת באפליקציה. ייתכן שהצד השני טרם סימן מצידו.
      </p>
    """
    return send_simple_email(to_addr, f"[iNeed] תיעוד: {action} את \"{title}\"", body)

def fetch_item_and_user(cur, item_id, device_id):
    """Look up the item plus the acting user's email/nickname, for the receipt."""
    cur.execute("""
        SELECT title, description, category, condition, image_url
        FROM items WHERE id = %s
    """, (item_id,))
    item = cur.fetchone()
    cur.execute("SELECT email, nickname FROM users WHERE device_id = %s", (device_id,))
    user = cur.fetchone()
    return (dict(item) if item else None), (dict(user) if user else None)

@app.post("/request/{request_id}/take")
def mark_request_taken(request_id: int, device_id: str):
    """Taker swipes 'לקחתי' on their awaiting-list request. Only allowed if the
    request belongs to this user AND has been approved by the giver."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT device_id, status, taken FROM requests WHERE id = %s", (request_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Request not found")
    if row["device_id"] != device_id:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Not your request")
    if row["status"] != "approved":
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="Request not approved yet")
    now = time.time()
    cur.execute("UPDATE requests SET taken = TRUE, taken_at = %s WHERE id = %s",
                (now, request_id))
    finish_reminders(cur, request_id, device_id)
    conn.commit()

    # Receipt for the taker. Best-effort — never block the confirmation on email.
    try:
        cur.execute("SELECT item_id FROM requests WHERE id = %s", (request_id,))
        item_id = cur.fetchone()["item_id"]
        item, user = fetch_item_and_user(cur, item_id, device_id)
        if item and user:
            send_transaction_email(user.get("email"), user.get("nickname"), item, "took", now)
    except Exception as e:
        print("Transaction email failed (take):", e)

    cur.close()
    conn.close()
    return {"ok": True}

@app.post("/item/{item_id}/exchange")
def mark_item_exchanged(item_id: int, device_id: str):
    """Owner swipes 'מסרתי'/'לקחתי' on their own item in My Items. Only allowed
    if the item belongs to this user AND at least one request on it is approved."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT device_id, status FROM items WHERE id = %s", (item_id,))
    row = cur.fetchone()
    if not row:
        cur.close(); conn.close()
        raise HTTPException(status_code=404, detail="Item not found")
    if row["device_id"] != device_id:
        cur.close(); conn.close()
        raise HTTPException(status_code=403, detail="Not your item")
    # Must have approved at least one requester
    cur.execute("SELECT COUNT(*) AS n FROM requests WHERE item_id = %s AND status = 'approved'",
                (item_id,))
    if cur.fetchone()["n"] == 0:
        cur.close(); conn.close()
        raise HTTPException(status_code=400, detail="No approved request on this item")
    now = time.time()
    cur.execute("UPDATE items SET status = 'exchanged', exchanged_at = %s WHERE id = %s",
                (now, item_id))
    cur.execute("""
        UPDATE exchange_reminders SET done = TRUE
        WHERE device_id = %s AND request_id IN (SELECT id FROM requests WHERE item_id = %s)
    """, (device_id, item_id))
    conn.commit()

    # Receipt for the owner. A 'give' item means they gave it; a 'take' item
    # means they were looking for it and received it. Best-effort.
    try:
        cur.execute("SELECT post_type FROM items WHERE id = %s", (item_id,))
        post_type = cur.fetchone()["post_type"]
        item, user = fetch_item_and_user(cur, item_id, device_id)
        if item and user:
            role = "gave" if post_type == "give" else "took"
            send_transaction_email(user.get("email"), user.get("nickname"), item, role, now)
    except Exception as e:
        print("Transaction email failed (exchange):", e)

    cur.close()
    conn.close()
    return {"ok": True}

@app.get("/pending-exchanges/{device_id}")
def get_pending_exchanges(device_id: str):
    """Items owned by this user that have an approved request but haven't been
    marked exchanged yet — used to remind them to confirm they gave/took it."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT DISTINCT i.id AS item_id, i.title, i.post_type
        FROM items i
        JOIN requests r ON r.item_id = i.id
        WHERE i.device_id = %s
          AND i.status != 'exchanged'
          AND r.status = 'approved'
    """, (device_id,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows

@app.get("/history/{device_id}")
def get_history(device_id: str):
    """A user's completed exchanges: items they own that are exchanged, plus
    requests they made that they marked taken. Each labeled מסרתי / לקחתי."""
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Items I own that are exchanged. Label by post_type: give -> מסרתי, take -> לקחתי.
    cur.execute("""
        SELECT i.id AS item_id, i.title, i.description, i.category, i.image_url,
               i.post_type, i.exchanged_at AS done_at
        FROM items i
        WHERE i.device_id = %s AND i.status = 'exchanged'
    """, (device_id,))
    owned = []
    for r in cur.fetchall():
        d = dict(r)
        d["role"] = "gave" if d["post_type"] == "give" else "took"
        d["source"] = "item"
        owned.append(d)

    # Requests I made that I marked taken -> always לקחתי.
    cur.execute("""
        SELECT r.id AS request_id, r.taken_at AS done_at,
               i.id AS item_id, i.title, i.description, i.category, i.image_url, i.post_type
        FROM requests r
        JOIN items i ON i.id = r.item_id
        WHERE r.device_id = %s AND r.taken = TRUE
    """, (device_id,))
    taken = []
    for r in cur.fetchall():
        d = dict(r)
        d["role"] = "took"
        d["source"] = "request"
        taken.append(d)

    cur.close()
    conn.close()
    combined = owned + taken
    combined.sort(key=lambda x: x.get("done_at") or 0, reverse=True)
    return combined

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
    cur.execute("""
        DELETE FROM exchange_reminders
        WHERE request_id IN (SELECT id FROM requests WHERE item_id = %s AND device_id = %s)
    """, (item_id, device_id))
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
        # 1. Reminders tied to any request this user is on either side of
        cur.execute("""
            DELETE FROM exchange_reminders
            WHERE device_id = %s
               OR request_id IN (
                   SELECT id FROM requests
                   WHERE device_id = %s
                      OR item_id IN (SELECT id FROM items WHERE device_id = %s)
               )
        """, (device_id, device_id, device_id))
        # 2. This user's own requests (interest they expressed on others' items)
        cur.execute("DELETE FROM requests WHERE device_id = %s", (device_id,))
        # 3. Others' requests pointing at THIS user's items
        cur.execute("""
            DELETE FROM requests
            WHERE item_id IN (SELECT id FROM items WHERE device_id = %s)
        """, (device_id,))
        # 4. Matches referencing this user's items (either side of the pair)
        cur.execute("""
            DELETE FROM matches
            WHERE give_item_id IN (SELECT id FROM items WHERE device_id = %s)
               OR take_item_id IN (SELECT id FROM items WHERE device_id = %s)
        """, (device_id, device_id))
        # 5. Image reports on this user's items
        cur.execute("""
            DELETE FROM image_reports
            WHERE item_id IN (SELECT id FROM items WHERE device_id = %s)
        """, (device_id,))
        # 6. Reports this user filed on any item (no FK, but tidy up)
        cur.execute("DELETE FROM image_reports WHERE reporter_device_id = %s", (device_id,))
        # 7. This user's items
        cur.execute("DELETE FROM items WHERE device_id = %s", (device_id,))
        # 8. The user row itself
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

# ── Background reminder sweep ────────────────────────────
# Started last, once every function above is defined. Daemon thread so it never
# blocks shutdown; Render keeps the web service alive so this ticks steadily.
threading.Thread(target=reminder_loop, daemon=True).start()

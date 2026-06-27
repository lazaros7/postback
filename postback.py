"""
postback.py — Secure Monetag Postback Server v3.0
==================================================
Handles server-side reward verification.
Frontend NEVER grants points — only this server does, after Monetag confirms.

Flow:
  1. Mini App shows ad → passes telegram_id as subId to Monetag
  2. Monetag verifies view → sends GET to /postback?subid=...&reward_event_type=reward
  3. This server validates → checks for duplicates → adds points → notifies user

Railway Variables required:
    BOT_TOKEN       — Telegram bot token
    DATABASE_URL    — PostgreSQL connection string
    MONETAG_SECRET  — (optional) secret key for postback verification
    POINTS_PER_AD   — points per verified view (default: 10)
"""

import os
import logging
import hashlib
import psycopg2
import psycopg2.extras
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── Config ─────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
DATABASE_URL   = os.getenv("DATABASE_URL", "")
MONETAG_SECRET = os.getenv("MONETAG_SECRET", "")
POINTS_PER_AD  = int(os.getenv("POINTS_PER_AD", "10"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)
app = Flask(__name__)


# ══════════════════════════════════════════════════════════════
#                    🗄️ Database helpers
# ══════════════════════════════════════════════════════════════

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_tables():
    """
    Create the rewarded_ads table if it doesn't exist.
    This is the deduplication table — every confirmed view gets one row.
    """
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS rewarded_ads (
                    id            SERIAL PRIMARY KEY,
                    click_id      VARCHAR(200) UNIQUE NOT NULL,
                    telegram_id   BIGINT NOT NULL,
                    reward_points INTEGER DEFAULT 10,
                    status        VARCHAR(20) DEFAULT 'pending',
                    postback_raw  TEXT,
                    rewarded_at   TIMESTAMP DEFAULT NOW()
                )
            """)
            # Index for fast duplicate lookups
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_rewarded_ads_click_id
                ON rewarded_ads(click_id)
            """)
            conn.commit()
        logger.info("Tables verified OK")
    except Exception as e:
        logger.error(f"Table creation error: {e}")
    finally:
        conn.close()


def user_exists(user_id: int) -> bool:
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT 1 FROM users WHERE telegram_id = %s", (user_id,))
            return c.fetchone() is not None
    finally:
        conn.close()


def is_duplicate(click_id: str) -> bool:
    """Return True if this click_id was already rewarded."""
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute(
                "SELECT 1 FROM rewarded_ads WHERE click_id = %s AND status = 'rewarded'",
                (click_id,)
            )
            return c.fetchone() is not None
    finally:
        conn.close()


def record_and_reward(user_id: int, click_id: str, points: int, raw: str) -> bool:
    """
    Atomically:
      1. Insert into rewarded_ads (will fail on duplicate click_id due to UNIQUE)
      2. Add points to users table
      3. Log transaction
    Returns True on success, False if duplicate or error.
    """
    conn = get_db()
    try:
        with conn.cursor() as c:
            # Insert the reward record — UNIQUE on click_id prevents double-reward
            try:
                c.execute("""
                    INSERT INTO rewarded_ads (click_id, telegram_id, reward_points, status, postback_raw)
                    VALUES (%s, %s, %s, 'rewarded', %s)
                """, (click_id, user_id, points, raw))
            except psycopg2.errors.UniqueViolation:
                conn.rollback()
                logger.warning(f"[DEDUP] Duplicate click_id blocked: {click_id}")
                return False

            # Add points to user balance
            c.execute("""
                UPDATE users
                SET points = points + %s,
                    total_earned = total_earned + %s
                WHERE telegram_id = %s
            """, (points, points, user_id))

            # Log in transactions table
            c.execute("""
                INSERT INTO transactions (user_id, type, points, source, note)
                VALUES (%s, 'earn', %s, 'watch_ad', 'Monetag verified postback')
            """, (user_id, points))

            # Log in ad_views table
            c.execute("""
                INSERT INTO ad_views (user_id, points)
                VALUES (%s, %s)
            """, (user_id, points))

            conn.commit()
            logger.info(f"[REWARD] +{points} pts → user {user_id} | click_id={click_id}")
            return True

    except Exception as e:
        conn.rollback()
        logger.error(f"[DB ERROR] user={user_id} click_id={click_id}: {e}")
        return False
    finally:
        conn.close()


def get_balance(user_id: int) -> int:
    conn = get_db()
    try:
        with conn.cursor() as c:
            c.execute("SELECT points FROM users WHERE telegram_id = %s", (user_id,))
            row = c.fetchone()
            return row["points"] if row else 0
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
#                    📬 Telegram notification
# ══════════════════════════════════════════════════════════════

def notify_user(user_id: int, points: int, new_balance: int):
    if not BOT_TOKEN:
        logger.warning("BOT_TOKEN not set — skipping notification")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    user_id,
                "text": (
                    f"🎉 *Ad Reward Confirmed!*\n\n"
                    f"✅ Monetag verified your ad view.\n"
                    f"⭐ *+{points} points* added to your account.\n"
                    f"💰 New balance: *{new_balance:,} points*\n\n"
                    f"Watch more ads to earn more!"
                ),
                "parse_mode": "Markdown"
            },
            timeout=10
        )
        if resp.ok:
            logger.info(f"[NOTIFY] Sent to user {user_id}")
        else:
            logger.warning(f"[NOTIFY] Failed: {resp.text}")
    except Exception as e:
        logger.error(f"[NOTIFY] Error: {e}")


# ══════════════════════════════════════════════════════════════
#                    🔐 Security helpers
# ══════════════════════════════════════════════════════════════

def verify_monetag_signature(params: dict) -> bool:
    """
    Optional: verify Monetag's postback signature if you set a secret.
    If MONETAG_SECRET is empty, skip verification (less secure but simpler).
    Monetag's signature method: MD5(secret + subid)
    Check Monetag docs for your specific signature format.
    """
    if not MONETAG_SECRET:
        return True  # No secret configured → accept all (OK for testing)

    received_hash = params.get("hash", "")
    subid = params.get("subid", "")
    expected = hashlib.md5(f"{MONETAG_SECRET}{subid}".encode()).hexdigest()
    return received_hash == expected


def build_click_id(params: dict) -> str:
    """
    Build a stable unique ID for this postback event.
    Prefer Monetag's own click_id if provided, otherwise derive one
    from subid + timestamp so duplicate Monetag retries are still caught.
    """
    # Monetag may send click_id, offer_id, or similar — adapt to their actual params
    monetag_id = (
        params.get("click_id") or
        params.get("clickid") or
        params.get("offer_id") or
        ""
    )
    if monetag_id:
        return monetag_id

    # Fallback: subid + unix timestamp rounded to 60s window
    # (prevents rewarding the same user twice within 1 minute for same event)
    subid = params.get("subid", "unknown")
    minute_bucket = int(datetime.utcnow().timestamp() // 60)
    return hashlib.md5(f"{subid}:{minute_bucket}".encode()).hexdigest()


# ══════════════════════════════════════════════════════════════
#                    🌐 Routes
# ══════════════════════════════════════════════════════════════

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Monetag Postback Server",
        "version": "3.0",
        "status":  "running"
    }), 200


@app.route("/postback", methods=["GET", "POST"])
def postback():
    """
    Main postback endpoint.
    Monetag URL to configure:
        https://YOUR-RAILWAY-URL/postback?subid={SUBID}&reward_event_type={REWARD_EVENT_TYPE}
    """
    params = request.args if request.method == "GET" else request.form
    raw    = str(dict(params))
    logger.info(f"[POSTBACK] Incoming: {raw}")

    # ── 1. Signature check ──────────────────────────────────────
    if not verify_monetag_signature(dict(params)):
        logger.warning("[SECURITY] Invalid signature — rejected")
        return "INVALID_SIGNATURE", 403

    # ── 2. Extract params ───────────────────────────────────────
    # Monetag sends user id as 'telegram_id' or 'subid' depending on postback config
    subid             = params.get("subid", "").strip()
    telegram_id_param = params.get("telegram_id", "").strip()
    reward_event_type = params.get("reward_event_type", "").strip()

    # Accept whichever param Monetag sends
    raw_uid = telegram_id_param or subid

    if not raw_uid:
        logger.warning(f"[POSTBACK] Missing user id — params: {dict(params)}")
        return "OK", 200

    try:
        user_id = int(raw_uid)
    except ValueError:
        logger.warning(f"[POSTBACK] Non-integer user id: {raw_uid!r}")
        return "OK", 200

    # ── 3. Only process confirmed reward events ─────────────────
    if reward_event_type not in ("reward", "valued"):
        logger.info(f"[POSTBACK] Skipping event type: {reward_event_type!r}")
        return "OK", 200

    # ── 4. Verify user exists in our DB ────────────────────────
    if not user_exists(user_id):
        logger.warning(f"[POSTBACK] Unknown user: {user_id}")
        return "OK", 200

    # ── 5. Build dedup key and check for duplicates ─────────────
    click_id = build_click_id(dict(params))
    if is_duplicate(click_id):
        logger.info(f"[POSTBACK] Duplicate ignored: {click_id}")
        return "OK", 200

    # ── 6. Award points atomically ──────────────────────────────
    success = record_and_reward(user_id, click_id, POINTS_PER_AD, raw)
    if success:
        new_balance = get_balance(user_id)
        notify_user(user_id, POINTS_PER_AD, new_balance)
    else:
        logger.error(f"[POSTBACK] reward failed for user {user_id}")

    # Always return 200 to prevent Monetag from retrying
    return "OK", 200


@app.route("/health", methods=["GET"])
def health():
    """Health check — Railway uses this to confirm the service is alive."""
    db_ok = False
    user_count = 0
    try:
        conn = get_db()
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) as n FROM users")
            user_count = c.fetchone()["n"]
        conn.close()
        db_ok = True
    except Exception as e:
        logger.error(f"Health check DB error: {e}")

    return jsonify({
        "status":        "ok" if db_ok else "db_error",
        "db":            "connected" if db_ok else "disconnected",
        "users_in_db":   user_count,
        "bot_token":     "set" if BOT_TOKEN else "MISSING",
        "points_per_ad": POINTS_PER_AD,
    }), 200 if db_ok else 500


@app.route("/balance/<int:user_id>", methods=["GET"])
def balance(user_id: int):
    """
    Balance polling endpoint for the Mini App.
    Mini App polls this after ad completion to show updated balance.
    Returns JSON — no auth needed since user_id is not secret in this context.
    """
    try:
        pts = get_balance(user_id)
        return jsonify({"user_id": user_id, "points": pts}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ══════════════════════════════════════════════════════════════
#                    🚀 Startup
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ensure_tables()
    port = int(os.getenv("PORT", 8080))
    logger.info(f"Starting secure postback server on port {port}")
    app.run(host="0.0.0.0", port=port)

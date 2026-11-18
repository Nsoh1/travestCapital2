import os
import uuid
import random
import string
import base64
from datetime import datetime, timedelta, timezone
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO, emit, join_room
from flask_compress import Compress
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__, template_folder="templates", static_folder="static")

# Configuration for Render & Supabase
db_url = os.getenv("DATABASE_URL", "sqlite:///travest_local.db")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
    "pool_size": 10,
    "max_overflow": 20,
}
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "travest-capital-super-secret-2026")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # 8MB max upload

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
DEFAULT_ADMIN_PIN = os.getenv("ADMIN_PIN", "236963")

Compress(app)
db = SQLAlchemy(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent", ping_timeout=20, ping_interval=10)

# ==========================================
# DATABASE MODELS (Supabase PostgreSQL Ready)
# ==========================================

class SystemSetting(db.Model):
    __tablename__ = "system_settings"
    id = db.Column(db.Integer, primary_key=True)
    min_withdrawal = db.Column(db.Float, default=500.0, nullable=False)
    admin_pin = db.Column(db.String(10), default="236963", nullable=False)
    spin_difficulty = db.Column(db.String(20), default="random", nullable=False)  # 'hard' or 'random'
    latest_news = db.Column(db.Text, default="Welcome to TravestCapital! Invest smartly and claim daily yields every 24 hours.")
    paystack_public_key = db.Column(db.String(120), default="")
    paystack_secret_key = db.Column(db.String(120), default="")

class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(25), unique=True, index=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    product_wallet = db.Column(db.Float, default=0.0, nullable=False)
    earnings_wallet = db.Column(db.Float, default=0.0, nullable=False)
    invite_code = db.Column(db.String(16), unique=True, index=True, nullable=False)
    referred_by_code = db.Column(db.String(16), nullable=True)
    first_share_commission_paid = db.Column(db.Boolean, default=False, nullable=False)
    vip2_hire_code = db.Column(db.String(16), unique=True, index=True, nullable=False)
    vip2_hires_count = db.Column(db.Integer, default=0, nullable=False)
    vip2_hire_claimed = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    last_free_spin_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class VipPlan(db.Model):
    __tablename__ = "vip_plans"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False)  # e.g., VIP1, VIP2
    price = db.Column(db.Float, nullable=False)      # e.g., 300.0
    daily_profit = db.Column(db.Float, nullable=False)  # e.g., 50.0
    duration_days = db.Column(db.Integer, default=15, nullable=False)
    is_vip2_tier = db.Column(db.Boolean, default=False, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)

class UserSubscription(db.Model):
    __tablename__ = "user_subscriptions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)
    vip_plan_id = db.Column(db.Integer, db.ForeignKey("vip_plans.id"), nullable=False)
    plan_name = db.Column(db.String(50), nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    daily_profit = db.Column(db.Float, nullable=False)
    days_total = db.Column(db.Integer, default=15, nullable=False)
    days_claimed = db.Column(db.Integer, default=0, nullable=False)
    last_claimed_at = db.Column(db.DateTime, nullable=True)
    purchased_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    expires_at = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default="active", nullable=False)  # active, expired

class SpinDevice(db.Model):
    __tablename__ = "spin_devices"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    specs = db.Column(db.String(200), nullable=False)
    image_data = db.Column(db.Text, nullable=False)  # Base64 Data URI for persistence across Render restarts
    spin_type = db.Column(db.String(20), nullable=False)  # 'free' (VIP2 only) or 'sale' (discount wheel)
    retail_price = db.Column(db.Float, default=0.0)
    discount_options = db.Column(db.String(255), default="500,800,1200,1500,2000,2500")  # Comma-separated 6 wheel values
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class SpinWinDelivery(db.Model):
    __tablename__ = "spin_win_deliveries"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    user_name = db.Column(db.String(100), nullable=False)
    device_name = db.Column(db.String(100), nullable=False)
    spin_type = db.Column(db.String(20), nullable=False)
    amount_paid = db.Column(db.Float, default=0.0)
    paystack_ref = db.Column(db.String(100), nullable=True)
    contact_phone = db.Column(db.String(30), nullable=False)
    region = db.Column(db.String(80), nullable=False)
    address = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(30), default="Pending Delivery", nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Transaction(db.Model):
    __tablename__ = "transactions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)
    tx_type = db.Column(db.String(30), nullable=False)  # deposit, withdrawal, share_purchase, claim, referral_bonus, hire_bonus, device_sale
    amount = db.Column(db.Float, nullable=False)
    reference = db.Column(db.String(100), unique=True, nullable=False)
    status = db.Column(db.String(20), default="success", nullable=False)
    details = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class ChatMessage(db.Model):
    __tablename__ = "chat_messages"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True, nullable=False)
    sender = db.Column(db.String(10), nullable=False)  # 'user' or 'admin'
    message = db.Column(db.Text, nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


# ==========================================
# INITIALIZATION & HELPERS
# ==========================================

def generate_code(prefix="TC"):
    chars = string.ascii_uppercase + string.digits
    return f"{prefix}-" + "".join(random.choices(chars, k=6))

def get_settings():
    setting = SystemSetting.query.first()
    if not setting:
        setting = SystemSetting(
            min_withdrawal=500.0,
            admin_pin=DEFAULT_ADMIN_PIN,
            spin_difficulty="random",
            latest_news="Welcome to TravestCapital! Purchase VIP shares and claim daily yields every 24 hours.",
            paystack_public_key=PAYSTACK_PUBLIC_KEY,
            paystack_secret_key=PAYSTACK_SECRET_KEY,
        )
        db.session.add(setting)
        db.session.commit()
    return setting

def seed_defaults():
    get_settings()
    if VipPlan.query.count() == 0:
        plans = [
            VipPlan(name="VIP1", price=300.0, daily_profit=50.0, duration_days=15, is_vip2_tier=False),
            VipPlan(name="VIP2", price=600.0, daily_profit=110.0, duration_days=15, is_vip2_tier=True),
            VipPlan(name="VIP3", price=1500.0, daily_profit=300.0, duration_days=15, is_vip2_tier=False),
        ]
        db.session.add_all(plans)
        db.session.commit()

with app.app_context():
    db.create_all()
    seed_defaults()

def verify_paystack_transaction(reference):
    settings = get_settings()
    secret = settings.paystack_secret_key or PAYSTACK_SECRET_KEY
    if not secret:
        # Allow local testing mode if key is not configured yet
        if reference.startswith("TEST_"):
            return True, 0.0
        return False, 0.0
    headers = {"Authorization": f"Bearer {secret}"}
    try:
        resp = requests.get(f"https://api.paystack.co/transaction/verify/{reference}", headers=headers, timeout=12)
        data = resp.json()
        if data.get("status") and data.get("data", {}).get("status") == "success":
            amount_ghs = float(data["data"]["amount"]) / 100.0
            return True, amount_ghs
    except Exception:
        pass
    return False, 0.0

def user_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        uid = session.get("user_id")
        if not uid:
            return jsonify({"success": False, "message": "Session expired. Please log in."}), 401
        user = db.session.get(User, uid)
        if not user:
            session.pop("user_id", None)
            return jsonify({"success": False, "message": "Account not found."}), 401
        if not user.is_active:
            session.pop("user_id", None)
            return jsonify({"success": False, "message": "Your account has been suspended by administration."}), 403
        return f(user, *args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("is_admin"):
            return jsonify({"success": False, "message": "Unauthorized admin access."}), 401
        return f(*args, **kwargs)
    return decorated

def get_user_vip_status(user_id):
    now = datetime.now(timezone.utc)
    subs = UserSubscription.query.filter_by(user_id=user_id, status="active").all()
    active_subs = []
    highest_vip = "Standard"
    has_vip2 = False
    for s in subs:
        exp = s.expires_at.replace(tzinfo=timezone.utc) if s.expires_at.tzinfo is None else s.expires_at
        if now >= exp or s.days_claimed >= s.days_total:
            s.status = "expired"
        else:
            active_subs.append(s)
            highest_vip = s.plan_name
            plan = db.session.get(VipPlan, s.vip_plan_id)
            if (plan and plan.is_vip2_tier) or "VIP2" in s.plan_name.upper():
                has_vip2 = True
    db.session.commit()
    return highest_vip, has_vip2, active_subs


# ==========================================
# UPTIME & PAGE ROUTES
# ==========================================

@app.route("/health")
@app.route("/ping")
def health_check():
    """Lightweight endpoint for UptimeRobot / Cron-job.org to keep Render Free Tier awake."""
    return jsonify({
        "status": "online",
        "service": "TravestCapital",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }), 200

@app.route("/")
def serve_user_app():
    return render_template("index.html")

@app.route("/admin")
def serve_admin_app():
    return render_template("admin.html")


# ==========================================
# AUTHENTICATION ROUTES
# ==========================================

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    full_name = (data.get("full_name") or "").strip()
    phone = (data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()
    referral_code = (data.get("referral_code") or "").strip().upper()

    if not full_name or not phone or not password:
        return jsonify({"success": False, "message": "Full name, phone number, and password are required."}), 400
    if len(password) < 4:
        return jsonify({"success": False, "message": "Password must be at least 4 characters."}), 400
    if User.query.filter_by(phone=phone).first():
        return jsonify({"success": False, "message": "This phone number is already registered."}), 409

    valid_ref = None
    if referral_code:
        inviter = User.query.filter_by(invite_code=referral_code).first()
        if not inviter:
            return jsonify({"success": False, "message": "Invalid referral invitation code."}), 400
        valid_ref = inviter.invite_code

    new_user = User(
        full_name=full_name,
        phone=phone,
        password_hash=generate_password_hash(password),
        invite_code=generate_code("INV"),
        referred_by_code=valid_ref,
        vip2_hire_code=generate_code("HIRE"),
    )
    db.session.add(new_user)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": "Account created successfully! Please log in with your credentials."
    }), 201

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    phone = (data.get("phone") or "").strip()
    password = (data.get("password") or "").strip()

    user = User.query.filter_by(phone=phone).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"success": False, "message": "Invalid phone number or password."}), 401
    if not user.is_active:
        return jsonify({"success": False, "message": "Account deactivated. Contact support."}), 403

    session["user_id"] = user.id
    session.permanent = True
    return jsonify({
        "success": True,
        "message": "Login successful",
        "user": {"id": user.id, "full_name": user.full_name, "phone": user.phone}
    })

@app.route("/api/auth/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return jsonify({"success": True})


# ==========================================
# USER DASHBOARD DATA & ACTIONS
# ==========================================

@app.route("/api/user/dashboard", methods=["GET"])
@user_required
def get_dashboard(user):
    settings = get_settings()
    highest_vip, has_vip2, active_subs = get_user_vip_status(user.id)
    now = datetime.now(timezone.utc)

    # Check free spin cooldown (24h)
    can_free_spin = False
    free_spin_wait_seconds = 0
    if has_vip2:
        if not user.last_free_spin_at:
            can_free_spin = True
        else:
            last_spin = user.last_free_spin_at.replace(tzinfo=timezone.utc) if user.last_free_spin_at.tzinfo is None else user.last_free_spin_at
            diff = (now - last_spin).total_seconds()
            if diff >= 86400:
                can_free_spin = True
            else:
                free_spin_wait_seconds = int(86400 - diff)

    subs_data = []
    for s in active_subs:
        can_claim = False
        seconds_left = 0
        if s.last_claimed_at is None:
            can_claim = True
        else:
            lc = s.last_claimed_at.replace(tzinfo=timezone.utc) if s.last_claimed_at.tzinfo is None else s.last_claimed_at
            elapsed = (now - lc).total_seconds()
            if elapsed >= 86400:
                can_claim = True
            else:
                seconds_left = int(86400 - elapsed)
        subs_data.append({
            "id": s.id,
            "plan_name": s.plan_name,
            "amount_paid": s.amount_paid,
            "daily_profit": s.daily_profit,
            "days_total": s.days_total,
            "days_claimed": s.days_claimed,
            "can_claim": can_claim,
            "seconds_left": seconds_left
        })

    plans = VipPlan.query.filter_by(is_active=True).order_by(VipPlan.price.asc()).all()
    plans_data = [{
        "id": p.id,
        "name": p.name,
        "price": p.price,
        "daily_profit": p.daily_profit,
        "duration_days": p.duration_days,
        "is_vip2_tier": p.is_vip2_tier
    } for p in plans]

    devices = SpinDevice.query.filter_by(is_active=True).order_by(SpinDevice.id.desc()).all()
    devices_data = [{
        "id": d.id,
        "name": d.name,
        "specs": d.specs,
        "image_data": d.image_data,
        "spin_type": d.spin_type,
        "retail_price": d.retail_price,
        "discount_options": [float(x.strip()) for x in d.discount_options.split(",") if x.strip()]
    } for d in devices]

    txs = Transaction.query.filter_by(user_id=user.id).order_by(Transaction.id.desc()).limit(25).all()
    tx_list = [{
        "type": t.tx_type,
        "amount": t.amount,
        "status": t.status,
        "details": t.details,
        "date": t.created_at.strftime("%Y-%m-%d %H:%M")
    } for t in txs]

    chats = ChatMessage.query.filter_by(user_id=user.id).order_by(ChatMessage.id.asc()).limit(100).all()
    chat_list = [{
        "id": c.id,
        "sender": c.sender,
        "message": c.message,
        "time": c.created_at.strftime("%H:%M")
    } for c in chats]

    return jsonify({
        "success": True,
        "user": {
            "id": user.id,
            "full_name": user.full_name,
            "phone": user.phone,
            "product_wallet": round(user.product_wallet, 2),
            "earnings_wallet": round(user.earnings_wallet, 2),
            "vip_level": highest_vip,
            "has_vip2": has_vip2,
            "invite_code": user.invite_code,
            "vip2_hire_code": user.vip2_hire_code,
            "vip2_hires_count": user.vip2_hires_count,
            "vip2_hire_claimed": user.vip2_hire_claimed,
            "can_free_spin": can_free_spin,
            "free_spin_wait_seconds": free_spin_wait_seconds
        },
        "config": {
            "min_withdrawal": settings.min_withdrawal,
            "latest_news": settings.latest_news,
            "paystack_public_key": settings.paystack_public_key or PAYSTACK_PUBLIC_KEY
        },
        "plans": plans_data,
        "subscriptions": subs_data,
        "devices": devices_data,
        "transactions": tx_list,
        "chats": chat_list
    })

@app.route("/api/user/deposit", methods=["POST"])
@user_required
def deposit_funds(user):
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    reference = (data.get("reference") or "").strip()

    if amount <= 0 or not reference:
        return jsonify({"success": False, "message": "Invalid deposit details."}), 400
    if Transaction.query.filter_by(reference=reference).first():
        return jsonify({"success": False, "message": "Duplicate transaction reference."}), 400

    verified, paid_amount = verify_paystack_transaction(reference)
    if not verified:
        return jsonify({"success": False, "message": "Paystack verification failed."}), 400

    credit = paid_amount if paid_amount > 0 else amount
    user.product_wallet += credit

    tx = Transaction(
        user_id=user.id,
        tx_type="deposit",
        amount=credit,
        reference=reference,
        status="success",
        details="Paystack Product Wallet Deposit"
    )
    db.session.add(tx)
    db.session.commit()
    return jsonify({"success": True, "message": f"GHS {credit:.2f} deposited to your Product Wallet!"})

@app.route("/api/user/withdraw", methods=["POST"])
@user_required
def withdraw_funds(user):
    settings = get_settings()
    data = request.get_json() or {}
    amount = float(data.get("amount", 0))
    account_number = (data.get("account_number") or "").strip()
    network_or_bank = (data.get("network_or_bank") or "").strip()

    if amount < settings.min_withdrawal:
        return jsonify({
            "success": False,
            "message": f"Minimum withdrawal amount is GHS {settings.min_withdrawal:.2f}."
        }), 400

    if user.earnings_wallet < amount:
        return jsonify({"success": False, "message": "Insufficient Earnings Wallet balance."}), 400

    if not account_number or not network_or_bank:
        return jsonify({"success": False, "message": "Enter your Mobile Money or Bank details."}), 400

    user.earnings_wallet -= amount
    ref = f"WDR-{uuid.uuid4().hex[:10].upper()}"
    tx = Transaction(
        user_id=user.id,
        tx_type="withdrawal",
        amount=amount,
        reference=ref,
        status="processing",
        details=f"Paystack Payout to {network_or_bank} ({account_number})"
    )
    db.session.add(tx)
    db.session.commit()
    return jsonify({"success": True, "message": f"Withdrawal request of GHS {amount:.2f} submitted via Paystack!"})

@app.route("/api/user/buy-share", methods=["POST"])
@user_required
def buy_share(user):
    data = request.get_json() or {}
    plan_id = data.get("plan_id")
    payment_method = data.get("payment_method")  # 'wallet' or 'paystack'
    paystack_ref = (data.get("reference") or "").strip()
    vip2_hire_code_input = (data.get("vip2_hire_code") or "").strip().upper()

    plan = db.session.get(VipPlan, plan_id)
    if not plan or not plan.is_active:
        return jsonify({"success": False, "message": "Selected VIP package is unavailable."}), 404

    if payment_method == "wallet":
        if user.product_wallet < plan.price:
            return jsonify({"success": False, "message": "Insufficient Product Wallet balance. Deposit or pay directly via Paystack."}), 400
        user.product_wallet -= plan.price
        tx_ref = f"WLT-{uuid.uuid4().hex[:10].upper()}"
    elif payment_method == "paystack":
        if not paystack_ref:
            return jsonify({"success": False, "message": "Missing Paystack payment reference."}), 400
        if Transaction.query.filter_by(reference=paystack_ref).first():
            return jsonify({"success": False, "message": "Transaction already processed."}), 400
        verified, paid_amt = verify_paystack_transaction(paystack_ref)
        if not verified:
            return jsonify({"success": False, "message": "Paystack payment could not be verified."}), 400
        tx_ref = paystack_ref
    else:
        return jsonify({"success": False, "message": "Invalid payment method."}), 400

    now = datetime.now(timezone.utc)
    sub = UserSubscription(
        user_id=user.id,
        vip_plan_id=plan.id,
        plan_name=plan.name,
        amount_paid=plan.price,
        daily_profit=plan.daily_profit,
        days_total=plan.duration_days,
        days_claimed=0,
        last_claimed_at=None,  # Allows immediate first claim!
        purchased_at=now,
        expires_at=now + timedelta(days=plan.duration_days),
        status="active"
    )
    db.session.add(sub)

    tx = Transaction(
        user_id=user.id,
        tx_type="share_purchase",
        amount=plan.price,
        reference=tx_ref,
        status="success",
        details=f"Purchased {plan.name} Shares ({payment_method.upper()})"
    )
    db.session.add(tx)

    # 1. One-time 10% Signup Referral Commission on First Share Purchase
    if user.referred_by_code and not user.first_share_commission_paid:
        referrer = User.query.filter_by(invite_code=user.referred_by_code).first()
        if referrer and referrer.id != user.id:
            commission = round(plan.price * 0.10, 2)
            referrer.earnings_wallet += commission
            user.first_share_commission_paid = True
            ref_tx = Transaction(
                user_id=referrer.id,
                tx_type="referral_bonus",
                amount=commission,
                reference=f"REF-{uuid.uuid4().hex[:10].upper()}",
                status="success",
                details=f"10% First-Share Commission from {user.full_name}"
            )
            db.session.add(ref_tx)

    # 2. Get Hired VIP2 Unique Code Tracking
    if (plan.is_vip2_tier or "VIP2" in plan.name.upper()) and vip2_hire_code_input:
        recruiter = User.query.filter_by(vip2_hire_code=vip2_hire_code_input).first()
        if recruiter and recruiter.id != user.id:
            recruiter.vip2_hires_count += 1

    db.session.commit()
    return jsonify({
        "success": True,
        "message": f"{plan.name} activated! Click 'Claim Daily Profit' now to collect your first GHS {plan.daily_profit:.2f}."
    })

@app.route("/api/user/claim-profit", methods=["POST"])
@user_required
def claim_daily_profit(user):
    data = request.get_json() or {}
    sub_id = data.get("subscription_id")
    sub = db.session.get(UserSubscription, sub_id)

    if not sub or sub.user_id != user.id or sub.status != "active":
        return jsonify({"success": False, "message": "Active VIP subscription not found."}), 404

    now = datetime.now(timezone.utc)
    exp = sub.expires_at.replace(tzinfo=timezone.utc) if sub.expires_at.tzinfo is None else sub.expires_at
    if now >= exp or sub.days_claimed >= sub.days_total:
        sub.status = "expired"
        db.session.commit()
        return jsonify({"success": False, "message": "This 15-day VIP plan has expired. Renew to continue earning."}), 400

    if sub.last_claimed_at is not None:
        lc = sub.last_claimed_at.replace(tzinfo=timezone.utc) if sub.last_claimed_at.tzinfo is None else sub.last_claimed_at
        if (now - lc).total_seconds() < 86400:
            return jsonify({"success": False, "message": "Daily reward already claimed. Return after 24 hours."}), 400

    sub.days_claimed += 1
    sub.last_claimed_at = now
    if sub.days_claimed >= sub.days_total:
        sub.status = "expired"

    user.earnings_wallet += sub.daily_profit
    tx = Transaction(
        user_id=user.id,
        tx_type="claim",
        amount=sub.daily_profit,
        reference=f"CLM-{uuid.uuid4().hex[:10].upper()}",
        status="success",
        details=f"{sub.plan_name} Daily Profit ({sub.days_claimed}/{sub.days_total})"
    )
    db.session.add(tx)
    db.session.commit()

    return jsonify({
        "success": True,
        "message": f"GHS {sub.daily_profit:.2f} credited to your Earnings Wallet!"
    })

@app.route("/api/user/claim-hire-bonus", methods=["POST"])
@user_required
def claim_hire_bonus(user):
    if user.vip2_hires_count < 10:
        return jsonify({"success": False, "message": f"You need 10 VIP2 subscribers. Current: {user.vip2_hires_count}/10."}), 400
    if user.vip2_hire_claimed:
        return jsonify({"success": False, "message": "GHS 2,000 Get Hired reward already claimed."}), 400

    user.vip2_hire_claimed = True
    user.earnings_wallet += 2000.0
    tx = Transaction(
        user_id=user.id,
        tx_type="hire_bonus",
        amount=2000.0,
        reference=f"HIRE-{uuid.uuid4().hex[:10].upper()}",
        status="success",
        details="Get Hired 10 VIP2 Subscribers Milestone Bonus"
    )
    db.session.add(tx)
    db.session.commit()
    return jsonify({"success": True, "message": "Congratulations! GHS 2,000.00 added to your Earnings Wallet!"})

@app.route("/api/user/spin", methods=["POST"])
@user_required
def execute_spin(user):
    data = request.get_json() or {}
    device_id = data.get("device_id")
    device = db.session.get(SpinDevice, device_id)
    if not device or not device.is_active:
        return jsonify({"success": False, "message": "Device not available."}), 404

    settings = get_settings()
    _, has_vip2, _ = get_user_vip_status(user.id)
    now = datetime.now(timezone.utc)

    if device.spin_type == "free":
        if not has_vip2:
            return jsonify({"success": False, "message": "Free Device Spin is strictly for active VIP2 members."}), 403
        if user.last_free_spin_at:
            ls = user.last_free_spin_at.replace(tzinfo=timezone.utc) if user.last_free_spin_at.tzinfo is None else user.last_free_spin_at
            if (now - ls).total_seconds() < 86400:
                return jsonify({"success": False, "message": "You can only spin for a free device once every 24 hours."}), 400

        user.last_free_spin_at = now
        db.session.commit()

        # Wheel indices: 0="WIN", 1="LOST", 2="WIN", 3="LOST", 4="WIN", 5="LOST"
        if settings.spin_difficulty == "hard":
            # 95% chance of Lost when admin sets to hard
            won = random.random() < 0.05
        else:
            won = random.choice([True, False])

        slice_index = random.choice([0, 2, 4]) if won else random.choice([1, 3, 5])
        return jsonify({
            "success": True,
            "spin_type": "free",
            "won": won,
            "slice_index": slice_index,
            "label": "WIN" if won else "LOST"
        })
    else:
        # Sale Discount Wheel
        options = [float(x.strip()) for x in device.discount_options.split(",") if x.strip()]
        while len(options) < 6:
            options.append(options[-1] if options else 500.0)
        options = options[:6]
        slice_index = random.randint(0, 5)
        won_price = options[slice_index]
        return jsonify({
            "success": True,
            "spin_type": "sale",
            "won": True,
            "slice_index": slice_index,
            "discounted_price": won_price
        })

@app.route("/api/user/submit-delivery", methods=["POST"])
@user_required
def submit_delivery(user):
    data = request.get_json() or {}
    device_id = data.get("device_id")
    spin_type = data.get("spin_type", "free")
    amount_paid = float(data.get("amount_paid", 0))
    paystack_ref = (data.get("paystack_ref") or "").strip()
    contact_phone = (data.get("contact_phone") or "").strip()
    region = (data.get("region") or "").strip()
    address = (data.get("address") or "").strip()

    device = db.session.get(SpinDevice, device_id)
    if not device:
        return jsonify({"success": False, "message": "Device not found."}), 404
    if not contact_phone or not region or not address:
        return jsonify({"success": False, "message": "Phone number, region, and delivery address are required."}), 400

    if spin_type == "sale":
        if not paystack_ref:
            return jsonify({"success": False, "message": "Paystack payment confirmation is required before submitting delivery details."}), 400
        verified, _ = verify_paystack_transaction(paystack_ref)
        if not verified:
            return jsonify({"success": False, "message": "Paystack payment verification failed."}), 400
        tx = Transaction(
            user_id=user.id,
            tx_type="device_sale",
            amount=amount_paid,
            reference=paystack_ref,
            status="success",
            details=f"Discounted Spin Purchase: {device.name}"
        )
        db.session.add(tx)

    order = SpinWinDelivery(
        user_id=user.id,
        user_name=user.full_name,
        device_name=device.name,
        spin_type=spin_type,
        amount_paid=amount_paid,
        paystack_ref=paystack_ref if spin_type == "sale" else "FREE-VIP2-WIN",
        contact_phone=contact_phone,
        region=region,
        address=address
    )
    db.session.add(order)
    db.session.commit()
    return jsonify({"success": True, "message": "Delivery details submitted! Admin will dispatch your device shortly."})

@app.route("/api/user/update-profile", methods=["POST"])
@user_required
def update_user_profile(user):
    data = request.get_json() or {}
    action = data.get("action")

    if action == "reset_password":
        old_pw = data.get("old_password", "")
        new_pw = data.get("new_password", "")
        if not check_password_hash(user.password_hash, old_pw):
            return jsonify({"success": False, "message": "Current password is incorrect."}), 400
        if len(new_pw) < 4:
            return jsonify({"success": False, "message": "New password must be at least 4 characters."}), 400
        user.password_hash = generate_password_hash(new_pw)
        db.session.commit()
        return jsonify({"success": True, "message": "Password updated successfully!"})

    elif action == "change_phone":
        new_phone = (data.get("new_phone") or "").strip()
        if not new_phone:
            return jsonify({"success": False, "message": "Enter a valid phone number."}), 400
        if User.query.filter(User.phone == new_phone, User.id != user.id).first():
            return jsonify({"success": False, "message": "Phone number already in use."}), 400
        user.phone = new_phone
        db.session.commit()
        return jsonify({"success": True, "message": "Phone number updated!"})

    return jsonify({"success": False, "message": "Invalid profile action."}), 400


# ==========================================
# ADMIN ROUTES (Secret 6-Digit PIN Login)
# ==========================================

@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    data = request.get_json() or {}
    pin = str(data.get("pin") or "").strip()
    settings = get_settings()
    if pin == settings.admin_pin:
        session["is_admin"] = True
        session.permanent = True
        return jsonify({"success": True, "message": "Admin authenticated."})
    return jsonify({"success": False, "message": "Invalid 6-digit security PIN."}), 401

@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return jsonify({"success": True})

@app.route("/api/admin/overview", methods=["GET"])
@admin_required
def admin_overview():
    settings = get_settings()
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # Financials: money paid by users today & total sum
    paid_types = ["deposit", "share_purchase", "device_sale"]
    all_paid_txs = Transaction.query.filter(Transaction.tx_type.in_(paid_types), Transaction.status == "success").all()
    total_revenue = sum(t.amount for t in all_paid_txs)
    today_revenue = sum(
        t.amount for t in all_paid_txs
        if (t.created_at.replace(tzinfo=timezone.utc) if t.created_at.tzinfo is None else t.created_at) >= today_start
    )

    users = User.query.order_by(User.id.desc()).all()
    users_data = []
    for u in users:
        vip_lvl, _, _ = get_user_vip_status(u.id)
        user_paid = sum(t.amount for t in all_paid_txs if t.user_id == u.id)
        users_data.append({
            "id": u.id,
            "full_name": u.full_name,
            "phone": u.phone,
            "product_wallet": round(u.product_wallet, 2),
            "earnings_wallet": round(u.earnings_wallet, 2),
            "total_registered_amount": round(user_paid, 2),
            "vip_level": vip_lvl,
            "is_active": u.is_active,
            "joined": u.created_at.strftime("%Y-%m-%d")
        })

    plans = VipPlan.query.order_by(VipPlan.price.asc()).all()
    plans_data = [{
        "id": p.id,
        "name": p.name,
        "price": p.price,
        "daily_profit": p.daily_profit,
        "duration_days": p.duration_days,
        "is_vip2_tier": p.is_vip2_tier,
        "is_active": p.is_active
    } for p in plans]

    devices = SpinDevice.query.order_by(SpinDevice.id.desc()).all()
    devices_data = [{
        "id": d.id,
        "name": d.name,
        "specs": d.specs,
        "image_data": d.image_data,
        "spin_type": d.spin_type,
        "retail_price": d.retail_price,
        "discount_options": d.discount_options
    } for d in devices]

    deliveries = SpinWinDelivery.query.order_by(SpinWinDelivery.id.desc()).all()
    deliveries_data = [{
        "id": o.id,
        "user_name": o.user_name,
        "device_name": o.device_name,
        "spin_type": o.spin_type,
        "amount_paid": o.amount_paid,
        "paystack_ref": o.paystack_ref,
        "contact_phone": o.contact_phone,
        "region": o.region,
        "address": o.address,
        "status": o.status,
        "date": o.created_at.strftime("%Y-%m-%d %H:%M")
    } for o in deliveries]

    withdrawals = Transaction.query.filter_by(tx_type="withdrawal").order_by(Transaction.id.desc()).limit(50).all()
    withdrawal_list = []
    for w in withdrawals:
        u = db.session.get(User, w.user_id)
        withdrawal_list.append({
            "id": w.id,
            "user_name": u.full_name if u else "User",
            "phone": u.phone if u else "",
            "amount": w.amount,
            "details": w.details,
            "status": w.status,
            "date": w.created_at.strftime("%Y-%m-%d %H:%M")
        })

    # Group chat threads by user
    threads = []
    for u in users:
        msgs = ChatMessage.query.filter_by(user_id=u.id).order_by(ChatMessage.id.asc()).all()
        if msgs:
            unread = sum(1 for m in msgs if m.sender == "user" and not m.is_read)
            threads.append({
                "user_id": u.id,
                "user_name": u.full_name,
                "phone": u.phone,
                "unread": unread,
                "messages": [{
                    "id": m.id,
                    "sender": m.sender,
                    "message": m.message,
                    "time": m.created_at.strftime("%H:%M")
                } for m in msgs]
            })

    return jsonify({
        "success": True,
        "stats": {
            "total_users": len(users),
            "today_revenue": round(today_revenue, 2),
            "total_revenue": round(total_revenue, 2),
            "pending_deliveries": sum(1 for d in deliveries if d.status != "Delivered")
        },
        "settings": {
            "min_withdrawal": settings.min_withdrawal,
            "spin_difficulty": settings.spin_difficulty,
            "latest_news": settings.latest_news,
            "paystack_public_key": settings.paystack_public_key,
            "paystack_secret_key": settings.paystack_secret_key
        },
        "users": users_data,
        "plans": plans_data,
        "devices": devices_data,
        "deliveries": deliveries_data,
        "withdrawals": withdrawal_list,
        "chat_threads": threads
    })

@app.route("/api/admin/user-control", methods=["POST"])
@admin_required
def admin_user_control():
    data = request.get_json() or {}
    user = db.session.get(User, data.get("user_id"))
    if not user:
        return jsonify({"success": False, "message": "User not found."}), 404

    action = data.get("action")
    if action == "toggle_status":
        user.is_active = not user.is_active
        db.session.commit()
        return jsonify({"success": True, "message": f"User {'activated' if user.is_active else 'deactivated'}."})
    elif action == "adjust_wallet":
        wallet_type = data.get("wallet_type")  # 'product' or 'earnings'
        amount = float(data.get("amount", 0))
        if wallet_type == "product":
            user.product_wallet = max(0.0, amount)
        else:
            user.earnings_wallet = max(0.0, amount)
        db.session.commit()
        return jsonify({"success": True, "message": "User wallet updated."})

    return jsonify({"success": False, "message": "Invalid action."}), 400

@app.route("/api/admin/vip-plans", methods=["POST"])
@admin_required
def admin_vip_plans():
    data = request.get_json() or {}
    action = data.get("action")

    if action == "add":
        plan = VipPlan(
            name=data.get("name", "VIP Custom"),
            price=float(data.get("price", 300)),
            daily_profit=float(data.get("daily_profit", 50)),
            duration_days=int(data.get("duration_days", 15)),
            is_vip2_tier=bool(data.get("is_vip2_tier", False)),
            is_active=True
        )
        db.session.add(plan)
        db.session.commit()
        return jsonify({"success": True, "message": f"{plan.name} added!"})

    elif action == "update":
        plan = db.session.get(VipPlan, data.get("plan_id"))
        if not plan:
            return jsonify({"success": False, "message": "Plan not found."}), 404
        plan.price = float(data.get("price", plan.price))
        plan.daily_profit = float(data.get("daily_profit", plan.daily_profit))
        db.session.commit()
        return jsonify({"success": True, "message": f"{plan.name} updated!"})

    elif action == "remove":
        plan = db.session.get(VipPlan, data.get("plan_id"))
        if plan:
            plan.is_active = False
            db.session.commit()
        return jsonify({"success": True, "message": "VIP tier removed."})

    elif action == "reset_defaults":
        VipPlan.query.delete()
        db.session.add_all([
            VipPlan(name="VIP1", price=300.0, daily_profit=50.0, duration_days=15, is_vip2_tier=False),
            VipPlan(name="VIP2", price=600.0, daily_profit=110.0, duration_days=15, is_vip2_tier=True),
            VipPlan(name="VIP3", price=1500.0, daily_profit=300.0, duration_days=15, is_vip2_tier=False),
        ])
        db.session.commit()
        return jsonify({"success": True, "message": "VIP tiers reset to default values."})

    return jsonify({"success": False, "message": "Invalid VIP action."}), 400

@app.route("/api/admin/devices", methods=["POST"])
@admin_required
def admin_manage_devices():
    # Supports multipart/form-data for image upload OR JSON
    if request.content_type and "multipart/form-data" in request.content_type:
        name = request.form.get("name", "").strip()
        specs = request.form.get("specs", "").strip()
        spin_type = request.form.get("spin_type", "free")
        retail_price = float(request.form.get("retail_price") or 0)
        discount_options = request.form.get("discount_options", "500,800,1200,1500,2000,2500").strip()
        file = request.files.get("image")

        if not name or not specs or not file:
            return jsonify({"success": False, "message": "Device name, specs, and image are required."}), 400

        raw_bytes = file.read()
        mime = file.mimetype or "image/png"
        b64_str = base64.b64encode(raw_bytes).decode("utf-8")
        image_data = f"data:{mime};base64,{b64_str}"

        device = SpinDevice(
            name=name,
            specs=specs,
            image_data=image_data,
            spin_type=spin_type,
            retail_price=retail_price,
            discount_options=discount_options
        )
        db.session.add(device)
        db.session.commit()
        return jsonify({"success": True, "message": f"{name} uploaded to Spin Wheel!"})

    data = request.get_json() or {}
    action = data.get("action")
    if action == "remove":
        dev = db.session.get(SpinDevice, data.get("device_id"))
        if dev:
            db.session.delete(dev)
            db.session.commit()
        return jsonify({"success": True, "message": "Device removed."})
    elif action == "update_options":
        dev = db.session.get(SpinDevice, data.get("device_id"))
        if dev:
            dev.discount_options = data.get("discount_options", dev.discount_options)
            dev.retail_price = float(data.get("retail_price", dev.retail_price))
            db.session.commit()
        return jsonify({"success": True, "message": "Device spin pricing updated."})

    return jsonify({"success": False, "message": "Invalid device command."}), 400

@app.route("/api/admin/settings", methods=["POST"])
@admin_required
def admin_update_settings():
    data = request.get_json() or {}
    settings = get_settings()

    if "min_withdrawal" in data:
        settings.min_withdrawal = float(data["min_withdrawal"])
    if "spin_difficulty" in data and data["spin_difficulty"] in ["hard", "random"]:
        settings.spin_difficulty = data["spin_difficulty"]
    if "latest_news" in data:
        settings.latest_news = data["latest_news"]
    if "paystack_public_key" in data:
        settings.paystack_public_key = data["paystack_public_key"].strip()
    if "paystack_secret_key" in data:
        settings.paystack_secret_key = data["paystack_secret_key"].strip()
    if "old_pin" in data and "new_pin" in data:
        if str(data["old_pin"]) != settings.admin_pin:
            return jsonify({"success": False, "message": "Old 6-digit PIN is incorrect."}), 400
        if len(str(data["new_pin"])) != 6 or not str(data["new_pin"]).isdigit():
            return jsonify({"success": False, "message": "New PIN must be exactly 6 digits."}), 400
        settings.admin_pin = str(data["new_pin"])

    db.session.commit()
    return jsonify({"success": True, "message": "System settings updated!"})

@app.route("/api/admin/order-status", methods=["POST"])
@admin_required
def admin_order_status():
    data = request.get_json() or {}
    kind = data.get("kind")  # 'delivery' or 'withdrawal'
    item_id = data.get("id")
    status = data.get("status")

    if kind == "delivery":
        order = db.session.get(SpinWinDelivery, item_id)
        if order:
            order.status = status
            db.session.commit()
    elif kind == "withdrawal":
        tx = db.session.get(Transaction, item_id)
        if tx:
            tx.status = status
            db.session.commit()
    return jsonify({"success": True, "message": "Status updated."})


# ==========================================
# REAL-TIME WEBSOCKET LIVE CHAT (Instant)
# ==========================================

@socketio.on("join_chat")
def handle_join_chat(data):
    role = data.get("role")
    if role == "admin" and session.get("is_admin"):
        join_room("admin_room")
    elif role == "user" and session.get("user_id"):
        join_room(f"user_{session['user_id']}")

@socketio.on("send_chat_message")
def handle_chat_message(data):
    sender = data.get("sender")
    text = (data.get("message") or "").strip()
    if not text:
        return

    now_str = datetime.now(timezone.utc).strftime("%H:%M")

    if sender == "user":
        uid = session.get("user_id")
        if not uid:
            return
        user = db.session.get(User, uid)
        msg = ChatMessage(user_id=uid, sender="user", message=text, is_read=False)
        db.session.add(msg)
        db.session.commit()

        payload = {
            "id": msg.id,
            "user_id": uid,
            "user_name": user.full_name if user else "Client",
            "phone": user.phone if user else "",
            "sender": "user",
            "message": text,
            "time": now_str
        }
        emit("new_chat_message", payload, room=f"user_{uid}")
        emit("new_chat_message", payload, room="admin_room")

    elif sender == "admin":
        if not session.get("is_admin"):
            return
        target_uid = int(data.get("user_id", 0))
        if not target_uid:
            return
        ChatMessage.query.filter_by(user_id=target_uid, sender="user", is_read=False).update({"is_read": True})
        msg = ChatMessage(user_id=target_uid, sender="admin", message=text, is_read=True)
        db.session.add(msg)
        db.session.commit()

        payload = {
            "id": msg.id,
            "user_id": target_uid,
            "sender": "admin",
            "message": text,
            "time": now_str
        }
        emit("new_chat_message", payload, room=f"user_{target_uid}")
        emit("new_chat_message", payload, room="admin_room")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)

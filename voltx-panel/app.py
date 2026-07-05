import os, json, secrets, hashlib
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import pytz

load_dotenv()

from models import db, Admin, BotConfig, Range, PhoneNumber, User, OTPLog, RangeStats

app = Flask(__name__)
app.config['SECRET_KEY']       = os.getenv('SECRET_KEY', 'voltx-secret-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///voltx.db').replace('postgres://', 'postgresql://')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

@login_manager.user_loader
def load_user(user_id):
    return Admin.query.get(int(user_id))

# ─────────────────────────────────────────────
# INIT DB
# ─────────────────────────────────────────────
def init_db():
    with app.app_context():
        db.create_all()
        # Default admin
        if not Admin.query.first():
            admin = Admin(
                username=os.getenv('ADMIN_USERNAME', 'admin'),
                password=generate_password_hash(os.getenv('ADMIN_PASSWORD', 'admin123'))
            )
            db.session.add(admin)
        # Default bot config
        if not BotConfig.query.first():
            cfg = BotConfig(
                bot_token=os.getenv('BOT_TOKEN', ''),
                chat_id=os.getenv('ADMIN_CHAT_ID', ''),
                ivasms_email=os.getenv('IVASMS_EMAIL', ''),
                ivasms_password=os.getenv('IVASMS_PASSWORD', ''),
            )
            db.session.add(cfg)
        db.session.commit()

# ─────────────────────────────────────────────
# ADMIN AUTH
# ─────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        admin = Admin.query.filter_by(username=request.form['username']).first()
        if admin and check_password_hash(admin.password, request.form['password']):
            login_user(admin)
            return redirect(url_for('admin_dashboard'))
        flash('Invalid credentials', 'error')
    return render_template('admin/login.html')

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))

# ─────────────────────────────────────────────
# ADMIN DASHBOARD
# ─────────────────────────────────────────────
@app.route('/admin')
@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    cfg      = BotConfig.query.first()
    ranges   = Range.query.order_by(Range.otp_count.desc()).all()
    otp_logs = OTPLog.query.order_by(OTPLog.received_at.desc()).limit(20).all()
    users    = User.query.all()
    stats = {
        'total_ranges': Range.query.count(),
        'active_ranges': Range.query.filter_by(is_active=True).count(),
        'total_otps': OTPLog.query.count(),
        'total_users': User.query.count(),
        'active_numbers': PhoneNumber.query.filter_by(is_used=True).count(),
    }
    return render_template('admin/dashboard.html', cfg=cfg, ranges=ranges,
                           otp_logs=otp_logs, users=users, stats=stats)

# ─────────────────────────────────────────────
# BOT CONFIG
# ─────────────────────────────────────────────
@app.route('/admin/bot-config', methods=['GET', 'POST'])
@login_required
def admin_bot_config():
    cfg = BotConfig.query.first()
    if request.method == 'POST':
        cfg.bot_token        = request.form.get('bot_token', cfg.bot_token)
        cfg.chat_id          = request.form.get('chat_id', cfg.chat_id)
        cfg.ivasms_email     = request.form.get('ivasms_email', cfg.ivasms_email)
        cfg.ivasms_password  = request.form.get('ivasms_password', cfg.ivasms_password)
        db.session.commit()
        flash('Bot config saved!', 'success')
        return redirect(url_for('admin_bot_config'))
    return render_template('admin/bot_config.html', cfg=cfg)

@app.route('/admin/bot/toggle', methods=['POST'])
@login_required
def admin_bot_toggle():
    cfg = BotConfig.query.first()
    cfg.is_running = not cfg.is_running
    db.session.commit()
    status = 'started' if cfg.is_running else 'stopped'
    return jsonify({'status': status, 'is_running': cfg.is_running})

# ─────────────────────────────────────────────
# COOKIES UPDATE (from Chrome extension or manual)
# ─────────────────────────────────────────────
@app.route('/admin/update-cookies', methods=['GET', 'POST'])
@login_required
def admin_update_cookies():
    cfg = BotConfig.query.first()
    if request.method == 'POST':
        cookies_raw = request.form.get('cookies_json', '').strip()
        csrf        = request.form.get('csrf_token_val', '').strip()
        try:
            cookies = json.loads(cookies_raw)
            cfg.ivasms_session  = json.dumps(cookies)
            cfg.csrf_token      = csrf
            cfg.session_updated = datetime.utcnow()
            db.session.commit()
            # Test session
            from ivasms import IVASMSClient
            client = IVASMSClient(cookies, csrf)
            valid = client.test_session()
            if valid:
                flash('✅ Cookies updated and session is valid!', 'success')
            else:
                flash('⚠️ Cookies saved but session test failed — may be expired.', 'warning')
        except json.JSONDecodeError:
            flash('❌ Invalid JSON format', 'error')
        return redirect(url_for('admin_update_cookies'))
    return render_template('admin/update_cookies.html', cfg=cfg)

# API endpoint for Chrome extension to POST cookies directly
@app.route('/api/update-cookies', methods=['POST'])
def api_update_cookies():
    data = request.get_json()
    if not data or 'cookies' not in data:
        return jsonify({'error': 'No cookies provided'}), 400
    api_key = request.headers.get('X-API-Key', '')
    expected = os.getenv('EXTENSION_API_KEY', 'voltx-extension-key')
    if api_key != expected:
        return jsonify({'error': 'Unauthorized'}), 401
    cfg = BotConfig.query.first()
    if cfg:
        cfg.ivasms_session  = json.dumps(data['cookies'])
        cfg.session_updated = datetime.utcnow()
        db.session.commit()
    return jsonify({'status': 'ok', 'count': len(data['cookies'])})

# ─────────────────────────────────────────────
# RANGE MANAGEMENT
# ─────────────────────────────────────────────
@app.route('/admin/ranges')
@login_required
def admin_ranges():
    ranges = Range.query.order_by(Range.otp_count.desc()).all()
    return render_template('admin/ranges.html', ranges=ranges)

@app.route('/admin/ranges/sync', methods=['POST'])
@login_required
def admin_sync_ranges():
    cfg = BotConfig.query.first()
    if not cfg or not cfg.ivasms_session:
        return jsonify({'error': 'No session — update cookies first'}), 400
    try:
        from ivasms import IVASMSClient
        cookies = json.loads(cfg.ivasms_session)
        client  = IVASMSClient(cookies, cfg.csrf_token)
        numbers = client.fetch_all_numbers()

        synced = 0
        for item in numbers:
            rng = Range.query.filter_by(range_value=item['range']).first()
            if not rng:
                rng = Range(range_value=item['range'], is_active=True)
                db.session.add(rng)
                db.session.flush()
                synced += 1
            # Add phone number if not exists
            existing = PhoneNumber.query.filter_by(number=item['number'], range_id=rng.id).first()
            if not existing:
                pn = PhoneNumber(number=item['number'], range_id=rng.id)
                db.session.add(pn)

        db.session.commit()
        return jsonify({'status': 'ok', 'new_ranges': synced, 'total_numbers': len(numbers)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/admin/ranges/<int:range_id>/toggle', methods=['POST'])
@login_required
def admin_toggle_range(range_id):
    rng = Range.query.get_or_404(range_id)
    rng.is_active = not rng.is_active
    db.session.commit()
    return jsonify({'status': 'ok', 'is_active': rng.is_active})

@app.route('/admin/ranges/suggested')
@login_required
def admin_suggested_ranges():
    suggested = Range.query.filter_by(is_suggested=True, is_active=True)\
                     .order_by(Range.success_rate.desc()).all()
    all_ranges = Range.query.order_by(Range.otp_count.desc()).all()
    return render_template('admin/suggested_ranges.html', suggested=suggested, all_ranges=all_ranges)

# ─────────────────────────────────────────────
# USER MANAGEMENT (Admin)
# ─────────────────────────────────────────────
@app.route('/admin/users')
@login_required
def admin_users():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template('admin/users.html', users=users)

@app.route('/admin/users/add', methods=['POST'])
@login_required
def admin_add_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username or not password:
        flash('Username and password required', 'error')
        return redirect(url_for('admin_users'))
    if User.query.filter_by(username=username).first():
        flash('Username already exists', 'error')
        return redirect(url_for('admin_users'))
    user = User(
        username=username,
        password=generate_password_hash(password),
        api_key=secrets.token_hex(32)
    )
    db.session.add(user)
    db.session.commit()
    flash(f'User {username} created!', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/users/<int:user_id>/toggle', methods=['POST'])
@login_required
def admin_toggle_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = not user.is_active
    db.session.commit()
    return jsonify({'status': 'ok', 'is_active': user.is_active})

# ─────────────────────────────────────────────
# OTP LOGS
# ─────────────────────────────────────────────
@app.route('/admin/otp-logs')
@login_required
def admin_otp_logs():
    page = request.args.get('page', 1, type=int)
    logs = OTPLog.query.order_by(OTPLog.received_at.desc()).paginate(page=page, per_page=50)
    return render_template('admin/otp_logs.html', logs=logs)

# ─────────────────────────────────────────────
# USER PANEL
# ─────────────────────────────────────────────
@app.route('/')
def index():
    return redirect(url_for('user_login'))

@app.route('/login', methods=['GET', 'POST'])
def user_login():
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form['username']).first()
        if user and user.is_active and check_password_hash(user.password, request.form['password']):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('user_dashboard'))
        flash('Invalid credentials or account disabled', 'error')
    return render_template('user/login.html')

@app.route('/logout')
def user_logout():
    session.clear()
    return redirect(url_for('user_login'))

def user_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('user_login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/dashboard')
@user_required
def user_dashboard():
    user = User.query.get(session['user_id'])
    my_numbers = PhoneNumber.query.filter_by(assigned_to=user.id, is_used=True)\
                   .order_by(PhoneNumber.assigned_at.desc()).all()
    suggested  = Range.query.filter_by(is_suggested=True, is_active=True)\
                   .order_by(Range.success_rate.desc()).limit(5).all()
    recent_otps = OTPLog.query.filter_by(user_id=user.id)\
                    .order_by(OTPLog.received_at.desc()).limit(10).all()
    return render_template('user/dashboard.html', user=user,
                           my_numbers=my_numbers, suggested=suggested,
                           recent_otps=recent_otps)

@app.route('/get-number', methods=['GET', 'POST'])
@user_required
def user_get_number():
    user = User.query.get(session['user_id'])
    suggested = Range.query.filter_by(is_suggested=True, is_active=True)\
                  .order_by(Range.success_rate.desc()).all()
    all_ranges = Range.query.filter_by(is_active=True)\
                   .order_by(Range.otp_count.desc()).all()

    if request.method == 'POST':
        range_id = request.form.get('range_id', type=int)
        rng = Range.query.get(range_id)
        if not rng or not rng.is_active:
            flash('Invalid or inactive range', 'error')
            return redirect(url_for('user_get_number'))

        # Get an unused number from this range
        phone = PhoneNumber.query.filter_by(
            range_id=rng.id, is_used=False, assigned_to=None
        ).first()

        if not phone:
            flash('No available numbers in this range right now. Try another range.', 'warning')
            return redirect(url_for('user_get_number'))

        now = datetime.utcnow()
        phone.is_used      = True
        phone.assigned_to  = user.id
        phone.assigned_at  = now
        phone.expires_at   = now + timedelta(hours=2)
        db.session.commit()

        flash(f'✅ Number assigned: {phone.number}', 'success')
        return redirect(url_for('user_view_number', number_id=phone.id))

    return render_template('user/get_number.html', user=user,
                           suggested=suggested, all_ranges=all_ranges)

@app.route('/number/<int:number_id>')
@user_required
def user_view_number(number_id):
    user   = User.query.get(session['user_id'])
    phone  = PhoneNumber.query.filter_by(id=number_id, assigned_to=user.id).first_or_404()
    return render_template('user/view_number.html', user=user, phone=phone)

@app.route('/api/check-otp/<int:number_id>')
@user_required
def api_check_otp(number_id):
    """Polling endpoint — user page calls this every 5s to check for OTP."""
    user  = User.query.get(session['user_id'])
    phone = PhoneNumber.query.filter_by(id=number_id, assigned_to=user.id).first()
    if not phone:
        return jsonify({'error': 'Not found'}), 404

    # Check if OTP arrived
    if phone.otp_received:
        return jsonify({
            'status': 'received',
            'otp': phone.otp_received,
            'message': phone.otp_message,
            'number': phone.number
        })

    # Check expiry
    if phone.expires_at and datetime.utcnow() > phone.expires_at:
        return jsonify({'status': 'expired'})

    return jsonify({'status': 'waiting'})

# ─────────────────────────────────────────────
# API for external use (with api_key)
# ─────────────────────────────────────────────
@app.route('/api/v1/get-number', methods=['POST'])
def api_get_number():
    api_key  = request.headers.get('X-API-Key', '')
    user     = User.query.filter_by(api_key=api_key, is_active=True).first()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    data     = request.get_json() or {}
    range_id = data.get('range_id')
    if range_id:
        rng = Range.query.get(range_id)
    else:
        rng = Range.query.filter_by(is_suggested=True, is_active=True)\
               .order_by(Range.success_rate.desc()).first()

    if not rng:
        return jsonify({'error': 'No available range'}), 404

    phone = PhoneNumber.query.filter_by(range_id=rng.id, is_used=False).first()
    if not phone:
        return jsonify({'error': 'No numbers available'}), 404

    now = datetime.utcnow()
    phone.is_used     = True
    phone.assigned_to = user.id
    phone.assigned_at = now
    phone.expires_at  = now + timedelta(hours=2)
    db.session.commit()

    return jsonify({
        'status': 'ok',
        'number_id': phone.id,
        'number': phone.number,
        'range': rng.range_value,
        'expires_at': phone.expires_at.isoformat()
    })

@app.route('/api/v1/check-otp/<int:number_id>', methods=['GET'])
def api_check_otp_external(number_id):
    api_key = request.headers.get('X-API-Key', '')
    user    = User.query.filter_by(api_key=api_key, is_active=True).first()
    if not user:
        return jsonify({'error': 'Unauthorized'}), 401

    phone = PhoneNumber.query.filter_by(id=number_id, assigned_to=user.id).first()
    if not phone:
        return jsonify({'error': 'Not found'}), 404

    if phone.otp_received:
        return jsonify({'status': 'received', 'otp': phone.otp_received,
                        'message': phone.otp_message})
    if phone.expires_at and datetime.utcnow() > phone.expires_at:
        return jsonify({'status': 'expired'})
    return jsonify({'status': 'waiting'})

# ─────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    from scheduler import start_scheduler
    start_scheduler(app)
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False)

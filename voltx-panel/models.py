from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime
import pytz

db = SQLAlchemy()

def dhaka_now():
    return datetime.now(pytz.timezone('Asia/Dhaka')).replace(tzinfo=None)


class Admin(db.Model, UserMixin):
    """Admin panel users"""
    __tablename__ = 'admins'
    id       = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=dhaka_now)


class BotConfig(db.Model):
    """Global bot settings (single row)"""
    __tablename__ = 'bot_config'
    id          = db.Column(db.Integer, primary_key=True)
    bot_token   = db.Column(db.String(200), nullable=False, default='')
    chat_id     = db.Column(db.String(100), nullable=False, default='')
    is_running  = db.Column(db.Boolean, default=False)
    ivasms_email    = db.Column(db.String(200), default='')
    ivasms_password = db.Column(db.String(200), default='')
    ivasms_session  = db.Column(db.Text, default='')   # JSON cookies
    csrf_token      = db.Column(db.String(500), default='')
    session_updated = db.Column(db.DateTime, nullable=True)
    updated_at  = db.Column(db.DateTime, default=dhaka_now, onupdate=dhaka_now)


class Range(db.Model):
    """Phone number ranges from ivasms.com"""
    __tablename__ = 'ranges'
    id          = db.Column(db.Integer, primary_key=True)
    range_value = db.Column(db.String(100), unique=True, nullable=False)
    is_active   = db.Column(db.Boolean, default=True)
    is_suggested= db.Column(db.Boolean, default=False)  # smart suggestion flag
    otp_count   = db.Column(db.Integer, default=0)      # total OTPs received
    otp_last_hour = db.Column(db.Integer, default=0)    # OTPs in last hour
    success_rate  = db.Column(db.Float, default=0.0)    # % of numbers that got OTP
    last_checked  = db.Column(db.DateTime, nullable=True)
    created_at    = db.Column(db.DateTime, default=dhaka_now)
    numbers       = db.relationship('PhoneNumber', backref='range', lazy=True, cascade='all, delete-orphan')


class PhoneNumber(db.Model):
    """Phone numbers fetched from ivasms.com"""
    __tablename__ = 'phone_numbers'
    id          = db.Column(db.Integer, primary_key=True)
    number      = db.Column(db.String(30), nullable=False)
    id_number   = db.Column(db.String(50), default='')
    range_id    = db.Column(db.Integer, db.ForeignKey('ranges.id'), nullable=False)
    is_used     = db.Column(db.Boolean, default=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    assigned_at = db.Column(db.DateTime, nullable=True)
    expires_at  = db.Column(db.DateTime, nullable=True)   # 2 hours after assigned
    otp_received= db.Column(db.String(20), default='')
    otp_message = db.Column(db.Text, default='')
    created_at  = db.Column(db.DateTime, default=dhaka_now)


class User(db.Model):
    """End users who request phone numbers"""
    __tablename__ = 'users'
    id          = db.Column(db.Integer, primary_key=True)
    username    = db.Column(db.String(80), unique=True, nullable=False)
    password    = db.Column(db.String(200), nullable=False)
    api_key     = db.Column(db.String(64), unique=True, nullable=True)
    balance     = db.Column(db.Float, default=0.0)
    is_active   = db.Column(db.Boolean, default=True)
    created_at  = db.Column(db.DateTime, default=dhaka_now)
    numbers     = db.relationship('PhoneNumber', backref='user', lazy=True)


class OTPLog(db.Model):
    """Log of all OTPs received"""
    __tablename__ = 'otp_logs'
    id          = db.Column(db.Integer, primary_key=True)
    number      = db.Column(db.String(30), nullable=False)
    range_value = db.Column(db.String(100), default='')
    service     = db.Column(db.String(100), default='Unknown')
    otp_code    = db.Column(db.String(20), default='')
    message     = db.Column(db.Text, default='')
    user_id     = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    received_at = db.Column(db.DateTime, default=dhaka_now)


class RangeStats(db.Model):
    """Hourly stats per range for smart range selection"""
    __tablename__ = 'range_stats'
    id          = db.Column(db.Integer, primary_key=True)
    range_value = db.Column(db.String(100), nullable=False)
    hour        = db.Column(db.DateTime, nullable=False)   # truncated to hour
    otp_count   = db.Column(db.Integer, default=0)
    number_count= db.Column(db.Integer, default=0)
    success_rate= db.Column(db.Float, default=0.0)

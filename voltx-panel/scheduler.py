"""
Background scheduler - runs every 20 minutes to check ranges and SMS.
Also handles auto-delete of expired numbers (2 hours).
"""
import json
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler(timezone='Asia/Dhaka')


def get_client(app):
    """Build IVASMSClient from DB config."""
    from ivasms import IVASMSClient
    from models import BotConfig
    with app.app_context():
        cfg = BotConfig.query.first()
        if not cfg or not cfg.ivasms_session:
            return None, None
        try:
            cookies = json.loads(cfg.ivasms_session)
        except Exception:
            return None, None
        return IVASMSClient(cookies, cfg.csrf_token), cfg


def job_check_sms(app):
    """Every 20 min: check all active ranges for new SMS/OTP."""
    from models import db, Range, PhoneNumber, OTPLog, BotConfig, RangeStats
    import pytz, hashlib

    with app.app_context():
        cfg = BotConfig.query.first()
        if not cfg or not cfg.is_running:
            return

        client, _ = get_client(app)
        if not client:
            logger.warning("SMS check skipped — no valid session")
            return

        if not client.test_session():
            logger.warning("Session expired — need new cookies")
            _notify_telegram(cfg, "⚠️ IVASMS session expired! Please update cookies in the panel.")
            return

        dhaka = pytz.timezone('Asia/Dhaka')
        now = datetime.now(dhaka).replace(tzinfo=None)
        seen_ids = set()

        active_ranges = Range.query.filter_by(is_active=True).all()
        for rng in active_ranges:
            numbers = client.fetch_number_list_for_range(rng.range_value)
            if not numbers:
                continue

            rng.last_checked = now
            new_otps = 0

            for num_info in numbers:
                number    = num_info['number']
                id_number = num_info['id_number']
                sms_list  = client.fetch_sms_for_number(number, rng.range_value, id_number)

                for sms in sms_list:
                    if sms['sms_id'] in seen_ids:
                        continue
                    seen_ids.add(sms['sms_id'])

                    if not sms['otp_code']:
                        continue

                    # Check if already logged
                    existing = OTPLog.query.filter_by(
                        number=number, otp_code=sms['otp_code']
                    ).first()
                    if existing:
                        continue

                    # Save OTP log
                    log = OTPLog(
                        number=number,
                        range_value=rng.range_value,
                        service=sms['service'],
                        otp_code=sms['otp_code'],
                        message=sms['text'],
                        received_at=now
                    )
                    db.session.add(log)

                    # Update phone number record if assigned
                    phone = PhoneNumber.query.filter_by(number=number, is_used=True).first()
                    if phone:
                        phone.otp_received = sms['otp_code']
                        phone.otp_message  = sms['text']

                    # Notify Telegram
                    _notify_telegram(cfg,
                        f"🔐 *OTP Received*\n"
                        f"📱 Number: `{number}`\n"
                        f"🌍 Range: `{rng.range_value}`\n"
                        f"⚙️ Service: {sms['service']}\n"
                        f"🔑 OTP: `{sms['otp_code']}`\n"
                        f"📜 Message:\n```{sms['text']}```"
                    )

                    new_otps += 1
                    rng.otp_count += 1

            # Update range stats
            rng.otp_last_hour = new_otps
            if new_otps > 0:
                _update_range_stats(app, rng.range_value, new_otps, len(numbers), now)

        db.session.commit()
        logger.info(f"SMS check done — checked {len(active_ranges)} ranges")


def job_auto_delete_numbers(app):
    """Every 10 min: delete numbers that have been assigned for 2+ hours."""
    from models import db, PhoneNumber

    with app.app_context():
        now = datetime.utcnow()
        expired = PhoneNumber.query.filter(
            PhoneNumber.is_used == True,
            PhoneNumber.expires_at != None,
            PhoneNumber.expires_at <= now
        ).all()

        for phone in expired:
            db.session.delete(phone)

        if expired:
            db.session.commit()
            logger.info(f"Auto-deleted {len(expired)} expired numbers")


def job_update_smart_ranges(app):
    """Every hour: recalculate which ranges are best and mark as suggested."""
    from models import db, Range, RangeStats

    with app.app_context():
        # Last 3 hours stats
        cutoff = datetime.utcnow() - timedelta(hours=3)
        ranges = Range.query.filter_by(is_active=True).all()

        scores = []
        for rng in ranges:
            stats = RangeStats.query.filter(
                RangeStats.range_value == rng.range_value,
                RangeStats.hour >= cutoff
            ).all()
            total_otp = sum(s.otp_count for s in stats)
            total_num = sum(s.number_count for s in stats) or 1
            score = total_otp / total_num
            scores.append((rng, score, total_otp))

        # Sort by score descending
        scores.sort(key=lambda x: x[1], reverse=True)

        # Top 3 ranges = suggested
        for i, (rng, score, total_otp) in enumerate(scores):
            rng.is_suggested = (i < 3 and total_otp > 0)
            rng.success_rate = round(score * 100, 2)

        db.session.commit()
        top = [(r.range_value, s) for r, s, _ in scores[:3]]
        logger.info(f"Smart ranges updated. Top: {top}")


def _update_range_stats(app, range_value, otp_count, number_count, now):
    from models import db, RangeStats
    hour = now.replace(minute=0, second=0, microsecond=0)
    stat = RangeStats.query.filter_by(range_value=range_value, hour=hour).first()
    if stat:
        stat.otp_count    += otp_count
        stat.number_count  = max(stat.number_count, number_count)
    else:
        stat = RangeStats(range_value=range_value, hour=hour,
                          otp_count=otp_count, number_count=number_count)
        db.session.add(stat)
    if stat.number_count:
        stat.success_rate = round(stat.otp_count / stat.number_count * 100, 2)


def _notify_telegram(cfg, message):
    if not cfg.bot_token or not cfg.chat_id:
        return
    try:
        import requests as req
        req.post(
            f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage",
            json={"chat_id": cfg.chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5
        )
    except Exception as e:
        logger.error(f"Telegram notify error: {e}")


def start_scheduler(app):
    if scheduler.running:
        return
    scheduler.add_job(
        func=job_check_sms,
        trigger=IntervalTrigger(minutes=20),
        args=[app],
        id='check_sms',
        replace_existing=True
    )
    scheduler.add_job(
        func=job_auto_delete_numbers,
        trigger=IntervalTrigger(minutes=10),
        args=[app],
        id='auto_delete',
        replace_existing=True
    )
    scheduler.add_job(
        func=job_update_smart_ranges,
        trigger=IntervalTrigger(hours=1),
        args=[app],
        id='smart_ranges',
        replace_existing=True
    )
    scheduler.start()
    logger.info("Scheduler started")

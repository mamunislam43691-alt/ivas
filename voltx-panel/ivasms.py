"""
IVASMS.com API wrapper - handles login, number fetching, SMS fetching.
Uses stored cookies (from Chrome extension or manual entry).
"""
import requests
import json
import re
import hashlib
import logging
from bs4 import BeautifulSoup
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

LOGIN_URL       = "https://www.ivasms.com/login"
SMS_LIST_URL    = "https://www.ivasms.com/portal/sms/received/getsms/number"
SMS_DETAILS_URL = "https://www.ivasms.com/portal/sms/received/getsms/number/sms"
NUMBERS_URL     = "https://www.ivasms.com/portal/numbers"
RETURN_ALL_URL  = "https://www.ivasms.com/portal/numbers/return/allnumber/bluck"

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


class IVASMSClient:
    def __init__(self, cookies: dict, csrf_token: str = ""):
        self.session = requests.Session()
        self.session.headers.update(BASE_HEADERS)
        if cookies:
            self.session.cookies.update(cookies)
        self.csrf_token = csrf_token

    def test_session(self) -> bool:
        """Check if session cookies are still valid."""
        try:
            r = self.session.get(
                "https://www.ivasms.com/portal/sms/received",
                timeout=10,
                allow_redirects=True
            )
            return r.status_code == 200 and "login" not in r.url
        except Exception as e:
            logger.error(f"Session test failed: {e}")
            return False

    def get_csrf_from_page(self, url: str) -> str:
        try:
            r = self.session.get(url, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            token = soup.find('input', {'name': '_token'})
            if token:
                return token['value']
            meta = soup.find('meta', {'name': 'csrf-token'})
            if meta:
                return meta.get('content', '')
        except Exception as e:
            logger.error(f"Error getting CSRF: {e}")
        return self.csrf_token

    def fetch_all_numbers(self) -> list:
        """Fetch all numbers and ranges from portal."""
        results = []
        start = 0
        page_size = 1000
        headers = {
            **BASE_HEADERS,
            "x-csrf-token": self.csrf_token,
            "Accept": "application/json",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
        }
        while True:
            params = {
                "draw": 2,
                "columns[1][data]": "Number",
                "columns[2][data]": "range",
                "order[0][column]": 1,
                "order[0][dir]": "desc",
                "start": start,
                "length": page_size,
                "search[value]": "",
                "_": int(datetime.now().timestamp() * 1000)
            }
            try:
                r = self.session.get(NUMBERS_URL, headers=headers, params=params, timeout=15)
                if r.status_code in [401, 403]:
                    logger.error("Session expired fetching numbers")
                    return results
                data = r.json()
                records = data.get('data', [])
                for rec in records:
                    range_val = str(rec.get('range', '')).strip()
                    number    = str(rec.get('Number', '')).strip()
                    if range_val and number:
                        results.append({'range': range_val, 'number': number})
                if len(records) < page_size or start + page_size >= data.get('recordsTotal', 0):
                    break
                start += page_size
            except Exception as e:
                logger.error(f"Error fetching numbers page {start}: {e}")
                break
        return results

    def fetch_sms_for_number(self, number: str, range_value: str, id_number: str) -> list:
        """Fetch SMS messages for a specific number. Returns list of OTP dicts."""
        headers = {
            **BASE_HEADERS,
            "x-csrf-token": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.ivasms.com/portal/sms/received",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        payload = {
            "_token": self.csrf_token,
            "Number": str(number),
            "Range": str(range_value),
            "id_number": id_number
        }
        try:
            r = self.session.post(SMS_DETAILS_URL, headers=headers, data=payload, timeout=10)
            if r.status_code in [401, 403]:
                return []
            soup = BeautifulSoup(r.text, 'html.parser')
            cards = soup.find_all("div", class_="card card-body border-bottom bg-soft-dark p-2 rounded-0")
            results = []
            for card in cards:
                p = card.find("p")
                text = p.get_text(strip=True) if p else card.get_text(strip=True)
                if not text:
                    continue
                otp_match = re.search(r'\b(\d{4,8})\b', text)
                otp_code = otp_match.group(1) if otp_match else ''
                service = 'WhatsApp' if 'WhatsApp' in text else \
                          'Facebook' if ('Facebook' in text or 'FB-' in text) else \
                          'Telegram' if 'Telegram' in text else 'Unknown'
                sms_id = hashlib.sha256(f"{number}{text}".encode()).hexdigest()
                results.append({
                    'sms_id': sms_id,
                    'number': number,
                    'range': range_value,
                    'text': text,
                    'otp_code': otp_code,
                    'service': service,
                })
            return results
        except Exception as e:
            logger.error(f"Error fetching SMS for {number}: {e}")
            return []

    def fetch_number_list_for_range(self, range_value: str) -> list:
        """Fetch list of numbers for a range."""
        headers = {
            **BASE_HEADERS,
            "x-csrf-token": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.ivasms.com/portal/sms/received",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        payload = {
            "_token": self.csrf_token,
            "start": (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),
            "end": datetime.now().strftime("%Y-%m-%d"),
            "range": range_value,
            "draw": 1,
            "length": 100
        }
        try:
            r = self.session.post(SMS_LIST_URL, headers=headers, data=payload, timeout=10)
            soup = BeautifulSoup(r.text, 'html.parser')
            cards = soup.find_all("div", class_="card card-body border-bottom bg-100 p-2 rounded-0")
            numbers = []
            for card in cards:
                div = card.find("div", class_="col-sm-4")
                if div:
                    number = div.get_text(strip=True)
                    onclick = div.get("onclick", "")
                    m = re.search(r"'(\d+)','(\d+)'", onclick)
                    id_number = m.group(2) if m else ""
                    numbers.append({"number": number, "id_number": id_number})
            return numbers
        except Exception as e:
            logger.error(f"Error fetching number list for {range_value}: {e}")
            return []

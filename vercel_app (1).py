# app.py — Drop-in replacement for IVAS SMS API on Vercel
# Auto-login using IVAS_EMAIL + IVAS_PASSWORD env vars. No cookies needed.
# Set these two env vars on Vercel and redeploy.
#
# Original scraping logic by @Arslan-MD
# Auto-login update by IVAS OTP Panel

from flask import Flask, request, jsonify
from datetime import datetime
import cloudscraper
import json
from bs4 import BeautifulSoup
import logging
import os
import gzip
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


class IVASSMSClient:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
        self.base_url = "https://www.ivasms.com"
        self.logged_in = False
        self.csrf_token = None

    def decompress_response(self, response):
        encoding = response.headers.get("Content-Encoding", "").lower()
        content = response.content
        try:
            if encoding == "gzip":
                content = gzip.decompress(content)
            elif encoding == "br" and HAS_BROTLI:
                content = brotli.decompress(content)
            return content.decode("utf-8", errors="replace")
        except Exception as e:
            logger.error(f"Decompress error: {e}")
            return response.text

    def login_with_credentials(self, email=None, password=None):
        """Auto-login using credentials. Accepts direct args or falls back to env vars."""
        email = email or os.environ.get("IVAS_EMAIL", "")
        password = password or os.environ.get("IVAS_PASSWORD", "")

        if not email or not password:
            logger.error("No IVAS credentials available (env vars or request headers)")
            return False

        try:
            # Step 1: Get login page and CSRF token
            logger.debug("Fetching login page...")
            r = self.scraper.get(f"{self.base_url}/login", timeout=20)
            html = self.decompress_response(r)
            soup = BeautifulSoup(html, "html.parser")
            token_input = soup.find("input", {"name": "_token"})
            if not token_input:
                logger.error(f"No CSRF token on login page (status {r.status_code})")
                return False
            token = token_input.get("value", "")

            # Step 2: Submit credentials
            logger.debug("Submitting credentials...")
            r2 = self.scraper.post(
                f"{self.base_url}/login",
                data={"_token": token, "email": email, "password": password},
                headers={"Referer": f"{self.base_url}/login"},
                timeout=20,
                allow_redirects=True,
            )
            logger.debug(f"Login response: status={r2.status_code} url={r2.url}")

            # Step 3: Load portal to get CSRF token for data requests
            logger.debug("Loading SMS portal...")
            r3 = self.scraper.get(f"{self.base_url}/portal/sms/received", timeout=20)
            html3 = self.decompress_response(r3)
            soup3 = BeautifulSoup(html3, "html.parser")
            csrf_input = soup3.find("input", {"name": "_token"})
            if not csrf_input:
                logger.error("No CSRF token on portal page — login may have failed")
                return False

            self.csrf_token = csrf_input.get("value", "")
            self.logged_in = True
            logger.debug("Logged in successfully with credentials!")
            return True

        except Exception as e:
            logger.exception(f"Login exception: {e}")
            return False

    def login_with_cookies(self, cookies_file="cookies.json"):
        """Fallback: load cookies from file or COOKIES_JSON env var."""
        try:
            if os.getenv("COOKIES_JSON"):
                cookies_raw = json.loads(os.getenv("COOKIES_JSON"))
            else:
                with open(cookies_file, "r") as f:
                    cookies_raw = json.load(f)

            if isinstance(cookies_raw, dict):
                cookies = cookies_raw
            elif isinstance(cookies_raw, list):
                cookies = {c["name"]: c["value"] for c in cookies_raw if "name" in c and "value" in c}
            else:
                return False

            for name, value in cookies.items():
                self.scraper.cookies.set(name, value, domain="www.ivasms.com")

            response = self.scraper.get(f"{self.base_url}/portal/sms/received", timeout=10)
            if response.status_code == 200:
                html = self.decompress_response(response)
                soup = BeautifulSoup(html, "html.parser")
                csrf_input = soup.find("input", {"name": "_token"})
                if csrf_input:
                    self.csrf_token = csrf_input.get("value")
                    self.logged_in = True
                    logger.debug("Logged in with cookies successfully")
                    return True
            return False
        except Exception as e:
            logger.error(f"Cookie login error: {e}")
            return False

    def set_credentials(self, email, password):
        """Store credentials on this instance so retries can reuse them."""
        self._email = email
        self._password = password

    def ensure_logged_in(self):
        if self.logged_in and self.csrf_token:
            return True
        email = getattr(self, "_email", None) or os.environ.get("IVAS_EMAIL", "")
        password = getattr(self, "_password", None) or os.environ.get("IVAS_PASSWORD", "")
        if email and password:
            return self.login_with_credentials(email=email, password=password)
        return self.login_with_cookies()

    def check_otps(self, from_date="", to_date=""):
        if not self.ensure_logged_in():
            return None

        try:
            payload = {"from": from_date, "to": to_date, "_token": self.csrf_token}
            headers = {
                "Accept": "text/html, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/portal/sms/received",
            }
            response = self.scraper.post(
                f"{self.base_url}/portal/sms/received/getsms",
                data=payload, headers=headers, timeout=15
            )

            if response.status_code in (401, 419):
                logger.warning("Session expired, re-logging...")
                self.logged_in = False
                self.csrf_token = None
                if not self.ensure_logged_in():
                    return None
                return self.check_otps(from_date, to_date)

            if response.status_code != 200:
                logger.error(f"getsms failed: {response.status_code}")
                return None

            html = self.decompress_response(response)
            soup = BeautifulSoup(html, "html.parser")

            def txt(sel):
                el = soup.select_one(sel)
                return el.text.strip() if el else "0"

            sms_details = []
            for item in soup.select("div.item"):
                col = item.select_one(".col-sm-4")
                if col:
                    country_number = col.text.strip()
                    if country_number:
                        sms_details.append({"country_number": country_number})

            return {
                "count_sms": txt("#CountSMS"),
                "paid_sms": txt("#PaidSMS"),
                "unpaid_sms": txt("#UnpaidSMS"),
                "revenue": txt("#RevenueSMS").replace(" USD", ""),
                "sms_details": sms_details,
            }
        except Exception as e:
            logger.exception(f"check_otps error: {e}")
            return None

    def get_sms_details(self, phone_range, from_date="", to_date=""):
        if not self.ensure_logged_in():
            return None
        try:
            payload = {"_token": self.csrf_token, "start": from_date, "end": to_date, "range": phone_range}
            headers = {
                "Accept": "text/html, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/portal/sms/received",
            }
            response = self.scraper.post(
                f"{self.base_url}/portal/sms/received/getsms/number",
                data=payload, headers=headers, timeout=15
            )
            if response.status_code != 200:
                return None

            html = self.decompress_response(response)
            soup = BeautifulSoup(html, "html.parser")
            number_details = []
            for card in soup.select("div.card.card-body"):
                col = card.select_one(".col-sm-4")
                if col:
                    phone_number = col.text.strip()
                    onclick = col.get("onclick", "")
                    id_number = onclick.split("'")[3] if onclick and len(onclick.split("'")) > 3 else ""
                    if phone_number:
                        number_details.append({
                            "phone_number": phone_number,
                            "id_number": id_number,
                        })
            return number_details
        except Exception as e:
            logger.exception(f"get_sms_details error: {e}")
            return None

    def get_otp_message(self, phone_number, phone_range, from_date="", to_date=""):
        if not self.ensure_logged_in():
            return None
        try:
            payload = {
                "_token": self.csrf_token,
                "start": from_date,
                "end": to_date,
                "Number": phone_number,
                "Range": phone_range,
            }
            headers = {
                "Accept": "text/html, */*; q=0.01",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Origin": self.base_url,
                "Referer": f"{self.base_url}/portal/sms/received",
            }
            response = self.scraper.post(
                f"{self.base_url}/portal/sms/received/getsms/number/sms",
                data=payload, headers=headers, timeout=15
            )
            if response.status_code != 200:
                return None
            html = self.decompress_response(response)
            soup = BeautifulSoup(html, "html.parser")
            msg = soup.select_one(".col-9.col-sm-6 p")
            return msg.text.strip() if msg else None
        except Exception as e:
            logger.exception(f"get_otp_message error: {e}")
            return None

    def get_all_otp_messages(self, sms_details, from_date="", to_date="", limit=None):
        all_otp = []
        for detail in sms_details:
            phone_range = detail["country_number"]
            number_details = self.get_sms_details(phone_range, from_date, to_date)
            if number_details:
                for nd in number_details:
                    if limit is not None and len(all_otp) >= limit:
                        return all_otp
                    otp = self.get_otp_message(nd["phone_number"], phone_range, from_date, to_date)
                    if otp:
                        all_otp.append({
                            "range": phone_range,
                            "phone_number": nd["phone_number"],
                            "otp_message": otp,
                        })
        return all_otp


app = Flask(__name__)
client = IVASSMSClient()

with app.app_context():
    if not client.ensure_logged_in():
        logger.error("Startup login failed — will retry on first request")


@app.route("/")
def welcome():
    return jsonify({
        "message": "Welcome to the IVAS SMS API",
        "status": "API is alive",
        "auth": "credentials" if (os.environ.get("IVAS_EMAIL") and os.environ.get("IVAS_PASSWORD")) else "cookies",
        "endpoints": {
            "/sms": "Get OTP messages for a specific date (DD/MM/YYYY). Example: /sms?date=01/05/2025&limit=10"
        },
    })


@app.route("/sms")
def get_sms():
    # Accept credentials from request headers (forwarded by the proxy) or env vars
    hdr_email = request.headers.get("X-IVAS-Email", "")
    hdr_password = request.headers.get("X-IVAS-Password", "")
    if hdr_email and hdr_password:
        if hdr_email != getattr(client, "_email", None) or hdr_password != getattr(client, "_password", None):
            # Credentials changed — force re-login
            client.logged_in = False
            client.csrf_token = None
        client.set_credentials(hdr_email, hdr_password)

    date_str = request.args.get("date")
    if not date_str:
        return jsonify({"error": "date parameter required in DD/MM/YYYY format"}), 400
    try:
        datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return jsonify({"error": "Invalid date format. Use DD/MM/YYYY"}), 400

    from_date = date_str
    to_date = request.args.get("to_date", "")
    if to_date:
        try:
            datetime.strptime(to_date, "%d/%m/%Y")
        except ValueError:
            return jsonify({"error": "Invalid to_date format. Use DD/MM/YYYY"}), 400

    limit_raw = request.args.get("limit")
    limit = None
    if limit_raw:
        try:
            limit = int(limit_raw)
            if limit <= 0:
                return jsonify({"error": "limit must be a positive integer"}), 400
        except ValueError:
            return jsonify({"error": "limit must be a valid integer"}), 400

    result = client.check_otps(from_date=from_date, to_date=to_date)
    if not result:
        return jsonify({"error": "Failed to fetch data — check IVAS_EMAIL/IVAS_PASSWORD env vars on Vercel, or ensure proxy forwards X-IVAS-Email/X-IVAS-Password headers"}), 401

    otp_messages = client.get_all_otp_messages(
        result.get("sms_details", []), from_date=from_date, to_date=to_date, limit=limit
    )

    return jsonify({
        "status": "success",
        "from_date": from_date,
        "to_date": to_date or "Not specified",
        "limit": limit if limit is not None else "Not specified",
        "sms_stats": {
            "count_sms": result["count_sms"],
            "paid_sms": result["paid_sms"],
            "unpaid_sms": result["unpaid_sms"],
            "revenue": result["revenue"],
        },
        "otp_messages": otp_messages,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

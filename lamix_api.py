import re
import json
import time
import logging
import requests
from config import PANEL_URL, PANEL_USERNAME, PANEL_PASSWORD

logger = logging.getLogger(__name__)

_session = None
_last_login = 0
_last_login_attempt = 0
SESSION_REFRESH_INTERVAL = 25 * 60  # re-login every 25 minutes proactively
MIN_LOGIN_GAP = 3  # seconds between consecutive login attempts


def _login(retries: int = 2) -> requests.Session:
    """Login with username/password + auto-solved math captcha."""
    global _last_login_attempt

    for attempt in range(retries):
        # Avoid hammering the login endpoint too quickly
        wait = MIN_LOGIN_GAP - (time.time() - _last_login_attempt)
        if wait > 0:
            time.sleep(wait)
        _last_login_attempt = time.time()

        s = requests.Session()
        r = s.get(f"{PANEL_URL}/login", timeout=15)

        m = re.search(r'What is (\d+)\s*\+\s*(\d+)\s*=', r.text)
        if not m:
            logger.error("Captcha pattern not found on login page (attempt %s)", attempt + 1)
            continue

        a, b = int(m.group(1)), int(m.group(2))
        answer = a + b

        payload = {
            'username': PANEL_USERNAME,
            'password': PANEL_PASSWORD,
            'capt':     str(answer),
        }

        r2 = s.post(f"{PANEL_URL}/signin", data=payload, timeout=15, allow_redirects=True)

        if "SMSDashboard" in r2.url or "Dashboard" in r2.text or "SMS Bulk" in r2.text:
            logger.info("✅ Auto-login successful")
            return s
        else:
            logger.warning("Login attempt %s failed. URL: %s", attempt + 1, r2.url)

    raise RuntimeError("Login failed after retries")


def get_session() -> requests.Session:
    """Return a valid session, logging in if needed or stale."""
    global _session, _last_login
    now = time.time()

    if _session is None or (now - _last_login) > SESSION_REFRESH_INTERVAL:
        try:
            new_session = _login()
            _session = new_session
            _last_login = now
        except Exception as e:
            logger.error("Login error: %s", e)
            if _session is None:
                raise

    return _session


def refresh_session():
    """Force a fresh login on next get_session() call."""
    global _session, _last_login
    _session = None
    _last_login = 0


def _ensure_logged_in(response: requests.Response) -> bool:
    """Check if response indicates session expired (redirected to login)."""
    if "login" in response.url.lower() and "What is" in response.text:
        return False
    return True


# ── Verify client ─────────────────────────────────────────────────────────────
def verify_client(username: str) -> dict | None:
    try:
        s = get_session()
        r = s.get(f"{PANEL_URL}/agent/SMSBulkAllocations", timeout=15)

        if not _ensure_logged_in(r):
            refresh_session()
            s = get_session()
            r = s.get(f"{PANEL_URL}/agent/SMSBulkAllocations", timeout=15)

        pattern = rf'<option[^>]*value=["\'](\d+)["\'][^>]*>\s*{re.escape(username)}\s*</option>'
        match = re.search(pattern, r.text, re.IGNORECASE)
        if match:
            return {"username": username, "client_id": match.group(1)}
        all_opts = re.findall(r'<option[^>]*value=["\'](\d+)["\'][^>]*>\s*([^<]+?)\s*</option>', r.text, re.I)
        for cid, cname in all_opts:
            if cname.strip().lower() == username.lower():
                return {"username": cname.strip(), "client_id": cid}
        return None
    except Exception as e:
        logger.error("verify_client: %s", e)
        return None


# ── Allocate ──────────────────────────────────────────────────────────────────
def allocate_numbers(client_id: str, range_id: str, qty: int, payterm: str = "3") -> dict:
    try:
        s = get_session()
        payload = {
            'action':   'allocate',
            'ntype':    '-2',
            'range[]':  range_id,
            'client[]': client_id,
            'payterm':  payterm,
            'payout':   '0',
            'qty':      str(qty),
        }

        from datetime import datetime, timedelta
        before_time = (datetime.now() - timedelta(seconds=5)).strftime('%Y-%m-%d %H:%M:%S')

        r = s.post(
            f"{PANEL_URL}/agent/SMSBulkAllocations",
            data=payload,
            headers={'Referer': f"{PANEL_URL}/agent/SMSBulkAllocations"},
            timeout=30,
        )

        if not ("Well Done" in r.text or "Allocated" in r.text):
            # First attempt failed (likely stale session) — refresh and retry once.
            logger.info("First allocation attempt had no success marker, refreshing session and retrying")
            refresh_session()
            s = get_session()
            r = s.post(
                f"{PANEL_URL}/agent/SMSBulkAllocations",
                data=payload,
                headers={'Referer': f"{PANEL_URL}/agent/SMSBulkAllocations"},
                timeout=30,
            )

        if "Well Done" in r.text or "Allocated" in r.text:
            cid_m = re.search(r'Client Id\s*-\s*(\d+)', r.text)
            cid   = cid_m.group(1) if cid_m else client_id

            time.sleep(2)

            cname_m = re.search(
                rf'<option[^>]*value=["\']{re.escape(client_id)}["\'][^>]*>\s*([^<]+?)\s*</option>',
                r.text, re.I
            )
            cname = cname_m.group(1).strip() if cname_m else None

            ealid = _find_matching_ealid(cname, qty, after_time=before_time)
            return {"success": True, "ealid": ealid, "client_id": cid}

        logger.warning("Alloc failed: %s", r.text[400:700])
        return {"success": False, "ealid": None, "client_id": None, "raw": r.text}

    except Exception as e:
        logger.error("allocate_numbers: %s", e)
        return {"success": False, "ealid": None, "client_id": None, "raw": ""}


# ── Find matching ealid ────────────────────────────────────────────────────────
def _find_matching_ealid(cname: str, qty: int, after_time: str = None) -> str | None:
    """
    Find the ealid of the most recent allocation row matching client name + qty.
    If after_time (datetime string 'YYYY-MM-DD HH:MM:SS') is given, only rows
    with a timestamp >= after_time are considered, to avoid matching stale
    older rows with the same client+qty combination.
    """
    try:
        s = get_session()
        r = s.get(
            f"{PANEL_URL}/agent/res/data_smsbulkallocations.php",
            headers={'Referer': f"{PANEL_URL}/agent/SMSBulkAllocations",
                     'X-Requested-With': 'XMLHttpRequest'},
            params={"sEcho":"1","iColumns":"5","iDisplayStart":"0","iDisplayLength":"100"},
            timeout=15,
        )
        data = json.loads(r.text)
        rows = data.get("aaData", [])
        # Server returns ascending (oldest first) regardless of sort params —
        # reverse so newest allocations are checked first.
        rows = list(reversed(rows))

        candidates = []
        for row in rows:
            row_time   = str(row[0]).strip()
            row_client = str(row[1]).strip()
            row_qty    = str(row[2]).strip()

            if not (cname and row_client.lower() == cname.lower() and row_qty == str(qty)):
                continue

            if after_time and row_time < after_time:
                continue

            m = re.search(r'ealid=([A-Za-z0-9+/=]+)', str(row[4]))
            if m:
                candidates.append((row_time, m.group(1)))

        if candidates:
            # Most recent (rows already sorted desc, but be safe)
            candidates.sort(key=lambda x: x[0], reverse=True)
            return candidates[0][1]

        # Fallback: ignore after_time, just match client+qty most recent
        if after_time:
            return _find_matching_ealid(cname, qty, after_time=None)

        # Last resort: match client name only, most recent
        for row in rows:
            row_client = str(row[1]).strip()
            if cname and row_client.lower() == cname.lower():
                m = re.search(r'ealid=([A-Za-z0-9+/=]+)', str(row[4]))
                if m:
                    return m.group(1)

        return None
    except Exception as e:
        logger.error("_find_matching_ealid: %s", e)
        return None


# ── Download numbers ──────────────────────────────────────────────────────────
def download_numbers(ealid: str) -> list[str]:
    try:
        s = get_session()
        r = s.get(
            f"{PANEL_URL}/agent/res/exportsmsbulktxt",
            params={"ealid": ealid},
            headers={'Referer': f"{PANEL_URL}/agent/SMSBulkAllocations"},
            timeout=30,
        )
        if r.status_code == 200:
            return [l.strip() for l in r.text.splitlines() if l.strip()]
        return []
    except Exception as e:
        logger.error("download_numbers: %s", e)
        return []


# ── Request more numbers for an empty range ───────────────────────────────────
def request_numbers_for_range(range_id: str, qty: int = 100) -> bool:
    """
    Call requestsmsnumberfinal.php to pull more numbers into this range
    from the upstream pool. Tries multiple payterms since ranges support
    different payment terms. Returns True if successful.
    """
    try:
        s = get_session()
    except Exception as e:
        logger.error("request_numbers_for_range: could not get session: %s", e)
        return False

    for payterm in ["1", "2", "3", "4", "5", "6", "7", "8", "9"]:
        try:
            r = s.post(
                f"{PANEL_URL}/agent/res/requestsmsnumberfinal.php",
                data={'rid': range_id, 'payterm': payterm, 'qty': str(qty)},
                headers={'Referer': f"{PANEL_URL}/agent/SMSRanges",
                         'X-Requested-With': 'XMLHttpRequest'},
                timeout=60,
            )
            text_lower = r.text.lower()
            if "successfully" in text_lower or "allocated" in text_lower:
                logger.info("Requested %s numbers for range %s (payterm=%s)", qty, range_id, payterm)
                return True
            if "gone wrong" in text_lower or "contact sales" in text_lower:
                logger.info("payterm=%s not valid for range %s, trying next", payterm, range_id)
                continue
            logger.warning("Unexpected response for range %s payterm %s: %s", range_id, payterm, r.text[:200])
        except Exception as e:
            logger.error("request_numbers_for_range payterm=%s: %s", payterm, e)

    logger.warning("All payterms failed for range %s", range_id)
    return False

"""Small helpers shared across the screener: errors, hashing, safe HTTP, file output, Telegram.
Originally from the market-pivot-watch pivot_watch package; copied here so the screener stands alone."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


class DataError(ValueError):
    """Data cannot support a verified signal."""


def fingerprint(config):
    # Any data/strategy configuration change rebaselines rather than creating a cross.
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def json_text(value):
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DataError("Unexpected provider redirect; request stopped")


def get_json(url, params=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "market-screener/1.0", "Accept": "application/json", **(headers or {})})
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            # Never include a response body, account URL, bearer token or raw exception.
            raise DataError(f"Provider HTTP {exc.code}; check entitlement, region and rate limits") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise DataError("Provider connection unavailable after 3 attempts") from None
        except (json.JSONDecodeError, UnicodeError):
            raise DataError("Provider returned invalid JSON") from None


def send(token, chat, text, photo):
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
        raise DataError('Invalid Telegram bot token format')
    boundary = uuid.uuid4().hex
    if photo and len(text) > 1024:
        # Telegram photo captions are hard-capped at 1024 chars.
        text = text[:1000].rstrip() + '\u2026'
    fields = {'chat_id': chat, 'caption' if photo else 'text': text}
    body = b''.join((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode()
                    for key, value in fields.items())
    if photo:
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="chart.png"\r\n'
                 'Content-Type: image/png\r\n\r\n').encode() + photo + b'\r\n'
    body += f'--{boundary}--\r\n'.encode()
    method = 'sendPhoto' if photo else 'sendMessage'
    request = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}', data=body,
                                     headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                result = json.load(response)
                if not isinstance(result, dict) or result.get('ok') is not True:
                    raise DataError('Telegram rejected the message')
                return
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read().decode('utf-8', 'replace')[:200]
            except Exception:
                pass
            last = DataError(f'Telegram HTTP {exc.code}: {detail}')
            if exc.code in (400, 429) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise last from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if isinstance(exc, DataError):
                raise
            # No retry: a timed-out POST may already have delivered.
            raise DataError('Telegram delivery could not be confirmed') from None
    raise last

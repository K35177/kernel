"""Telegram WebApp HMAC validation, independent of HTTP and frontend SDKs."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramIdentity:
    id: int
    username: str | None
    display_name: str
    fingerprint: str
    proof_expires_at: float


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def validate_init_data(raw: str, token: str, max_age=300, *, now=None) -> TelegramIdentity:
    now = time.time() if now is None else now
    try:
        if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
            raise ValueError
        if re.search(r'%(?![0-9a-fA-F]{2})', raw):
            raise ValueError
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          encoding='utf-8', errors='strict', max_num_fields=32)
        values = dict(pairs)
        if len(values) != len(pairs) or any(not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', k) for k in values):
            raise ValueError
        received = values.pop('hash')
        if not re.fullmatch(r'[a-fA-F0-9]{64}', received):
            raise ValueError
        check = '\n'.join(f'{key}={values[key]}' for key in sorted(values))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, bytes.fromhex(received)):
            raise ValueError
        if not re.fullmatch(r'[0-9]{1,12}', values['auth_date']):
            raise ValueError
        auth_date = int(values['auth_date'])
        if auth_date > now + 30 or now - auth_date > max_age:
            raise ValueError
        user = json.loads(values['user'], object_pairs_hook=_object)
        if (not isinstance(user, dict) or type(user.get('id')) is not int
                or not 0 < user['id'] < 2**52 or user.get('is_bot', False) is not False):
            raise ValueError
        first = user['first_name']
        last = user.get('last_name', '')
        username = user.get('username')
        if (not isinstance(first, str) or not first.strip() or len(first) > 128
                or not isinstance(last, str) or len(last) > 128
                or (username is not None and (not isinstance(username, str)
                    or not re.fullmatch(r'[a-zA-Z0-9_]{1,64}', username)))):
            raise ValueError
        return TelegramIdentity(user['id'], username, (first + ' ' + last).strip(),
                                hashlib.sha256(expected).hexdigest(), auth_date + max_age + 1)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AuthenticationError('Недействительные или просроченные Telegram initData.') from None

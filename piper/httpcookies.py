"""Host-owned HTTP cookie state; the native Biscuit DTO loses security attributes.

Only previously granted HTTP destinations may query/update this jar. Its state
never enters the project mirror or the child's filesystem. The caller holds the
existing physical-project lock for the entire run, including persistence.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import time
import urllib.parse
import zipfile
from datetime import datetime, timezone

import common

TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
MAX_COOKIES = 3000
MAX_STATE = 16 << 20


def host(value):
    """Canonical DNS/IP spelling without treating a sibling as a suffix match."""
    value = value.lower().rstrip('.')
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        value = value.encode('idna').decode('ascii')
        if len(value) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', x)
                                   for x in value.split('.')):
            raise ValueError('invalid cookie host')
        return value


def domain_match(name, domain):
    if name == domain:
        return True
    try:
        ipaddress.ip_address(name)
        return False
    except ValueError:
        return name.endswith('.' + domain)


def path_match(path, prefix):
    return path == prefix or path.startswith(prefix) and (prefix.endswith('/') or path[len(prefix):].startswith('/'))


class Suffixes:
    """Read the configured JDK's PSL, including private, wildcard and exception rules.

    No network refresh or script-supplied path. Without a usable database, only
    host-only cookies are accepted; missing PSL must not enable supercookies.
    """
    def __init__(self, java):
        self.rules = None
        path = os.path.join(os.path.dirname(os.path.dirname(java)), 'lib', 'security', 'public_suffix_list.dat')
        try:
            if os.path.getsize(path) > 4 << 20:
                return
            with zipfile.ZipFile(path) as source:
                if len(source.infolist()) > 4096 or sum(x.file_size for x in source.infolist()) > 4 << 20:
                    return
                rules = set()
                for entry in source.infolist():
                    for line in source.read(entry).decode('utf-8').splitlines():
                        if not line or line[0] not in ('\0', '\1'):
                            return
                        rule = line[1:]
                        prefix = '*' if rule.startswith('*.') else '!' if rule.startswith('!') else ''
                        name = rule[2:] if prefix == '*' else rule[1:] if prefix else rule
                        rules.add((prefix + '.' if prefix == '*' else prefix) + host(name))
            if len(rules) > 1000:
                self.rules = rules
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, UnicodeError):
            pass

    def public(self, name):
        if self.rules is None:
            return True
        labels = name.split('.')
        longest = 1  # PSL's implicit wildcard, also for private intranet TLDs.
        for i in range(len(labels)):
            suffix = '.'.join(labels[i:])
            if '!' + suffix in self.rules:
                return False
            if suffix in self.rules:
                longest = max(longest, len(labels) - i)
            if i and '*.' + suffix in self.rules:
                longest = max(longest, len(labels) - i + 1)
        return len(labels) == longest


def cookie_date(value):
    """RFC6265 date-token algorithm, including legacy two-digit years."""
    tokens = re.split(r'[\x09\x20-\x2f\x3b-\x40\x5b-\x60\x7b-\x7e]+', value)
    clock = day = month = year = None
    months = ('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec')
    for token in tokens:
        match = re.match(r'^(\d{1,2}):(\d{1,2}):(\d{1,2})(?:\D|$)', token)
        if clock is None and match:
            clock = tuple(map(int, match.groups()))
        elif day is None and re.match(r'^\d{1,2}(?:\D|$)', token):
            day = int(re.match(r'\d+', token)[0])
        elif month is None and token[:3].lower() in months:
            month = months.index(token[:3].lower()) + 1
        elif year is None and re.match(r'^\d{2,4}(?:\D|$)', token):
            year = int(re.match(r'\d+', token)[0])
    if year is None or clock is None or day is None or month is None:
        return None
    year += 1900 if 70 <= year <= 99 else 2000 if 0 <= year <= 69 else 0
    try:
        return datetime(year, month, day, *clock, tzinfo=timezone.utc).timestamp() if year >= 1601 else None
    except (ValueError, OverflowError):
        return None


def parse(header, url, received, suffixes):
    """Return an attribute-preserving record or ignore an invalid Set-Cookie."""
    if not isinstance(header, str) or len(header.encode('utf-8')) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in header):
        return None
    parts = header.split(';')
    name, sep, value = parts[0].strip().partition('=')
    name, value = name.strip(), value.strip()
    if not sep or not TOKEN.fullmatch(name):
        return None
    raw = value[1:-1] if value.startswith('"') and value.endswith('"') else value
    if any(c in '\\",; ' for c in raw):
        return None
    attrs = {}
    for part in parts[1:]:
        key, _, val = part.strip().partition('=')
        attrs[key.lower()] = val.strip()
    parsed = urllib.parse.urlsplit(url)
    origin = host(parsed.hostname or '')
    secure = 'secure' in attrs
    if secure and parsed.scheme != 'https':
        return None
    domain, host_only = origin, True
    if attrs.get('domain'):
        try:
            # A trailing dot is invalid here; accepting it changes cookie scope.
            if attrs['domain'].endswith('.'):
                return None
            domain = host(attrs['domain'].lstrip('.'))
        except (ValueError, UnicodeError):
            return None
        if not domain_match(origin, domain):
            return None
        if suffixes.public(domain):
            if domain != origin:
                return None
            domain, host_only = origin, True
        else:
            host_only = False
    path = attrs.get('path', '')
    if not path.startswith('/'):
        path = parsed.path.rpartition('/')[0] if parsed.path.startswith('/') else ''
        path = path or '/'
    if (name.startswith('__Secure-') and not secure or name.startswith('__Host-') and
            (not secure or 'domain' in attrs or attrs.get('path') != '/')):
        return None
    expires = cookie_date(attrs['expires']) if 'expires' in attrs else None
    age = attrs.get('max-age', '')
    if re.fullmatch(r'-?\d+', age):
        # Bound integer parsing and timestamps, without truncating the cookie value.
        number = int((age.lstrip('0') or '0')[:20]) if not age.startswith('-') else 0
        expires = 0 if not number else min(253402300799, received + number)
    return dict(name=name, value=value, domain=domain, host_only=host_only, path=path, secure=secure,
                http_only='httponly' in attrs, same_site=attrs.get('samesite', '').lower(), expires=expires)


class Jar:
    def __init__(self, directory, scope, suffixes, secrets):
        self.directory = directory
        self.filename = hashlib.sha256(scope.encode()).hexdigest() + '.json'
        self.suffixes, self.secrets = suffixes, secrets
        self.entries, self.allowed = [], set()
        try:
            data = common.safe_read(directory, self.filename, MAX_STATE + 1, single_link=True)
            if len(data) > MAX_STATE:
                raise ValueError('HTTP cookie state exceeds storage limit')
            records = json.loads(data)
        except FileNotFoundError:
            records = []
        if not isinstance(records, list) or len(records) > MAX_COOKIES:
            raise ValueError('invalid HTTP cookie state')
        for record in records:
            if (not isinstance(record, dict) or set(record) != {'header', 'url', 'received', 'order'} or
                    type(record['received']) not in (float, int) or not math.isfinite(record['received']) or
                    type(record['order']) is not int or not 0 <= record['order'] < 1 << 53 or
                    not isinstance(record['url'], str) or len(record['url']) > 8192):
                raise ValueError('invalid HTTP cookie state')
            self._receive(record['header'], record['url'], record['received'], record['order'])

    def _receive(self, header, url, received, order=None):
        self.entries = [e for e in self.entries if e['cookie']['expires'] is None or e['cookie']['expires'] > time.time()]
        item = parse(header, url, received, self.suffixes)
        if item is None:
            return
        self.secrets.append(item['value'])
        key = lambda entry: (entry['cookie']['name'], entry['cookie']['domain'], entry['cookie']['path'])
        identity = (item['name'], item['domain'], item['path'])
        # Insecure responses cannot replace/delete an overlapping Secure cookie.
        if urllib.parse.urlsplit(url).scheme != 'https' and any(
                e['cookie']['secure'] and e['cookie']['name'] == item['name'] and
                (domain_match(item['domain'], e['cookie']['domain']) or domain_match(e['cookie']['domain'], item['domain'])) and
                path_match(item['path'], e['cookie']['path']) for e in self.entries):
            return
        old = next((e for e in self.entries if key(e) == identity), None)
        order = old['order'] if old else order if order is not None else 1 + max((e['order'] for e in self.entries), default=-1)
        self.entries = [e for e in self.entries if key(e) != identity and
                        (e['cookie']['expires'] is None or e['cookie']['expires'] > time.time())]
        if item['expires'] is None or item['expires'] > time.time():
            if len(self.entries) >= MAX_COOKIES:
                # RFC6265 permits bounded storage; evict the oldest rather than stop requests.
                self.entries.remove(min(self.entries, key=lambda e: e['order']))
            self.entries.append(dict(header=header, url=url, received=received, order=order, cookie=item))

    def __call__(self, message, deadline):
        if time.monotonic() >= deadline:
            raise ValueError('HTTP cookie deadline')
        if (set(message) not in ({'kind', 'url'}, {'kind', 'url', 'headers'}) or
                not isinstance(message['url'], str) or len(message['url']) > 8192 or
                any(ord(c) < 32 for c in message['url'])):
            raise ValueError('invalid HTTP cookie message')
        parsed = urllib.parse.urlsplit(message['url'])
        origin = host(parsed.hostname or '')
        if parsed.scheme not in ('https', 'http') or parsed.username is not None or parsed.fragment or parsed.query:
            raise ValueError('HTTP cookie destination was not granted')
        if (origin, parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)) not in self.allowed:
            if message['kind'] == 'cookie_select' and set(message) == {'kind', 'url'}:
                # Reveal no credential. The existing transport broker reports the
                # refused destination as a failed request, preserving earlier results.
                return dict(cookies=[])
            raise ValueError('HTTP cookie destination was not granted')
        if message['kind'] == 'cookie_select' and set(message) == {'kind', 'url'}:
            matches = [e for e in self.entries if (e['cookie']['expires'] is None or e['cookie']['expires'] > time.time()) and
                       (origin == e['cookie']['domain'] if e['cookie']['host_only'] else domain_match(origin, e['cookie']['domain'])) and
                       (not e['cookie']['secure'] or parsed.scheme == 'https') and path_match(parsed.path or '/', e['cookie']['path'])]
            matches.sort(key=lambda e: (-len(e['cookie']['path']), e['order']))
            return dict(cookies=[{k:e['cookie'][k] for k in ('name', 'value', 'domain', 'path')} for e in matches])
        if (message['kind'] != 'cookie_save' or set(message) != {'kind', 'url', 'headers'} or
                not isinstance(message['headers'], list) or len(message['headers']) > 256 or
                any(not isinstance(x, str) for x in message['headers'])):
            raise ValueError('invalid HTTP cookie update')
        for header in message['headers']:
            self._receive(header, message['url'], time.time())
        if message['headers']:
            os.makedirs(self.directory, mode=0o700, exist_ok=True)
            records = [{k:v for k,v in e.items() if k != 'cookie'} for e in self.entries]
            # Ensure a valid response cannot create a state file too large to reopen.
            sizes = [(e, len(json.dumps(record, ensure_ascii=False, indent=2).encode()) + 64)
                     for e, record in zip(self.entries, records)]
            total, evicted = sum(size for _, size in sizes) + 4, set()
            for entry, size in sorted(sizes, key=lambda pair: pair[0]['order']):
                if total <= MAX_STATE:
                    break
                evicted.add(id(entry))
                total -= size
            if evicted:
                records = [record for e, record in zip(self.entries, records) if id(e) not in evicted]
                self.entries = [e for e in self.entries if id(e) not in evicted]
            common.write_json(os.path.join(self.directory, self.filename), records)
        return {}

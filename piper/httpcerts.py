"""Immutable client TLS material from a user-owned HTTP environment configuration.

No key bytes, passphrases or source paths are returned in an approval/result. The
native JVM can read only these per-run copies under its existing read-only jail.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess

import common

MAX_FILE = 1 << 20


def _openssl():
    # A project or native script must not turn PATH into a host command receiving
    # the passphrase. Use standard host installations, never inherited PATH/. .
    return shutil.which('openssl', path='/opt/homebrew/bin:/usr/local/bin:' + os.defpath)


def snapshot(root, env_dir, configuration, work):
    """Resolve IDEA certificate strings/objects relative to the private env source.

    Callers must establish that the configuration comes from a user-owned private
    env document. No path supplied by an HTTP script or runtime message reaches here.
    """
    certificate = configuration.get('clientCertificate')
    key = configuration.get('clientCertificateKey')
    if not certificate:
        if key:
            raise ValueError('clientCertificateKey requires clientCertificate; no request was sent')
        return None
    directory = os.path.join(work, 'tls')
    os.mkdir(directory, 0o700)
    config, fingerprints, secrets = {}, [], []
    for name, value, formats, extensions in (
            ('certificate', certificate, {'PEM', 'DER', 'P12'},
             {'.pem':'PEM', '.crt':'PEM', '.p12':'P12', '.pkcs12':'P12', '.pfx':'P12', '.der':'DER', '.cer':'DER'}),
            ('key', key, {'PEM', 'DER'}, {'.pem':'PEM', '.key':'PEM', '.der':'DER'})):
        if value is None:
            continue
        if isinstance(value, str):
            path, kind = value, None
        elif isinstance(value, dict) and not set(value) - {'path', 'format'}:
            path, kind = value.get('path'), value.get('format')
        else:
            raise ValueError('invalid client certificate configuration; no request was sent')
        if (not isinstance(path, str) or not path or len(path) > 4096 or '{{' in path or
                '\\' in path or any(ord(char) < 32 for char in path) or path.startswith('~')):
            raise ValueError('invalid client certificate path; no request was sent')
        if kind is None:
            kind = extensions.get(os.path.splitext(path)[1].lower())
        if not isinstance(kind, str) or kind not in formats:
            raise ValueError('unknown client certificate format; specify format in SSLConfiguration; no request was sent')
        source = os.path.normpath(path if os.path.isabs(path) else os.path.join(root, env_dir or '', path))
        try:
            data = common.safe_read('/', source.lstrip('/'), MAX_FILE + 1, single_link=True)
        except (OSError, ValueError):
            raise ValueError('cannot safely read configured client certificate material; no request was sent') from None
        if not data or len(data) > MAX_FILE:
            raise ValueError('client certificate material is empty or exceeds 1 MiB; no request was sent')
        destination = os.path.join(directory, name)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
        config[name] = dict(path=destination, format=kind)
        fingerprints.append(hashlib.sha256(data).hexdigest())
        secrets.append(destination)
        if kind == 'PEM':
            text = data.decode('ascii', 'strict')
            secrets.extend([text] + [line for line in text.splitlines() if len(line) >= 16 and not line.startswith('-----')])
    if config['certificate']['format'] != 'P12' and 'key' not in config:
        raise ValueError('PEM/DER client certificates require clientCertificateKey; no request was sent')
    with open(os.path.join(work, 'ssl-client.json'), 'x', encoding='utf-8') as output:
        json.dump(config, output)
    os.chmod(os.path.join(work, 'ssl-client.json'), 0o400)
    return dict(fingerprints=fingerprints, secrets=secrets,
                needs_passphrase=configuration.get('hasCertificatePassphrase') is True,
                entry=dict(file=certificate if isinstance(certificate, str) else certificate['path'],
                           bytes=len(common.safe_read(work, 'tls/certificate', MAX_FILE, single_link=True)), sha256=fingerprints[0]))


def unlock(work, passphrase, timeout=15):
    """Decrypt only the frozen configured key; never read an IDE/keychain store.

    OpenSSL receives the passphrase in its private short-lived environment, never
    argv, a file, the daemon environment or an error message. This also preserves
    passphrases containing newlines, which OpenSSL's stdin source would truncate.
    """
    config = json.loads(common.safe_read(work, 'ssl-client.json', 16384, single_link=True))
    executable = _openssl()
    if not executable:
        raise ValueError('OpenSSL is required to load the encrypted client certificate; no request was sent')
    if not isinstance(passphrase, str) or not passphrase or '\0' in passphrase or len(passphrase.encode('utf-8')) > 8192:
        raise ValueError('invalid client certificate passphrase; no request was sent')
    item = config['certificate'] if config['certificate']['format'] == 'P12' else config.get('key')
    if item is None:
        raise ValueError('encrypted client identity has no private key; no request was sent')
    args = ([executable, 'pkcs12', '-in', item['path'], '-nodes', '-passin', 'env:PIPER_HTTP_CERT_PASSPHRASE']
            if item['format'] == 'P12' else [executable, 'pkey', '-in', item['path'], '-inform', item['format'],
                                            '-passin', 'env:PIPER_HTTP_CERT_PASSPHRASE'])
    environment = dict(PATH=os.defpath, LC_ALL='C', PIPER_HTTP_CERT_PASSPHRASE=passphrase)
    try:
        result = subprocess.run(args, env=environment, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('encrypted client identity could not be loaded; no request was sent') from None
    finally:
        environment.clear()
        passphrase = None
    if result.returncode or not result.stdout or len(result.stdout) > 2 * MAX_FILE:
        raise ValueError('the certificate passphrase is wrong or the encrypted identity is invalid; no request was sent')
    material = {'key': result.stdout}
    if item['format'] == 'P12':
        # Native PEM certificate readers require certificate blocks alone. OpenSSL
        # PKCS12 output can put the key first and includes bag attributes; never
        # hand that mixed stream to the vendor's CertificateFactory.
        keys = re.findall(rb'-----BEGIN PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]+-----END PRIVATE KEY-----', result.stdout)
        certs = re.findall(rb'-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----', result.stdout)
        if len(keys) != 1 or not certs:
            raise ValueError('encrypted client identity must contain one private key and its certificates; no request was sent')
        material = dict(key=keys[0] + b'\n', certificate=b'\n'.join(certs) + b'\n')
    secrets = []
    for name, data in material.items():
        target = os.path.join(work, 'tls', 'unlocked.pem' if name == 'key' else 'unlocked-certificate.pem')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
        config[name] = dict(path=target, format='PEM')
        text = data.decode('ascii', 'strict')
        secrets += [target, text] + [line for line in text.splitlines() if len(line) >= 16 and not line.startswith('-----')]
    temporary = os.path.join(work, 'ssl-client-unlocked.json')
    with open(temporary, 'x') as output:
        json.dump(config, output)
    os.chmod(temporary, 0o400)
    os.replace(temporary, os.path.join(work, 'ssl-client.json'))
    return secrets


def configure_context(context, work):
    """Load only the already frozen identity into the host token transport.

    OpenSSL only converts local immutable bytes. Its stdout contains key material
    and must never be logged; diagnostics are deliberately replaced below.
    """
    config = json.loads(common.safe_read(work, 'ssl-client.json', 16384, single_link=True))
    converted = {}
    try:
        for name, item in config.items():
            source = item['path']
            kind = item['format']
            if kind == 'PEM':
                converted[name] = source
                continue
            executable = _openssl()
            if not executable:
                raise ValueError('OpenSSL is required for this client identity')
            args = ([executable, 'pkcs12', '-in', source, '-nodes', '-passin', 'pass:'] if kind == 'P12' else
                    [executable, 'x509' if name == 'certificate' else 'pkey', '-inform', 'DER', '-in', source])
            result = subprocess.run(args, capture_output=True, timeout=15)
            if result.returncode or not result.stdout or len(result.stdout) > 2 * MAX_FILE:
                raise ValueError('client identity conversion failed')
            target = os.path.join(work, 'tls', name + '-host.pem')
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(result.stdout)
            converted[name] = target
        # Empty password prevents OpenSSL from prompting on the daemon's stdin.
        context.load_cert_chain(converted['certificate'], converted.get('key'), password=b'')
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError('configured client identity could not be loaded for managed authentication; no token request was sent') from None

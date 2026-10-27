#!/usr/bin/env python3
"""Export only public GUI assets for an explicitly selected trusted HTTPS mobile origin."""
import argparse
import base64
import hashlib
import html
import json
from pathlib import Path
import re
import shutil
import sys

SK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SK / "piper"))
from mobile import https_url  # noqa: E402


def export(destination: Path, relay: str) -> None:
    relay = https_url(relay)
    web = SK / "piper" / "web"
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("export destination must be empty")
    if not (web / "vendor" / "iroh" / "piper_mobile_bg.wasm").is_file():
        raise ValueError("build the optional mobile transport first")
    # Only public assets: no Piper home, credentials, sources, checkout or design files.
    destination.mkdir(parents=True, exist_ok=True)
    public = destination / "piper"
    public.mkdir(exist_ok=True)
    for folder in ("dist", "vendor"):
        shutil.copytree(web / folder, public / folder, dirs_exist_ok=True)
    for name in ("app.css", "mobile.css", "manifest.webmanifest", "icon.svg", "icon-192.png", "icon-512.png", "themes.json"):
        shutil.copyfile(web / name, public / name)
    shutil.copyfile(web / "mobile-sw.js", destination / "service-worker.js")
    page = (web / "index.html").read_text()
    boot = {"version": 0, "port": 0, "origins": [], "dashboardUrl": "", "theme": "auto",
            "themes": json.loads((web / "themes.json").read_text())}
    page = page.replace('nonce="<!--PIPER_NONCE-->"', '')
    page = page.replace("<!--PIPER_BOOT-->", '<script>window.__PIPER__=' + json.dumps(boot).replace("<", "\\u003c") + '</script>')
    page = page.replace('/piper/dist/main.js', '/piper/dist/mobileboot.js')
    page = page.replace('</head>', '<meta name="referrer" content="no-referrer">\n'
        '<meta name="piper-mobile-relay" content="' + html.escape(relay, quote=True) + '">\n'
        '<link rel="stylesheet" href="/piper/mobile.css">\n</head>')
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(code.encode()).digest()).decode() + "'"
              for code in re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', page, re.S)]
    csp = ("default-src 'self'; script-src 'self' 'wasm-unsafe-eval' " + " ".join(hashes)
           + "; style-src 'self' 'unsafe-inline'; connect-src 'self' " + relay.replace("https://", "wss://", 1)
           + "; worker-src 'self'; img-src 'self' data: blob:; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'")
    page = page.replace('<head>', '<head>\n<meta http-equiv="Content-Security-Policy" content="' + html.escape(csp, quote=True) + '">')
    (destination / "index.html").write_text(page)
    # Cloudflare Pages / Netlify format. Other hosts must configure equivalent HTTP headers.
    (destination / "_headers").write_text("/*\n  Content-Security-Policy: " + csp + "; frame-ancestors 'none'\n"
        "  Referrer-Policy: no-referrer\n  X-Content-Type-Options: nosniff\n  Cache-Control: no-store\n"
        "  Strict-Transport-Security: max-age=31536000\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--relay", required=True)
    args = parser.parse_args()
    try:
        export(args.destination, args.relay)
    except (ValueError, OSError) as e:
        raise SystemExit(str(e)) from None
    print("Mobile client exported. Deploy only to your trusted HTTPS origin with the supplied security headers.")

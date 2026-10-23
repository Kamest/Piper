#!/usr/bin/env python3
"""docs/media/showcase.py - a piperd with curated demo data for the README and site screenshots.

Built on the e2e harness (tests/piper-e2e/run.py: fake hole, own PIPER_HOME, nothing real touched) with four
believable projects, a Global agent that planned a release and started three sessions, a push waiting on its
approval card, readable transcripts and a terminal per session. Nothing here is a test: the regression fixtures stay
in tests/piper-e2e/serve.py.

Run in the test sandbox (prints `SHOWCASE <gui url>` and stays up until Ctrl-C / SIGTERM):
    python3 scripts/piper-sandbox.py -- python3 docs/media/showcase.py [--port 17991]
docs/media/capture.sh drives it with agent-browser and writes docs/media/*.png and demo.gif.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.join(os.path.dirname(os.path.dirname(HERE)), "tests", "piper-e2e")

ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
ap.add_argument("--port", type=int, default=17991)
OPT = ap.parse_args()
if OPT.port == 17895:
    sys.exit("showcase.py: 17895 is the live piperd's port - pick another (--port)")
WORKDIR = tempfile.mkdtemp(prefix="piper-showcase-")
sys.argv = [sys.argv[0], "--port", str(OPT.port), "--work", WORKDIR, "--keep"]
sys.path.insert(0, E2E)
import run as R  # noqa: E402

R.SECTIONS.clear()
STOP = threading.Event()
NOW = time.time()
PEOPLE = [("Alice Chen", "alice@example.com"), ("Ben Okafor", "ben@example.com"), ("Maya Patel", "maya@example.com")]


def iso(ago: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW - ago))


def commit(root: str, msg: str, who: int, files: dict | None = None, ago: float = 0) -> None:
    for rel, text in (files or {}).items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        R.git(root, "add", rel)
    name, mail = PEOPLE[who % len(PEOPLE)]
    date = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - ago))
    R.sh("git", "-C", root, "-c", "user.name=" + name, "-c", "user.email=" + mail, "commit", "-q", "--allow-empty",
         "-m", msg, env=dict(R.GIT_ENV, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date,
                             GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=mail, GIT_COMMITTER_NAME=name,
                             GIT_COMMITTER_EMAIL=mail))


def first(root: str, who: int, ago: float) -> None:
    """The harness's "init <key>" commit as a person's first commit."""
    name, mail = PEOPLE[who]
    date = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(NOW - ago))
    R.sh("git", "-C", root, "commit", "-q", "--amend", "--reset-author", "-m", "Initial commit",
         env=dict(R.GIT_ENV, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date, GIT_AUTHOR_NAME=name,
                  GIT_AUTHOR_EMAIL=mail, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=mail))


def write(root: str, rel: str, text: str, mode: str = "w") -> None:
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, mode) as f:
        f.write(text)


# ------------------------------------------------------------------ projects
CART_TS = """import { Money, add, multiply } from './money';
import type { CartLine, SavedCard } from './types';

export interface Cart {
  id: string;
  lines: CartLine[];
  currency: string;
}

export function subtotal(cart: Cart): Money {
  return cart.lines.reduce((sum, l) => add(sum, multiply(l.price, l.quantity)), Money.zero(cart.currency));
}

export function canCheckout(cart: Cart): boolean {
  return cart.lines.length > 0 && cart.lines.every((l) => l.quantity > 0);
}
"""
MONEY_TS = """export class Money {
  static zero(currency: string) {
    return new Money(0, currency);
  }
  constructor(readonly cents: number, readonly currency: string) {}
}

export function add(a: Money, b: Money): Money {
  if (a.currency !== b.currency) throw new Error(`currency mismatch: ${a.currency} / ${b.currency}`);
  return new Money(a.cents + b.cents, a.currency);
}

export function multiply(m: Money, n: number): Money {
  return new Money(Math.round(m.cents * n), m.currency);
}
"""
CHECKOUT_TSX = """import { AddressForm } from './AddressForm';
import { getCart } from '@/lib/session';

export default async function Checkout() {
  const cart = await getCart();
  return (
    <main className="checkout">
      <h1>Checkout</h1>
      <AddressForm defaultValue={cart.address} persist />
    </main>
  );
}
"""
PACKAGE_JSON = json.dumps({"name": "storefront", "private": True,
                           "scripts": {"dev": "next dev", "build": "next build", "test": "vitest run"},
                           "dependencies": {"next": "15.1.0", "react": "19.0.0", "react-dom": "19.0.0"},
                           "devDependencies": {"vitest": "2.1.8", "@playwright/test": "1.49.1"}}, indent=2) + "\n"
TYPES_TS = """export interface CartLine {
  sku: string;
  price: import('./money').Money;
  quantity: number;
}

export interface SavedCard {
  id: string;
  brand: 'visa' | 'mastercard' | 'amex';
  last4: string;
  isDefault: boolean;
  expired: boolean;
}
"""
CARDS_TSX = """import { listCards } from '@/lib/payments';

export default async function Cards() {
  const cards = await listCards();
  return (
    <ul className="cards">
      {cards.map((c) => (
        <li key={c.id}>{c.brand} •••• {c.last4}{c.isDefault && ' · default'}</li>
      ))}
    </ul>
  );
}
"""
CART_TS_NEW = CART_TS + """
/** Express checkout skips the address step when the customer has a default saved card. */
export function expressEligible(cart: Cart, cards: SavedCard[]): boolean {
  return canCheckout(cart) && cards.some((c) => c.isDefault && !c.expired);
}
"""
EXPRESS_TSX = """'use client';
import { useTransition } from 'react';
import { expressEligible } from '@/lib/cart';
import { chargeSavedCard } from '@/lib/payments';

export function ExpressPay({ cart, cards }: Props) {
  const [pending, start] = useTransition();
  if (!expressEligible(cart, cards)) return null;
  const card = cards.find((c) => c.isDefault)!;
  return (
    <button className="btn-express" disabled={pending}
            onClick={() => start(() => chargeSavedCard(cart.id, card.id))}>
      Pay with •••• {card.last4}
    </button>
  );
}
"""
LEDGER_GO = """package ledger

import (
\t"context"
\t"errors"
)

var ErrDuplicate = errors.New("ledger: duplicate idempotency key")

// Charge books a charge once per idempotency key.
func (l *Ledger) Charge(ctx context.Context, key string, amount Money) (*Entry, error) {
\ttx, err := l.db.BeginTx(ctx, nil)
\tif err != nil {
\t\treturn nil, err
\t}
\tdefer tx.Rollback()
\tif _, err := tx.ExecContext(ctx, `INSERT INTO charges (key, amount) VALUES ($1, $2)
\t\tON CONFLICT (key) DO NOTHING`, key, amount); err != nil {
\t\treturn nil, err
\t}
\treturn l.entry(ctx, tx, key)
}
"""
PAYMENTS_HTTP = """### Create a charge
POST {{host}}/v1/charges
Content-Type: application/json
Idempotency-Key: {{$uuid}}

{"amount": 4990, "currency": "EUR", "card": "card_demo_4242"}

### Charge status
GET {{host}}/v1/charges/ch_123
"""


def setup_projects() -> None:
    sf = R.make_project("acme/storefront", {
        "README.md": "# Storefront\n\nThe Acme web shop (Next.js 15, App Router).\n\n```sh\npnpm dev\n```\n",
        "package.json": json.dumps({"name": "storefront", "private": True, "scripts": {
            "dev": "next dev", "build": "next build", "test": "vitest run"}}, indent=2) + "\n",
        "lib/cart.ts": CART_TS,
        "lib/money.ts": "export class Money {\n  static zero(c: string) { return new Money(0, c); }\n"
                        "  constructor(readonly cents: number, readonly currency: string) {}\n}\n",
        "lib/payments.ts": "export async function chargeSavedCard(cartId: string, cardId: string) {\n"
                           "  return fetch('/api/checkout/express', { method: 'POST', body: JSON.stringify({ cartId, cardId }) });\n}\n",
        "app/checkout/page.tsx": "export default function Checkout() {\n  return <main>Checkout</main>;\n}\n",
        "tests/cart.test.ts": "import { describe, it, expect } from 'vitest';\n\ndescribe('cart', () => {\n"
                              "  it('needs a line', () => expect(true).toBe(true));\n});\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": [".env.local"]}}, indent=2) + "\n",
        ".gitignore": "node_modules/\n.next/\n.env.local\n",
    }, commit=["README.md", "package.json", "lib/cart.ts", "lib/money.ts", "lib/payments.ts", "app/checkout/page.tsx",
               "tests/cart.test.ts", ".hole/settings.json", ".gitignore"])
    write(sf, ".env.local", "STRIPE_SECRET_KEY=sk_test_demo_only\n")
    first(sf, 0, 9 * 86400)
    commit(sf, "feat(cart): multi-currency subtotals", 1, {"lib/money.ts": MONEY_TS}, 6 * 86400)
    commit(sf, "fix(checkout): keep the address on back navigation", 2, {"app/checkout/page.tsx": CHECKOUT_TSX}, 4 * 86400)
    commit(sf, "chore: upgrade to Next.js 15.1", 0, {"package.json": PACKAGE_JSON}, 2 * 86400)
    commit(sf, "feat(account): saved cards list", 1, {"lib/types.ts": TYPES_TS, "app/account/cards/page.tsx": CARDS_TSX}, 86400)
    R.git(sf, "push", "-qf", "origin", "main", check_rc=False)
    R.git(sf, "switch", "-qc", "feat/express-checkout")
    write(sf, "lib/cart.ts", CART_TS_NEW)
    write(sf, "app/checkout/ExpressPay.tsx", EXPRESS_TSX)
    write(sf, "tests/express.test.ts", "import { expressEligible } from '../lib/cart';\n\n"
                                        "it('needs a default card', () => {\n  expect(expressEligible(cart, [])).toBe(false);\n});\n")
    R.PROJECTS["acme/storefront"] = sf

    pay = R.make_project("acme/payments-api", {
        "README.md": "# Payments API\n\nCharges, refunds and the ledger behind them (Go 1.23, Postgres).\n",
        "go.mod": "module example.com/acme/payments\n\ngo 1.23\n",
        "cmd/server/main.go": "package main\n\nfunc main() {\n\trun()\n}\n",
        "internal/ledger/ledger.go": LEDGER_GO.replace("\n\t\tON CONFLICT (key) DO NOTHING", ""),
        "internal/ledger/ledger_test.go": "package ledger\n\nimport \"testing\"\n\nfunc TestCharge(t *testing.T) {}\n",
        "http/charges.http": PAYMENTS_HTTP,
        "http/http-client.env.json": json.dumps({"dev": {"host": "http://localhost:8080"},
                                                 "staging": {"host": "https://staging.payments.example.com"},
                                                 "prod": {"host": "https://payments.example.com"}}, indent=2) + "\n",
        ".hole/settings.json": json.dumps({"files": {"exclude": ["http/http-client.private.env.json"]}}, indent=2) + "\n",
    })
    first(pay, 2, 12 * 86400)
    commit(pay, "feat(refunds): partial refunds", 0, {"internal/refunds/refunds.go": "package refunds\n\n// Partial books a refund of part of a charge.\nfunc Partial() {}\n"}, 5 * 86400)
    commit(pay, "perf(ledger): batch entry reads", 2, {"internal/ledger/batch.go": "package ledger\n\n// entries reads up to 500 entries per query.\nconst batch = 500\n"}, 3 * 86400)
    commit(pay, "fix(api): 409 on a reused idempotency key", 1, {"cmd/server/errors.go": "package main\n\n// 409 Conflict for a key reused with another body.\n"}, 2 * 86400)
    R.git(pay, "push", "-qf", "origin", "main", check_rc=False)
    R.git(pay, "switch", "-qc", "fix/idempotent-charge")
    commit(pay, "fix(ledger): one charge per idempotency key under concurrency", 0, {
        "internal/ledger/ledger.go": LEDGER_GO,
        "internal/ledger/ledger_test.go": "package ledger\n\nimport \"testing\"\n\n"
                                          "func TestChargeTwiceSameKey(t *testing.T) {\n\t// two goroutines, one key\n}\n"}, ago=1800)
    R.PROJECTS["acme/payments-api"] = pay

    R.PROJECTS["acme/mobile-app"] = R.make_project("acme/mobile-app", {
        "README.md": "# Mobile app\n\nReact Native client (iOS + Android).\n",
        "App.tsx": "export default function App() {\n  return <Root />;\n}\n",
        "src/sync/offline.ts": "export const QUEUE = 'cart-ops';\n",
        ".hole/settings.json": "{}\n",
    })
    R.PROJECTS["acme/docs"] = R.make_project("acme/docs", {
        "README.md": "# Developer docs\n", "guides/checkout.md": "# Checkout\n", ".hole/settings.json": "{}\n"})
    with open(os.path.join(R.PH, "projects.json"), "w") as f:
        json.dump({"projects": {k: {"path": v} for k, v in R.PROJECTS.items()}}, f)


# ------------------------------------------------------------------ the agent's terminal
B, D, X = "\033[1m", "\033[2m", "\033[0m"
OR, GR, BL, GY, RD = "\033[38;5;209m", "\033[38;5;114m", "\033[38;5;111m", "\033[38;5;245m", "\033[38;5;203m"


def screen(cwd: str, prompt: str, steps: list, tail: str) -> str:
    """An agent screen without box drawing: a grid pane reflows it to any width."""
    out = ["%s✻%s %sClaude Code%s  %s%s%s" % (OR, X, B, X, GY, cwd, X), "", "%s>%s %s" % (OR, X, prompt), ""]
    for head, detail, color in steps:
        out.append("%s●%s %s" % (color, X, head))
        for d in detail:
            out.append("  %s⎿  %s%s" % (GY, d, X))
        out.append("")
    out += [tail, "", "%s>%s " % (GY, X), "  %s? for shortcuts%s" % (GY, X)]
    return "\r\n".join(out) + "\r\n"


SCREENS = {
    "storefront": screen("/workspace/acme/storefront", "Add express checkout for customers with a saved card", [
        ("%sRead%s(lib/cart.ts)" % (B, X), ["Read 17 lines"], GR),
        ("%sUpdate%s(lib/cart.ts)" % (B, X), ["Updated lib/cart.ts with 5 additions"], GR),
        ("%sWrite%s(app/checkout/ExpressPay.tsx)" % (B, X), ["Wrote 17 lines to app/checkout/ExpressPay.tsx"], GR),
        ("%sBash%s(pnpm vitest run tests/express.test.ts)" % (B, X), ["%s✓%s 6 tests passed (412 ms)" % (GR, GY)], GR),
    ], "%s●%s Express checkout renders for a default, unexpired card. Wiring the button into the checkout page…\r\n"
       "  %s✻ Thinking… (esc to interrupt)%s" % (BL, X, OR, X)),
    "payments-api": screen("/workspace/acme/payments-api", "Two concurrent charges with one idempotency key book twice", [
        ("%sGrep%s(\"INSERT INTO charges\")" % (B, X), ["Found 1 file"], GR),
        ("%sBash%s(go test -race ./internal/ledger/...)" % (B, X), ["%sFAIL%s TestChargeTwiceSameKey: 2 entries, want 1" % (RD, GY)], RD),
        ("%sUpdate%s(internal/ledger/ledger.go)" % (B, X), ["ON CONFLICT (key) DO NOTHING inside the transaction"], GR),
        ("%sBash%s(go test -race ./...)" % (B, X), ["%sok%s  example.com/acme/payments/internal/ledger  1.84s" % (GR, GY)], GR),
    ], "%s●%s Fixed. Committed %sfix(ledger): one charge per idempotency key under concurrency%s.\r\n"
       "  Ready for review – the push waits for you." % (BL, X, B, X)),
    "docs": screen("/workspace/acme/docs", "Document the express checkout flow", [
        ("%sWrite%s(guides/checkout.md)" % (B, X), ["Wrote 48 lines"], GR)], "%s●%s Done." % (BL, X)),
}
SCREENS["mobile-app"] = screen("/workspace/acme/mobile-app", "Bring express checkout to the app, offline-safe", [
    ("%sRead%s(src/sync/offline.ts)" % (B, X), ["Read 1 line"], GR),
    ("%sBash%s(npx jest src/sync)" % (B, X), ["Running…"], OR),
], "  %s✻ Thinking… (esc to interrupt)%s" % (OR, X))
SCREENS["advisor"] = screen("/workspace", "Plan the express checkout release across our projects and start the work", [
    ("%slist_projects%s" % (B, X), ["4 projects"], GR),
    ("%sspawn_session%s(acme/payments-api, \"Idempotent charges\")" % (B, X), ["started"], GR),
    ("%sspawn_session%s(acme/storefront, \"Express checkout UI\")" % (B, X), ["started"], GR),
    ("%sspawn_session%s(acme/mobile-app, \"Offline express checkout\")" % (B, X), ["started"], GR),
], "%s●%s Three sessions are running. Each push waits for your approval." % (BL, X))
SCREENS["default"] = screen("/workspace", "", [], "")

HOLE = r"""#!/bin/sh
# showcase stand-in for `hole start <agent> <dir>`: hole's startup lines, an instance file, a scripted agent screen
set -u
[ "${1:-}" != version ] || { echo "hole 2.4.0"; exit 0; }
agent=$2; dir=$3; shift 3
base=${agent%%:*}
info() { printf '\033[36m[INFO]\033[0m %s\n' "$1" >&2; }
trap 'info "Exited $base CLI. Sandbox destroyed."; exit 0' TERM HUP
id=abc123   # fake-docker (tests/piper-unit) knows the containers of this instance id
name="hole-sandbox-$(basename "$dir")-00000000-$id"
mkdir -p "$HOME/.hole/instances"
printf '{"instanceName":"%s","instanceID":"%s","projectPath":"%s","agent":"%s","cliPID":%s}\n' \
  "$name" "$id" "$dir" "$agent" "$$" > "$HOME/.hole/instances/$name.json"
info "Launching sandbox for: $dir"
info "Project name: $(basename "$dir")-00000000"
info "Instance ID: $id"
info "Agent image: hole-sandbox/agent-global:fake (shared)"
info "Starting $base agent..."
info "Attaching to $base agent..."
printf '\033[2J\033[H'
s="SCREENS/$(basename "$dir").ans"
[ -f "$s" ] || s="SCREENS/default.ans"
cat "$s"
stty -echo 2>/dev/null   # the task piperd types stays off the scripted screen
while IFS= read -r l; do :; done
"""


def setup_home() -> None:
    R.setup_home()
    R.ENV["PIPER_PROD_TICK"] = "0"
    R.ENV["PIPER_LIMITS_TICK"] = "0"
    os.unlink(os.path.join(R.REAL, ".claude", ".credentials.json"))   # no login: piperd calls nobody
    d = os.path.join(R.WORK, "screens")
    os.makedirs(d, exist_ok=True)
    for k, text in SCREENS.items():
        with open(os.path.join(d, k + ".ans"), "w") as f:
            f.write(text)
    with open(os.path.join(R.BIN, "hole"), "w") as f:
        f.write(HOLE.replace("SCREENS", d))
    with open(os.path.join(R.PH, "limits.json"), "w") as f:   # the limits panel as a logged-in plan shows it
        json.dump({"v": 1, "status": "ok", "error": "", "plan": "max", "fetched_at": NOW,
                   "session": {"percent": 38, "resets_at": NOW + 2.3 * 3600, "severity": "normal"},
                   "week": {"percent": 61, "resets_at": NOW + 3.4 * 86400, "severity": "normal"},
                   "scoped": [{"label": "Opus", "percent": 44, "resets_at": NOW + 3.4 * 86400, "severity": "normal"}]}, f)
    with open(os.path.join(R.PH, "features.json"), "w") as f:
        json.dump({"base": "dev", "onboarded": True, "modules": {"composer": True}, "seen": ["security-2"]}, f)
    sys.path.insert(0, R.PIPER)
    import security   # noqa: E402  the Recommended profile, as Settings > Security saves it
    R.ENV["PIPER_SECURITY_TREE"] = "1"
    with open(os.path.join(R.PH, "policy.json"), "w") as f:
        json.dump({"protected_branches": ["main"],
                   "security": {i: True for i in security.IDS if i in security.RECOMMENDED_ON}}, f)


# ------------------------------------------------------------------ conversations
def cwd_of(sid: str, key: str) -> str:
    s = R.sess(sid)
    return R.PROJECTS.get(key) or s.get("path") or s.get("project_path") or s.get("cwd") or R.PP


def transcript_path(sid: str, key: str) -> tuple[str, str]:
    # the dir piperd reads it from (Session.claude_src): the real ~/.claude under claude_dir=host
    cfg = os.path.join(R.REAL, ".claude") if R.sess(sid).get("claude_mode") == "host" else R.box(sid)
    d = os.path.join(cfg, "projects",
                     "-" + cwd_of(sid, key).strip("/").replace("/", "-").replace(".", "-"))
    os.makedirs(d, exist_ok=True)
    conv = str(uuid.uuid4())
    return os.path.join(d, conv + ".jsonl"), conv


def write_convo(sid: str, key: str, title: str, turns: list, working: bool = False) -> None:
    """turns: ("user", text) | ("assistant", [text or (tool, input, result, error)]) with ages counting down."""
    path, conv = transcript_path(sid, key)
    R.hook(sid, "SessionStart", session_id=conv, source="startup")
    ago, n, recs = 40 * 60, 0, []
    for role, body in turns:
        ago -= 90
        if role == "user":
            recs.append({"type": "user", "sessionId": conv, "cwd": cwd_of(sid, key), "timestamp": iso(ago),
                         "message": {"role": "user", "content": [{"type": "text", "text": body}]}})
            continue
        content, results = [], []
        for part in body:
            if isinstance(part, str):
                content.append({"type": "text", "text": part})
            else:
                n += 1
                name, inp, out, err = part
                content.append({"type": "tool_use", "id": "t%d" % n, "name": name, "input": inp})
                if out is not None:
                    results.append({"type": "tool_result", "tool_use_id": "t%d" % n, "content": out, "is_error": err})
        recs.append({"type": "assistant", "sessionId": conv, "timestamp": iso(ago),
                     "message": {"id": "msg-%s-%d" % (conv[:8], len(recs)), "role": "assistant", "content": content,
                                 "usage": {"input_tokens": 1200, "output_tokens": 2400 + 300 * len(recs),
                                           "cache_read_input_tokens": 180_000, "cache_creation_input_tokens": 9_000}}})
        if results:
            recs.append({"type": "user", "sessionId": conv, "timestamp": iso(ago - 20),
                         "message": {"role": "user", "content": results}})
    recs.append({"type": "ai-title", "sessionId": conv, "aiTitle": title})
    with open(path, "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")
    last_user = next(b for r, b in reversed(turns) if r == "user")
    R.hook(sid, "UserPromptSubmit", session_id=conv, prompt=last_user)
    if not working:
        R.hook(sid, "Stop", session_id=conv)


PLAN = """## Express checkout – release plan

| # | Project | Work | Depends on |
|---|---------|------|------------|
| 1 | `acme/payments-api` | idempotent charges under concurrency | – |
| 2 | `acme/storefront` | express checkout button for saved cards | 1 |
| 3 | `acme/mobile-app` | the same flow in the app, offline-safe | 1 |

Each runs as its own session in its own sandbox. **2** and **3** build on the new charge semantics of **1**.

**Release gate:** `go test -race ./...` and the storefront e2e suite green, then one push per project – each push asks you first."""

STOREFRONT = [
    ("user", "Add express checkout for customers with a saved card. Payments-api exposes POST /api/checkout/express."),
    ("assistant", ["I'll start with the eligibility rule in the cart, then the button.",
                   ("Read", {"file_path": "lib/cart.ts"}, "17 lines", False),
                   ("Edit", {"file_path": "lib/cart.ts", "old_string": "}\n", "new_string": "…expressEligible…"},
                    "The file lib/cart.ts has been updated.", False),
                   ("Write", {"file_path": "app/checkout/ExpressPay.tsx", "content": EXPRESS_TSX},
                    "File created successfully.", False),
                   ("Bash", {"command": "pnpm vitest run tests/express.test.ts"}, "✓ 6 passed (412 ms)", False)]),
    ("assistant", ["### Done so far\n\n- `expressEligible()` in lib/cart.ts:18 – a default, unexpired card and a "
                   "valid cart\n- `<ExpressPay>` renders **Pay with •••• 4242** and charges through `chargeSavedCard()`\n"
                   "- 6 new tests, all green\n\nNext: put the button on the checkout page above the address form."]),
    ("user", "Looks good – put it on the checkout page and run the e2e suite."),
    ("assistant", [("Read", {"file_path": "app/checkout/page.tsx"}, "3 lines", False),
                   ("Bash", {"command": "pnpm playwright test checkout"}, None, False)]),
]
PAYMENTS = [
    ("user", "Two concurrent charges with the same idempotency key book twice. Reproduce it, fix it, add a race test."),
    ("assistant", ["Reproducing with the race detector first.",
                   ("Grep", {"pattern": "INSERT INTO charges", "path": "internal"}, "internal/ledger/ledger.go", False),
                   ("Bash", {"command": "go test -race ./internal/ledger/..."},
                    "--- FAIL: TestChargeTwiceSameKey\n    ledger_test.go:14: 2 entries, want 1", True),
                   ("Edit", {"file_path": "internal/ledger/ledger.go"}, "The file has been updated.", False),
                   ("Bash", {"command": "go test -race ./..."}, "ok  example.com/acme/payments/internal/ledger 1.84s", False),
                   ("Bash", {"command": "git commit -am 'fix(ledger): one charge per idempotency key under concurrency'"},
                    "[fix/idempotent-charge 3f2a91c] fix(ledger): one charge per idempotency key", False)]),
    ("assistant", ["## Fixed\n\nThe insert now runs `ON CONFLICT (key) DO NOTHING` **inside** the transaction, so the "
                   "second writer reads the first one's entry instead of booking a new one "
                   "(internal/ledger/ledger.go:19).\n\n**Verified:** `go test -race ./...` – the new "
                   "`TestChargeTwiceSameKey` fails before the change and passes after it.\n\n"
                   "**Not done:** the push – `fix/idempotent-charge` is ready for your review."]),
]
MOBILE = [
    ("user", "Bring express checkout to the app. Queue the charge while offline and replay it once, with the same "
             "idempotency key."),
    ("assistant", ["Looking at the offline queue first.",
                   ("Read", {"file_path": "src/sync/offline.ts"}, "1 line", False),
                   ("Bash", {"command": "npx jest src/sync"}, None, False)]),
]
AGENT = [
    ("user", "Plan the express checkout release across our projects and start the work."),
    ("assistant", ["Reading the three projects first.",
                   ("list_projects", {}, "acme/storefront, acme/payments-api, acme/mobile-app, acme/docs", False),
                   ("spawn_session", {"project": "acme/payments-api", "title": "Idempotent charges"}, "started", False),
                   ("spawn_session", {"project": "acme/storefront", "title": "Express checkout UI"}, "started", False),
                   ("spawn_session", {"project": "acme/mobile-app", "title": "Offline express checkout"}, "started", False)]),
    ("assistant", [PLAN]),
]


def seed() -> dict:
    sids = {"advisor": R.ready_session("advisor")}
    ok = lambda card: R.decide(card, True)   # noqa: E731
    for key, title, task in (("acme/payments-api", "Idempotent charges",
                              "Two concurrent charges with one idempotency key book twice. Fix it with a race test."),
                             ("acme/storefront", "Express checkout UI",
                              "Add express checkout for customers with a saved card."),
                             ("acme/mobile-app", "Offline express checkout",
                              "Bring express checkout to the app. Queue the charge while offline and replay it once, "
                              "with the same idempotency key.")):
        r = R.tool(sids["advisor"], "spawn_session", {"project": key, "title": title, "task": task}, answer=ok)
        sid = (r.get("result") or {}).get("id") if isinstance(r.get("result"), dict) else None
        if not sid:
            raise RuntimeError("spawn_session %s: %s" % (key, json.dumps(r)[:300]))
        if R.wait_state(sid, ("blocked", "ready", "waiting", "working"), 20).get("state") == "blocked":
            R.post("/api/sessions/%s/approve" % sid)   # a project's first start asks for its settings
            R.wait_state(sid, ("ready", "waiting", "working"), 20)
        sids[key] = sid
    docs = R.ready_session("acme/docs", task="")
    write_convo(docs, "acme/docs", "Checkout guide", [("user", "Document the express checkout flow"),
                                                      ("assistant", ["Done – guides/checkout.md."])])
    R.stop(docs)
    write_convo(sids["acme/payments-api"], "acme/payments-api", "Idempotent charges under concurrency", PAYMENTS)
    write_convo(sids["acme/storefront"], "acme/storefront", "Express checkout for saved cards", STOREFRONT, working=True)
    write_convo(sids["advisor"], "advisor", "Express checkout release", AGENT)
    write_convo(sids["acme/mobile-app"], "acme/mobile-app", "Offline express checkout", MOBILE, working=True)
    # the push the payments session asked for waits for the human: its card stays in the queue
    threading.Thread(target=R.tool, args=(sids["acme/payments-api"], "git_push", {}), kwargs={"timeout": 3600},
                     daemon=True).start()
    R.wait(lambda: R.pending(), 15)
    return sids


def _on_signal(*_a) -> None:
    STOP.set()


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    rc = 0
    try:
        R.start_git_daemon()
        setup_projects()
        setup_home()
        R.mint()
        R.start_piperd()
        sids = seed()
        url = "http://127.0.0.1:%d/#token=%s" % (OPT.port, R.TOK["gui"])
        with open(os.path.join(R.WORK, "showcase.json"), "w") as f:
            json.dump({"url": url, "sessions": sids, "projects": R.PROJECTS, "work": R.WORK}, f, indent=1)
        print("SHOWCASE %s" % url, flush=True)
        while not STOP.wait(1):
            if R.PROC["piperd"].poll() is not None:
                print("piperd exited - see %s/piperd.out" % R.WORK, flush=True)
                rc = 1
                break
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rc = 1
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        R.teardown()
        shutil.rmtree(os.path.dirname(R.SOCK), ignore_errors=True)
        if rc == 0:
            shutil.rmtree(WORKDIR, ignore_errors=True)
        print("TORN DOWN", flush=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())

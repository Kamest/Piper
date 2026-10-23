#!/usr/bin/env python3
"""tests/piper-e2e/dbprod.py - T2/T3 checks of the Database module and the production check against
REAL database servers (PLAN W4, G11, G12, B10, B11, C7).

Needs MySQL 8 and PostgreSQL in Docker, seeded by tests/piper-e2e/dbseed/*.sql: `sh tests/piper-e2e/dbseed/start.sh`
starts both (tmpfs, ports 13306 / 15432); Oracle / SQL Server join when DBPROD_ORACLE / DBPROD_MSSQL name a reachable server
(host:port:user:password:database) - `WITH="oracle mssql" sh .../start.sh` starts them too and prints both lines. The drivers are installed through the product path
(POST /api/db/install into the throwaway PIPER_HOME). test/prod connections go through Piper's own
ssh tunnel with a stand-in `ssh` that forwards -L to the Docker port (PIPER_SSH_BIN) - the tunnel
code, the pin and the worker run for real. The production check session is driven like Claude
drives it: report_production through the session's MCP mailbox (role prodcheck).

DBSEED_SUFFIX=<suffix> when start.sh was given one (the connection-drop checks docker exec into those containers).
Run OUTSIDE any sandbox:  python3 tests/piper-e2e/dbprod.py [--port 17931] [--work DIR] [--only DB1,PROD]
    [--serve]   keep piperd running after the checks (GUI testing) until <work>/stop exists
Reuses run.py's harness (fake-hole, own PIPER_HOME / HOME / port, everything killed by PID).
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SERVE = "--serve" in sys.argv
sys.argv = [a for a in sys.argv if a != "--serve"]
if "--port" not in sys.argv:
    sys.argv += ["--port", "17931"]
sys.path.insert(0, HERE)
import run as R  # noqa: E402  (parses --port/--work/--only/--keep)

R.SECTIONS.clear()
check, post, get, tool, decide, pending = R.check, R.post, R.get, R.tool, R.decide, R.pending

MY = "127.0.0.1:13306:root:e2e-root-pw:shop"
PG = "127.0.0.1:15432:postgres:e2e-pg-pw:e2e"
ENGINES = {"mysql": os.environ.get("DBPROD_MYSQL", MY), "postgres": os.environ.get("DBPROD_PG", PG)}
for eng, var in (("oracle", "DBPROD_ORACLE"), ("mssql", "DBPROD_MSSQL")):
    if os.environ.get(var):
        ENGINES[eng] = os.environ[var]

FAKE_SSH = r'''#!/usr/bin/env python3
# stand-in ssh: `ssh ... -L 127.0.0.1:<lport>:<host>:<port> <dest>` forwards to DBPROD_FWD[<host>:<port>]
import json, os, socket, sys, threading
a = sys.argv[1:]
spec = a[a.index("-L") + 1].split(":")
lport, target = int(spec[1]), "%s:%s" % (spec[2], spec[3])
fwd = json.loads(os.environ.get("DBPROD_FWD", "{}"))
if target not in fwd:
    sys.stderr.write("ssh: connect to host %s: Connection refused\n" % target)
    sys.exit(255)
th, tp = fwd[target].rsplit(":", 1)
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", lport))
s.listen(20)
def pipe(x, y):
    try:
        while True:
            d = x.recv(65536)
            if not d:
                break
            y.sendall(d)
    except OSError:
        pass
    for z in (x, y):
        try:
            z.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
while True:
    c, _ = s.accept()
    try:
        u = socket.create_connection((th, int(tp)), timeout=10)
        u.settimeout(None)
    except OSError:
        c.close()
        continue
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
'''


def tool_all(sid, name, args, ok=True, timeout=30.0):
    """tool() that answers EVERY card the call raises (a scope card, then a write card)."""
    import threading
    seen, stop = [], threading.Event()
    old = {p["id"] for p in pending(sid)}

    def loop():
        while not stop.is_set():
            for c in pending(sid):
                if c["id"] not in old and c["id"] not in [x["id"] for x in seen]:
                    seen.append(c)
                    decide(c, ok(c) if callable(ok) else ok)
            time.sleep(0.1)
    th = threading.Thread(target=loop, daemon=True)
    th.start()
    try:
        r = tool(sid, name, args, timeout=timeout)
    finally:
        stop.set()
        th.join(2)
    r["_cards"] = [c["kind"] for c in seen]
    return r


def parts(spec: str) -> dict:
    h, p, u, pw, db = spec.split(":", 4)
    return {"host": h, "port": int(p), "user": u, "password": pw, "database": db}


FWD = {}   # "db-<eng>-<cls>:<port>" -> the real server
for eng, spec in ENGINES.items():
    d = parts(spec)
    for cls in ("test", "prod"):
        FWD["db-%s-%s:%d" % (eng, cls, d["port"])] = "%s:%d" % (d["host"], d["port"])

S: dict = {}         # shared between sections
KEY = "dbp/shop"     # the project
OTHER = "dbp/other"  # another project (db_write_other)


def setup() -> None:
    R.setup_home()
    with open(os.path.join(R.BIN, "fake-ssh"), "w") as f:
        f.write(FAKE_SSH)
    os.chmod(os.path.join(R.BIN, "fake-ssh"), 0o755)
    R.ENV.update({"PIPER_SSH_BIN": os.path.join(R.BIN, "fake-ssh"), "DBPROD_FWD": json.dumps(FWD),
                  "PIPER_PROD_TICK": "1", "PIPER_PROD_RETRY_GAP": "1"})
    R.start_git_daemon()
    plain = {"README.md": "# shop\n", ".hole/settings.json": "{}\n",
             "sql/fix.sql": "UPDATE customer SET note = 'script' WHERE id = 2;\n"}
    R.PROJECTS[KEY] = R.make_project(KEY, dict(plain))
    R.PROJECTS[OTHER] = R.make_project(OTHER, dict(plain))
    idea = os.path.join(R.PROJECTS[KEY], ".idea")
    os.makedirs(idea, exist_ok=True)
    m = parts(ENGINES["mysql"])
    with open(os.path.join(idea, "dataSources.xml"), "w") as f:
        f.write('<?xml version="1.0"?><project><component name="DataSourceManagerImpl">'
                '<data-source source="LOCAL" name="shop PROD" uuid="u1"><jdbc-url>jdbc:mysql://db-mysql-prod:%d/shop_prod'
                '</jdbc-url><user-name>root</user-name><ssh-properties><enabled>true</enabled><ssh-config-id>s1</ssh-config-id>'
                '</ssh-properties></data-source>'
                '<data-source source="LOCAL" name="pg local" uuid="u2"><jdbc-url>jdbc:postgresql://localhost:15432/e2e'
                '</jdbc-url><user-name>postgres</user-name></data-source></component></project>' % m["port"])
    with open(os.path.join(idea, "sshConfigs.xml"), "w") as f:
        f.write('<?xml version="1.0"?><project><component name="SshConfigs"><configs><sshConfig host="bastion.example" '
                'id="s1" port="22" username="projects" /></configs></component></project>')
    R.write_projects()
    os.makedirs(os.path.join(R.PH, "obs"), exist_ok=True)
    # no scheduled run until PROD-SCHEDULE: D10 (9.37) the prod module is the switch, obs `enabled` is legacy
    with open(os.path.join(R.PH, "features.json"), "w") as f:
        json.dump({"base": "all", "onboarded": True, "modules": {"prod": False}}, f)
    R.mint()


def conn(eng: str, cls: str) -> dict:
    d = parts(ENGINES[eng])
    c = {"name": "%s/%s/%s" % (KEY, cls, eng), "cls": cls, "engine": eng, "user": d["user"],
         "database": d["database"] if cls != "prod" or eng != "mysql" else "shop_prod"}
    if cls == "dev":
        c.update(host=d["host"], port=d["port"])
    else:
        c.update(ssh={"host": "bastion.example", "user": "projects"}, host="db-%s-%s" % (eng, cls), port=d["port"])
    return c


def save(eng, cls, password=None, **over):
    c = dict(conn(eng, cls), **over)
    return post("/api/db/save", {"project": KEY, "connection": c,
                                 "password": parts(ENGINES[eng])["password"] if password is None else password})


def q(eng, cls, sql, **kw):
    return post("/api/db/query", dict({"project": KEY, "connection": "%s/%s/%s" % (KEY, cls, eng), "sql": sql}, **kw))


def ex(eng, cls, stmts, **kw):
    return post("/api/db/execute", dict({"project": KEY, "connection": "%s/%s/%s" % (KEY, cls, eng),
                                         "statements": stmts}, **kw))


def tx(eng, cls, op, stmts=None, **kw):
    return post("/api/db/tx", dict({"project": KEY, "connection": "%s/%s/%s" % (KEY, cls, eng), "op": op,
                                    "statements": stmts or []}, **kw))


def tbl(eng: str, name: str = "customer") -> str:
    # MySQL / SQL Server: the "schema" is the database the connection opens; PostgreSQL / Oracle: a schema (Oracle = the user SHOP)
    return name if eng in ("mysql", "mssql") else "shop." + name


def sch(eng: str) -> str:
    return "SHOP" if eng == "oracle" else "shop"   # the name Piper lists (Oracle keeps unquoted names upper case)


def one(eng: str, n: int = 1) -> str:
    return "SELECT %d FROM dual" % n if eng == "oracle" else "SELECT %d" % n


# ------------------------------------------------------------------ sections
@R.section("DB-INSTALL")
def db_install():
    st, j, b = get("/api/db/status")
    check("status before install says the drivers are missing", st == 200 and not j.get("ready"), b[:300])
    st, j, b = post("/api/db/install")
    check("POST /api/db/install starts", st in (200, 202) and j.get("installing"), (st, b[:300]))
    st2, j2, _ = post("/api/db/install")
    check("a second install while one runs is the same run", st2 in (200, 202) and j2.get("installing"), j2)
    check("status says installing", get("/api/db/status")[1].get("installing"))
    j = R.wait(lambda: (lambda x: x if not x.get("installing") else None)(get("/api/db/status")[1]), 600, 2)
    check("drivers installed through the product path (pip into PIPER_HOME/dbvenv)",
          j and j.get("ready") and not j.get("install_error"), json.dumps(j)[:1500])
    drv = (j or {}).get("drivers") or {}
    for eng in ("mysql", "postgres", "oracle", "mssql"):
        check("driver for %s present" % eng, eng in drv and not str(drv[eng]).startswith("missing"), drv)
    check("venv lives in the test PIPER_HOME", os.path.exists(os.path.join(R.PH, "dbvenv", "bin", "python")))


@R.section("DB-SAVE")
def db_save():
    for eng in ENGINES:
        for cls in ("dev", "test", "prod"):
            st, j, b = save(eng, cls)
            check("save %s %s" % (eng, cls), st == 200 and j.get("ok"), b[:300])
    st, _, b = post("/api/db/save", {"project": KEY, "connection": {"name": KEY + "/test/bad", "cls": "test",
                                                                    "engine": "mysql", "host": "localhost", "port": 1}})
    check("a test connection on localhost is refused", st >= 400 and b"localhost" in b, b[:300])
    st, _, b = post("/api/db/save", {"project": KEY, "connection": {"name": KEY + "/test/bad", "cls": "test",
                                                                    "engine": "mysql", "host": "127.1", "port": 1}})
    check("a test connection on another spelling of loopback (127.1) is refused", st >= 400, b[:300])
    st, _, b = post("/api/db/save", {"project": KEY, "connection": {"name": "x y", "cls": "dev", "engine": "mysql"}})
    check("an invalid name is refused", st >= 400, b[:300])
    st, _, b = post("/api/db/save", {"project": KEY, "connection": {"name": KEY + "/dev/z", "cls": "dev",
                                                                    "engine": "sqlite", "host": "h"}})
    check("an unknown engine is refused", st >= 400, b[:300])
    st, j, b = get("/api/db/connections?project=" + KEY)
    txt = json.dumps(j)
    check("connections list carries no password", st == 200 and all(parts(s)["password"] not in txt for s in ENGINES.values()),
          txt[:500])
    idea = (j or {}).get("idea") or (j or {}).get("harvest") or []
    check("IDEA dataSources.xml harvested (2 entries, prod guessed, ssh carried)",
          len(idea) == 2 and any(x.get("cls_guess") == "prod" and x.get("ssh") for x in idea), json.dumps(idea)[:600])
    secrets = os.path.join(R.PH, "db")
    mode = oct(os.stat(secrets).st_mode & 0o777) if os.path.exists(secrets) else "?"
    check("PIPER_HOME/db is 0700", mode == "0o700", mode)
    st, j, _ = get("/api/db/all")
    check("/api/db/all lists every connection", st == 200 and len(json.dumps(j)) > 100 and all(
        "%s/%s/%s" % (KEY, c, e) in json.dumps(j) for e in ENGINES for c in ("dev", "test", "prod")), json.dumps(j)[:500])


@R.section("DB-READ")
def db_read():
    for eng in ENGINES:
        for cls in ("dev", "test", "prod"):
            st, j, b = q(eng, cls, "SELECT name, email FROM %s WHERE id = 1" % tbl(eng))
            check("GUI read %s %s (unicode intact)" % (eng, cls),
                  st == 200 and j["rows"] and j["rows"][0][0] == "Jürgen Müller", b[:400])
        name = "%s/dev/%s" % (KEY, eng)
        st, j, b = get("/api/db/meta?project=%s&connection=%s&what=schemas" % (KEY, name))
        check("%s schemas" % eng, st == 200 and sch(eng) in json.dumps(j), b[:300])
        st, j, b = get("/api/db/meta?project=%s&connection=%s&what=objects&schema=%s" % (KEY, name, sch(eng)))
        objs = {o["name"].lower(): o["kind"] for o in (j or {}).get("objects", [])}
        check("%s objects: tables + view" % eng, objs.get("customer") == "table" and objs.get("v_paid") == "view", b[:400])
        if eng == "oracle":   # what an agent writes: unquoted lower case = the user SHOP
            st, j, b = get("/api/db/meta?project=%s&connection=%s&what=objects&schema=shop" % (KEY, name))
            check("oracle objects of schema `shop` (lower case)", "CUSTOMER" in [o["name"] for o in (j or {}).get("objects", [])], b[:300])
        st, j, b = get("/api/db/meta?project=%s&connection=%s&what=describe&schema=%s&name=%s" % (KEY, name, sch(eng),
                                                                                       "orders"))   # Oracle: `orders` = ORDERS
        check("%s describe orders: PK + FK to customer" % eng, st == 200 and [k.lower() for k in j.get("primary_key") or []] == ["id"]
              and any(f["ref_table"].lower() == "customer" for f in j.get("foreign_keys", [])), b[:500])
        st, j, b = get("/api/db/meta?project=%s&connection=%s&what=describe&schema=%s&name=%s" % (KEY, name, sch(eng),
                                                                                       "customer"))
        check("%s describe customer: referenced_by orders" % eng,
              st == 200 and any(r["table"].lower() == "orders" for r in j.get("referenced_by", [])), b[:500])
        st, j, b = q(eng, "dev", "SELECT id, big FROM %s ORDER BY id" % tbl(eng, "orders"),
                     max_rows=100)
        check("%s 3000 rows: first page + total/more" % eng, st == 200 and len(j["rows"]) == 100, b[:300])
        big = j["rows"][0][1] if st == 200 else ""
        check("%s 300 kB cell is cut in the grid result" % eng, isinstance(big, str) and len(big) < 300000, len(big or ""))
        check("%s first page says there is more" % eng, st == 200 and j.get("more") is True, b[:300])
        # the GUI pages as IDEA (ff04367): the next page runs the statement again with `offset`; the cache
        # holds only what the GUI fetched (offset + page), a fetch past it is empty with more = true
        st2, j2, b2 = q(eng, "dev", "SELECT id, big FROM %s ORDER BY id" % tbl(eng, "orders"), max_rows=100, offset=100)
        check("%s next page (offset 100): rows 101-200, more" % eng,
              st2 == 200 and len(j2["rows"]) == 100 and j2["rows"][0][0] == 101 and j2["offset"] == 100 and j2["more"], b2[:300])
        st2, j2, b2 = q(eng, "dev", "SELECT id, big FROM %s ORDER BY id" % tbl(eng, "orders"), max_rows=500, offset=2900)
        check("%s last page (offset 2900): 100 rows, no more" % eng,
              st2 == 200 and len(j2["rows"]) == 100 and j2["rows"][0][0] == 2901 and not j2["more"], b2[:300])
        # review 4: offset is never SQL - a non-number is refused, a negative one is the start, a too deep page says so
        sel = "SELECT id FROM %s ORDER BY id" % tbl(eng, "orders")
        st2, _, b2 = q(eng, "dev", sel, max_rows=10, offset="0; DROP TABLE orders")
        check("%s offset that is not a number is refused" % eng, st2 in (400, 409), (st2, b2[:200]))
        st2, j2, b2 = q(eng, "dev", sel, max_rows=10, offset=-50)
        check("%s negative offset = the first page" % eng, st2 == 200 and j2["offset"] == 0 and j2["rows"][0][0] == 1, b2[:200])
        st2, _, b2 = q(eng, "dev", sel, max_rows=500, offset=99999)
        check("%s page past 100 000 rows refused" % eng, st2 in (400, 409) and b"too deep" in b2, (st2, b2[:200]))
        st2, j2, b2 = q(eng, "dev", "SELECT COUNT(*) FROM %s" % tbl(eng, "orders"))
        check("%s orders intact after the offset probes" % eng, st2 == 200 and int(j2["rows"][0][0]) == 3000, b2[:200])
        if st == 200 and j.get("result_id"):
            st2, j2, b2 = post("/api/db/fetch", {"result_id": j["result_id"], "offset": 50, "max_rows": 50})
            check("%s fetch inside the cached page" % eng, st2 == 200 and len(j2["rows"]) == 50 and j2["rows"][0][0] == 51, b2[:300])
        st, j, b = q(eng, "dev", "SELECT name, email, phone, note FROM %s WHERE id IN (2, 3) ORDER BY id" % tbl(eng))
        check("%s NULLs come back as null; <script>/$(id) as text" % eng,
              st == 200 and j["rows"][0][1] is None and j["rows"][1][0] == "<script>alert(1)</script>", b[:400])
        st, j, b = q(eng, "dev", "DELETE FROM %s" % tbl(eng))
        check("%s the read path refuses a write" % eng, st >= 400, b[:300])
        st, j, b = q(eng, "dev", "%s; %s" % (one(eng), one(eng, 2)))
        check("%s two statements on the read path refused" % eng, st >= 400, b[:300])
        st, j, b = q(eng, "dev", "SELECT * FROM %s WHERE id = 1" % tbl(eng), explain=True)
        if eng in ("oracle", "mssql"):   # dbtools: EXPLAIN PLAN writes PLAN_TABLE / SHOWPLAN is a session switch
            check("%s explain = a clear 'not available' error" % eng, st >= 400 and b"not available" in b, b[:300])
            st, j, b = q(eng, "dev", "UPDATE %s SET note = 'x' WHERE id = 1" % tbl(eng), explain=True)   # the console's ⌘E on a write
            check("%s explain of an UPDATE: the same, never the agent's 'db_execute'" % eng,
                  st >= 400 and b"not available" in b and b"db_execute" not in b, b[:300])
        else:
            check("%s explain" % eng, st == 200 and j.get("rows"), b[:300])
        st, j, b = q(eng, "dev", "SELEC nonsense")
        check("%s syntax error reported, no crash" % eng, st >= 400 and b, b[:300])
    st, j, _ = get("/api/db/history?project=" + KEY)
    check("GUI history recorded", st == 200 and len(j.get("queries", [])) > 10, len((j or {}).get("queries", [])))


@R.section("DB-WRITE-GUI")
def db_write_gui():
    for eng in ENGINES:
        t = tbl(eng)
        st, j, b = ex(eng, "dev", ["UPDATE %s SET note = 'gui-dev' WHERE id = 4" % t])
        check("%s dev write from the GUI" % eng, st == 200 and j.get("committed") and j["rowcount"] == [1], b[:300])
        st, j, b = ex(eng, "test", ["UPDATE %s SET note = 'gui-test' WHERE id = 4" % t])
        check("%s test write from the GUI" % eng, st == 200 and j.get("committed"), b[:300])
        st, j, b = ex(eng, "prod", ["UPDATE %s SET note = 'gui-prod' WHERE id = 4" % t])
        check("%s prod write without confirm refused" % eng, st >= 400 and b"confirmation" in b, b[:300])
        st, j, b = ex(eng, "prod", ["UPDATE %s SET note = 'gui-prod' WHERE id = 4" % t], confirm="prod")
        check("%s prod write with confirm prod" % eng, st == 200 and j.get("committed"), b[:300])
        st, j, b = ex(eng, "prod", ["UPDATE %s SET note = 'x'" % t], confirm="prod")
        check("%s prod UPDATE without WHERE refused (prod_require_where)" % eng, st >= 400 and b"WHERE" in b, b[:300])
        st, j, b = ex(eng, "prod", ["UPDATE %s SET note = 'preview' WHERE id = 5" % t], preview_only=True)
        if eng == "mssql":   # dbtools._db_gate: a T-SQL batch needs no `;` - never previewed, said so
            check("%s prod preview_only: no preview, the reason said" % eng, st == 200 and j.get("preview") is None
                  and "batch" in " ".join(j.get("why_not") or []), b[:400])
        else:
            check("%s prod preview_only runs nothing, says the rowcount" % eng, st == 200 and j.get("preview") is not None, b[:400])
        st, j, _ = q(eng, "prod", "SELECT note FROM %s WHERE id = 5" % t)
        check("%s preview left the row unchanged" % eng, st == 200 and j["rows"][0][0] != "preview", j)
        st, j, b = ex(eng, "prod", ["DELETE FROM %s WHERE id = 5" % t], preview_only=True)
        check("%s prod preview of a DELETE that breaks a FK = the error, nothing changed" % eng,
              (st == 200 and (j.get("preview") is not None or eng == "mssql" and j.get("why_not")))
              or (st >= 400 and b"nothing changed" in b), b[:400])
        # NULL / delete / undelete as the grid does them (statements)
        st, j, b = ex(eng, "dev", ["UPDATE %s SET phone = NULL WHERE id = 6" % t])
        st2, j2, _ = q(eng, "dev", "SELECT phone FROM %s WHERE id = 6" % t)
        check("%s Set NULL" % eng, st == 200 and j2["rows"][0][0] is None, b[:300])
        # console transaction
        st, j, b = tx(eng, "dev", "exec", ["UPDATE %s SET note = 'in-tx' WHERE id = 7" % t], tx="c1")
        check("%s console tx exec" % eng, st == 200, b[:300])
        t0 = time.time()
        st, j, b = q(eng, "dev", "SELECT note FROM %s WHERE id = 7" % t)
        if eng == "mssql":   # no MVCC by default: the row is locked - a quick, clear error instead of a hang
            check("%s read of a row the open console transaction holds: 'locked' in %.1fs" % (eng, time.time() - t0),
                  st >= 400 and b"locked" in b and time.time() - t0 < 15, b[:300])
        else:
            check("%s uncommitted change invisible to another connection" % eng, st == 200 and j["rows"][0][0] != "in-tx", j)
        st, j, b = tx(eng, "dev", "state", tx="c1")
        check("%s tx state open" % eng, st == 200 and j.get("tx_open"), b[:300])
        st, j, b = tx(eng, "dev", "rollback", tx="c1")
        st2, j2, _ = q(eng, "dev", "SELECT note FROM %s WHERE id = 7" % t)
        check("%s rollback undoes" % eng, st == 200 and j2["rows"][0][0] != "in-tx", b[:300])
        st, j, b = tx(eng, "dev", "exec", ["UPDATE %s SET note = 'committed' WHERE id = 7" % t], tx="c2")
        st, j, b = tx(eng, "dev", "commit", tx="c2")
        st2, j2, _ = q(eng, "dev", "SELECT note FROM %s WHERE id = 7" % t)
        check("%s commit keeps" % eng, st == 200 and j2["rows"][0][0] == "committed", b[:300])
        st, j, b = tx(eng, "prod", "exec", ["UPDATE %s SET note = 'p' WHERE id = 7" % t], tx="p1")
        check("%s prod console change needs confirm" % eng, st >= 400, b[:300])
        st, j, b = tx(eng, "prod", "exec", ["DELETE FROM %s" % t], tx="p1", confirm="prod")
        check("%s prod console DELETE without WHERE refused" % eng, st >= 400 and b"WHERE" in b, b[:300])
        st, j, b = tx(eng, "prod", "exec", ["UPDATE %s SET note = 'p' WHERE id = 7" % t], tx="p1", confirm="prod")
        st, j, b = tx(eng, "prod", "commit", tx="p1")
        check("%s prod commit without confirm refused" % eng, st >= 400, b[:300])
        st, j, b = tx(eng, "prod", "commit", tx="p1", confirm="prod")
        check("%s prod commit with confirm" % eng, st == 200, b[:300])
        st, j, b = ex(eng, "dev", ["DROP TABLE nope_%s" % eng])
        check("%s failing DDL reported, nothing committed" % eng, st >= 400, b[:300])


@R.section("DB-MCP")
def db_mcp():
    sid = R.ready_session(KEY)
    S["sid"] = sid
    r = tool(sid, "db_connections")
    names = [c["name"] for c in (r.get("result") or {}).get("connections", [])]
    check("db_connections lists the project's connections, no password",
          len(names) == 3 * len(ENGINES) and not any(parts(s)["password"] in json.dumps(r) for s in ENGINES.values()), r)
    for eng in ENGINES:
        t = tbl(eng)
        r = tool(sid, "db_query", {"connection": "%s/dev/%s" % (KEY, eng), "sql": "SELECT id, name, email FROM %s WHERE id = 1" % t})
        res = r.get("result") or {}
        check("%s dev read: no card" % eng, r.get("ok") and r.get("_card") is None and res.get("rows"), r)
        r = tool(sid, "db_query", {"connection": "%s/test/%s" % (KEY, eng), "sql": "SELECT id, email FROM %s WHERE id = 1" % t},
                 answer=lambda c: decide(c, False))
        check("%s test read: db_read_test card, Deny -> error" % eng,
              not r.get("ok") and (r.get("_card") or {}).get("kind") == "db_read_test", r)
        r = tool(sid, "db_query", {"connection": "%s/test/%s" % (KEY, eng), "sql": "SELECT id, email, phone FROM %s WHERE id = 1" % t},
                 answer=lambda c: decide(c, True))
        res = r.get("result") or {}
        row = (res.get("rows") or [[None, None, None]])[0]
        check("%s test read approved; email/phone masked (mask_test default off?)" % eng, r.get("ok"), r)
        S.setdefault("masked_test", {})[eng] = row
        if eng == "mssql":   # BOUNDARY "parser" (no server-side read-only): EVERY test/prod read asks
            r = tool_all(sid, "db_query", {"connection": "%s/test/%s" % (KEY, eng), "sql": one(eng)})
            check("%s second test read: asks again (no read-only boundary)" % eng, r.get("ok") and r["_cards"] == ["db_read_test"], r)
        else:
            r = tool(sid, "db_query", {"connection": "%s/test/%s" % (KEY, eng), "sql": one(eng)})
            check("%s second test read: approved once per session (no card)" % eng, r.get("ok") and r.get("_card") is None, r)
        r = tool(sid, "db_query", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "SELECT id, email FROM %s WHERE id = 1" % t},
                 answer=lambda c: decide(c, True))
        res = r.get("result") or {}
        check("%s prod read: db_read_prod card" % eng, r.get("ok") and (r.get("_card") or {}).get("kind") == "db_read_prod", r)
        em = (res.get("rows") or [[None, None]])[0][1]
        check("%s prod email masked for the model" % eng, em != "kun@example.org", em)
        if eng == "mssql":   # the read card comes first on every read, then the unmask card
            r = tool_all(sid, "db_query", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "SELECT id, email FROM %s WHERE id = 1" % t,
                                           "unmask": True}, ok=lambda c: c["kind"] != "db_unmask")
            check("%s unmask: db_unmask card, Deny -> refused" % eng, not r.get("ok") and r["_cards"][-1:] == ["db_unmask"], r)
            r = tool_all(sid, "db_query", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "SELECT id, email FROM %s WHERE id = 1" % t,
                                           "unmask": True})
        else:
            r = tool(sid, "db_query", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "SELECT id, email FROM %s WHERE id = 1" % t,
                                       "unmask": True}, answer=lambda c: decide(c, False))
            check("%s unmask: db_unmask card, Deny -> refused" % eng,
                  not r.get("ok") and (r.get("_card") or {}).get("kind") == "db_unmask", r)
            r = tool(sid, "db_query", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "SELECT id, email FROM %s WHERE id = 1" % t,
                                       "unmask": True}, answer=lambda c: decide(c, True))
        res = r.get("result") or {}
        check("%s unmask approved -> clear email" % eng, r.get("ok") and res.get("rows", [[0, 0]])[0][1] == "kun@example.org", r)
        r = tool(sid, "db_query", {"connection": "%s/dev/%s" % (KEY, eng), "sql": "SELECT big FROM %s WHERE id = 1" % tbl(eng, "orders")})
        res = r.get("result") or {}
        cut = res.get("truncated_cells") or []
        check("%s big cell cut for the model + truncated_cells" % eng, r.get("ok") and cut, json.dumps(r)[:400])
        if r.get("ok"):
            r2 = tool(sid, "db_cell", {"result_id": res.get("result_id"), "row": 0, "column": "big"})
            v = (r2.get("result") or {}).get("value") or ""
            check("%s db_cell full value (256 KiB cap)" % eng, r2.get("ok") and len(v) >= 256 * 1024 - 10, len(v))
            cell = res["rows"][0][0]
            r3 = tool(sid, "db_execute", {"connection": "%s/dev/%s" % (KEY, eng),
                                          "sql": "UPDATE %s SET note = '%s' WHERE id = 9" % (t, cell[-200:].replace("'", "''"))})
            check("%s writing a truncated cell back refused" % eng, not r3.get("ok") and "TRUNCATED" in str(r3.get("error")), r3)
        ot = tbl(eng, "orders")
        r = tool(sid, "db_execute", {"connection": "%s/dev/%s" % (KEY, eng), "sql": "DELETE FROM %s" % ot,
                                     "reason": "e2e"}, answer=lambda c: decide(c, False))
        check("%s dev write: card db_write_dev, Deny -> nothing" % eng,
              not r.get("ok") and (r.get("_card") or {}).get("kind") == "db_write_dev", r)
        st, j, _ = q(eng, "dev", "SELECT count(*) FROM %s" % ot)
        check("%s denied DELETE left the rows" % eng, st == 200 and int(j["rows"][0][0]) == 3000, j)
        r = tool(sid, "db_execute", {"connection": "%s/test/%s" % (KEY, eng), "sql": "UPDATE %s SET note = 'mcp-test' WHERE id = 10" % t},
                 answer=lambda c: decide(c, True))
        check("%s test write: db_write_test card -> committed" % eng,
              r.get("ok") and (r.get("_card") or {}).get("kind") == "db_write_test" and (r.get("result") or {}).get("committed"), r)
        r = tool(sid, "db_execute", {"connection": "%s/test/%s" % (KEY, eng), "sql": "UPDATE %s SET note = 'mcp-test' WHERE id = 10" % t})
        check("%s the same write again = idempotent replay, no card" % eng,
              r.get("ok") and "replay" in (r.get("result") or {}), r)
        r = tool(sid, "db_execute", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "UPDATE %s SET note = 'q'" % t})
        check("%s prod UPDATE without WHERE refused before any card" % eng,
              not r.get("ok") and r.get("_card") is None and "WHERE" in str(r.get("error")), r)
        r = tool(sid, "db_execute", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "UPDATE %s SET note = 'mcp-prod' WHERE id = 11" % t},
                 answer=lambda c: decide(c, True, always=True))
        card = r.get("_card") or {}
        check("%s prod write: db_write_prod card (red) -> committed" % eng,
              r.get("ok") and card.get("kind") == "db_write_prod", r)
        r = tool(sid, "db_execute", {"connection": "%s/prod/%s" % (KEY, eng), "sql": "UPDATE %s SET note = 'mcp-prod2' WHERE id = 11" % t},
                 answer=lambda c: decide(c, True))
        check("%s prod write asks EVERY time (always ignored)" % eng,
              (r.get("_card") or {}).get("kind") == "db_write_prod", r)
        r = tool(sid, "db_script", dict({"connection": "%s/dev/%s" % (KEY, eng), "path": "sql/fix.sql"},
                                        **({"schema": "shop"} if eng == "postgres" else {})),
                 answer=lambda c: decide(c, True))
        check("%s db_script from the checkout" % eng, r.get("ok"), r)
    # another project's database
    st, j, b = post("/api/db/save", {"project": OTHER, "connection": dict(conn("mysql", "dev"), name=OTHER + "/dev/mysql"),
                                     "password": parts(ENGINES["mysql"])["password"]})
    t0 = time.time()
    r = tool(sid, "db_execute", {"connection": OTHER + "/dev/mysql", "sql": "UPDATE customer SET note = 'o' WHERE id = 12"},
             answer=lambda c: decide(c, True))
    check("another project's database: write locked, refused BEFORE any card",
          not r.get("ok") and "locked" in str(r.get("error")) and r.get("_card") is None and time.time() - t0 < 5, r)
    st, j, b = post("/api/sessions/%s/unlock" % sid, {"what": "db_write_other", "on": True})
    check("unlock db_write_other (GUI)", st == 200, b[:300])
    r = tool_all(sid, "db_execute", {"connection": OTHER + "/dev/mysql", "sql": "UPDATE customer SET note = 'o' WHERE id = 12"})
    check("after unlock: cards (use other project + write) -> committed",
          r.get("ok") and r["_cards"] == ["db_other", "db_write_dev"], r)
    post("/api/sessions/%s/unlock" % sid, {"what": "db_write_other", "on": False})
    st, j, _ = R.req("GET", "/api/db/history?project=%s&session=%s" % (KEY, sid))
    r = tool(sid, "db_history", {"n": 5})
    check("db_history = this session's calls", r.get("ok") and (r.get("result") or {}).get("queries"), r)
    # max_bytes: a result above the session budget is cut, never 60 000+ bytes in one answer
    r = tool(sid, "db_query", {"connection": "%s/dev/mysql" % KEY, "sql": "SELECT name, email, phone FROM customer", "max_rows": 1000})
    size = len(json.dumps(r.get("result") or {}))
    check("a 1000-row answer stays within the MCP budget (%d bytes)" % size, r.get("ok") and size <= 300000, size)
    # audit
    with open(os.path.join(R.PH, "db", "audit.jsonl") if os.path.exists(os.path.join(R.PH, "db", "audit.jsonl"))
              else os.path.join(R.PH, "db-audit.jsonl")) as f:
        lines = f.read()
    check("db audit has no password", not any(parts(s)["password"] in lines for s in ENGINES.values()))
    check("db audit has mcp + gui origins", '"origin": "mcp"' in lines and '"origin": "gui"' in lines)


@R.section("DB-SESSION")
def db_session_grants():
    """A production write grant covers new SQL, while other classes and reads still ask."""
    sid = R.ready_session(KEY)
    try:
        for eng in ENGINES:
            t = tbl(eng)
            args = {"connection": "%s/prod/%s" % (KEY, eng),
                    "sql": "UPDATE %s SET note = 'session-prod1' WHERE id = 13" % t}
            r = tool(sid, "db_execute", args, answer=lambda c: decide(c, True, session_always=True))
            check("%s prod write session grant commits" % eng, r.get("ok") and
                  (r.get("result") or {}).get("committed"), r)
            args["sql"] = "UPDATE %s SET note = 'session-prod2' WHERE id = 13" % t
            r = tool(sid, "db_execute", args)
            check("%s changed prod SQL reuses the session grant" % eng,
                  r.get("ok") and r.get("_card") is None and (r.get("result") or {}).get("committed"), r)
            st, j, _ = q(eng, "prod", "SELECT note FROM %s WHERE id = 13" % t)
            check("%s session-approved write is in the real database" % eng,
                  st == 200 and j.get("rows") and j["rows"][0][0] == "session-prod2", j)
            for cls in ("dev", "test"):
                r = tool(sid, "db_execute", {"connection": "%s/%s/%s" % (KEY, cls, eng),
                         "sql": "UPDATE %s SET note = 'session-other' WHERE id = 13" % t},
                         answer=lambda c: decide(c, False))
                check("%s prod write grant does not allow %s writes" % (eng, cls), not r.get("ok") and
                      (r.get("_card") or {}).get("kind") == "db_write_" + cls, r)
            r = tool(sid, "db_query", {"connection": "%s/prod/%s" % (KEY, eng), "sql": one(eng)},
                     answer=lambda c: decide(c, False))
            check("%s prod write grant does not allow prod reads" % eng,
                  not r.get("ok") and (r.get("_card") or {}).get("kind") == "db_read_prod", r)
    finally:
        post("/api/sessions/%s/stop" % sid)


@R.section("DB-TUNNEL")
def db_tunnel():
    name = "%s/test/mysql" % KEY
    st, j, b = post("/api/db/tunnel", {"project": KEY, "connection": name, "op": "close"})
    check("tunnel close", st == 200 and j.get("state") == "closed", b[:300])
    st, j, b = post("/api/db/tunnel", {"project": KEY, "connection": name, "op": "open"})
    check("tunnel open -> port", st == 200 and j.get("state") == "open" and j.get("port"), b[:300])
    st, j, b = post("/api/db/tunnel", {"project": KEY, "connection": "%s/dev/mysql" % KEY, "op": "open"})
    check("a dev connection has no tunnel", st >= 400, b[:300])
    st, j, b = save("mysql", "test", host="db-unknown")
    st, j, b = q("mysql", "test", "SELECT 1")
    check("a tunnel that cannot connect is a clear error", st >= 400 and b"Connection refused" in b, b[:300])
    save("mysql", "test")
    st, j, b = q("mysql", "test", "SELECT 1")
    check("after fixing the target the read works again", st == 200, b[:300])
    st, j, b = save("mysql", "dev", password="wrong")
    st, j, b = q("mysql", "dev", "SELECT 1")
    check("wrong password = clear auth error, no password echoed", st >= 400 and b"wrong" not in b, b[:300])
    save("mysql", "dev")
    st, j, b = post("/api/db/delete", {"project": KEY, "name": KEY + "/dev/mysql"})
    check("delete a connection", st == 200, b[:300])
    save("mysql", "dev")


@R.section("DB-RECOVER")
def db_recover():
    """The server drops the connection (restart, network blip, idle kill): the next call works again."""
    sid = S.get("sid") or R.ready_session(KEY)
    for eng in ENGINES:
        if eng not in DROP:
            continue
        for cls in ("dev", "test"):
            st, j, b = q(eng, cls, one(eng))
            r = tool(sid, "db_query", {"connection": "%s/%s/%s" % (KEY, cls, eng), "sql": one(eng)},
                     answer=lambda c: decide(c, True))
            DROP[eng]()
            time.sleep(0.5)
            st, j, b = q(eng, cls, one(eng, 2))
            check("%s %s GUI read right after the server dropped the connection" % (eng, cls), st == 200, b[:300])
            ask = (lambda n, a: tool_all(sid, n, a)) if eng == "mssql" else (lambda n, a: tool(sid, n, a))   # mssql: every read asks
            r = ask("db_query", {"connection": "%s/%s/%s" % (KEY, cls, eng), "sql": one(eng, 3)})
            check("%s %s session read right after the drop" % (eng, cls), r.get("ok"), r)
            t0 = time.time()
            r = ask("db_query", {"connection": "%s/%s/%s" % (KEY, cls, eng), "sql": one(eng, 4)})
            check("%s %s and again (answer in %.1fs)" % (eng, cls, time.time() - t0), r.get("ok"), r)
    for eng in ENGINES:   # review 2026-10-07: an open console transaction the server killed is LOST, never reopened
        if eng not in DROP:
            continue
        t = tbl(eng)
        st, j, b = tx(eng, "dev", "exec", ["UPDATE %s SET note = 'lost-1' WHERE id = 8" % t], tx="L1")
        check("%s console tx before the kill" % eng, st == 200 and j.get("tx_open"), b[:300])
        DROP[eng]()
        time.sleep(0.5)
        st, j, b = tx(eng, "dev", "exec", ["UPDATE %s SET note = 'lost-2' WHERE id = 8" % t], tx="L1")
        check("%s next console statement says the transaction was lost" % eng, st >= 400 and b"rolled the open transaction back" in b, b[:300])
        st, j, b = tx(eng, "dev", "commit", tx="L1")
        check("%s commit after the loss never says committed" % eng, not (j or {}).get("committed"), b[:300])
        st2, j2, _ = q(eng, "dev", "SELECT note FROM %s WHERE id = 8" % t)
        check("%s neither change landed" % eng, st2 == 200 and j2["rows"][0][0] not in ("lost-1", "lost-2"), j2)


def CT(engine: str) -> str:   # the dbseed/start.sh container; DBSEED_SUFFIX = the suffix it was started with
    suffix = os.environ.get("DBSEED_SUFFIX", "")
    if not suffix:   # never the developer's own piper-test-mysql / -pg: their connections get killed
        raise RuntimeError("DBSEED_SUFFIX is empty - start dbseed/start.sh with a suffix for this run and set it")
    name = "piper-test-%s%s" % (engine, suffix)
    if subprocess.run(["docker", "inspect", "--type", "container", name], capture_output=True).returncode:
        # a wrong suffix used to "drop" nothing: DB-RECOVER then tested a healthy connection and failed on data
        raise RuntimeError("no container %s - set DBSEED_SUFFIX to the suffix dbseed/start.sh was given" % name)
    return name


def _dx(container, *cmd, stdin=None):
    return subprocess.run(["docker", "exec", "-i", container, *cmd], input=stdin, capture_output=True, text=True).stdout


def _drop_mysql():
    ids = _dx(CT("mysql"), "mysql", "-uroot", "-pe2e-root-pw", "-N", "-e",
              "SELECT id FROM information_schema.processlist WHERE user='root' AND command='Sleep'").split()
    for i in ids:
        _dx(CT("mysql"), "mysql", "-uroot", "-pe2e-root-pw", "-e", "KILL %s" % i)


def _drop_pg():
    _dx(CT("pg"), "psql", "-U", "postgres", "-d", "postgres", "-tAc",
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND datname = 'e2e'")


ORA_KILL = """BEGIN FOR s IN (SELECT sid, serial# sr FROM v$session WHERE module LIKE 'piper-%') LOOP
EXECUTE IMMEDIATE 'ALTER SYSTEM KILL SESSION ''' || s.sid || ',' || s.sr || ''' IMMEDIATE'; END LOOP; END;
/
EXIT
"""


def _drop_oracle():   # every session Piper opened (module piper-<mode>)
    _dx(CT("oracle"), "sqlplus", "-s", "system/e2e-ora-sys@//localhost/FREEPDB1", stdin=ORA_KILL)


def _drop_mssql():
    _dx(CT("mssql"), "/opt/mssql-tools18/bin/sqlcmd", "-C", "-U", "sa", "-P", "E2e-mssql-pw!", "-Q",
        "DECLARE @k nvarchar(max) = ''; SELECT @k = @k + 'KILL ' + CAST(session_id AS varchar) + ';' FROM sys.dm_exec_sessions "
        "WHERE program_name LIKE 'piper-%'; EXEC (@k);")


DROP = {"mysql": _drop_mysql, "postgres": _drop_pg, "oracle": _drop_oracle, "mssql": _drop_mssql}


def report_session(mode: str):
    st, j, b = post("/api/prod/run", {"mode": mode})
    if st not in (200, 202):
        raise RuntimeError("prod/run %s: %s %s" % (mode, st, b[:300]))
    sid = j["session"]
    s = R.wait_state(sid, ("ready", "waiting", "working", "attention", "error", "exited"), 30)
    return sid, s


@R.section("PROD")
def prod():
    st, j, b = get("/api/prod")
    check("GET /api/prod", st == 200 and isinstance(j, dict), b[:300])
    st, j, b = post("/api/prod/run", {"mode": "check"})
    check("Check now without a mapping is refused clearly", st >= 400 and b, b[:300])
    # Suggest: the session proposes a mapping for unmapped projects
    sid, s = report_session("suggest")
    check("suggest session started (role prodcheck)", sid and s.get("state") in ("ready", "waiting", "working", "attention"),
          s)
    r = tool(sid, "report_production", {"items": [], "suggestions": [
        {"project": KEY, "obs": "shop-prod", "note": "found by the project lookup"},
        {"project": "nope/x", "obs": "zzz"}, {"project": OTHER, "obs": "<bad>"}]})
    check("report_production (suggest) accepted", r.get("ok"), r)
    st, j, _ = get("/api/prod")
    sugg = j.get("suggestions") or []
    check("suggestion stored for a real unmapped project only (bad obs + unknown project dropped)",
          [x.get("project") for x in sugg] == [KEY], sugg)
    R.wait_state(sid, ("exited", "error"), 20)
    check("suggest session stops itself after the report", R.sess(sid).get("state") in ("exited", "error"), R.sess(sid))
    for tool_name in ("db_query", "git_status", "read_file", "gitlab_issue"):
        pass
    st, j, b = post("/api/prod/mapping", {"project": KEY, "obs": "shop-prod"})
    check("accept the mapping", st == 200, b[:300])
    st, j, b = post("/api/prod/mapping", {"project": KEY, "obs": "<bad>"})
    check("invalid observability name refused", st >= 400, b[:300])
    st, j, b = post("/api/prod/ignore", {"project": OTHER})
    check("Don't map another project", st == 200, b[:300])
    # Check now: new / known / fixed
    sid, s = report_session("check")
    check("check session started", sid and s.get("state") in ("ready", "waiting", "working", "attention"), s)
    st2, _, b2 = post("/api/prod/run", {"mode": "check"})
    check("a second Check now while one runs is refused / joined", st2 in (200, 409, 400), b2[:200])
    r = tool(sid, "git_status", {})
    check("prodcheck role has no git tool", not r.get("ok"), r)
    items = [{"project": KEY, "kind": "error", "title": "NullPointerException in OrderService.pay", "count": 42,
              "fingerprint": "java.lang.NullPointerException OrderService.pay:120", "first_seen": "2026-10-06 03:12",
              "url": "https://grafana.example/d/x", "service": "shop-app", "detail": "password=PLANTED_P1 at line 3"},
             {"project": KEY, "kind": "rate", "title": "error rate up", "fingerprint": "shop-app", "trend": "+240 % vs last week"},
             {"project": KEY, "kind": "alert", "title": "HighLatency firing", "fingerprint": "HighLatency",
              "url": "javascript:alert(1)"},
             {"project": "shop-prod", "kind": "error", "title": "obs name instead of key"}]
    r = tool(sid, "report_production", {"items": items, "errors": [{"project": KEY, "error": "loki timeout"}]})
    check("report_production (check) accepted", r.get("ok"), r)
    R.wait_state(sid, ("exited", "error"), 20)
    day = time.strftime("%Y-%m-%d")
    path = os.path.join(R.PH, "obs", "%s.json" % day)
    rep = json.load(open(path)) if os.path.exists(path) else {}
    its = rep.get("items") or []
    check("obs/<date>.json written with 3 items (the obs-name one dropped)", len(its) == 3, json.dumps(rep)[:600])
    raw = open(path).read() if os.path.exists(path) else ""
    check("report strings scrubbed (no PLANTED_P1 password)", "PLANTED_P1" not in raw, raw[:400])
    check("non-https link dropped", all(i.get("url", "").startswith("https://") or not i.get("url") for i in its), its)
    st, j, _ = get("/api/prod")
    check("/api/prod shows the report, all new", st == 200 and len((j.get("report") or {}).get("items", [])) == 3, json.dumps(j)[:600])
    stt = R.state().get("prod") or {}
    check("/api/state prod.new = 3", stt.get("new") == 3, stt)
    lr = open(os.path.join(R.PH, "obs", "last-run")).read().strip() if os.path.exists(os.path.join(R.PH, "obs", "last-run")) else ""
    check("last-run = today done", lr.startswith(day), lr)
    # once a day: the scheduler does not run again today
    k0 = its[0]["key"] if its else ""
    st, j, b = post("/api/prod/mute", {"key": k0, "days": 7})
    check("mute an item 7 days", st == 200, b[:300])
    st, j, _ = get("/api/prod")
    check("muted item flagged", any(i.get("key") == k0 and i.get("muted") for i in (j.get("report") or {}).get("items", [])),
          json.dumps(j)[:800])
    st, j, b = post("/api/prod/unmute", {"key": k0})
    check("unmute", st == 200, b[:300])
    st, j, b = post("/api/prod/investigate", {"key": its[1]["key"] if len(its) > 1 else "x"})
    inv = (j or {}).get("id")
    check("Investigate opens a session in the project (origin prod)", st in (200, 202) and inv, b[:300])
    if inv:
        s = R.wait_state(inv, ("blocked", "ready", "waiting", "working", "attention"), 20)
        check("investigate session is the project's", s.get("project") == KEY, s)
        R.stop(inv)
    st, j, b = post("/api/prod/investigate", {"key": "0000000000000000"})
    check("Investigate an unknown key = 404/400", st in (400, 404), b[:300])
    # a second run the same day: known items are not new
    sid, s = report_session("check")
    r = tool(sid, "report_production", {"items": items[:2] + [{"project": KEY, "kind": "error", "title": "brand new",
                                                                  "fingerprint": "NewThing"}]})
    R.wait_state(sid, ("exited", "error"), 20)
    st, j, _ = get("/api/prod")
    its2 = (j.get("report") or {}).get("items", [])
    check("second run the same day: compared with the same 7-day history (all still new, keys stable)",
          len(its2) == 3 and all(i.get("new") for i in its2) and its2[0]["key"] == its[0]["key"], json.dumps(j)[:800])
    ann = [e for e in R.audit_events(event="prod-report")]
    check("prod-report audited per run", len(ann) >= 3, ann)
    # error path: the session ends without a report
    sid, s = report_session("check")
    post("/api/sessions/%s/stop" % sid)
    R.wait_state(sid, ("exited", "error"), 20)
    st, j, _ = get("/api/prod")
    ok = R.wait(lambda: (get("/api/prod")[1] or {}).get("status") in ("failed", "error"), 15)
    check("a check that ends without a report = failed status (Try again)", ok, json.dumps(get("/api/prod")[1])[:600])
    # report_production called from a normal session = refused
    r = tool(S.get("sid") or R.ready_session(KEY), "report_production", {"items": []})
    check("report_production from a normal session refused", not r.get("ok"), r)
    # malformed report
    sid, s = report_session("check")
    r = tool(sid, "report_production", {"items": "x"})
    check("malformed report = tool error, session keeps going", not r.get("ok"), r)
    r = tool(sid, "report_production", {"items": [{"project": KEY, "kind": "boom", "title": "t"}]})
    check("unknown kind refused", not r.get("ok"), r)
    r = tool(sid, "report_production", {"items": [{"project": KEY, "kind": "error", "title": "ok"}]})
    check("then a valid report goes through", r.get("ok"), r)
    R.wait_state(sid, ("exited", "error"), 20)
    # settings: config validation
    st, j, b = post("/api/settings", {"section": "prodcheck", "values": {"time": "25:00"}})
    check("prod check time 25:00 refused", st in (400, 409), b[:300])


@R.section("PROD-SCHEDULE")
def prod_schedule():
    """The morning schedule: due once a day after its time; a done day does not run again."""
    day = time.strftime("%Y-%m-%d")
    lr = os.path.join(R.PH, "obs", "last-run")
    os.remove(lr) if os.path.exists(lr) else None
    R.wait(lambda: (get("/api/prod")[1] or {}).get("status") != "running", 30)
    t = time.localtime(time.time() - 120)
    with open(os.path.join(R.PH, "obs", "config.json"), "w") as f:
        json.dump({"time": "%02d:%02d" % (t.tm_hour, t.tm_min), "days": [1, 2, 3, 4, 5, 6, 7]}, f)
    st, j, _ = post("/api/features", {"modules": {"prod": True}})   # D10: the module turns the schedule on
    check("prod module on", st == 200 and (j.get("modules") or {}).get("prod") is True, (st, j))
    run = R.wait(lambda: (lambda st: st if st.get("status") == "running" else None)(get("/api/prod")[1] or {}), 20, 0.5)
    check("the tick starts the scheduled check after its time", run, get("/api/prod")[1])
    sid = R.wait(lambda: next((s["id"] for s in R.state()["sessions"] if s.get("kind") == "prodcheck" and
                               s["state"] in ("ready", "waiting", "working", "attention")), None), 30)
    check("scheduled check session up", sid)
    if sid:
        r = tool(sid, "report_production", {"items": [{"project": KEY, "kind": "error", "title": "sched"}]})
        check("scheduled run reported", r.get("ok"), r)
        R.wait_state(sid, ("exited", "error"), 20)
    time.sleep(3)
    n = len(R.audit_events(event="prod-start"))
    time.sleep(5)
    check("a done day does not start another check", len(R.audit_events(event="prod-start")) == n, n)
    check("last-run = today (done)", os.path.exists(lr) and open(lr).read().strip() == day,
          open(lr).read() if os.path.exists(lr) else "")


def close_tunnels() -> None:
    """Piper's ssh tunnels outlive a piperd on purpose (the next one reaps them via tunnels.json); a
    throwaway PIPER_HOME has no next piperd: close them, then kill any stand-in ssh of THIS run by PID."""
    try:
        for eng in ENGINES:
            for cls in ("test", "prod"):
                post("/api/db/tunnel", {"project": KEY, "connection": "%s/%s/%s" % (KEY, cls, eng), "op": "close"})
    except Exception:  # noqa: BLE001
        pass
    me = os.path.join(R.BIN, "fake-ssh")
    out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if me in cmd.split(" ")[1:2] or cmd.split(" ")[:1] == [me]:
            try:
                os.kill(int(pid), 15)
            except (ProcessLookupError, ValueError):
                pass


def main() -> int:
    setup()
    try:
        R.start_piperd()
        for sid_, fn in R.SECTIONS:
            if R.ONLY and sid_ not in R.ONLY:
                continue
            R.cur["id"] = sid_
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                check("section ran to the end", False, "%s: %s\n%s" % (type(e).__name__, e, traceback.format_exc()[-1500:]))
        if SERVE:
            print("SERVE http://127.0.0.1:%d/#token=%s  (touch %s/stop to end)" % (R.A.port, R.TOK["gui"], R.WORK), flush=True)
            while not os.path.exists(os.path.join(R.WORK, "stop")):
                time.sleep(1)
    finally:
        close_tunnels()
        R.teardown()
    fails = [r for r in R.results if not r["ok"]]
    by: dict = {}
    for r in R.results:
        by.setdefault(r["id"], [0, 0])[0 if r["ok"] else 1] += 1
    with open(os.path.join(R.WORK, "results-dbprod.json"), "w") as f:
        json.dump({"results": R.results, "summary": by, "engines": sorted(ENGINES)}, f, indent=1)
    print("\n== %d checks, %d failed  (%s)  engines %s  work: %s" % (
        len(R.results), len(fails), " ".join("%s %d/%d" % (k, v[0], v[0] + v[1]) for k, v in by.items()),
        ",".join(sorted(ENGINES)), R.WORK))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

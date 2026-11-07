"""Headless A14 regressions against the real main client and a private piperd."""
import _env
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import unittest
from unittest import mock

import common
import features
import inbox
import piperd
import projects
import sessions
import settings


def run():
    case = unittest.TestCase()
    manager = _env.manager(case, background=True)
    key = "acme/settings-regression"
    root = _env.make_project("settings-regression", {"README.md": "Synthetic settings fixture\n"})
    _env.register(key, root, gitlab=["acme/shop"])
    common.write_json(common.POLICY_FILE, {**manager.policy, "ask": ["settings_change"], "automation_start_opts": True})
    manager.reload_policy()
    settings.update("databases", {"ssh_user": "before-user"})
    settings.update("gitlab", {"token": "synthetic-stored-token"})
    original_rules = manager.inbox_rules([{"id": "fixture-rule", "gl": "acme/shop", "on": ["new_note"], "enabled": True}])["rules"]
    features.save(onboarded=True)
    old_manager, old_port = piperd.MANAGER, piperd.PORT
    server = piperd.ThreadingHTTPServer(("127.0.0.1", 0), piperd.Handler)
    piperd.MANAGER, piperd.PORT = manager, server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    browser = "settings-regression-%s" % os.getpid()
    checks, screenshots = [], []
    verify_calls = []

    def opener(request, timeout):
        case.assertEqual(request.get_header("Private-token"), "synthetic-stored-token")
        verify_calls.append(True)
        return io.BytesIO(b'{"username":"fixture","name":"Synthetic user"}')
    manager.gitlab_opener = opener

    def cli(*args):
        result = subprocess.run(["agent-browser", "--session", browser, "--json", *args], capture_output=True, text=True, timeout=30)
        case.assertEqual(result.returncode, 0, (result.stdout + result.stderr).replace(common.own_token("gui"), "[token]"))
        data = json.loads(result.stdout)
        case.assertTrue(data["success"], data)
        return data.get("data", {}).get("result")

    def ready(code):
        until = time.monotonic() + 20
        while time.monotonic() < until:
            if cli("eval", code) is True:
                return
            time.sleep(0.1)
        raise AssertionError("Browser condition timed out: " + code)

    def check(name, condition=True):
        case.assertTrue(condition, name)
        checks.append(name)
        print("CHECK " + name, flush=True)

    def shot(name):
        path = Path(os.environ["TEST_SANDBOX_WORK"], "settings-" + name + ".png")
        if name.startswith("verify-"):
            cli("eval", "document.querySelectorAll('.toast .tclose').forEach(b=>b.click());true")
            cli("eval", "document.querySelector('section[data-sid=gitlab] .st-bar').scrollIntoView({block:'center'});true")
        cli("screenshot", str(path))
        screenshots.append(name)
        # Export before the mandatory sandbox deletes its private workspace.
        if os.environ.get("PIPER_E2E_EXPORT_PNG") == "1":
            print("SCREENSHOT " + json.dumps({"name": "settings-" + name + ".png", "data": base64.b64encode(path.read_bytes()).decode()}), flush=True)

    def section(sid):
        cli("click", '#actbar [aria-label="Settings"]')
        ready('!!document.querySelector(\'.settings [data-nav="%s"]\')' % sid)
        cli("click", '.settings [data-nav="%s"]' % sid)
        ready('!!document.querySelector(\'section[data-sid="%s"]\')' % sid)

    def button(text, parent="document"):
        # The visible existing button is selected by its product copy, not a fixture hook.
        cli("eval", "Array.from(%s.querySelectorAll('button')).find(b=>b.textContent.trim()===%s)?.click();true" % (parent, json.dumps(text)))

    def open_db():
        cli("click", '#actbar [aria-label="All tools"]')
        ready('!!document.querySelector(\'.nav-tools [data-menu-id="db"]\')')
        cli("click", '.nav-tools [data-menu-id="db"]:not(.msecondary)')
        ready('!!document.querySelector(".db-connpage")')

    def add_ssh():
        button("Add connection", "document.querySelector('.db-connpage')")
        ready('!!document.querySelector(".db-form [name=via]")')
        cli("click", ".db-form button[name=project]")
        ready('!!document.querySelector(\'[role="option"]\')')
        cli("eval", "Array.from(document.querySelectorAll('[role=option]')).find(o=>o.textContent.includes(%s))?.click();true" % json.dumps(key))
        ready('!!document.querySelector(".db-form [name=via] option[value=ssh]")')
        cli("select", ".db-form [name=via]", "ssh")

    try:
        cli("open", piperd.gui_url())
        cli("set", "viewport", "1280", "900")
        ready('!!document.querySelector(\'#actbar [aria-label="Settings"]\')')
        cli("eval", 'window.settingsErrors=[];window.addEventListener("error",e=>settingsErrors.push(e.message));window.addEventListener("unhandledrejection",e=>settingsErrors.push(String(e.reason)));true')
        section("interface")
        cli("select", "#st-interface-theme", "light")
        settings.update("interface", {"theme": "indigo", "restore_view": False})
        cli("click", 'section[data-sid="interface"] .st-save')
        ready('document.querySelector(".st-note")?.textContent.includes("Your draft is kept")===true')
        check("F01 draft survives conflict", cli("eval", 'document.querySelector("#st-interface-theme").value') == "light")
        check("F01 dirty save remains enabled", cli("eval", 'document.querySelector(".st-save").disabled') is False)
        check("F01 unrelated external field rebased", cli("eval", 'document.querySelector("#st-interface-restore_view").checked') is False)
        check("F01 conflict toast describes kept draft without reload advice", cli("eval", "Array.from(document.querySelectorAll('.toast span')).some(t=>t.textContent.includes('Your draft is kept')&&!t.textContent.includes('reload'))") is True)
        shot("conflict-keeps-draft")
        cli("click", 'section[data-sid="interface"] .st-save')
        ready('document.querySelector(".st-note")?.textContent.startsWith("Saved:")===true')
        interface = next(s for s in settings.describe()["sections"] if s["id"] == "interface")
        values = {f["key"]: f["value"] for f in interface["fields"]}
        check("F01 explicit retry saves only own change", values["theme"] == "light" and values["restore_view"] is False)
        cli("select", "#st-interface-theme", "dark")
        settings.update("interface", {"theme": "indigo"})
        cli("click", 'section[data-sid="interface"] .st-save')
        ready('document.querySelector(".st-note")?.textContent.includes("Your draft is kept")===true')
        cli("click", 'section[data-sid="interface"] .st-undo')
        check("F01 Discard uses current saved value", cli("eval", 'document.querySelector("#st-interface-theme").value') == "indigo")

        section("gitlab")
        cli("fill", "#st-gitlab-token", "synthetic-new-draft")
        button("Verify saved token")
        ready('document.querySelector(".st-note")?.textContent.includes("Save the token change first")===true')
        check("F02 draft is not verified or sent", not verify_calls)
        check("F02 token draft preserved", cli("eval", 'document.querySelector("#st-gitlab-token").value') == "synthetic-new-draft")
        shot("verify-draft-save-first")
        cli("click", 'section[data-sid="gitlab"] .st-undo')
        button("Verify saved token")
        ready('document.querySelector(".st-note")?.textContent.includes("The saved token belongs to user fixture")===true')
        check("F02 explicitly verifies stored token", len(verify_calls) == 1)
        shot("verify-saved-token")
        settings.update("gitlab", {}, clear=["token"])
        cli("fill", "#st-gitlab-token", "synthetic-first-token")
        button("Verify saved token")
        ready('document.querySelector(".st-note")?.textContent.includes("Save the token change first")===true')
        check("F02 missing stored token also requires Save first", len(verify_calls) == 1)
        cli("click", 'section[data-sid="gitlab"] .st-undo')

        # Load the existing Inbox data before opening its normal rules editor.
        cli("eval", "Array.from(document.querySelectorAll('#actbar button')).find(b=>['Inbox','GitLab'].includes(b.getAttribute('aria-label')))?.click();true")
        ready('!!document.querySelector(".inbox")')
        section("automatika")
        button("Edit rules…")
        ready('!!document.querySelector(".rules .rule")')
        cli("eval", "Array.from(document.querySelectorAll('.rule')).find(r=>r.textContent.includes('acme/settings-regression')).querySelectorAll('.rule-events input')[1].click();true")
        manager.inbox_rules([dict(original_rules[0], enabled=False)])
        button("Save", "document.querySelector('.olayer:last-child')")
        ready('document.querySelector(".rules + p")?.textContent.includes("changed meanwhile")===true')
        check("F03 concurrent disable remains durable", common.read_json(inbox.RULES_FILE, {})["rules"][0]["enabled"] is False)
        check("F03 editor retains new trigger", cli("eval", "Array.from(document.querySelectorAll('.rule')).find(r=>r.textContent.includes('acme/settings-regression')).querySelectorAll('.rule-events input')[1].checked") is True)
        shot("automation-conflict-keeps-draft")
        button("Cancel", "document.querySelector('.olayer:last-child')")

        # A real GUI decision stores the session grant; subsequent language changes reuse it.
        # The earlier direct second-actor writes must be reflected in the manager before
        # measuring this separate grant's lifetime, just as the production route reloads.
        manager.reload_policy()
        owner = sessions.Session(project=key, path=root, state="ready", title="Synthetic settings owner", gitlab=["acme/shop"])
        owner.save()
        manager.sessions[owner.id] = owner
        manager.bump()
        outcome = []
        def change_language(value):
            try:
                outcome.append(manager.settings_tool(owner, "piper_settings_change", {"section":"sessions", "values":{"language":value}}))
            except Exception as e:
                outcome.append(str(e))
        thread = threading.Thread(target=change_language, args=("First synthetic language",), daemon=True)
        thread.start()
        ready('!!document.querySelector("#pending .psplit .pcaret")')
        cli("click", "#pending .psplit .pcaret")
        ready('Array.from(document.querySelectorAll("[role=menuitem]")).some(b=>b.textContent.includes("Allow for this session"))')
        shot("language-session-grant")
        cli("eval", "Array.from(document.querySelectorAll('[role=menuitem]')).find(b=>b.textContent.includes('Allow for this session')).click();true")
        thread.join(5)
        check("F04 GUI grant survived approved language save", manager.approval_scope("settings_change", {}) in owner.approvals)
        change_language("Second synthetic language")
        check("F04 second same-owner change needs no new card", len(outcome) == 2 and all(isinstance(o, dict) for o in outcome) and not manager.pending_list())
        check("F04 second language is durable", common.read_json(common.POLICY_FILE, {})["language"] == "Second synthetic language")

        # The real agent spawn path uses remembered options; no external model or privileges exercised.
        projects.remember_session_defaults(key, "claude", {"docker":True,"unrestricted":True,"idea":True})
        advisor = sessions.Session(kind="advisor", project="advisor", path=common.ADVISOR_DIR, state="ready")
        with mock.patch.object(manager, "_start"):
            child = manager.tool(advisor, "spawn_session", {"project":key, "task":"Synthetic startup options"})
        created = manager.get(child["id"])
        check("F05 omitted spawn opts use project defaults", created.opts == {"docker":True,"unrestricted":True,"idea":True})
        check("F05 durable opts match remembered defaults", common.read_json(os.path.join(created.dir,"meta.json"), {})["opts"] == created.opts)

        open_db()
        add_ssh()
        ready('document.querySelector(".db-form [name=ssh_user]").value==="before-user"')
        cli("fill", ".db-form [name=ssh_user]", "manual-draft-user")
        # Invoke the real main Settings controls while preserving its existing DB modal.
        # Programmatic DOM activation is a regression seam for the metadata notification;
        # normal Save -> Add below exercises the visible user flow without a reload.
        cli("eval", "document.querySelector('#actbar [aria-label=Settings]').click();true")
        ready('!!document.querySelector(".settings [data-nav=databases]")')
        cli("eval", "document.querySelector('.settings [data-nav=databases]').click();true")
        ready('!!document.querySelector("#st-databases-ssh_user")')
        cli("eval", "const input=document.querySelector('#st-databases-ssh_user');input.value='after-user';input.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('section[data-sid=databases] .st-save').click();true")
        ready('document.querySelector("section[data-sid=databases] .st-note")?.textContent.startsWith("Saved:")===true')
        check("F06 open connection draft remains unchanged", cli("eval", 'document.querySelector(".db-form [name=ssh_user]").value') == "manual-draft-user")
        check("F06 new SSH default is durable", manager.db_config()["ssh_user"] == "after-user")
        shot("ssh-existing-draft-kept")
        button("Cancel", "document.querySelector('.olayer:last-child')")
        open_db()
        add_ssh()
        ready('document.querySelector(".db-form [name=ssh_user]").value==="after-user"')
        check("F06 cached Database view uses saved default without reload")
        shot("ssh-new-default-no-reload")
        button("Cancel", "document.querySelector('.olayer:last-child')")
        check("no unhandled browser errors", cli("eval", "window.settingsErrors") == [])
        print("RESULT " + json.dumps({"checks":checks,"screenshots":screenshots,"error":None}), flush=True)
    finally:
        subprocess.run(["agent-browser", "--session", browser, "close"], capture_output=True, timeout=20)
        server.shutdown()
        server.server_close()
        piperd.MANAGER, piperd.PORT = old_manager, old_port
        _env.clear_sessions(manager)
        case.doCleanups()


if __name__ == "__main__":
    run()

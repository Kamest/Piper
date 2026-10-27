"""common.py: the scrubber, the bidi / invisible-character display and friends."""
import importlib.util
import os
import re
import unittest

import _env
import common



class CommonTest(unittest.TestCase):
    def test_gui_database_proxy_budget_covers_supported_timeout_and_both_write_phases(self):
        for method, path in [('POST', '/api/db/query'), ('POST', '/api/db/execute'),
                             ('POST', '/api/db/tx'), ('GET', '/api/db/meta?refresh=1')]:
            with self.subTest(method=method, path=path):
                self.assertGreater(common.gui_proxy_timeout(method, path), 2 * 3600 + 30)
        self.assertEqual(common.gui_proxy_timeout('GET', '/api/state'), 28)
        self.assertEqual(common.gui_proxy_timeout('POST', '/api/db/cancel', default=120), 120)
    def test_a_card_shows_invisible_characters(self):
        """Review 3: show_bidi showed bidi controls but not zero-width characters or Unicode tags - a GitLab
        comment / SQL on a card could carry text the human never saw (tags spell hidden ASCII)."""
        hidden = "".join(chr(0xE0000 + ord(c)) for c in "rm -rf")
        self.assertEqual(common.show_bidi("ok\u200bx\ufeff" + hidden),
                         "ok[U+200B]x[U+FEFF]" + "".join("[U+%04X]" % (0xE0000 + ord(c)) for c in "rm -rf"))
        self.assertEqual(common.show_bidi({"a": ["\u2060"]}), {"a": ["[U+2060]"]})
        self.assertEqual(common.no_bidi("a\u200bb"), "a\u200bb")   # titles: only the bidi controls go
        # B-N5-1: ZWJ and variation selectors join emoji (as text.ts showBidi) - shown as they are; the rest stays masked
        fam = "\U0001F468\u200d\U0001F469\u200d\U0001F467 \u2764\ufe0f \u0031\ufe0f\u20e3"
        self.assertEqual(common.show_bidi("x " + fam), "x " + fam)
        self.assertEqual(common.show_bidi("\u200c\u202e\u2066"), "[U+200C][U+202E][U+2066]")

    def test_scrub_masks_a_bare_token(self):
        """A token pasted without a key name (no `token=`) used to pass through verbatim."""
        for tok in ("glpat-AbCdEf1234567890xyz", "ghp_0123456789abcdefABCDEF", "github_pat_11AAA_bbbCCC0123",
                    "xoxb-1234-5678-abcdEFGH", "gldt-abcdefgh12345"):
            out = common.scrub("see %s here" % tok)
            self.assertNotIn(tok, out)
            self.assertIn("[scrubbed]", out)
            self.assertTrue(out.startswith("see ") and out.endswith(" here"))
        self.assertEqual(common.scrub("ghp_short"), "ghp_short")   # too short to be a token
        self.assertEqual(common.scrub("a glpat-like word"), "a glpat-like word")

    def test_scrub_is_linear_on_hostile_input(self):
        """Review 2026-10-07: `[\\w.-]*` before the key word backtracked on every dash - 16 kB of "a-"
        took 3.4 s (minutes for 64 kB), and http bodies / DB cells are scrubbed in piperd's threads."""
        import time
        for text in ("a-" * 65536, "x_" * 65536, "password" * 16000, "a." * 65536):
            t0 = time.time()
            common.scrub(text)
            self.assertLess(time.time() - t0, 0.5, text[:10])
        for line, want in (("spring.datasource.password = s3cr3t", "spring.datasource.password = [scrubbed]"),
                           ("MY_SECRET_TOKEN: xyz", "MY_SECRET_TOKEN: [scrubbed]"),
                           ("x.y.z.private_key=AAA", "x.y.z.private_key=[scrubbed]")):
            self.assertEqual(common.scrub(line), want)

    def test_scrub_masks_every_common_secret_shape(self):
        """Round 11: env names glued to the key word, CLI flags with a space, quoted JSON values,
        private key blocks, AWS key ids, JWTs and mysql -pSECRET all passed through verbatim."""
        cases = (
            ("PGPASSWORD=s3cr3t psql", "s3cr3t"), ("export MYSQL_PWD=s3cr3t", "s3cr3t"),
            ("DB_PASSWORD=s3cr3t", "s3cr3t"), ("export X_TOKEN=s3cr3t", "s3cr3t"),
            ("pg_dump --password s3cr3t db", "s3cr3t"), ("tool --api-key s3cr3t", "s3cr3t"),
            ("mysql -u root -ps3cr3t shop", "s3cr3t"), ("mysqldump -h db -ps3cr3t x", "s3cr3t"),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("https://u:s3cr3t@host/x", "s3cr3t"), ("redis://:s3cr3t@host:6379", "s3cr3t"),
            ('{"password": "two words", "user": "x"}', "two words"), ('{"api_key":"s3cr3t"}', "s3cr3t"),
            ("{'secret': 's3cr3t'}", "s3cr3t"),
            ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nQUJD==\n-----END RSA PRIVATE KEY-----",
             "MIIEowIBAAKCAQEA"),
            ('"k": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBg\\n-----END PRIVATE KEY-----"', "MIIEvQIBADANBg"),
            ("id AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
            ("t eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end", "c2lnbmF0dXJl"),
        )
        for line, secret in cases:
            out = common.scrub(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("[scrubbed]", out, line)
        self.assertIn("-----END RSA PRIVATE KEY-----", common.scrub(cases[14][0]))
        self.assertTrue(common.scrub("t eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl end").endswith(" end"))
        for benign in ("endpoint = https://minio.example.com:9000", "secretName: minio-creds", "container acme-admin  Started",
                       "mkdir -p /tmp/x", "the token expired", "ls -pla", "mysql -P 3306 -h db", "mysql -p shop",
                       "Passwords must be long.", "eyJ is a prefix", "AKIA prefix", "git log --pretty", "x = a - b",
                       "grep -pfoo file", "mysecret is here"):
            self.assertEqual(common.scrub(benign), benign)

    def test_every_scrub_rule_is_linear(self):
        import time
        for text in ("a-" * 65536, "a." * 65536, "://:" * 32000, "://u:" * 26000, "--password " * 12000,
                     "mysql -p" * 16000, "-----BEGIN PRIVATE KEY-----" * 4800, "eyJaaaaa." * 14000,
                     '"password": "' * 10000, "PGPASSWORD" * 13000, "mysql \n" * 18000, "AKIA" * 32000,
                     "a-" * 65536 + "password=x", "password: " * 13000,
                     "-eyJ" * 32768, "a-eyJ" * 26000, "_eyJabcde" * 14000):   # review 3: the JWT rule was quadratic
            # CPU time, not wall clock: a loaded machine stretched 0.3 s to 0.57 s (round 13); every quadratic
            # rule seen so far took >= 3.4 s here, so 1 s still catches them
            t0 = time.process_time()
            common.scrub(text)
            self.assertLess(time.process_time() - t0, 1.0, text[:12])

    def test_names_from_before_the_rename_still_work(self):
        """without PIPER_HOME an existing ~/.hole-orch stays the home until ~/.piper
        exists, and ORCH_* variables fill the PIPER_* ones they name."""
        import subprocess
        import sys
        import tempfile
        home = tempfile.mkdtemp(dir=_env.ROOT)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPER_", "ORCH_"))}
        env.update(PIPER_REAL_HOME=home, ORCH_PORT="17001", ORCH_BIND="127.0.0.2", PIPER_BIND="127.0.0.3")

        def run():
            return subprocess.run([sys.executable, "-c", "import os, common; print(common.PIPER_HOME, "
                                   "os.environ['PIPER_PORT'], os.environ['PIPER_BIND'])"],
                                  cwd=os.path.dirname(common.__file__), env=env, capture_output=True,
                                  text=True, check=True).stdout.split()
        self.assertEqual(run(), [os.path.join(home, ".piper"), "17001", "127.0.0.3"])   # PIPER_* wins
        os.mkdir(os.path.join(home, ".hole-orch"))
        self.assertEqual(run()[0], os.path.join(home, ".hole-orch"))
        os.mkdir(os.path.join(home, ".piper"))
        self.assertEqual(run()[0], os.path.join(home, ".piper"))

    def test_the_old_home_moves_to_piper_once_nothing_runs_from_it(self):
        """piperd's first start with no live ptyhost moves ~/.hole-orch to ~/.piper, leaves a
        symlink, rebases the session records and the Global agent's transcript dirs; a live ptyhost
        (its socket answers) keeps the old home."""
        import socket
        import subprocess
        import sys
        import tempfile
        home = tempfile.mkdtemp(dir=_env.ROOT)
        old, new = os.path.join(home, ".hole-orch"), os.path.join(home, ".piper")
        enc = lambda p: re.sub(r"[^A-Za-z0-9]", "-", p)   # noqa: E731
        os.makedirs(os.path.join(old, "sessions", "a1", "claude", "projects", enc(old + "/advisor")))
        os.makedirs(os.path.join(home, ".claude", "projects", enc(old + "/advisor")))
        common.write_json(os.path.join(old, "sessions", "a1", "meta.json"),
                          {"id": "a1", "path": old + "/advisor", "mount_roots": [old + "/advisor", "/p/x"],
                           "coverage": [{"root": old + "x"}]})
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PIPER_", "ORCH_"))}
        env.update(PIPER_REAL_HOME=home, PIPER_HOME=old)   # what an old service file bakes in

        def run():
            return subprocess.run([sys.executable, "-c", "import common; print(common.migrate_home() or '-', common.PIPER_HOME)"],
                                  cwd=os.path.dirname(common.__file__), env=env, capture_output=True,
                                  text=True, check=True).stdout.split()
        live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            live.bind(os.path.join(old, "ptyhost.sock"))
            live.listen(1)
        except OSError:
            live.close()
            live = None   # no AF_UNIX bind here (sandbox): the busy case stays untested
        if live is not None:
            self.assertEqual(run(), ["busy", old])
            live.close()
            os.unlink(os.path.join(old, "ptyhost.sock"))
        self.assertEqual(run(), ["moved", old])
        self.assertTrue(os.path.islink(old) and os.path.realpath(old) == os.path.realpath(new))
        self.assertEqual(run(), ["-", new])   # the restarted piperd: done, new home
        meta = common.read_json(os.path.join(new, "sessions", "a1", "meta.json"), {})
        self.assertEqual((meta["path"], meta["mount_roots"]), (new + "/advisor", [new + "/advisor", "/p/x"]))
        self.assertEqual(meta["coverage"], [{"root": old + "x"}])   # only the old dir itself, not a prefix
        self.assertTrue(os.path.isdir(os.path.join(new, "sessions", "a1", "claude", "projects", enc(new + "/advisor"))))
        link = os.path.join(home, ".claude", "projects", enc(new + "/advisor"))
        self.assertTrue(os.path.islink(link))   # the user's own ~/.claude: a link, the dir itself stays
        self.assertTrue(os.path.isdir(os.path.join(home, ".claude", "projects", enc(old + "/advisor"))))
        with open(os.path.join(new, "audit.log")) as f:
            self.assertIn('"home-migrated"', f.read())

    def test_a_broken_json_file_is_reported_and_copied_once(self):
        """projects.json is read on every /api/state: a broken one is reported once, not every poll."""
        import contextlib
        import io
        import tempfile
        path = os.path.join(tempfile.mkdtemp(dir=_env.ROOT), "projects.json")
        with open(path, "w") as f:
            f.write("{broken")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(common.read_json(path, {}), {})
            os.unlink(path + ".corrupt")
            self.assertEqual(common.read_json(path, {}), {})
        self.assertEqual(out.getvalue().count("not valid JSON"), 1)
        self.assertFalse(os.path.exists(path + ".corrupt"))   # not copied again
        with open(path, "w") as f:
            f.write("{broken again")   # another breakage: reported again
        with contextlib.redirect_stdout(out):
            common.read_json(path, {})
        self.assertEqual(out.getvalue().count("not valid JSON"), 2)
        self.assertTrue(os.path.exists(path + ".corrupt"))

    def test_tokens_and_audit_modes(self):
        tok = common.mint_token("t")
        self.assertIn(tok, common.load_tokens())
        self.assertEqual(os.stat(common.TOKENS_FILE).st_mode & 0o777, 0o600)
        common.audit({"event": "x"})
        self.assertEqual(os.stat(common.AUDIT_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(common.PIPER_HOME).st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()

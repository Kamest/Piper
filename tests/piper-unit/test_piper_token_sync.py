"""piper.sh copies a GitLab token from the env only into an empty Piper home, never over a stored one."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import _env

ROOT = Path(__file__).resolve().parents[2]


class PiperTokenSyncTest(unittest.TestCase):
    def run_home(self, stored=None, command="mattermost-webhook", stdin="", **env_tokens):
        """Run a piper.sh command; return (stderr, stored token or None, its mode)."""
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "piper"
            if stored is not None:
                home.mkdir(mode=0o700)
                (home / "gitlab-token").write_text(stored)
            env = {k: v for k, v in os.environ.items() if k not in ("GITLAB_TOKEN", "GITLAB_PAT")}
            env.update(env_tokens, PIPER_HOME=str(home))
            result = subprocess.run(["sh", str(ROOT / "scripts/piper.sh"), command],
                                    env=env, input=stdin, text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            token = home / "gitlab-token"
            mode = token.stat().st_mode & 0o777 if token.exists() else None
            return result.stderr, (token.read_text() if token.exists() else None), mode

    def test_env_token_fills_an_empty_home(self):
        err, token, mode = self.run_home(GITLAB_PAT="glpat-env")
        self.assertEqual(token, "glpat-env")
        self.assertEqual(mode, 0o600)
        self.assertIn("synced from env", err)

    def test_stored_token_is_not_overwritten_by_a_different_env_token(self):
        # regression: a stale GITLAB_PAT in a non-interactive shell replaced the token set in the GUI
        err, token, _ = self.run_home(stored="glpat-gui", GITLAB_PAT="glpat-stale")
        self.assertEqual(token, "glpat-gui")
        self.assertIn("differs", err)
        self.assertNotIn("glpat-stale", err)
        self.assertNotIn("glpat-gui", err)

    def test_gitlab_token_command_replaces_the_stored_token_without_the_env_warning(self):
        err, token, mode = self.run_home(stored="glpat-old", command="gitlab-token", stdin="glpat-new\n",
                                         GITLAB_PAT="glpat-stale")
        self.assertEqual(token, "glpat-new")
        self.assertEqual(mode, 0o600)
        self.assertNotIn("differs", err)

    def test_matching_env_token_is_silent(self):
        err, token, _ = self.run_home(stored="glpat-same", GITLAB_TOKEN="glpat-same")
        self.assertEqual(token, "glpat-same")
        self.assertNotIn("gitlab-token", err)


if __name__ == "__main__":
    unittest.main()

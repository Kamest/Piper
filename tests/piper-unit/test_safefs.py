"""The session config dir is sandbox-writable: planted symlinks must never redirect piperd."""
import json
import os
import tempfile
import unittest

import _env  # noqa: F401
import common
import digest
import sessioncfg


class SafeFsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        self.secret = os.path.join(self.outside, "secret")
        with open(self.secret, "w") as f:
            f.write("TOP-SECRET")

    def test_read_refuses_symlinked_file_and_parent(self):
        os.symlink(self.secret, os.path.join(self.cfg, "piper-events.jsonl"))
        with self.assertRaises(OSError):
            common.safe_read(self.cfg, "piper-events.jsonl")
        os.symlink(self.outside, os.path.join(self.cfg, "projects"))
        with self.assertRaises(OSError):
            common.safe_read(self.cfg, "projects/secret")
        with self.assertRaises(OSError):
            common.safe_read(self.cfg, "../" + os.path.basename(self.outside) + "/secret")

    def test_write_replaces_planted_symlink_without_following(self):
        os.makedirs(os.path.join(self.cfg, "piper-mailbox", "in"))
        target = os.path.join(self.cfg, "piper-mailbox", "in", "abc.json")
        os.symlink(self.secret, target)
        common.safe_write(self.cfg, "piper-mailbox/in/abc.json", b'{"ok":true}')
        self.assertEqual(open(self.secret).read(), "TOP-SECRET")
        self.assertFalse(os.path.islink(target))
        self.assertEqual(json.load(open(target)), {"ok": True})

    def test_write_through_symlinked_dir_refused(self):
        os.makedirs(os.path.join(self.cfg, "piper-mailbox"))
        os.symlink(self.outside, os.path.join(self.cfg, "piper-mailbox", "in"))
        with self.assertRaises(OSError):
            common.safe_write(self.cfg, "piper-mailbox/in/x.json", b"{}")
        self.assertEqual(sorted(os.listdir(self.outside)), ["secret"])

    def test_credentials_copy_back_refuses_symlink(self):
        os.symlink(self.secret, os.path.join(self.cfg, ".credentials.json"))
        self.assertFalse(sessioncfg.copy_back_credentials(self.cfg, ""))
        self.assertFalse(os.path.exists(os.path.join(common.TEMPLATE_DIR, ".credentials.json")))

    def test_digest_ignores_symlinked_transcript(self):
        d = os.path.join(self.cfg, "projects", "-p")
        os.makedirs(d)
        with open(os.path.join(self.outside, "t.jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "other session secret"}}) + "\n")
        os.symlink(os.path.join(self.outside, "t.jsonl"), os.path.join(d, "s.jsonl"))
        self.assertEqual(digest.digest(self.cfg, None), [])
        with open(os.path.join(d, "real.jsonl"), "w") as f:
            f.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi token=abc"}]}}) + "\n")
        self.assertEqual(digest.digest(self.cfg, None), [{"role": "assistant", "text": "hi token=[scrubbed]", "ts": None}])

    def test_rebuild_starts_fresh_and_keeps_only_transcripts_and_login(self):
        cfg = os.path.join(tempfile.mkdtemp(), "claude")
        sessioncfg.build(cfg, "/home/in", "session")
        os.makedirs(os.path.join(cfg, "projects", "-p"))
        open(os.path.join(cfg, "projects", "-p", "a.jsonl"), "w").close()
        os.makedirs(os.path.join(cfg, ".hole"))
        open(os.path.join(cfg, ".hole", "settings.json"), "w").write("{broken")
        os.symlink(self.outside, os.path.join(cfg, "skills"))
        with open(os.path.join(cfg, ".credentials.json"), "w") as f:
            f.write('{"claudeAiOauth": {}}')
        sessioncfg.build(cfg, "/home/in", "session")
        self.assertTrue(os.path.exists(os.path.join(cfg, "projects", "-p", "a.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(cfg, ".hole")))
        self.assertFalse(os.path.islink(os.path.join(cfg, "skills")))
        self.assertEqual(json.load(open(os.path.join(cfg, ".credentials.json"))), {"claudeAiOauth": {}})
        self.assertEqual(sorted(os.listdir(self.outside)), ["secret"])
        self.assertEqual([n for n in os.listdir(os.path.dirname(cfg)) if ".prev-" in n], [])


class JournalTest(unittest.TestCase):
    """overwrite_in_place: a file hidden from Claude is rewritten in its own inode (an over-mount stays),
    and a redo journal under $PIPER_HOME makes that crash-safe."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.f = os.path.join(self.root, "secret.properties")
        with open(self.f, "wb") as fh:
            fh.write(b"db.password=old\n")
        os.chmod(self.f, 0o640)
        import shutil
        for d in (common.JOURNAL_DIR, common.RECOVERED_DIR):
            shutil.rmtree(d, ignore_errors=True)

    def _crash(self, new: bytes, file_now: bytes | None, old: bytes = b"db.password=old\n"):
        """the journal of a write that died after its commit record, the file as the crash left it"""
        from unittest import mock
        with mock.patch.object(common, "safe_write", side_effect=OSError("crash")):
            with self.assertRaises(OSError):
                common.overwrite_in_place(self.root, "secret.properties", new, old, mode=0o640, exact_mode=True)
        if file_now is None:
            os.unlink(self.f)
        else:
            with open(self.f, "r+b") as fh:   # in place, like the interrupted write
                fh.truncate(0)
                fh.write(file_now)

    def test_in_place_keeps_the_inode_and_leaves_no_journal(self):
        ino = os.stat(self.f).st_ino
        common.overwrite_in_place(self.root, "secret.properties", b"db.password=new\n", b"db.password=old\n",
                                  mode=0o640, exact_mode=True)
        self.assertEqual(open(self.f, "rb").read(), b"db.password=new\n")
        self.assertEqual((os.stat(self.f).st_ino, os.stat(self.f).st_mode & 0o777), (ino, 0o640))
        self.assertEqual(os.listdir(self.root), ["secret.properties"])   # never a tmp file next to it
        self.assertEqual(os.listdir(common.JOURNAL_DIR), [])

    def test_a_read_only_file_is_rewritten_in_its_inode(self):
        """review 2026-10-08: in place, a 0444 file (ours) gets u+w on its own inode for the write and its
        mode back after - no tmp+rename (that drops an over-mount); a link is never followed."""
        os.chmod(self.f, 0o444)
        ino = os.stat(self.f).st_ino
        common.overwrite_in_place(self.root, "secret.properties", b"db.password=new\n", b"db.password=old\n",
                                  mode=0o444, exact_mode=True)
        self.assertEqual(open(self.f, "rb").read(), b"db.password=new\n")
        self.assertEqual((os.stat(self.f).st_ino, os.stat(self.f).st_mode & 0o7777), (ino, 0o444))
        self.assertEqual(os.listdir(self.root), ["secret.properties"])
        common.safe_write(self.root, "secret.properties", b"plain\n", in_place=True)   # no exact_mode: mode kept
        self.assertEqual(open(self.f, "rb").read(), b"plain\n")
        self.assertEqual((os.stat(self.f).st_ino, os.stat(self.f).st_mode & 0o7777), (ino, 0o444))
        other = tempfile.mkdtemp()
        target = os.path.join(other, "t")
        with open(target, "wb") as fh:
            fh.write(b"outside\n")
        os.chmod(target, 0o444)
        os.symlink(target, os.path.join(self.root, "link"))
        with self.assertRaises(OSError):
            common.safe_write(self.root, "link", b"x", in_place=True)
        self.assertEqual((open(target, "rb").read(), os.stat(target).st_mode & 0o777), (b"outside\n", 0o444))
        os.mkfifo(os.path.join(self.root, "fifo"), 0o444)             # neither hangs nor gets written
        with self.assertRaises(OSError):
            common.safe_write(self.root, "fifo", b"x", in_place=True)
        self.assertEqual(os.stat(os.path.join(self.root, "fifo")).st_mode & 0o777, 0o444)

    def test_recover_completes_a_write_cut_short(self):
        for now in (b"db.pass", b"", b"db.password=old\n"):   # a beginning of the new / untouched
            self._crash(b"db.password=new\n", now)
            ino = os.stat(self.f).st_ino
            r = common.journal_recover()
            self.assertEqual([x["outcome"] for x in r], ["completed"], now)
            self.assertEqual(open(self.f, "rb").read(), b"db.password=new\n")
            self.assertEqual(os.stat(self.f).st_ino, ino)
            self.assertEqual(os.listdir(common.JOURNAL_DIR), [])
            with open(self.f, "wb") as fh:
                fh.write(b"db.password=old\n")

    def test_recover_never_overwrites_a_later_change(self):
        self._crash(b"db.password=new\n", b"edited by hand meanwhile\n")
        r = common.journal_recover()
        self.assertEqual([x["outcome"] for x in r], ["kept"])
        self.assertEqual(open(self.f, "rb").read(), b"edited by hand meanwhile\n")
        kept = sorted(os.listdir(common.RECOVERED_DIR))
        self.assertEqual([n.rsplit(".", 1)[1] for n in kept], ["json", "new", "old"])   # both versions kept
        self.assertNotIn("db.password", json.dumps(r))                                 # the audit: names only

    def test_recover_never_writes_into_a_stand_in(self):
        """review 2026-10-08: a sandbox could replace the file (or its directory) with an empty stand-in
        while piperd was down - b"" is a prefix of anything; the pinned identity tells them apart."""
        self._crash(b"db.password=new\n", b"")
        os.rename(self.f, self.f + ".aside")   # the old inode stays alive: ext4/overlayfs (CI) would hand an
        open(self.f, "wb").close()             # unlinked one straight back to the new file - same path, new inode
        self.assertEqual([x["outcome"] for x in common.journal_recover()], ["kept"])
        self.assertEqual(open(self.f, "rb").read(), b"")

    def test_recover_done_gone_and_orphans(self):
        self._crash(b"db.password=new\n", b"db.password=new\n")
        self.assertEqual([x["outcome"] for x in common.journal_recover()], ["done"])
        self._crash(b"db.password=new\n", None)                           # deleted meanwhile: not recreated
        self.assertEqual([x["outcome"] for x in common.journal_recover()], ["kept"])
        self.assertFalse(os.path.exists(self.f))
        with open(os.path.join(common.JOURNAL_DIR, "x.new"), "wb") as fh:   # no commit record: never started
            fh.write(b"half")
        common.journal_recover()
        self.assertEqual(os.listdir(common.JOURNAL_DIR), [])



if __name__ == "__main__":
    unittest.main()

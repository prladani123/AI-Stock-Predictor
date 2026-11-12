"""A write cut short (a crash, Ctrl-C, a full disk) must leave the file as it was: every tool writes a
temp file next to it and swaps it in, so a half-written state file never breaks the next run."""
import json
import os
import shutil
import stat
import tempfile
import unittest

from _load import load

WRITERS = [  # (skill folder, module, the function that writes its JSON)
    ("bharat-news", "news", "save_state"),
    ("bharat-insider", "insider", "save_state"),
    ("bharat-entry", "entry", "write_json"),
    ("bharat-exit", "exit", "write_json"),
    ("bharat-scanner", "scan", "write_json"),
    ("bharat-timemachine", "timemachine", "_write"),
    ("bharat-alerts", "alerts", "_write"),
    ("bharat-broker", "ticket", "write_json"),
    ("india", "india", "write_json"),
]


class InterruptedWrite(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def test_a_write_that_fails_halfway_keeps_the_old_file_and_leaves_no_temp_file(self):
        for folder, mod, fn in WRITERS:
            write = getattr(load(folder, mod), fn)
            path = os.path.join(self.d, mod, "state.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                json.dump({"kept": True}, fh)
            # json.dump streams: the long "a" goes out, then object() cannot be written
            with self.assertRaises(TypeError):
                write(path, {"a": "x" * 20000, "b": object()})
            with open(path) as fh:
                self.assertEqual(json.load(fh), {"kept": True}, "%s.%s" % (mod, fn))
            self.assertEqual(os.listdir(os.path.dirname(path)), ["state.json"], "%s.%s" % (mod, fn))

    def test_a_write_makes_its_folder_and_leaves_no_temp_file(self):
        for folder, mod, fn in WRITERS:
            write = getattr(load(folder, mod), fn)
            path = os.path.join(self.d, mod, "out.json")
            write(path, {"ok": 1})
            with open(path) as fh:
                self.assertEqual(json.load(fh), {"ok": 1}, mod)
            self.assertEqual(os.listdir(os.path.dirname(path)), ["out.json"], mod)

    def test_two_runs_writing_the_same_file_do_not_share_a_temp_file(self):
        # another run (a scheduled one, say) is halfway through its own temp file: it must be left alone
        for folder, mod, fn in WRITERS:
            write = getattr(load(folder, mod), fn)
            path = os.path.join(self.d, mod, "state.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            other = path + ".tmp"
            with open(other, "w") as fh:
                fh.write('{"half": ')
            write(path, {"mine": 1})
            with open(path) as fh:
                self.assertEqual(json.load(fh), {"mine": 1}, mod)
            with open(other) as fh:
                self.assertEqual(fh.read(), '{"half": ', "%s.%s wrote over another run's temp file" % (mod, fn))

    def test_a_write_keeps_the_files_mode(self):
        india = load("india", "india")
        path = os.path.join(self.d, "private.json")
        with open(path, "w") as fh:
            fh.write("{}")
        os.chmod(path, 0o640)
        india.write_json(path, {"a": 1})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o640)
        # a new file gets the usual mode for this umask, not the temp file's private 0600
        mask = os.umask(0o022)
        try:
            fresh = os.path.join(self.d, "new.json")
            india.write_json(fresh, {"a": 1})
        finally:
            os.umask(mask)
        self.assertEqual(stat.S_IMODE(os.stat(fresh).st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()

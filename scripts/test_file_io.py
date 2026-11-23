"""Shared file lock: python -B scripts/test_file_io.py. Standard library only; runs on any OS."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))

from file_io import exclusive_file_lock  # noqa: E402

HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
from file_io import exclusive_file_lock
with exclusive_file_lock(sys.argv[2]):
    print("locked", flush=True)
    sys.stdin.readline()  # hold until the test says release
"""


class ExclusiveFileLockTests(unittest.TestCase):
    def test_creates_the_lock_file_with_one_byte_and_is_reusable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config/accounts/store.lock"
            with exclusive_file_lock(path):
                pass
            # Windows refuses to read a locked byte, even from the same process.
            self.assertEqual(path.read_bytes(), b"0")
            with exclusive_file_lock(path):
                pass
            self.assertEqual(path.read_bytes(), b"0")

    def test_excludes_another_process_until_released(self):
        import threading
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "store.lock"
            acquired = threading.Event()

            def contend():
                with exclusive_file_lock(path):
                    acquired.set()

            with subprocess.Popen([sys.executable, "-B", "-c", HOLDER, str(ROOT / "agent"), str(path)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as holder:
                try:
                    self.assertEqual(holder.stdout.readline().strip(), "locked")
                    contender = threading.Thread(target=contend, daemon=True)
                    contender.start()
                    self.assertFalse(acquired.wait(0.5), "acquired while another process held the lock")
                finally:
                    holder.stdin.write("release\n")
                    holder.stdin.flush()
                self.assertTrue(acquired.wait(15), "never acquired after the holder released")
                contender.join(15)
                holder.wait(timeout=30)

    def test_account_and_report_locks_keep_their_paths(self):
        from unittest.mock import patch
        import account_state
        import adventure_report
        with patch("account_state.exclusive_file_lock") as account, \
                patch("adventure_report.exclusive_file_lock") as report:
            account_state.account_lock("/data")
            adventure_report._report_lock(Path("/data"))
        account.assert_called_once_with(Path("/data") / "config/accounts/store.lock")
        report.assert_called_once_with(Path("/data") / "config/adventure_reports/store.lock")


if __name__ == "__main__":
    unittest.main()

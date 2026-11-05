"""启动器崩溃保护的回归测试。

用假的 steam 程序和临时 HOME 跑真实的 scripts/launcher.sh，不碰真实 Steam。
需要 Linux 环境（真机上跑）：依赖 bash、flock、GNU stat。
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEGV = ('/home/deck/.local/share/Steam/steam.sh: line 995: 65827 Segmentation fault'
        '         (core dumped) "$STEAMROOT/$STEAMEXEPATH" "$@"')


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("flock"),
                     "需要 Linux + flock")
class LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.sls = self.home / ".local/share/SLSsteam"
        self.logs = self.home / ".local/share/Steam/logs"
        self.sls.mkdir(parents=True)
        self.logs.mkdir(parents=True)
        (self.home / "suos").mkdir()
        for lib in ("library-inject.so", "SLSsteam.so"):
            (self.sls / lib).write_text("x")
        self.console = self.logs / "console-linux.txt"
        self.failfile = self.home / ".SLSsteam-crash-count"
        self.seen = self.home / "seen_ld_audit"
        self.fake = self.home / "fake-steam"

        text = (ROOT / "scripts/launcher.sh").read_text(encoding="utf-8").replace("\r\n", "\n")
        for old, new in (('STEAMBIN="/usr/bin/steam"', f'STEAMBIN="{self.fake}"'),
                         ("MONITOR_SECS=30", "MONITOR_SECS=2"),
                         ("*/ubuntu12_32/steam)", "*/no-such-steam-binary)"),
                         ("__SUOS_DIR__", str(self.home / "suos"))):
            self.assertIn(old, text)
            text = text.replace(old, new)
        self.launcher = self.home / "launcher.sh"
        self.launcher.write_text(text, encoding="utf-8")

    def _fake(self, body: str) -> None:
        self.fake.write_text(f'#!/bin/bash\necho "${{LD_AUDIT:-none}}" >> "{self.seen}"\n{body}\n')
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)

    def _run(self) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if k not in ("LD_AUDIT", "SLS_NOINJECT")}
        env["HOME"] = str(self.home)
        env["SUOS_DIR"] = str(self.home / "suos")
        return subprocess.run(["bash", str(self.launcher)], env=env, timeout=30,
                              capture_output=True, text=True)

    def _count(self) -> int:
        return int(self.failfile.read_text()) if self.failfile.exists() else 0

    def _log(self) -> str:
        return (self.home / "suos/logs/launcher.log").read_text(encoding="utf-8")

    def _set_failfile(self, count: int, age: float) -> None:
        self.failfile.write_text(str(count))
        ts = time.time() + age
        os.utime(self.failfile, (ts, ts))

    def test_swallowed_segfault_counts_as_crash(self) -> None:
        self._fake(f"echo '{SEGV}' >> '{self.console}'\nexit 0")
        self._run()
        self.assertEqual(self._count(), 1)
        self.assertIn("崩溃（第 1 次）", self._log())

    def test_clean_quick_exit_is_not_a_crash(self) -> None:
        self._fake("exit 0")
        self.assertEqual(self._run().returncode, 0)
        self.assertEqual(self._count(), 0)

    def test_old_segfault_in_log_is_ignored(self) -> None:
        self.console.write_text(SEGV + "\n")
        self._fake("exit 0")
        self._run()
        self.assertEqual(self._count(), 0)

    def test_crash_after_log_rotation_is_seen(self) -> None:
        self.console.write_text("x" * 4096)
        self._fake(f"echo '{SEGV}' > '{self.console}'\nexit 0")
        self._run()
        self.assertEqual(self._count(), 1)

    def test_nonzero_exit_counts_as_crash(self) -> None:
        self._fake("exit 3")
        self.assertEqual(self._run().returncode, 3)
        self.assertEqual(self._count(), 1)

    def test_survivor_clears_count(self) -> None:
        self._set_failfile(2, 60)
        self._fake("sleep 4")
        self._run()
        self.assertFalse(self.failfile.exists())

    def test_crash_returns_quickly(self) -> None:
        self._fake("exit 3")
        launcher = self.launcher.read_text(encoding="utf-8").replace("MONITOR_SECS=2", "MONITOR_SECS=20")
        self.launcher.write_text(launcher, encoding="utf-8")
        start = time.monotonic()
        self._run()
        self.assertLess(time.monotonic() - start, 10, "崩了不该等满监控时长")

    def test_three_crashes_disable_injection(self) -> None:
        self._set_failfile(3, 60)
        self._fake("exit 0")
        self._run()
        self.assertEqual(self.seen.read_text().strip(), "none")

    def test_updated_library_retries_injection(self) -> None:
        self._set_failfile(3, -60)
        self._fake("exit 0")
        self._run()
        self.assertIn("SLSsteam.so", self.seen.read_text())
        self.assertFalse(self.failfile.exists())

    def test_concurrent_call_keeps_injection_but_not_monitoring(self) -> None:
        self._fake(f"echo '{SEGV}' >> '{self.console}'\nexit 0")
        lock = self.home / ".SLSsteam-launcher.lock"
        holder = subprocess.Popen(["flock", str(lock), "sleep", "8"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.terminate)
        time.sleep(0.3)
        self._run()
        self.assertIn("SLSsteam.so", self.seen.read_text())
        self.assertEqual(self._count(), 0, "没拿到锁的调用不能计数")


if __name__ == "__main__":
    unittest.main()

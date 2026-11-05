"""Sudama 缓存回归测试：套娃缓存能自愈，下载失败绝不污染缓存。

全部用临时目录，不碰真实缓存，也不访问网络。
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from suos import fetch, sudama  # noqa: E402


def key_table(extra: dict | None = None) -> dict:
    table = {str(i): f"{i:064x}" for i in range(1, sudama.MIN_ENTRIES + 5)}
    table.update(extra or {})
    return table


def wrap(data: dict, ts: float, layers: int) -> dict:
    out: dict = data
    for _ in range(layers):
        out = {"timestamp": ts, "data": out}
    return out


class SudamaCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for name, value in (("_CACHE_DIR", d),
                            ("_CACHE", d / "sudama_depotkeys.json"),
                            ("_RAW", d / "sudama_depotkeys.raw.json"),
                            ("_mem", None)):
            p = mock.patch.object(sudama, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.cache = d / "sudama_depotkeys.json"
        self.raw = d / "sudama_depotkeys.raw.json"

    def _no_refresh(self):
        p = mock.patch.object(sudama, "_refresh_in_background")
        started = p.start()
        self.addCleanup(p.stop)
        return started

    def test_nested_cache_is_read_and_healed(self) -> None:
        # 真机上的现场：套了 4 层，外层时间戳是刚写的，最内层才是真实下载时间
        real_ts = time.time() - 3600
        inner = wrap(key_table({"4012811": "ab" * 32}), real_ts, 1)
        self.cache.write_text(json.dumps(wrap(inner, time.time(), 3)))

        data = sudama.load()

        self.assertEqual(data["4012811"], "ab" * 32)
        self.assertGreaterEqual(len(data), sudama.MIN_ENTRIES)
        on_disk = json.loads(self.cache.read_text())
        self.assertEqual(sudama._unwrap(on_disk)[2], 1, "应该被改写成只有一层")
        self.assertAlmostEqual(on_disk["timestamp"], real_ts, delta=1)

    def test_outer_timestamp_cannot_fake_freshness(self) -> None:
        old = time.time() - sudama.CACHE_TTL - 3600
        inner = wrap(key_table(), old, 1)
        self.cache.write_text(json.dumps(wrap(inner, time.time(), 2)))
        refresh = self._no_refresh()

        self.assertTrue(sudama.load())
        refresh.assert_called_once()

    def test_stale_cache_returns_at_once_without_blocking_download(self) -> None:
        self.cache.write_text(json.dumps(wrap(key_table(), time.time() - sudama.CACHE_TTL - 1, 1)))
        refresh = self._no_refresh()
        with mock.patch.object(sudama, "_download") as dl:
            self.assertTrue(sudama.load())
        dl.assert_not_called()
        refresh.assert_called_once()

    def test_failed_download_never_touches_cache(self) -> None:
        good = wrap(key_table(), time.time() - sudama.CACHE_TTL - 1, 1)
        self.cache.write_text(json.dumps(good))
        before = self.cache.read_text()
        with mock.patch.object(fetch, "fetch_resumable", return_value=False):
            self.assertIsNone(sudama._download())
        self.assertEqual(self.cache.read_text(), before)

    def test_repeated_failures_do_not_nest(self) -> None:
        self.cache.write_text(json.dumps(wrap(key_table(), time.time() - sudama.CACHE_TTL - 1, 1)))
        with mock.patch.object(fetch, "fetch_resumable", return_value=False), \
                mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            for _ in range(4):
                sudama._mem = None
                self.assertGreaterEqual(len(sudama.load(force=True)), sudama.MIN_ENTRIES)
        self.assertEqual(sudama._unwrap(json.loads(self.cache.read_text()))[2], 1)

    def test_garbage_download_is_rejected(self) -> None:
        def fake(url, dest, **kw):
            Path(dest).write_text(json.dumps({"error": "rate limited"}))
            return True
        with mock.patch.object(fetch, "fetch_resumable", side_effect=fake):
            self.assertIsNone(sudama._download())
        self.assertFalse(self.cache.exists())

    def test_good_download_is_saved_with_one_layer(self) -> None:
        table = key_table({"4012811": "cd" * 32})

        def fake(url, dest, **kw):
            self.assertEqual(Path(dest), self.raw, "下载不能直接写缓存文件")
            Path(dest).write_text(json.dumps(table))
            return True
        with mock.patch.object(fetch, "fetch_resumable", side_effect=fake):
            self.assertEqual(sudama._download()["4012811"], "cd" * 32)
        self.assertEqual(sudama._unwrap(json.loads(self.cache.read_text()))[2], 1)

    def test_small_table_is_not_a_key_table(self) -> None:
        self.assertFalse(sudama._is_key_table({"timestamp": 1.0, "data": {}}))
        self.assertFalse(sudama._is_key_table({"1": "a" * 64}))


if __name__ == "__main__":
    unittest.main()

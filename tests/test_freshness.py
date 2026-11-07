"""密钥库新鲜度回归测试（2026-11-07）。

覆盖三个导致"新游戏搜不到 / 内容仍处于加密状态"的真实缺陷：
  1. fetch_resumable 对 chunked（无 Content-Length）响应恒返回 False
  2. 服务器不支持 Range 时删掉已下载进度
  3. multisource 不解析 DLC 自有 depot；缓存旧时不会为缺失的密钥刷新

全部离线：HTTP 用本机临时服务器，数据源全部 mock。
"""
from __future__ import annotations

import http.server
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from suos import depotkeys, fetch, manifesthub3, multisource, sudama, updater  # noqa: E402

PAYLOAD = (b'{"1": "' + b"a" * 64 + b'"}' + b" " * 200_000)


class _Handler(http.server.BaseHTTPRequestHandler):
    """mode 由服务器属性控制，模拟不同 CDN 的行为。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 测试输出保持安静
        pass

    def do_GET(self) -> None:  # noqa: N802
        # probe() 只读 1 字节就关连接，服务器这边继续写会断 —— 这是预期行为
        try:
            self._serve()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _serve(self) -> None:
        mode = self.server.mode  # type: ignore[attr-defined]
        self.server.hits += 1    # type: ignore[attr-defined]
        rng = self.headers.get("Range")

        if mode == "chunked_no_range":
            # 复刻 api.993499094.xyz：忽略 Range、chunked、没有 Content-Length
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("ETag", 'W/"v1"')
            self.end_headers()
            for i in range(0, len(PAYLOAD), 65536):
                piece = PAYLOAD[i:i + 65536]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return

        if mode == "ranges":
            start = 0
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                start = int(a)
                end = int(b) if b else len(PAYLOAD) - 1
                body = PAYLOAD[start:end + 1]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
            else:
                body = PAYLOAD
                self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(500)
        self.send_header("Content-Length", "0")
        self.end_headers()


class _Server:
    def __init__(self, mode: str) -> None:
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.mode = mode   # type: ignore[attr-defined]
        self.httpd.hits = 0      # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/depotkeys.json"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class FetchResumableTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = Path(self.tmp.name) / "out.json"

    def test_chunked_without_content_length_succeeds(self) -> None:
        """★ 旧实现在这里恒返回 False —— Sudama 缓存冻结五周的真因"""
        with _Server("chunked_no_range") as srv:
            ok = fetch.fetch_resumable(srv.url, self.dest, timeout=5, max_seconds=30)
        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_probe_reports_no_range_and_unknown_size(self) -> None:
        with _Server("chunked_no_range") as srv:
            pr = fetch.probe(srv.url, timeout=5)
        self.assertTrue(pr.ok)
        self.assertFalse(pr.ranges)
        self.assertEqual(pr.size, 0)
        self.assertEqual(pr.etag, 'W/"v1"')

    def test_no_range_server_does_not_destroy_existing_part(self) -> None:
        """旧实现收到 200 就 unlink(.part)。现在全量下载走独立临时文件。"""
        part = self.dest.with_suffix(self.dest.suffix + ".part")
        part.write_bytes(PAYLOAD[:1000])
        with _Server("chunked_no_range") as srv, \
                mock.patch.object(fetch, "_stream_to",
                                  return_value=(0, False, "ConnectionResetError")):
            self.assertFalse(fetch.fetch_resumable(
                srv.url, self.dest, timeout=5, max_seconds=10))
        self.assertEqual(part.read_bytes(), PAYLOAD[:1000], "已有进度不能被删")

    def test_min_bytes_rejects_truncated_chunked_body(self) -> None:
        with _Server("chunked_no_range") as srv:
            ok = fetch.fetch_resumable(srv.url, self.dest, timeout=5, max_seconds=30,
                                       min_bytes=len(PAYLOAD) + 1)
        self.assertFalse(ok)
        self.assertFalse(self.dest.exists())

    def test_range_server_resumes_from_part(self) -> None:
        part = self.dest.with_suffix(self.dest.suffix + ".part")
        part.write_bytes(PAYLOAD[:50_000])
        with _Server("ranges") as srv:
            ok = fetch.fetch_resumable(srv.url, self.dest, timeout=5, max_seconds=30)
        self.assertTrue(ok)
        self.assertEqual(self.dest.read_bytes(), PAYLOAD)

    def test_github_proxies_only_for_github_urls(self) -> None:
        self.assertTrue(fetch.is_github_url("https://raw.githubusercontent.com/a/b/c"))
        self.assertFalse(fetch.is_github_url("https://api.993499094.xyz/depotkeys.json"))
        seen: list[str] = []

        def fake_probe(uri, timeout=0):
            seen.append(uri)
            return fetch.Probe(ok=False)

        with mock.patch.object(fetch, "probe", side_effect=fake_probe):
            fetch.fetch_resumable("https://api.993499094.xyz/depotkeys.json",
                                  self.dest, max_seconds=5)
        self.assertEqual(seen, ["https://api.993499094.xyz/depotkeys.json"])


def _appinfo(depots: dict, *, has_in_dlc: bool = False, dlcs: str = "") -> dict:
    node: dict = {}
    for dep, meta in depots.items():
        entry: dict = {"manifests": {"public": {"gid": meta["gid"], "size": meta["size"]}}}
        for f in ("dlcappid", "depotfromapp"):
            if f in meta:
                entry[f] = str(meta[f])
        node[str(dep)] = entry
    if has_in_dlc:
        node["hasdepotsindlc"] = "1"
    return {"depots": node, "extended": {"listofdlc": dlcs} if dlcs else {}}


class MultisourceDlcAndRefreshTest(unittest.TestCase):
    """用 Yarimono(2696050) 的真实 depot 结构做 fixture。"""

    APPINFO = {
        2696050: _appinfo({
            2696055: {"gid": "4797632950615304316", "size": 975099719},
            3171570: {"gid": "6273957131003220084", "size": 70918255, "dlcappid": 3171570},
            3593651: {"gid": "8139483366419726494", "size": 233745653,
                      "dlcappid": 3593650, "depotfromapp": 3593650},
        }, has_in_dlc=True, dlcs="3171570,3593650"),
        3171570: _appinfo({
            3171570: {"gid": "9160537544495656538", "size": 88226425},
            3171571: {"gid": "3309968121601332384", "size": 88226425},
        }),
        3593650: _appinfo({
            3593650: {"gid": "7850549861192466480", "size": 233745653},
            3593651: {"gid": "8139483366419726494", "size": 233745653},
        }),
    }
    KEYS = {str(d): f"{d:064x}" for d in (2696055, 3171570, 3171571, 3593650, 3593651)}

    def _patch(self, sud: dict, comm: dict):
        patches = [
            mock.patch.object(updater, "_steamcmd_info",
                              side_effect=lambda a, timeout=25: self.APPINFO.get(a)),
            mock.patch.object(manifesthub3, "fetch_all", return_value=None),
            mock.patch("suos.server_api.install_plan", side_effect=RuntimeError("offline")),
            mock.patch.object(sudama, "load", return_value=sud),
            mock.patch.object(depotkeys, "load_community_keys", return_value=comm),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_dlc_own_depots_get_keys(self) -> None:
        """★ 3593650 / 3171571 只出现在 DLC 自己的 appinfo 里"""
        self._patch(self.KEYS, {})
        with mock.patch.object(multisource, "_refresh_stale_sources", return_value={}):
            r = multisource.resolve(2696050, verbose=False)
        self.assertIn(3593650, r["keys"])
        self.assertIn(3171571, r["keys"])
        self.assertEqual(sorted(r["dlc_appids"]), [3171570, 3593650])
        missing = [d for d in r["content_depots"] if d not in r["keys"]]
        self.assertEqual(missing, [])
        # 同一 depot 两个视角 gid 不一致：钉本体视角，候选里两个都要有
        self.assertEqual(r["manifest_pins"][3171570], "6273957131003220084")
        self.assertEqual(r["manifest_candidates"][3171570],
                         ["6273957131003220084", "9160537544495656538"])

    def test_zero_key_is_never_a_real_key(self) -> None:
        self._patch({"2696055": "0" * 64}, {})
        with mock.patch.object(multisource, "_refresh_stale_sources", return_value={}):
            r = multisource.resolve(2696050, verbose=False)
        self.assertNotIn(2696055, r["keys"])

    def test_missing_key_triggers_refresh_and_recovers(self) -> None:
        """★ 旧缓存没有新游戏的密钥 → 同步刷新一次 → 找回"""
        stale = {k: v for k, v in self.KEYS.items() if k != "3593650"}
        self._patch(stale, {})
        with mock.patch.object(multisource, "_refresh_stale_sources",
                               return_value={"sudama": self.KEYS}) as refresh:
            r = multisource.resolve(2696050, verbose=False)
        refresh.assert_called_once()
        self.assertIn(3593650, r["keys"])
        self.assertEqual(r["sources"]["refreshed"], ["sudama"])

    def test_no_refresh_when_nothing_missing(self) -> None:
        self._patch(self.KEYS, {})
        with mock.patch.object(multisource, "_refresh_stale_sources") as refresh:
            multisource.resolve(2696050, verbose=False)
        refresh.assert_not_called()

    def test_fresh_cache_is_not_refreshed(self) -> None:
        with mock.patch.object(sudama, "age", return_value=60), \
                mock.patch.object(depotkeys, "age", return_value=60), \
                mock.patch.object(sudama, "refresh_if_remote_changed") as s, \
                mock.patch.object(depotkeys, "refresh_if_remote_changed") as c:
            self.assertEqual(multisource._refresh_stale_sources(False), {})
        s.assert_not_called()
        c.assert_not_called()

    def test_unchanged_remote_returns_nothing(self) -> None:
        with mock.patch.object(sudama, "age", return_value=10 ** 6), \
                mock.patch.object(depotkeys, "age", return_value=10 ** 6), \
                mock.patch.object(sudama, "refresh_if_remote_changed", return_value=None), \
                mock.patch.object(depotkeys, "refresh_if_remote_changed", return_value=None):
            self.assertEqual(multisource._refresh_stale_sources(False), {})


if __name__ == "__main__":
    unittest.main()

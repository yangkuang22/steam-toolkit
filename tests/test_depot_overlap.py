"""depot 重叠覆盖检查 + Lua depot 排序的测试。

场景照搬 Yarimono(2696050) 的真实结构：5 个 depot 都带 index.html，plugins.js 只在
本体和 DLC2 里有；SteamOS 那台的挂载顺序把 DLC1(3171571) 排到了最后，
DLC1 的旧文件盖掉了 DLC2 的新文件。

运行：cd ~/steam-toolkit && python3 -m unittest tests.test_depot_overlap
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from suos import cli, depot_overlap as dov, steam  # noqa: E402

APPID = 2696050
BASE, DLC1A, DLC1, DLC2A, DLC2B = 2696055, 3171570, 3171571, 3593650, 3593651
GIDS = {BASE: 4797632950615304316, DLC1A: 6273957131003220084, DLC1: 3309968121601332384,
        DLC2A: 7850549861192466480, DLC2B: 8139483366419726494}

INDEX = "data\\www\\index.html"
PLUGINS = "data\\www\\js\\plugins.js"
SHARED = "data\\www\\img\\same.png"

V_BASE, V_DLC1, V_DLC2 = b"<title>base</title>", b"<title>v2.19</title>", b"<title>v3.08</title>"
P_BASE, P_DLC2 = b"var $plugins = [1];", b"var $plugins = [1, 2, 3];"

CONTENT = {
    BASE: {INDEX: V_BASE, PLUGINS: P_BASE, SHARED: b"png"},
    DLC1A: {INDEX: V_DLC1},
    DLC1: {INDEX: V_DLC1},
    DLC2A: {INDEX: V_DLC2, PLUGINS: P_DLC2, SHARED: b"png"},
    DLC2B: {INDEX: V_DLC2, PLUGINS: P_DLC2, SHARED: b"png"},
}

ORDER_WINDOWS = [DLC1A, BASE, DLC2B, DLC1, DLC2A]    # 末位 DLC2，正常
ORDER_STEAMOS = [DLC1A, BASE, DLC2B, DLC2A, DLC1]    # 末位 DLC1，出事的那台

GOOD_DISK = {INDEX: V_DLC2, PLUGINS: P_DLC2, SHARED: b"png"}


# ── 构造二进制清单 ────────────────────────────────────────────────────

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _fv(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(value)


def _fb(num: int, data: bytes) -> bytes:
    return _varint((num << 3) | 2) + _varint(len(data)) + data


def build_manifest(depot: int, gid: int, files: dict[str, bytes],
                   dirs: tuple[str, ...] = (), encrypted: bool = False) -> bytes:
    payload = b""
    for name, data in files.items():
        mapping = (_fb(1, name.encode()) + _fv(2, len(data)) + _fv(3, 0)
                   + _fb(5, hashlib.sha1(data).digest()))
        payload += _fb(1, mapping)
    for name in dirs:
        payload += _fb(1, _fb(1, name.encode()) + _fv(2, 0) + _fv(3, dov.FLAG_DIRECTORY))
    meta = _fv(1, depot) + _fv(2, gid) + _fv(4, int(encrypted))
    return (struct.pack("<II", dov.PAYLOAD_MAGIC, len(payload)) + payload
            + struct.pack("<II", dov.METADATA_MAGIC, len(meta)) + meta
            + struct.pack("<II", dov.SIGNATURE_MAGIC, 0)
            + struct.pack("<I", dov.END_MAGIC))


# ── 伪造一个 Steam 根目录 ────────────────────────────────────────────

class FakeSteam:
    def __init__(self, tmp: Path, order: list[int], disk: dict[str, bytes],
                 skip_manifest: tuple[int, ...] = ()):
        self.root = tmp
        (tmp / "config").mkdir()
        (tmp / "config" / "config.vdf").write_text('"InstallConfigStore"\n{\n}\n')
        (tmp / "depotcache").mkdir()
        apps = tmp / "steamapps"
        game = apps / "common" / "YARISUTEMESUBUTA"
        game.mkdir(parents=True)

        depots = "".join(
            f'\t\t"{d}"\n\t\t{{\n\t\t\t"manifest"\t\t"{GIDS[d]}"\n\t\t\t"size"\t\t"1"\n\t\t}}\n'
            for d in order)
        (apps / f"appmanifest_{APPID}.acf").write_text(
            '"AppState"\n{\n\t"appid"\t\t"2696050"\n\t"name"\t\t"Yarimono"\n'
            '\t"installdir"\t\t"YARISUTEMESUBUTA"\n\t"InstalledDepots"\n\t{\n'
            + depots + "\t}\n}\n")

        for d in order:
            if d not in skip_manifest:
                (tmp / "depotcache" / f"{d}_{GIDS[d]}.manifest").write_bytes(
                    build_manifest(d, GIDS[d], CONTENT[d]))
        for rel, data in disk.items():
            p = game / rel.replace("\\", "/")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        self.game = game

    @property
    def paths(self) -> steam.SteamPaths:
        return steam.SteamPaths(root=self.root, config=self.root / "config",
                                depotcache=self.root / "depotcache",
                                appcache=self.root / "appcache",
                                steamapps=self.root / "steamapps")


class _TmpCase(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.tmp = Path(self._td.name)
        # 基线目录指到临时目录，绝不读写用户真实的 ~/.config/SteamUnlockOS
        self._old_accept = dov.ACCEPT_DIR
        self.accept_dir = Path(tempfile.mkdtemp(dir=self._td.name, prefix="accept-"))
        dov.ACCEPT_DIR = self.accept_dir

    def tearDown(self) -> None:
        dov.ACCEPT_DIR = self._old_accept
        self._td.cleanup()

    def env(self, order=ORDER_WINDOWS, disk=None, **kw) -> FakeSteam:
        return FakeSteam(self.tmp, order, GOOD_DISK if disk is None else disk, **kw)


# ── 清单解析 ─────────────────────────────────────────────────────────

class TestParseManifest(unittest.TestCase):
    def test_roundtrip(self):
        raw = build_manifest(DLC2A, GIDS[DLC2A], {INDEX: V_DLC2}, dirs=("data\\www",))
        m = dov.parse_manifest(raw)
        self.assertEqual((m.depot_id, m.gid, m.filenames_encrypted),
                         (DLC2A, GIDS[DLC2A], False))
        self.assertEqual(list(m.files), ["data/www/index.html"], "目录要跳过，反斜杠要归一")
        rel, ver = m.files["data/www/index.html"]
        self.assertEqual(rel, "data/www/index.html")
        self.assertEqual(ver, dov.FileVersion(len(V_DLC2), hashlib.sha1(V_DLC2).hexdigest()))

    def test_encrypted_flag(self):
        m = dov.parse_manifest(build_manifest(1, 2, {"a": b"x"}, encrypted=True))
        self.assertTrue(m.filenames_encrypted)

    def test_zip_wrapped(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("z", build_manifest(1, 2, {"a\\b.txt": b"x"}))
        self.assertIn("a/b.txt", dov.parse_manifest(buf.getvalue()).files)

    def test_rejects_garbage_and_truncation(self):
        with self.assertRaises(dov.ManifestError):
            dov.parse_manifest(b"definitely not a manifest")
        raw = build_manifest(1, 2, {"a": b"x" * 50})
        with self.assertRaises(dov.ManifestError):
            dov.parse_manifest(raw[:20])

    def test_priority_helpers(self):
        self.assertEqual(dov.default_priority([BASE, DLC2B, DLC1, DLC1]), [DLC2B, DLC1, BASE])
        self.assertEqual(dov.lua_depot_order([DLC2B, BASE, DLC1]), [BASE, DLC1, DLC2B])


# ── 检查逻辑 ─────────────────────────────────────────────────────────

class TestCheckApp(_TmpCase):
    def check(self, fake: FakeSteam, priority=None) -> dov.AppReport:
        return dov.check_app(fake.paths, APPID, priority)

    def find(self, rep: dov.AppReport, rel: str) -> dov.PathFinding:
        return next(f for f in rep.findings if f.path == rel.replace("\\", "/"))

    def test_correct_install(self):
        rep = self.check(self.env())
        self.assertEqual(rep.severity, 0)
        self.assertEqual(rep.mount_order, ORDER_WINDOWS)
        self.assertEqual(rep.priority[0], DLC2B)
        self.assertEqual({f.path for f in rep.findings},
                         {"data/www/index.html", "data/www/js/plugins.js"},
                         "内容一致的 same.png 不算冲突")
        self.assertTrue(all(f.status is dov.Status.OK for f in rep.findings))
        self.assertEqual(rep.order_risks, [])

    def test_old_dlc_overwrote_new(self):
        """复现事故：plugins.js 是 DLC2 的，index.html 却被 DLC1 盖了。"""
        disk = dict(GOOD_DISK, **{INDEX: V_DLC1})
        rep = self.check(self.env(ORDER_STEAMOS, disk))
        self.assertEqual(rep.severity, 2)
        idx = self.find(rep, INDEX)
        self.assertIs(idx.status, dov.Status.WRONG_DEPOT)
        self.assertEqual(idx.expected_depot, DLC2B)
        self.assertEqual(idx.actual_depots, (DLC1A, DLC1))
        self.assertEqual(idx.order_winner, DLC1)
        self.assertTrue(idx.order_risk)
        plugins = self.find(rep, PLUGINS)
        self.assertIs(plugins.status, dov.Status.OK)
        self.assertFalse(plugins.order_risk, "DLC1 不含 plugins.js，顺序不影响它")

    def test_files_right_but_order_risky(self):
        """文件是对的（手动拷过），但登记顺序还是坏的：验证完整性就会再坏。"""
        rep = self.check(self.env(ORDER_STEAMOS))
        self.assertEqual(rep.severity, 1)
        self.assertEqual([f.path for f in rep.order_risks], ["data/www/index.html"])

    def test_patched_file_is_only_info(self):
        disk = dict(GOOD_DISK, **{INDEX: b"<title>v4.00 patched</title>"})
        rep = self.check(self.env(disk=disk))
        self.assertIs(self.find(rep, INDEX).status, dov.Status.MODIFIED)
        self.assertEqual(rep.severity, 0)

    def test_patch_shipping_old_depot_content_is_not_an_error(self):
        """实证：v4.00 补丁自带了与本体 2696055 旧版逐字节相同的图片。
        按挂载顺序 Steam 只会落胜出者(DLC2)，所以这只能是外部写入，不算盖错。"""
        rep = self.check(self.env(disk=dict(GOOD_DISK, **{INDEX: V_BASE})))
        idx = self.find(rep, INDEX)
        self.assertIs(idx.status, dov.Status.MODIFIED)
        self.assertEqual(idx.actual_depots, (BASE,))
        self.assertEqual(rep.severity, 0)

    def test_patched_same_size_still_detected(self):
        same_size = b"<title>v4.00</title>"
        self.assertEqual(len(same_size), len(V_DLC2))
        rep = self.check(self.env(disk=dict(GOOD_DISK, **{INDEX: same_size})))
        self.assertIs(self.find(rep, INDEX).status, dov.Status.MODIFIED)

    def test_missing_file(self):
        disk = {k: v for k, v in GOOD_DISK.items() if k != INDEX}
        rep = self.check(self.env(disk=disk))
        self.assertIs(self.find(rep, INDEX).status, dov.Status.MISSING)
        self.assertEqual(rep.severity, 2)

    def test_missing_manifest_is_warning_and_rest_still_checked(self):
        rep = self.check(self.env(skip_manifest=(DLC1,)))
        self.assertEqual(rep.severity, 1)
        self.assertTrue(any("3171571" in p for p in rep.problems))
        self.assertNotIn(DLC1, rep.priority)
        self.assertIs(self.find(rep, INDEX).status, dov.Status.OK)

    def test_priority_override(self):
        disk = dict(GOOD_DISK, **{INDEX: V_DLC1})
        rep = self.check(self.env(disk=disk), priority=[DLC1])
        self.assertEqual(rep.priority[0], DLC1)
        self.assertIs(self.find(rep, INDEX).status, dov.Status.OK)

    def test_case_insensitive_lookup(self):
        disk = {"data\\www\\Index.HTML": V_DLC2, PLUGINS: P_DLC2}
        rep = self.check(self.env(disk=disk))
        self.assertIs(self.find(rep, INDEX).status, dov.Status.OK)

    def test_single_depot_and_not_installed(self):
        fake = self.env(order=[BASE], disk={INDEX: V_BASE})
        self.assertEqual(self.check(fake).findings, [])
        self.assertEqual(dov.multi_depot_apps(fake.paths), [])
        rep = dov.check_app(fake.paths, 999)
        self.assertEqual(rep.severity, 1)


class TestAcceptBaseline(_TmpCase):
    def test_accept_then_change(self):
        fake = self.env(ORDER_STEAMOS, dict(GOOD_DISK, **{INDEX: V_DLC1}))
        self.assertEqual(dov.check_app(fake.paths, APPID).severity, 2)

        n = dov.save_accepted(dov.check_app(fake.paths, APPID, hash_all=True))
        self.assertEqual(n, 1)
        rep = dov.check_app(fake.paths, APPID, accepted=dov.load_accepted(APPID))
        idx = next(f for f in rep.findings if f.path == "data/www/index.html")
        self.assertIs(idx.status, dov.Status.ACCEPTED)
        self.assertEqual(rep.severity, 1, "认可了文件，但挂载顺序风险仍要提示")

        (fake.game / "data/www/index.html").write_bytes(b"<title>changed again</title>")
        rep = dov.check_app(fake.paths, APPID, accepted=dov.load_accepted(APPID))
        idx = next(f for f in rep.findings if f.path == "data/www/index.html")
        self.assertIsNot(idx.status, dov.Status.ACCEPTED, "内容再变就不再认可")

    def test_accept_records_size_mismatched_files(self):
        patched = b"<title>v4.00 patched build</title>"
        fake = self.env(disk=dict(GOOD_DISK, **{INDEX: patched}))
        dov.save_accepted(dov.check_app(fake.paths, APPID, hash_all=True))
        self.assertEqual(dov.load_accepted(APPID),
                         {"data/www/index.html": hashlib.sha1(patched).hexdigest()})
        rep = dov.check_app(fake.paths, APPID, accepted=dov.load_accepted(APPID))
        self.assertIs(rep.findings[0].status, dov.Status.ACCEPTED)

    def test_corrupt_baseline_is_ignored(self):
        dov.accepted_path(APPID).write_text("{not json")
        self.assertEqual(dov.load_accepted(APPID), {})
        dov.accepted_path(APPID).write_text('{"files": ["wrong shape"]}')
        self.assertEqual(dov.load_accepted(APPID), {})


class TestCli(_TmpCase):
    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = cli.main(["--steam-root", str(self.tmp), *argv])
        return rc, out.getvalue()

    def test_healthcheck_reports_bug(self):
        self.env(ORDER_STEAMOS, dict(GOOD_DISK, **{INDEX: V_DLC1}))
        rc, out = self.run_cli("check-overlap", "--all", "--healthcheck")
        self.assertEqual(rc, 2)
        self.assertIn("✗", out)

    def test_healthcheck_clean(self):
        self.env()
        rc, out = self.run_cli("check-overlap", "--all", "--healthcheck")
        self.assertEqual(rc, 0)
        self.assertIn("均正常", out)

    def test_verbose_lists_wrong_file(self):
        self.env(ORDER_STEAMOS, dict(GOOD_DISK, **{INDEX: V_DLC1}))
        rc, out = self.run_cli("check-overlap", str(APPID))
        self.assertEqual(rc, 2)
        self.assertIn("data/www/index.html", out)
        self.assertIn("期望 3593651", out)

    def test_accept_flow(self):
        self.env(ORDER_STEAMOS, dict(GOOD_DISK, **{INDEX: V_DLC1}))
        self.assertEqual(self.run_cli("check-overlap", str(APPID))[0], 2)
        rc, out = self.run_cli("check-overlap", str(APPID), "--accept")
        self.assertIn("已把 1 个", out)
        self.assertEqual(rc, 1, "文件已认可，只剩挂载顺序风险")
        self.assertTrue(dov.accepted_path(APPID).is_file())
        self.assertEqual(self.run_cli("check-overlap", "--all", "--healthcheck")[0], 1)

    def test_bad_arguments(self):
        self.env()
        self.assertEqual(self.run_cli("check-overlap")[0], cli.OVERLAP_INTERNAL_ERROR)
        self.assertEqual(self.run_cli("check-overlap", "--all", "--priority", "x")[0],
                         cli.OVERLAP_INTERNAL_ERROR)
        self.assertEqual(self.run_cli("check-overlap", "--all", "--accept")[0],
                         cli.OVERLAP_INTERNAL_ERROR)


if __name__ == "__main__":
    unittest.main()

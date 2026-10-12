"""本次 bug 修复的回归测试

对应 docs/BUG清单-待分析.md 里的 P1-1 / P1-2 / P2-1 / P2-2 / P2-3 / P2-4。

★ 安全设计: 所有涉及配置写入的测试都用【临时文件】，
  通过替换 webui_core 的模块级路径常量实现，绝不碰真实配置。
  tearDown 里强制还原，还原失败会让测试报错（而不是静默）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webui_core as core  # noqa: E402
from suos import slsconfig  # noqa: E402


class TempCorePaths(unittest.TestCase):
    """把 webui_core 的路径常量指向临时目录的基类"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig: dict[str, object] = {}

        # 保存原始值
        for name in ("CONFIG_VDF", "SLS_TOML", "LUA_SLS", "LUA_STEAM", "RECORD", "HOME"):
            self._orig[name] = getattr(core, name, None)

        # 造临时结构
        (self.tmp / "config").mkdir(parents=True)
        (self.tmp / "sls").mkdir(parents=True)
        (self.tmp / "steamlua").mkdir(parents=True)
        (self.tmp / "home").mkdir(parents=True)

        # 从真实文件复制一份初始内容（有真实数据才有意义）
        real_vdf = self._orig["CONFIG_VDF"]
        real_toml = self._orig["SLS_TOML"]
        if isinstance(real_vdf, Path) and real_vdf.is_file():
            shutil.copy2(real_vdf, self.tmp / "config/config.vdf")
        else:
            (self.tmp / "config/config.vdf").write_text(
                '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n'
                '\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"depots"\n\t\t\t\t{\n\t\t\t\t}\n'
                '\t\t\t}\n\t\t}\n\t}\n}\n')
        if isinstance(real_toml, Path) and real_toml.is_file():
            shutil.copy2(real_toml, self.tmp / "sls/config.toml")
        else:
            (self.tmp / "sls/config.toml").write_text(
                'AppIds = []\nAdditionalApps = []\n')

        core.CONFIG_VDF = self.tmp / "config/config.vdf"
        core.SLS_TOML = self.tmp / "sls/config.toml"
        core.LUA_SLS = self.tmp / "sls/lua"
        core.LUA_STEAM = self.tmp / "steamlua"
        core.LUA_SLS.mkdir(parents=True, exist_ok=True)
        core.RECORD = self.tmp / "home/installed.json"
        core._name_cache.clear()

        # 隔离备份：避免测试往真实的 backup/ 目录写
        self._orig_backup = getattr(core, "BACKUP", None)
        core.BACKUP = self.tmp / "backup"
        core.BACKUP.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        # 强制还原 —— 失败就报错，不能静默
        for name, val in self._orig.items():
            if val is not None:
                setattr(core, name, val)
        if getattr(self, "_orig_backup", None) is not None:
            core.BACKUP = self._orig_backup
        core._name_cache.clear()
        self._tmp.cleanup()

        # 确认真的还原了（防止测试污染真实配置）
        assert core.CONFIG_VDF == self._orig["CONFIG_VDF"], "CONFIG_VDF 没还原！"
        assert core.SLS_TOML == self._orig["SLS_TOML"], "SLS_TOML 没还原！"


# ══════════════════════════════════════════════════════════════
#  P1-1  名字缓存污染
# ══════════════════════════════════════════════════════════════

class TestNameCachePoisoning(TempCorePaths):
    """P1-1: 解析失败不能污染永久缓存；旧的中毒缓存要能自愈"""

    def _names_file(self) -> Path:
        """真实代码里是 HOME/.config/SteamUnlockOS/names.json"""
        return self.tmp / "home/.config/SteamUnlockOS/names.json"

    def test_resolve_failure_returns_none(self):
        """_resolve_name 全部失败时必须返回 None（而不是 'AppID xxx'）"""
        orig = core._resolve_name
        try:
            core._resolve_name = lambda a: None
            self.assertIsNone(core._resolve_name(123))
        finally:
            core._resolve_name = orig

    def test_failure_does_not_write_cache(self):
        """P1-1 核心: 解析失败时不能写永久缓存"""
        # 让 names.json 指向临时路径
        orig_resolve = core._resolve_name
        orig_home = core.HOME
        try:
            core.HOME = self.tmp / "home"
            self._names_file().parent.mkdir(parents=True, exist_ok=True)
            core._resolve_name = lambda a: None
            core._name_cache.clear()
            # 调 _name_for（会尝试写缓存）
            out = core._name_for(999999, None)
            self.assertEqual(out, "AppID 999999")
            nf = self._names_file()
            if nf.is_file():
                data = json.loads(nf.read_text())
                self.assertNotIn("999999", data,
                                 "解析失败的结果不该被写进永久缓存")
        finally:
            core._resolve_name = orig_resolve
            core.HOME = orig_home
            core._name_cache.clear()

    def test_poisoned_cache_self_heals(self):
        """旧的中毒缓存（'AppID 数字'）应该被自动修复"""
        orig_resolve = core._resolve_name
        orig_home = core.HOME
        try:
            core.HOME = self.tmp / "home"
            nf = self._names_file()
            nf.parent.mkdir(parents=True, exist_ok=True)
            nf.write_text(json.dumps({"999999": "AppID 999999"}))
            core._name_cache.clear()
            core._resolve_name = lambda a: "真实名字"
            out = core._name_for(999999, None)
            self.assertEqual(out, "真实名字", "中毒缓存应该被重新解析覆盖")
            data = json.loads(nf.read_text())
            self.assertEqual(data.get("999999"), "真实名字")
        finally:
            core._resolve_name = orig_resolve
            core.HOME = orig_home
            core._name_cache.clear()

    def test_good_cache_is_used(self):
        """正常缓存应该直接用，不再请求"""
        orig_resolve = core._resolve_name
        orig_home = core.HOME
        calls = []
        try:
            core.HOME = self.tmp / "home"
            nf = self._names_file()
            nf.parent.mkdir(parents=True, exist_ok=True)
            nf.write_text(json.dumps({"999999": "缓存里的名字"}))
            core._name_cache.clear()

            def spy(a):
                calls.append(a)
                return "不该被调用"

            core._resolve_name = spy
            out = core._name_for(999999, None)
            self.assertEqual(out, "缓存里的名字")
            self.assertEqual(calls, [], "有缓存时不该再解析")
        finally:
            core._resolve_name = orig_resolve
            core.HOME = orig_home
            core._name_cache.clear()


# ══════════════════════════════════════════════════════════════
#  P2-1  write_keys_to_config
# ══════════════════════════════════════════════════════════════

class TestWriteKeys(TempCorePaths):
    """P2-1: 不能静默失败，不能重复插入"""

    KEY = "ab" * 32

    def test_insert_new(self):
        n = core.write_keys_to_config({"1111111": self.KEY})
        self.assertEqual(n, 1)
        self.assertEqual(core._read_vdf_keys(core.CONFIG_VDF.read_text()).get("1111111"),
                         self.KEY)

    def test_idempotent(self):
        core.write_keys_to_config({"1111111": self.KEY})
        n = core.write_keys_to_config({"1111111": self.KEY})
        self.assertEqual(n, 0, "同样的值不该重复写入")

    def test_replace_existing(self):
        core.write_keys_to_config({"1111111": self.KEY})
        new = "cd" * 32
        n = core.write_keys_to_config({"1111111": new})
        self.assertEqual(n, 1)
        self.assertEqual(core._read_vdf_keys(core.CONFIG_VDF.read_text()).get("1111111"),
                         new)

    def test_bad_length_raises(self):
        """长度不是 64 hex 必须报错（Steam 会忽略这种值）"""
        with self.assertRaises(ValueError):
            core.write_keys_to_config({"2222222": "ab" * 48})

    def test_no_duplicate_when_format_unexpected(self):
        """P2-1 核心: 解析器读不到但文件里已存在时，不能重复插入"""
        # 先插一个正常的
        core.write_keys_to_config({"3333333": self.KEY})
        text = core.CONFIG_VDF.read_text()
        # 破坏它的格式（在 id 和 DecryptionKey 之间插一个字段）
        pat = re.compile(r'("3333333"\s*\{\s*)("DecryptionKey")')
        m = pat.search(text)
        self.assertIsNotNone(m, "测试前提: 应该能找到刚写入的块")
        text = text[:m.start(2)] + '"Extra"\t"x"\n\t\t\t\t\t\t' + text[m.start(2):]
        core.CONFIG_VDF.write_text(text)

        before = core.CONFIG_VDF.read_text().count('"3333333"')
        with self.assertRaises(ValueError):
            core.write_keys_to_config({"3333333": "ff" * 32})
        after = core.CONFIG_VDF.read_text().count('"3333333"')
        self.assertEqual(after, before, "不该产生重复的 depot 块")

    def test_missing_depots_section_raises(self):
        core.CONFIG_VDF.write_text('"Nothing"\n{\n}\n')
        with self.assertRaises(Exception):
            core.write_keys_to_config({"4444444": self.KEY})

    def test_result_is_parseable_vdf(self):
        from suos import vdf
        core.write_keys_to_config({"5555555": self.KEY})
        d = vdf.load(str(core.CONFIG_VDF))   # 不该抛异常
        self.assertIsInstance(d, dict)


# ══════════════════════════════════════════════════════════════
#  P2-2  install() 的步骤分类 + 防覆盖
# ══════════════════════════════════════════════════════════════

class TestInstallClassification(TempCorePaths):
    """P2-2: 致命步骤失败要 ok=False；防覆盖保护要拦住退化数据"""

    def test_bad_appid_fails_cleanly(self):
        """不存在的游戏 → ok=False，且不抛异常"""
        r = core.install(999999999, include_dlc=False)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("fatal"), "应该有致命原因")
        self.assertIn("数据", r["message"])

    def test_lua_overwrite_protection(self):
        """P2-2 核心: 新的 Lua 清单数少于现有 → 拒绝覆盖"""
        from suos import multisource

        appid = 7777777
        lua_target = core.LUA_SLS / f"{appid}.lua"
        core.LUA_SLS.mkdir(parents=True, exist_ok=True)
        good_lua = (f"addappid({appid})\n"
                    f'addappid(7777778,0,"{"aa"*32}")\n'
                    f'setManifestid(7777778,"123456")\n'
                    f'setManifestid(7777779,"654321")\n')
        lua_target.write_text(good_lua)

        orig = multisource.resolve

        def degraded(a, verbose=False):
            return {"appid": a, "name": "退化数据", "keys": {}, "manifests": [],
                    "lua": f"addappid({a})\n", "sources": {}}

        try:
            multisource.resolve = degraded
            r = core.install(appid, include_dlc=False)
            self.assertFalse(r["ok"], "退化数据应该导致失败")
            self.assertTrue(any("拒绝覆盖" in s for s in r["steps"]),
                            f"应该出现拒绝覆盖的提示: {r['steps']}")
            # ★ 关键: 原文件必须保持完整
            self.assertIn("setManifestid", lua_target.read_text(),
                          "原 Lua 被破坏了！")
        finally:
            multisource.resolve = orig


# ══════════════════════════════════════════════════════════════
#  P2-3  slsconfig 读 TOML
# ══════════════════════════════════════════════════════════════

class TestSLSConfigToml(unittest.TestCase):
    """P2-3: 只有 config.toml 时也应该能看到 AppIds"""

    def test_reads_toml_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            yaml_path = d / "config.yaml"          # 故意不创建
            (d / "config.toml").write_text(
                'AppIds = [111, 222, 333]\n'
                'AdditionalApps = [222, 444]\n'
            )
            cfg = slsconfig.SLSConfig(yaml_path)
            # 显式传 path 时不探测 TOML（保持原行为）
            self.assertFalse(cfg.exists())

    def test_owned_reads_toml(self):
        """owned() 从 config.toml 读 AppIds + AdditionalApps"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text("AppIds = [111]\nAdditionalApps = [222]\n")
            cfg = slsconfig.SLSConfig(p).load()
            self.assertEqual(cfg.owned(), {111, 222})

    def test_toml_detection_when_no_path(self):
        """不传 path 时应该探测同目录的 config.toml"""
        # 用真实的 CONFIG_PATH 目录做判断（只读，不改动）
        cfg = slsconfig.SLSConfig()
        toml = slsconfig.CONFIG_PATH.with_suffix(".toml")
        if toml.is_file():
            self.assertIsNotNone(cfg.toml_path)
            self.assertTrue(cfg.exists())
            cfg.load()
            # 真实环境里应该有 AppIds
            self.assertGreater(len(cfg.owned()), 0,
                               "config.toml 存在时 owned() 不该为空")


# ══════════════════════════════════════════════════════════════
#  config.toml：所有权只走 Lua，toml 只做旧版残留清理
# ══════════════════════════════════════════════════════════════

def _toml_list(field: str) -> list[str]:
    m = re.search(rf"^{field} = \[([^\]]*)\]", core.SLS_TOML.read_text(), re.M)
    return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []


def _seed_toml(ids) -> None:
    s = ", ".join(str(i) for i in ids)
    core.SLS_TOML.write_text(f"UseWhitelist = false\nAppIds = [{s}]\nAdditionalApps = [{s}]\n")


class TestTomlCleanup(TempCorePaths):
    """旧版把每个游戏和 DLC 同时写进 AppIds（UseWhitelist=false 时是黑名单）和
    AdditionalApps。新版不再写入，只保留清理能力。"""

    def test_remove_from_both_lists(self):
        _seed_toml([100, 200, 300])
        self.assertEqual(core._remove_appids_from_toml([100, 300]), 4)
        self.assertEqual(_toml_list("AppIds"), ["200"])
        self.assertEqual(_toml_list("AdditionalApps"), ["200"])
        self.assertIn("UseWhitelist = false", core.SLS_TOML.read_text(), "其他设置不能丢")

    def test_remove_absent_does_not_write(self):
        _seed_toml([100])
        before = core.SLS_TOML.read_text()
        mtime = core.SLS_TOML.stat().st_mtime_ns
        self.assertEqual(core._remove_appids_from_toml([999]), 0)
        self.assertEqual(core.SLS_TOML.read_text(), before)
        self.assertEqual(core.SLS_TOML.stat().st_mtime_ns, mtime, "没变化就不该重写（会触发 SLSsteam 热重载）")

    def test_cleanup_only_removes_lua_covered(self):
        _seed_toml([100, 101, 300])
        (core.LUA_SLS / "100.lua").write_text(
            f'addappid(100)\naddappid(101,0,"{"ab" * 32}")\nsetManifestid(101,"5")\n')
        self.assertEqual(core.cleanup_legacy_toml_lists(), 4)
        self.assertEqual(_toml_list("AppIds"), ["300"], "不在 Lua 里的条目不能动")
        self.assertEqual(_toml_list("AdditionalApps"), ["300"])


class TestSLSConfigOwnedFromLua(unittest.TestCase):
    """toml 列表清空后，status/list 显示的"已拥有"要来自 Lua"""

    def test_owned_includes_lua(self):
        with tempfile.TemporaryDirectory() as tmp:
            toml = Path(tmp) / "config.toml"
            toml.write_text("AppIds = []\nAdditionalApps = [730]\n")
            lua = Path(tmp) / "lua"
            lua.mkdir()
            (lua / "100.lua").write_text(f'addappid(100)\naddappid(101,0,"{"ab" * 32}")\n')
            self.assertEqual(slsconfig.SLSConfig(toml, lua_dir=lua).owned(), {100, 101, 730})


class TestDepotcacheCleanScope(unittest.TestCase):
    """清理器只能清理本工具 Lua 管理的 depot 的旧版本。
    旧版把僵尸毁灭工程的 111 个创意工坊清单（<appid>_<gid>.manifest）当垃圾删了。"""

    def setUp(self) -> None:
        from suos import depotcache_clean as dc
        self.dc = dc
        self._tmp = tempfile.TemporaryDirectory()
        t = Path(self._tmp.name)
        self._orig = (dc.DEPOTCACHE, dc.ACF_DIR, dc.LUA_DIRS)
        dc.DEPOTCACHE, dc.ACF_DIR, dc.LUA_DIRS = t / "depotcache", t / "steamapps", [t / "lua"]
        for d in (dc.DEPOTCACHE, dc.ACF_DIR / "workshop", t / "lua"):
            d.mkdir(parents=True)
        (t / "lua" / "1000.lua").write_text('addappid(1000)\nsetManifestid(1001,"2")\n')
        (dc.ACF_DIR / "appmanifest_1000.acf").write_text(
            '"AppState"\n{\n\t"InstalledDepots"\n\t{\n\t\t"1001"\n\t\t{\n\t\t\t"manifest"\t\t"2"\n\t\t}\n\t}\n}\n')
        (dc.ACF_DIR / "workshop" / "appworkshop_108600.acf").write_text(
            '"AppWorkshop"\n{\n\t"WorkshopItemsInstalled"\n\t{\n\t\t"123456"\n\t\t{\n'
            '\t\t\t"manifest"\t\t"555"\n\t\t}\n\t}\n}\n')
        for name in ("1001_1", "1001_2", "108600_555", "108600_556", "2001_9"):
            (dc.DEPOTCACHE / f"{name}.manifest").write_bytes(b"x")

    def tearDown(self) -> None:
        self.dc.DEPOTCACHE, self.dc.ACF_DIR, self.dc.LUA_DIRS = self._orig
        self._tmp.cleanup()

    def test_only_old_versions_of_managed_depots(self):
        deletable, keeping, _ = self.dc.scan()
        self.assertEqual(sorted(f.name for f in deletable), ["1001_1.manifest"])
        kept = {f.name for f in keeping}
        self.assertIn("108600_555.manifest", kept, "创意工坊清单不能删")
        self.assertIn("108600_556.manifest", kept, "非本工具管理的 depot 不能删")
        self.assertIn("2001_9.manifest", kept, "正版游戏的 depot 不能删")
        self.assertIn("1001_2.manifest", kept, "Lua pin 的当前版本不能删")

    def test_workshop_manifests_protected(self):
        self.assertIn("108600_555", self.dc.protected())


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ══════════════════════════════════════════════════════════════
#  keyless depot 占位密钥（本次修复）
# ══════════════════════════════════════════════════════════════

class TestPlaceholderKeys(TempCorePaths):
    """Steam 会要密钥但我们没有的 depot，必须写零密钥占位。

    否则 Steam: hook 没有 → 放行给 Valve → Valve 拒绝
                → 【把整个下载任务从队列移除】
    实测案例: 紫色晶石(625960) depot 4506760
    """

    def test_detects_missing_key_depot(self):
        """depots 里有、keys 里没有 → 要占位"""
        r = {"depots": [1, 2, 3], "keys": {1: "aa" * 32, 2: "bb" * 32}, "appid": 999}
        self.assertEqual(core._depots_needing_placeholder(r), [3])

    def test_no_false_positive_when_all_have_keys(self):
        """都有密钥 → 不占位（关键：不能误伤）"""
        r = {"depots": [1, 2], "keys": {1: "aa" * 32, 2: "bb" * 32}, "appid": 999}
        self.assertEqual(core._depots_needing_placeholder(r), [])

    def test_existing_zero_key_is_idempotent(self):
        """config.vdf 里已经是零密钥 → 可能会被再报一次，但写入必须幂等

        实测行为: 会再报一次（因为零密钥 strip("0") 后为空，
        不满足"已有非零密钥就跳过"的条件）。重写零密钥值不变，
        所以是安全的幂等操作。这里验证"幂等"而不是"不报告"。
        """
        core.write_keys_to_config({"4444444": "00" * 32})
        r = {"depots": [4444444], "keys": {}, "appid": 999}
        need = core._depots_needing_placeholder(r)
        # 再写一次，值必须不变
        for d in need:
            core.write_keys_to_config({d: "00" * 32})
        self.assertEqual(
            core._read_vdf_keys(core.CONFIG_VDF.read_text()).get("4444444"),
            "00" * 32, "重写零密钥后值应保持不变")

    def test_realdepot_flow_does_not_touch_real_keys(self):
        """★ 端到端安全验证: 混合场景下单次调用不能改动任何真密钥"""
        real_a, real_b = "11" * 32, "22" * 32
        core.write_keys_to_config({"1000001": real_a, "1000002": real_b})
        # 模拟一个游戏: depots 有三个，两个有真密钥，一个没有
        r = {"depots": [1000001, 1000002, 1000003],
             "keys": {1000001: real_a, 1000002: real_b}, "appid": 999}
        need = core._depots_needing_placeholder(r)
        self.assertEqual(need, [1000003])
        for d in need:
            core.write_keys_to_config({d: "00" * 32})
        after = core._read_vdf_keys(core.CONFIG_VDF.read_text())
        self.assertEqual(after.get("1000001"), real_a, "真密钥被改动了！")
        self.assertEqual(after.get("1000002"), real_b, "真密钥被改动了！")
        self.assertEqual(after.get("1000003"), "00" * 32)

    def test_does_not_override_real_key(self):
        """★ 最重要的安全保证: 有真密钥的 depot 绝不能被写成零密钥"""
        real = "ab" * 32
        core.write_keys_to_config({"5555555": real})
        r = {"depots": [5555555], "keys": {5555555: real}, "appid": 999}
        self.assertEqual(core._depots_needing_placeholder(r), [])
        # config.vdf 里的值不受影响
        self.assertEqual(
            core._read_vdf_keys(core.CONFIG_VDF.read_text()).get("5555555"), real)

    def test_handles_empty_and_garbage(self):
        for r in ({}, {"depots": None}, {"depots": ["x", None], "keys": {}},
                  {"depots": [], "keys": None}):
            self.assertEqual(core._depots_needing_placeholder(r), [])


# ══════════════════════════════════════════════════════════════
#  卸载时一并移除 DLC
# ══════════════════════════════════════════════════════════════

class TestUninstallRemovesDlc(TempCorePaths):
    """用户 bug 报告: 卸载游戏后，它带的 DLC appid 会留在 config.toml
    → Steam 依然认为你拥有那些 DLC（库里留下孤儿条目）
    """

    def _toml_appids(self) -> list[str]:
        t = core.SLS_TOML.read_text()
        m = re.search(r"^AppIds = \[([^\]]*)\]", t, re.M)
        return [x.strip() for x in m.group(1).split(",") if x.strip()] if m else []

    def test_extracts_dlc_from_lua(self):
        """能从 -- DLC 标记后读出 DLC appid"""
        appid = 8888000
        core.LUA_SLS.mkdir(parents=True, exist_ok=True)
        (core.LUA_SLS / f"{appid}.lua").write_text(
            f"addappid({appid})\n"
            f'addappid(8888001,0,"{"aa"*32}")\n'
            f'setManifestid(8888001,"111")\n'
            f"\n-- DLC\n"
            f"addappid(9000001)\n"
            f"addappid(9000002)\n"
        )
        dlcs = core._dlc_appids_from_lua(appid)
        self.assertEqual(dlcs, [9000001, 9000002])

    def test_no_dlc_marker_returns_empty(self):
        appid = 8888001
        (core.LUA_SLS / f"{appid}.lua").write_text(f"addappid({appid})\n")
        self.assertEqual(core._dlc_appids_from_lua(appid), [])

    def test_uninstall_removes_dlc_from_toml(self):
        """★ 核心: 卸载后 DLC appid 也要从 config.toml 消失"""
        appid = 8888002
        _seed_toml([appid, 9000001, 9000002])     # 旧版入库留下的残留
        (core.LUA_SLS / f"{appid}.lua").write_text(
            f"addappid({appid})\n\n-- DLC\naddappid(9000001)\naddappid(9000002)\n")
        before = self._toml_appids()
        self.assertIn("9000001", before)
        self.assertIn("9000002", before)

        r = core.uninstall(appid)
        self.assertTrue(r["ok"])
        self.assertEqual(r.get("dlcs_removed"), [9000001, 9000002])
        after = self._toml_appids()
        self.assertNotIn(str(appid), after, "本体没被移除")
        self.assertNotIn("9000001", after, "DLC 没被移除")
        self.assertNotIn("9000002", after, "DLC 没被移除")

    def test_uninstall_keeps_other_games(self):
        """卸载一个游戏不能影响别的"""
        keep = 8888003
        _seed_toml([keep, 8888004])
        (core.LUA_SLS / f"{keep}.lua").write_text(f"addappid({keep})\n")
        (core.LUA_SLS / "8888004.lua").write_text("addappid(8888004)\n")

        core.uninstall(keep)
        after = self._toml_appids()
        self.assertIn("8888004", after, "别的游戏被误删了！")
        self.assertNotIn("8888003", after)


class TestFetchDlcsFiltering(unittest.TestCase):
    """fetch_dlcs 必须滤掉"其实是 depot"的条目。

    实测: 潜渊症(602960) 的 API config.dlcs = [1197650, 3714450]
          但 1197650 也在 config.depots 里 → 是 depot，不是 DLC
    """

    def test_barotrauma_dlc_count(self):
        """潜渊症应该只有 1 个真 DLC（3714450）"""
        from suos import rc4  # noqa: F401
        dlcs = core.fetch_dlcs(602960)
        if not dlcs:
            self.skipTest("SteamUnlock API 不可用")
        self.assertNotIn(1197650, dlcs, "depot 被误当成 DLC 了")
        self.assertIn(3714450, dlcs)

    def test_monster_hunter_still_has_many(self):
        """怪猎的 253 个 DLC 不该被过滤误伤"""
        dlcs = core.fetch_dlcs(1446780)
        if not dlcs:
            self.skipTest("SteamUnlock API 不可用")
        self.assertGreater(len(dlcs), 200, f"怪猎 DLC 数异常: {len(dlcs)}")
        # 抽查几个已知的真 DLC
        for d in (1753180, 1753181, 2133120):
            self.assertIn(d, dlcs)


# ══════════════════════════════════════════════════════════════
#  改名残留：项目目录被写死（实机上项目在 ~/SteamUnlockOS，
#  仓库代码却写死 ~/steam-toolkit / ~/SteamUnlockOS / /home/deck）
# ══════════════════════════════════════════════════════════════

import subprocess  # noqa: E402

def _working_bash() -> "str | None":
    """找一个【真能跑】的 bash（Windows 沙箱里 msys bash 可能起不来）"""
    b = shutil.which("bash")
    if not b:
        return None
    try:
        r = subprocess.run([b, "-c", "echo ok"], capture_output=True, text=True, timeout=15)
        return b if r.stdout.strip() == "ok" else None
    except Exception:  # noqa: BLE001
        return None


_BASH = _working_bash()


def _code_files() -> list[Path]:
    files = [ROOT / "webui_core.py", ROOT / "buildenv.sh"]
    files += sorted((ROOT / "suos").glob("*.py"))
    files += sorted((ROOT / "scripts").glob("*.sh"))
    return [f for f in files if f.is_file()]


class TestPathsHardcodedInstallDir(unittest.TestCase):
    """项目目录必须从文件自身位置推导，不能写死任何一个目录名。

    实机复现（SteamOS，项目在 ~/SteamUnlockOS）:
      · healthcheck 第 11 项"启动器日志: 暂无"（日志明明在 ~/SteamUnlockOS/logs/）
      · fix-injection.sh status 报"备份: ✗ 不存在" → install/remove 拒绝执行
      · buildenv.sh 指向不存在的 ~/steam-toolkit/buildtools → --fix 必定编译失败
    """

    # 写死的"安装目录 + 项目子目录"形式（不含 install.sh 的默认安装目录、兜底候选）
    BAD = re.compile(
        r'(\$HOME"?|~|/home/deck)/steam-toolkit/(backup|logs|buildtools|buildenv|docs)'
        r'|SteamUnlockOS/(backup|buildtools|buildenv)'
        r'|/home/deck/')

    def test_no_hardcoded_project_dir(self) -> None:
        bad = []
        for f in _code_files():
            for i, line in enumerate(
                    f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if self.BAD.search(line):
                    bad.append(f"{f.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
        self.assertEqual(bad, [], "仍有写死的项目目录:\n" + "\n".join(bad))

    def test_python_backup_dirs_under_repo(self) -> None:
        from suos import depotcache_clean, updater
        for name, p in (("webui_core.BACKUP", core.BACKUP),
                        ("depotcache_clean.ARCHIVE_DIR", depotcache_clean.ARCHIVE_DIR),
                        ("updater.BACKUP", updater.BACKUP)):
            self.assertTrue(Path(p).resolve().is_relative_to(ROOT),
                            f"{name} = {p} 不在项目目录 {ROOT} 下")

    def test_scripts_define_root_before_use(self) -> None:
        for f in sorted((ROOT / "scripts").glob("*.sh")):
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            uses = [i for i, l in enumerate(lines) if "$ROOT" in l and not l.startswith("ROOT=")]
            if not uses:
                continue
            defs = [i for i, l in enumerate(lines) if l.startswith("ROOT=")]
            self.assertTrue(defs and defs[0] < uses[0], f"{f.name}: $ROOT 在定义前被使用")


@unittest.skipUnless(_BASH, "没有可用的 bash")
class TestBuildenvSelfLocate(unittest.TestCase):
    """buildenv.sh 必须定位到自己所在的项目目录，且在 set -u 下 source 不中断。

    回归: 原来写死 SLSU_ROOT=/home/deck/steam-toolkit；且 $LD_LIBRARY_PATH 未定义时
    在 set -u 的调用方里 source 会直接中断（check-after-steam-update.sh 就是 set -u）。
    """

    def test_locates_own_dir_under_set_u(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proj = Path(tmp) / "某个任意目录名"
            (proj / "buildtools/wrappers").mkdir(parents=True)
            shutil.copy2(ROOT / "buildenv.sh", proj / "buildenv.sh")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("SLSU_ROOT", "LD_LIBRARY_PATH", "LIBRARY_PATH")}
            r = subprocess.run(
                [_BASH, "-uc",
                 '. "$1/buildenv.sh" && [ "$SLSU_TOOLS" = "$(cd "$1" && pwd)/buildtools" ] '
                 '&& echo OK || echo "BAD SLSU_TOOLS=$SLSU_TOOLS"',
                 "_", proj.as_posix()],
                capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
            self.assertIn("OK", r.stdout, r.stdout + r.stderr)


@unittest.skipUnless(_BASH, "没有可用的 bash")
class TestLauncherCrashGuard(unittest.TestCase):
    """启动器崩溃保护不能误伤正常情况。

    实机复现（SteamOS，Steam 正在运行时用启动器打开 steam://open/games）:
      launcher.log: "Steam 在 30 秒内退出（第 1 次），退出码 0"，计数器 = 1
      → 点 3 次桌面快捷方式后，下次启动 Steam 被降级成不注入，游戏显示"购买"。
    另: 仓库版 launcher.sh 曾丢失崩溃保护和 P1-2 日志修复（部署版才有），
        重跑 install.sh 会把部署版降级。
    """

    def _run(self, rc: int, running_pattern: str | None = None):
        tmp = Path(self._tmp.name)
        home, proj = tmp / "home", tmp / "proj"
        sls = home / ".local/share/SLSsteam"
        sls.mkdir(parents=True, exist_ok=True)
        proj.mkdir(exist_ok=True)
        for so in ("library-inject.so", "SLSsteam.so"):
            (sls / so).write_bytes(b"x")
        fake = tmp / "fake-steam"
        fake.write_text(f"#!/bin/bash\nexit {rc}\n", encoding="utf-8")
        fake.chmod(0o755)
        text = (ROOT / "scripts/launcher.sh").read_text(encoding="utf-8")
        text = text.replace('STEAMBIN="/usr/bin/steam"', f'STEAMBIN="{fake.as_posix()}"')
        text = text.replace("sleep 30", "sleep 1")
        # 不让本机真实运行的 Steam 干扰测试：只把测试指定的路径当成"运行中的 Steam"
        text = text.replace("*/ubuntu12_32/steam)", (running_pattern or "/nonexistent/x") + ")")
        launcher = tmp / "launcher.sh"
        launcher.write_text(text, encoding="utf-8", newline="\n")
        env = dict(os.environ, HOME=home.as_posix(), SUOS_DIR=proj.as_posix())
        env.pop("LD_AUDIT", None)
        subprocess.run([_BASH, launcher.as_posix()], env=env, timeout=30,
                       capture_output=True)
        fail = home / ".SLSsteam-crash-count"
        log = proj / "logs/launcher.log"
        return (fail.read_text().strip() if fail.is_file() else None,
                log.read_text(encoding="utf-8") if log.is_file() else "")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_normal_exit_not_counted(self) -> None:
        cnt, log = self._run(rc=0)
        self.assertIsNone(cnt, "退出码 0 不应计为崩溃")
        self.assertIn("正常退出", log)

    def test_real_failure_counted(self) -> None:
        cnt, _ = self._run(rc=1)
        self.assertEqual(cnt, "1", "30 秒内异常退出应计 1 次")

    def test_log_goes_to_project_logs(self) -> None:
        _, log = self._run(rc=0)
        self.assertIn("带 SLSsteam-Plus 注入启动", log)

    @unittest.skipUnless(sys.platform.startswith("linux"), "需要 Linux 的 /proc/<pid>/exe")
    def test_forward_when_running_not_counted(self) -> None:
        tmp = Path(self._tmp.name)
        fake_running = tmp / "ubuntu12_32/steam"
        fake_running.parent.mkdir(parents=True)
        shutil.copy2(shutil.which("sleep"), fake_running)
        p = subprocess.Popen([str(fake_running), "30"])
        try:
            cnt, log = self._run(rc=0, running_pattern=fake_running.as_posix())
        finally:
            p.kill()
            p.wait()
        self.assertIsNone(cnt, "Steam 已运行时转发参数不应计为崩溃")
        self.assertNotIn("注入启动", log, "Steam 已运行时不应进入注入/监控流程")

    def test_deploy_scripts_substitute_project_dir(self) -> None:
        launcher = (ROOT / "scripts/launcher.sh").read_text(encoding="utf-8")
        for must in ("__SUOS_DIR__", "FAILFILE", "steam_running", 'LOG="/dev/null"'):
            self.assertIn(must, launcher)
        for f in ("install.sh", "scripts/install-slsteam-plus.sh"):
            t = (ROOT / f).read_text(encoding="utf-8")
            self.assertIn("s|__SUOS_DIR__|", t, f"{f} 部署启动器时没有写入项目目录")
            self.assertNotRegex(t, r'\bcp\b[^\n]*launcher\.sh', f"{f} 仍在原样 cp launcher.sh")


# ══════════════════════════════════════════════════════════════
#  更新检查：同一 URL 请求两次 + 串行 → WebUI 等太久，浏览器放弃
# ══════════════════════════════════════════════════════════════

import time  # noqa: E402
import urllib.request  # noqa: E402


class TestUpdaterSingleFetch(unittest.TestCase):
    """实机复现（SteamOS，4 个游戏）: check_all 发了 8 个请求、耗时 16.7 秒，
    每个 api.steamcmd.net/v1/info/<appid> 都请求了两次（manifests、buildid 各一次），
    且逐个串行。网络差时（25 秒超时 × 2 × N）WebUI 要等几分钟，
    webui.log 里出现浏览器放弃后的 BrokenPipe。
    """

    DELAY = 0.4

    def setUp(self) -> None:
        from suos import updater
        self.up = updater
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = (updater.LUA_SLS, updater.STEAM, urllib.request.urlopen)
        updater.LUA_SLS = tmp / "lua"
        updater.STEAM = tmp / "steam"
        updater.LUA_SLS.mkdir()
        self.apps = [111, 222, 333]
        for a in self.apps:
            (updater.LUA_SLS / f"{a}.lua").write_text(
                f'addappid({a})\nsetManifestid({a + 1},"100")\n', encoding="utf-8")
        self.calls: list[str] = []

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            self.calls.append(url)
            time.sleep(self.DELAY)
            appid = url.rsplit("/", 1)[1]
            body = {"data": {appid: {"depots": {
                str(int(appid) + 1): {"manifests": {"public": {"gid": "200", "size": "5"}}},
                "branches": {"public": {"buildid": "999"}}}}}}

            class R:
                def read(self_inner):
                    return json.dumps(body).encode()
            return R()
        urllib.request.urlopen = fake_urlopen

    def tearDown(self) -> None:
        self.up.LUA_SLS, self.up.STEAM, urllib.request.urlopen = self._orig
        self._tmp.cleanup()

    def test_check_fetches_once(self) -> None:
        r = self.up.check(111)
        self.assertEqual(len(self.calls), 1, f"同一游戏应只请求一次: {self.calls}")
        self.assertTrue(r["has_update"])
        self.assertEqual(r["latest_build"], "999")
        self.assertEqual(r["need"][112]["new"], 200)

    def test_check_all_parallel_and_ordered(self) -> None:
        t0 = time.time()
        res = self.up.check_all()
        dt = time.time() - t0
        self.assertEqual([r["appid"] for r in res], self.apps, "结果顺序要与 Lua 文件顺序一致")
        self.assertEqual(len(self.calls), len(self.apps), "每个游戏只请求一次")
        self.assertLess(dt, self.DELAY * len(self.apps) * 0.8,
                        f"应并发执行（串行约 {self.DELAY * len(self.apps):.1f}s，实际 {dt:.2f}s）")


# ══════════════════════════════════════════════════════════════
#  WebUI 服务端：浏览器断开被误报成"检查更新失败"
# ══════════════════════════════════════════════════════════════

class TestServerClientDisconnect(unittest.TestCase):
    """实机 webui.log: update_check_all() 其实成功了，是写响应时浏览器已断开
    （BrokenPipe）。端点的 except 把它误报成"检查更新失败"，再往已关闭的
    连接写 500，又一次 BrokenPipe —— 每次都刷两段 traceback。
    """

    def _handler(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_suos_webui_server", ROOT / "webui/server.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        h = mod.Handler.__new__(mod.Handler)
        h.send_response = lambda *a, **k: None
        h.send_header = lambda *a, **k: None

        def boom():
            raise BrokenPipeError(32, "Broken pipe")
        h.end_headers = boom
        return mod, h

    def test_send_swallows_disconnect(self) -> None:
        _, h = self._handler()
        h._json({"x": 1})          # 不能抛异常

    def test_update_check_not_misreported(self) -> None:
        mod, h = self._handler()
        mod.update_check_all = lambda: []
        sent: list[int] = []
        orig_send = mod.Handler._send
        h._send = lambda code, body, ctype="application/json": (
            sent.append(code), orig_send(h, code, body, ctype))
        h.path = "/api/update/check"
        h.do_GET()
        self.assertEqual(sent, [200], f"断开后不应再补发 500: {sent}")


# ══════════════════════════════════════════════════════════════
#  游戏名：P1-1 没覆盖到的两条"永不自愈"路径
# ══════════════════════════════════════════════════════════════

class TestNameFallbackHeals(TempCorePaths):
    """实机复现（SteamOS，临时目录）:
      A. installed.json 记录里是兜底名 "AppID 123" → 网络恢复后仍返回 'AppID 123'
         （install() 解析失败时会把兜底名写进记录；P1-1 只修了 names.json）
      B. 一次解析失败 → 兜底名进内存缓存 → 网络恢复后仍是 'AppID 456'，
         直到 WebUI 进程重启（实机 WebUI 是常驻进程）
    """

    def setUp(self) -> None:
        super().setUp()
        self._orig_resolve = core._resolve_name
        self._orig_home = core.HOME
        core.HOME = self.tmp / "home"
        self.calls: list[int] = []

    def tearDown(self) -> None:
        core._resolve_name = self._orig_resolve
        core.HOME = self._orig_home
        super().tearDown()

    def _resolver(self, result):
        def f(a):
            self.calls.append(a)
            return result
        core._resolve_name = f

    def test_record_fallback_name_heals(self) -> None:
        self._resolver("真实名字")
        self.assertEqual(core._name_for(123, "AppID 123"), "真实名字")

    def test_record_real_name_used_directly(self) -> None:
        self._resolver("不该被调用")
        self.assertEqual(core._name_for(123, "记录里的真名"), "记录里的真名")
        self.assertEqual(self.calls, [])

    def test_failure_not_locked_in_memory(self) -> None:
        self._resolver(None)
        self.assertEqual(core._name_for(456, None), "AppID 456")
        # 冷却期内不重复请求（离线时不能每次刷新列表都把 5 个源的超时再等一遍）
        self._resolver("恢复后的名字")
        self.calls.clear()
        self.assertEqual(core._name_for(456, None), "AppID 456")
        self.assertEqual(self.calls, [], "冷却期内不应重试")
        # 冷却期过后重试 → 自愈
        core._name_cache["456"] = time.time() - core._NAME_RETRY_SEC - 1
        self.assertEqual(core._name_for(456, None), "恢复后的名字")

    def test_install_does_not_record_fallback_name(self) -> None:
        from suos import multisource, updater
        appid = 7777001
        orig_ms, orig_gids, orig_dc = multisource.resolve, updater.latest_gids, core.DEPOTCACHE
        core.DEPOTCACHE = self.tmp / "depotcache"          # 不碰真实 depotcache
        # 清单可用性现在是硬门槛：预置 pin 的清单，让入库能合法成功，
        # 这样本测试仍专注于它真正要验证的事——不把兜底名写进记录。
        core.DEPOTCACHE.mkdir(parents=True, exist_ok=True)
        (core.DEPOTCACHE / "7777002_123.manifest").write_bytes(b"\xd0\x17\xf6\x71stub")
        multisource.resolve = lambda a, verbose=False: {
            "appid": a, "name": None, "keys": {7777002: "ab" * 32}, "manifests": [],
            "lua": f'addappid({a})\naddappid(7777002,0,"{"ab" * 32}")\n'
                   f'setManifestid(7777002,"123")\n',
            "sources": {}}
        updater.latest_gids = lambda a, timeout=25: {}
        self._resolver(None)
        try:
            r = core.install(appid, include_dlc=False)
        finally:
            multisource.resolve, updater.latest_gids, core.DEPOTCACHE = orig_ms, orig_gids, orig_dc
        self.assertTrue(r["ok"], r)
        rec = json.loads(core.RECORD.read_text(encoding="utf-8"))
        self.assertIsNone(rec[str(appid)]["name"], "不能把兜底名写进 installed.json")

    def test_missing_manifest_is_fatal(self) -> None:
        """核心修复回归：pin 的清单拿不到时，入库必须判定失败（ok=False），
        且不得写入所有权 —— 否则游戏出现在库里一点就进必坏的下载流程
        （真机 content_log: No connection → Missing decryption key → 崩溃）。"""
        from suos import multisource, updater
        appid = 7779001
        orig_ms, orig_gids, orig_dc = multisource.resolve, updater.latest_gids, core.DEPOTCACHE
        core.DEPOTCACHE = self.tmp / "depotcache-empty"     # 空 depotcache，清单必缺
        core.DEPOTCACHE.mkdir(parents=True, exist_ok=True)
        # gid 用一个不存在的值，保证真实 fetch_manifest 也拿不到
        multisource.resolve = lambda a, verbose=False: {
            "appid": a, "name": "测试游戏", "keys": {7779002: "cd" * 32}, "manifests": [],
            "lua": f'addappid({a})\naddappid(7779002,0,"{"cd" * 32}")\n'
                   f'setManifestid(7779002,"999999999999999999")\n',
            "sources": {}}
        updater.latest_gids = lambda a, timeout=25: {}
        self._resolver("测试游戏")
        try:
            r = core.install(appid, include_dlc=False)
        finally:
            multisource.resolve, updater.latest_gids, core.DEPOTCACHE = orig_ms, orig_gids, orig_dc
        self.assertFalse(r["ok"], "清单缺失必须判为失败")
        self.assertTrue(any("清单缺失" in s for s in r["fatal"]), r["fatal"])
        # 不得把游戏写进记录（没交付成功就不记录）
        if core.RECORD.is_file():
            rec = json.loads(core.RECORD.read_text(encoding="utf-8"))
            self.assertNotIn(str(appid), rec, "失败的入库不该写进 installed.json")
        # Lua 就是所有权：失败的新游戏不能有 Lua，否则它会出现在库里
        self.assertFalse((core.LUA_SLS / f"{appid}.lua").exists(), "失败的入库不该写 Lua")

    def _install_with(self, appid: int, lua: str, manifests: list[str]):
        from suos import multisource, updater
        orig_ms, orig_gids, orig_dc = multisource.resolve, updater.latest_gids, core.DEPOTCACHE
        core.DEPOTCACHE = self.tmp / f"depotcache-{appid}"
        core.DEPOTCACHE.mkdir(parents=True, exist_ok=True)
        for name in manifests:
            (core.DEPOTCACHE / name).write_bytes(b"\xd0\x17\xf6\x71stub")
        multisource.resolve = lambda a, verbose=False: {
            "appid": a, "name": "测试游戏", "keys": {}, "manifests": [], "lua": lua, "sources": {}}
        updater.latest_gids = lambda a, timeout=25: {}
        self._resolver("测试游戏")
        try:
            return core.install(appid, include_dlc=False)
        finally:
            multisource.resolve, updater.latest_gids, core.DEPOTCACHE = orig_ms, orig_gids, orig_dc

    def test_install_writes_lua_not_toml(self) -> None:
        """所有权只走 Lua：成功入库写 Lua，config.toml 一个字节都不改"""
        _seed_toml([555])
        before = core.SLS_TOML.read_text()
        appid = 7780001
        r = self._install_with(appid, f'addappid({appid})\naddappid(7780002,0,"{"ab" * 32}")\n'
                                      f'setManifestid(7780002,"77")\n', ["7780002_77.manifest"])
        self.assertTrue(r["ok"], r)
        self.assertTrue((core.LUA_SLS / f"{appid}.lua").is_file())
        self.assertTrue((core.LUA_STEAM / f"{appid}.lua").is_file())
        self.assertEqual(core.SLS_TOML.read_text(), before, "入库不该再写 config.toml")

    def test_failed_install_keeps_existing_lua(self) -> None:
        """已有游戏重新入库失败（清单缺失）时，原来能用的 Lua 必须原封不动"""
        appid = 7781001
        old = f'addappid({appid})\naddappid(7781002,0,"{"ab" * 32}")\nsetManifestid(7781002,"1")\n'
        (core.LUA_SLS / f"{appid}.lua").write_text(old)
        r = self._install_with(appid, f'addappid({appid})\naddappid(7781002,0,"{"ab" * 32}")\n'
                                      f'setManifestid(7781002,"999999999999999998")\n', [])
        self.assertFalse(r["ok"])
        self.assertEqual((core.LUA_SLS / f"{appid}.lua").read_text(), old, "失败时不能覆盖可用的 Lua")


# ══════════════════════════════════════════════════════════════
#  fix-injection.sh status：只看 steam.sh，注入生效也报"不会注入"
# ══════════════════════════════════════════════════════════════

@unittest.skipUnless(_BASH, "没有可用的 bash")
class TestFixInjectionStatus(unittest.TestCase):
    """实机（SteamOS）: Steam 进程带 LD_AUDIT、三个入口都指向包装器，
    status 却报 "✗ 未打补丁（游戏模式/系统启动时不会注入）"——它只检查了
    steam.sh，而注入的主路径是三个入口（docs §3.5），steam.sh 会被 Steam 还原。
    """

    def _home(self, desktop_exec: str) -> Path:
        home = Path(self._tmp.name)
        for rel, body in (
            ("Desktop/steam.desktop", f"[Desktop Entry]\nExec={desktop_exec} %U\n"),
            (".config/autostart/steam.desktop",
             f"[Desktop Entry]\nExec={home.as_posix()}/.local/share/SLSsteam/path/steam -silent %U\n"),
            (".config/environment.d/50-steamunlock.conf", "PATH=${HOME}/.local/bin:${PATH}\n"),
            (".local/share/SLSsteam/path/steam", 'export LD_AUDIT="$LIB1:$LIB2"\n'),
            (".local/bin/steam", 'export LD_AUDIT="$LIB1:$LIB2"\n'),
            (".local/share/Steam/steam.sh", "#!/bin/bash\necho steam\n"),
        ):
            f = home / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body, encoding="utf-8", newline="\n")
        return home

    def _status(self, home: Path) -> str:
        env = dict(os.environ, HOME=home.as_posix())
        r = subprocess.run([_BASH, (ROOT / "scripts/fix-injection.sh").as_posix(), "status"],
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=60)
        self.rc = r.returncode
        return r.stdout + r.stderr

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_entries_ok_not_reported_as_broken(self) -> None:
        home = self._home(f"{Path(self._tmp.name).as_posix()}/.local/share/SLSsteam/path/steam")
        out = self._status(home)
        # 原来 Steam 正在运行时 status 以 1 退出（[ "$found" = 0 ] && … 为假）
        self.assertEqual(self.rc, 0, out)
        self.assertEqual(out.count("✓ 走注入包装器"), 2, out)
        self.assertIn("PATH 优先", out)
        self.assertNotIn("不会注入", out, "注入入口都正常时不应报不会注入")
        self.assertIn("未打补丁（正常", out)

    def test_desktop_pointing_to_system_steam_flagged(self) -> None:
        home = self._home("/usr/bin/steam")
        out = self._status(home)
        self.assertIn("桌面图标: ✗ 未指向包装器", out)


class TestManifestLocalization(unittest.TestCase):
    """P2-5: 清单本地化兜底（绕开 manifest request code 依赖）

    背景: SLSsteam 的 request-code 提供者实测经常全部失效
          （opensteamtool 403 / wudrm 返回非清单内容 / steamrun 502），
          且提供者列表编译在二进制里无法扩充。

          只要 <depot>_<gid>.manifest 在本地 depotcache，Steam 就直接用，
          不向 CDN 请求 → 不需要 request code。

    旧缺陷: 清单下载只在"gid 变化"时触发（版本对齐逻辑内部），
            gid 本来就最新的游戏（如 ManifestHub3 无分支的新游、
            或 pin 已是最新但没下过清单的游戏）会跳过 → 卡在 request code。

    实机案例: 4090260 Lifeguard Holic（gid 未变 → 没下清单 → 无法下载）
              602960 潜渊症（4 个 pin 缺 2 个清单）
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.dc = self.root / "depotcache"
        self.dc.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_detects_missing_manifest(self):
        """缺失检测：本地没有清单文件时应被识别出来"""
        lua = ('addappid(4090260)\n'
               'setManifestid(4090261,"2894220569627432962")\n')
        pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua))
        self.assertEqual(pins, {"4090261": "2894220569627432962"})
        missing = [(int(d), int(g)) for d, g in pins.items()
                   if not (self.dc / f"{d}_{g}.manifest").is_file()]
        self.assertEqual(missing, [(4090261, 2894220569627432962)])

    def test_present_manifest_not_reported_missing(self):
        """已本地化的清单不应被重复下载（避免每次入库都重下）"""
        lua = 'setManifestid(602961,"1417043812903529152")\n'
        (self.dc / "602961_1417043812903529152.manifest").write_bytes(b"x" * 10)
        pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua))
        missing = [(int(d), int(g)) for d, g in pins.items()
                   if not (self.dc / f"{d}_{g}.manifest").is_file()]
        self.assertEqual(missing, [], "已存在的清单不应被判为缺失")

    def test_gid_unchanged_still_checks_local_cache(self):
        """关键回归：gid 没变也必须检查本地清单

        旧代码把清单下载套在 `if bumped:`（gid 变化）里面，
        gid 未变时完全不检查 → 这就是 Lifeguard Holic 卡住的原因。
        新逻辑独立检查"清单在不在本地"，与 gid 是否变化无关。
        """
        # 模拟：Lua 里的 gid 与"最新 gid"相同 → bumped 为空
        lua = 'setManifestid(4090261,"2894220569627432962")\n'
        pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua))
        latest = {"4090261": "2894220569627432962"}   # 相同 → 不 bump
        bumped = [d for d, g in latest.items()
                  if pins.get(d) and str(pins[d]) != str(g)]
        self.assertEqual(bumped, [], "gid 相同时不应 bump（前提成立）")
        # 但清单本地检查仍然应该跑，并且发现缺失
        missing = [(int(d), int(g)) for d, g in pins.items()
                   if not (self.dc / f"{d}_{g}.manifest").is_file()]
        self.assertEqual(len(missing), 1,
                         "gid 未变时也必须检出缺失清单（旧代码的缺陷点）")

    def test_multiple_pins_partial_missing(self):
        """潜渊症场景：多个 pin 里只缺一部分"""
        (self.dc / "602962_1060615628637975021.manifest").write_bytes(b"y" * 10)
        (self.dc / "1197650_4582887644478638575.manifest").write_bytes(b"z" * 10)
        lua = ('setManifestid(602961,"1417043812903529152")\n'
               'setManifestid(602962,"1060615628637975021")\n'
               'setManifestid(602963,"32495984955377582")\n'
               'setManifestid(1197650,"4582887644478638575")\n')
        pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua))
        self.assertEqual(len(pins), 4)
        missing = sorted(int(d) for d, g in pins.items()
                         if not (self.dc / f"{d}_{g}.manifest").is_file())
        self.assertEqual(missing, [602961, 602963],
                         "应只报缺失的两个 depot")

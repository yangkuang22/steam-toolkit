"""SLSsteam 所有权【只读】查询（供 `status` / `plan` / `list` 显示"已声明拥有"）。

SLSsteam 的已拥有集合 = config.toml 的 AdditionalApps ∪ Lua 里的 addappid
（源码 config.cpp: reconcileIntoConfig）。本工具入库只写 Lua，config.toml 的
AppIds / AdditionalApps 只可能是旧版残留或用户手动条目，所以两处都读。
注意 UseWhitelist=false 时 AppIds 其实是黑名单，这里沿用旧口径一并计入，仅供显示。

实际的入库写入全部由 `webui_core.install()` 完成（Lua + config.vdf 密钥）。

历史说明：早期版本这里有一整套写 `config.yaml` 的方法，但 SLSsteam 从某个版本起
改用 `config.toml`，写 YAML 等于写进一个 SLSsteam 根本不读的文件。那些方法已删除，
避免"以为配置了、实际没生效"的隐患。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = ["SLSConfig", "SLS_DIR", "CONFIG_PATH", "find_slssteam"]

SLS_DIR = Path.home() / ".config" / "SLSsteam"
CONFIG_PATH = SLS_DIR / "config.yaml"
PLUGIN_DIR = SLS_DIR / "plugins"


def find_slssteam() -> dict[str, object]:
    """探测 SLSsteam 是否已安装（两个 .so 是否存在）。"""
    candidates = [
        Path.home() / ".local/share/SLSsteam",
        Path.home() / ".steam/steam",
        Path.home() / ".local/share/Steam",
    ]
    found: dict[str, object] = {"installed": False, "dirs": [], "lib": None, "inject": None}
    for d in candidates:
        lib = d / "SLSsteam.so"
        inj = d / "library-inject.so"
        if lib.is_file() or inj.is_file():
            found["installed"] = True
            found["lib"] = str(lib) if lib.is_file() else found["lib"]
            found["inject"] = str(inj) if inj.is_file() else found["inject"]
            found["dirs"].append(str(d))  # type: ignore[union-attr]
    return found


class SLSConfig:
    """SLSsteam 所有权的【只读】视图（config.toml 列表 + Lua addappid）。"""

    def __init__(self, path: Path | None = None, lua_dir: Path | None = None):
        self.path = Path(path) if path else CONFIG_PATH
        self.data: dict = {}
        self.raw: str = ""
        self.toml_path: Path | None = None
        # 默认读 SLSsteam 的 lua 目录；显式传 path（测试）时只读传入的文件，除非也给了 lua_dir
        self.lua_dir: Path | None = Path(lua_dir) if lua_dir else None
        if path is None:
            cand = CONFIG_PATH.with_suffix(".toml")
            if cand.is_file():
                self.toml_path = cand
            if self.lua_dir is None:
                self.lua_dir = SLS_DIR / "lua"
        elif str(path).endswith(".toml"):
            # 允许直接传入 .toml 路径（测试/显式调用）
            self.toml_path = Path(path)

    # ---------------------------------------------------------------- 读
    def load(self) -> "SLSConfig":
        """保留以兼容旧调用；实际读取在 owned() 里按需做（只读 config.toml）。"""
        self.raw = ""
        self.data = {}
        return self

    def exists(self) -> bool:
        """配置存在吗（显式传入的路径存在，或探测到的 config.toml 存在）。"""
        if self.path.is_file():
            return True
        return bool(self.toml_path and self.toml_path.is_file())

    # ---------------------------------------------------------------- 查询
    def owned(self) -> set[int]:
        """已声明拥有的 appid —— Lua 的 addappid + config.toml 的 AppIds / AdditionalApps。"""
        out: set[int] = set()
        if self.lua_dir and self.lua_dir.is_dir():
            for f in self.lua_dir.glob("*.lua"):
                try:
                    out.update(int(x) for x in re.findall(
                        r"addappid\(\s*(\d+)", f.read_text(encoding="utf-8", errors="replace")))
                except Exception:
                    pass
        if self.toml_path and self.toml_path.is_file():
            try:
                txt = self.toml_path.read_text(encoding="utf-8", errors="replace")
                for key in ("AppIds", "AdditionalApps"):
                    m = re.search(rf"^{key}\s*=\s*\[([^\]]*)\]", txt, re.M)
                    if not m:
                        continue
                    for x in m.group(1).split(","):
                        x = x.strip()
                        if x.isdigit():
                            out.add(int(x))
            except Exception:
                pass
        return out

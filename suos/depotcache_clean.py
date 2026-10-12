"""depotcache 清理 —— 删除无引用的旧清单

背景:
  每次游戏更新都会下载新清单到 Steam/depotcache/，
  但旧版本的清单从不删除。实测堆积了 692 个文件 / 191 MB，
  其中 648 个 / 157 MB 是无引用的旧版本。

安全设计:
  ⓪ 只管自己: 只清理我们 Lua 里 setManifestid 过的 depot 的旧版本。
     正版游戏的 depot、创意工坊（清单以 <appid>_<gid> 存在同一目录）一概不碰——
     旧版本曾把僵尸毁灭工程 111 个工坊模组的清单当垃圾删掉。
  ① 白名单: Lua pin 的 + ACF 记录的 + 创意工坊记录的，绝不删
  ② 先归档: 打包成 tar.zst 后才删原文件，可一键恢复
  ③ 校验:   归档完整性验证通过才执行删除

用法:
  python3 -m suos.depotcache_clean --check     # 只报告，不动文件
  python3 -m suos.depotcache_clean --apply     # 归档 + 删除
  python3 -m suos.depotcache_clean --restore   # 从归档恢复
"""
from __future__ import annotations

import re
import subprocess
import tarfile
import time
from pathlib import Path

STEAM = Path.home() / ".local/share/Steam"
DEPOTCACHE = STEAM / "depotcache"
ACF_DIR = STEAM / "steamapps"
LUA_DIRS = [
    Path.home() / ".config/SLSsteam/lua",
    STEAM / "config/lua",
]
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "backup/depotcache-archive"   # 项目目录下
KEEP_ARCHIVES = 2          # 只保留最近 N 个归档（防止无限堆积）


def _lua_pins() -> list[tuple[str, str]]:
    pins: list[tuple[str, str]] = []
    for d in LUA_DIRS:
        if not d.is_dir():
            continue
        for f in d.glob("*.lua"):
            pins += re.findall(r'setManifestid\((\d+),\s*"(\d+)"', f.read_text(errors="replace"))
    return pins


def managed_depots() -> set[str]:
    """我们自己管理的 depot（Lua 里 setManifestid 过的）。只有它们的旧版本才可能被清理。"""
    return {dep for dep, _ in _lua_pins()}


def protected() -> set[str]:
    """绝对不删的清单（Lua pin 的 + ACF 记录的 + 创意工坊记录的）"""
    keep: set[str] = {f"{dep}_{gid}" for dep, gid in _lua_pins()}
    for f in ACF_DIR.glob("appmanifest_*.acf"):
        t = f.read_text(errors="replace")
        for dep, gid in re.findall(r'"(\d{5,8})"\s*\{\s*"manifest"\s*"(\d+)"', t):
            keep.add(f"{dep}_{gid}")
    for f in (ACF_DIR / "workshop").glob("appworkshop_*.acf"):
        m = re.match(r"appworkshop_(\d+)\.acf$", f.name)
        if not m:
            continue
        for gid in re.findall(r'"manifest"\s*"(\d+)"', f.read_text(errors="replace")):
            keep.add(f"{m.group(1)}_{gid}")
    return keep


def scan() -> tuple[list[Path], list[Path], dict]:
    """返回 (可删文件, 保留文件, 统计)"""
    keep_keys = protected()
    ours = managed_depots()
    files = list(DEPOTCACHE.glob("*.manifest"))
    deletable, keeping = [], []
    for f in files:
        m = re.match(r"(\d+)_(\d+)\.manifest$", f.name)
        if not m or m.group(1) not in ours or f"{m.group(1)}_{m.group(2)}" in keep_keys:
            keeping.append(f)
        else:
            deletable.append(f)
    stats = {
        "total": len(files),
        "deletable": len(deletable),
        "keeping": len(keeping),
        "deletable_bytes": sum(f.stat().st_size for f in deletable),
        "total_bytes": sum(f.stat().st_size for f in files),
    }
    return deletable, keeping, stats


def archive_and_remove(deletable: list[Path], verbose: bool = True) -> Path | None:
    """打包后删除。返回归档路径。"""
    if not deletable:
        return None
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tar_path = ARCHIVE_DIR / f"depotcache-{stamp}.tar.zst"

    # 用系统 tar + zstd（SteamOS 都有）
    file_list = ARCHIVE_DIR / f".list-{stamp}.txt"
    file_list.write_text("\n".join(str(f) for f in deletable))
    try:
        r = subprocess.run(
            ["tar", "--zstd", "-cf", str(tar_path), "-C", "/", "-T", str(file_list)],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            if verbose:
                print(f"  ✗ 打包失败: {r.stderr[:200]}")
            return None
    finally:
        file_list.unlink(missing_ok=True)

    # 校验归档
    try:
        r2 = subprocess.run(["tar", "--zstd", "-tf", str(tar_path)],
                            capture_output=True, text=True, timeout=600)
        n_in_tar = len([x for x in r2.stdout.splitlines() if x.strip()])
        if n_in_tar != len(deletable):
            if verbose:
                print(f"  ✗ 校验失败: 归档里 {n_in_tar} 个，预期 {len(deletable)} 个")
            return None
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  ✗ 校验异常: {exc}")
        return None

    # 校验通过 → 删除原文件
    removed = 0
    for f in deletable:
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass

    # ★ 只保留最近 KEEP_ARCHIVES 个归档（否则每次 autocheck 都会新增一个，
    #   长期下来会无限堆积）
    olds = sorted(ARCHIVE_DIR.glob("depotcache-*.tar.zst"))
    if len(olds) > KEEP_ARCHIVES:
        for old_tar in olds[:-KEEP_ARCHIVES]:
            try:
                old_tar.unlink()
                if verbose:
                    print(f"  （清理旧归档 {old_tar.name}）")
            except Exception:
                pass
    if verbose:
        print(f"  ✓ 归档 {len(deletable)} 个到 {tar_path.name}")
        print(f"  ✓ 删除原文件 {removed} 个")
    return tar_path


def restore(tar_path: Path | None = None) -> int:
    """从归档恢复"""
    if tar_path is None:
        cands = sorted(ARCHIVE_DIR.glob("depotcache-*.tar.zst"))
        if not cands:
            print("找不到归档")
            return 0
        tar_path = cands[-1]
    r = subprocess.run(["tar", "--zstd", "-xf", str(tar_path), "-C", "/"],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        print(f"恢复失败: {r.stderr[:200]}")
        return 0
    n = len(list(DEPOTCACHE.glob("*.manifest")))
    print(f"已恢复，当前缓存 {n} 个文件")
    return n


def _cleanup_temp_lists() -> None:
    """清理打包失败残留的 .list-*.txt"""
    try:
        for f in ARCHIVE_DIR.glob(".list-*.txt"):
            f.unlink(missing_ok=True)
    except Exception:
        pass


def main() -> int:
    _cleanup_temp_lists()
    import sys
    args = sys.argv[1:]
    if "--restore" in args:
        restore()
        return 0

    deletable, keeping, st = scan()
    print("=" * 62)
    print(" depotcache 清理" + ("（只检查）" if "--check" in args or "--apply" not in args else "（执行）"))
    print("=" * 62)
    print(f"  总文件:   {st['total']:>5} 个  {st['total_bytes']/1048576:>7.1f} MB")
    print(f"  保留:     {st['keeping']:>5} 个（非本工具管理的 depot / Lua pin / ACF / 创意工坊）")
    print(f"  可清理:   {st['deletable']:>5} 个  {st['deletable_bytes']/1048576:>7.1f} MB")
    print()
    if "--apply" not in args:
        print("  加 --apply 执行（会先归档再删，可恢复）")
        return 0
    archive_and_remove(deletable)
    _, _, st2 = scan()
    print()
    print(f"  清理后: {st2['total']} 个  {st2['total_bytes']/1048576:.1f} MB")
    print(f"  节省:   {(st['total_bytes']-st2['total_bytes'])/1048576:.1f} MB")
    print(f"  归档在: {ARCHIVE_DIR}")
    print(f"  恢复:   python3 -m suos.depotcache_clean --restore")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

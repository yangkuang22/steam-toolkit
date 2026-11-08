"""Steam Toolkit 命令行入口。

用法（在 ~/steam-toolkit 下）：
    python3 -m suos.cli status                    看环境
    python3 -m suos.cli install-mh3 379720        ★ 一键入库（推荐）
    python3 -m suos.cli install-mh3 379720 --dlc  入库并带上全部 DLC
    python3 -m suos.cli install-mh3 379720 --dry-run   只预览数据，不写入
    python3 -m suos.cli list                      列出本地已备好清单、可入库的游戏

所有入库命令（install / install-mh3 / prepare / install-server）现在都走
同一条实现 webui_core.install()，与图形界面完全一致。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from . import steam, vdf
from .slsconfig import SLSConfig, find_slssteam

BACKUP_DIR = Path(__file__).resolve().parent.parent / "backup"
MANIFEST_DIR = Path(__file__).resolve().parent.parent / "manifests"


# ══════════════════════════════════════════════════════════════
#  统一入库入口
#
#  所有"往 Steam 里加游戏"的命令（install / install-mh3 / prepare /
#  install-server）都走这里 → webui_core.install()，和图形界面完全同一条
#  代码路径、同一套防护（清单可用性硬门槛、下载重试、占位密钥安全红线、
#  Lua 格式对齐）。历史上这里有三条各自残缺的独立路径，是"修好一个游戏、
#  下一个又坏"的根源，已合并。
# ══════════════════════════════════════════════════════════════

def _webui_core():
    """加载仓库根目录下的 webui_core（唯一的入库实现）。"""
    root = str(Path(__file__).resolve().parent.parent)
    if root not in sys.path:
        sys.path.insert(0, root)
    import webui_core  # noqa: E402
    return webui_core


def _cli_preview(appid: int) -> int:
    """只查不写：这个 appid 有没有可用数据。"""
    r = _webui_core().search(appid)
    print(f"appid     : {r['appid']}")
    print(f"游戏名    : {r.get('name') or '（查不到）'}")
    print(f"有数据    : {'是' if r.get('found') else '否'}")
    print(f"密钥      : {r.get('depot_count', 0)} 个"
          f"（来源 {', '.join(r.get('key_sources') or []) or '无'}）")
    print(f"已入库    : {'是' if r.get('installed') else '否'}")
    msg = r.get("message")
    if msg:
        print(f"\n{msg}")
    print("\n（这是预览，未写入任何文件。真正入库请去掉 --dry-run / 加 --apply）")
    return 0 if r.get("found") else 1


def _cli_install(appid: int, include_dlc: bool) -> int:
    """真正入库：webui_core.install()。"""
    print(f"== 入库 appid {appid}" + ("（含 DLC）" if include_dlc else "") + " ==")
    res = _webui_core().install(appid, include_dlc=include_dlc)
    for s in res.get("steps", []):
        print(f"  {s}")
    print()
    print(res.get("message", ""))
    if res.get("ok"):
        print("\n下一步：重启 Steam（~/.local/share/SLSsteam/path/steam），再在库里点「安装」")
        return 0
    print("\n⚠ 入库未完成——请勿重启 Steam，按上面的提示稍后重试")
    return 1


def _do_install(args: argparse.Namespace, *, preview_default: bool) -> int:
    """统一调度：决定预览还是真入库，然后都走 webui_core。

    preview_default=True  → 不加 --apply 就只预览（install / install-server 的历史行为）
    preview_default=False → 直接入库（prepare / install-mh3 的历史行为）
    --dry-run 任何情况下都只预览。
    """
    appid = int(args.appid)
    if getattr(args, "dry_run", False) or (preview_default and not getattr(args, "apply", False)):
        return _cli_preview(appid)
    return _cli_install(appid, include_dlc=getattr(args, "dlc", False))


@dataclass
class DepotPlan:
    depotid: int
    gid: int
    manifest_name: str
    key: str | None
    from_cache: bool


@dataclass
class InstallPlan:
    appid: int
    title: str
    depots: list[DepotPlan]
    already_owned: bool

    @property
    def missing_keys(self) -> list[int]:
        return [d.depotid for d in self.depots if not d.key]


def _depot_index(paths: steam.SteamPaths) -> dict[int, tuple[int, str]]:
    """{depotid: (gid, 文件名)}，每个 depot 取 gid 最大的清单。

    来源：Steam 的 depotcache + 我们的 manifests/ 裸清单目录（后者优先补缺）。
    """
    index = steam.best_manifests([MANIFEST_DIR], paths=paths)
    if MANIFEST_DIR.is_dir():
        for entry in MANIFEST_DIR.iterdir():
            if entry.suffix != ".manifest":
                continue
            parts = entry.stem.split("_")
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                depotid, gid = int(parts[0]), int(parts[1])
                if depotid not in index or index[depotid][0] < gid:
                    index[depotid] = (gid, entry.name)
    return index


def _manifests_for_app(paths: steam.SteamPaths, appid: int) -> list[tuple[int, int, str]]:
    """找出某 appid 的 appmanifest_<appid>.acf 里列出的 depot 及各 depotid 的清单。

    ACF 里的 InstalledDepots 段给出该游戏用到的 depotid；清单 GID 从 depotcache 找。
    ACF 不存在时返回空列表（需要服务端提供清单，暂未接入）。
    """
    acf = steam.acf_path(paths, appid)
    if acf is None:
        return []
    from . import vdf

    try:
        data = vdf.load(acf)
    except Exception:
        return []
    node = data.get("AppState", data)
    depots = node.get("InstalledDepots") or {}
    out: list[tuple[int, int, str]] = []
    index = _depot_index(paths)
    for depotid_str in depots:
        if not str(depotid_str).isdigit():
            continue
        depotid = int(depotid_str)
        if depotid in index:
            gid, name = index[depotid]
            out.append((depotid, gid, name))
    return out


def build_plan(paths: steam.SteamPaths, appid: int) -> InstallPlan:
    keys = steam.load_depot_keys(paths)
    owned = SLSConfig().load().owned()
    installed = steam.installed_appids(paths)

    entries = _manifests_for_app(paths, appid)
    index = _depot_index(paths)

    depots: list[DepotPlan] = []
    if entries:
        for depotid, gid, name in entries:
            depots.append(
                DepotPlan(
                    depotid=depotid,
                    gid=gid,
                    manifest_name=name,
                    key=keys.get(depotid),
                    from_cache=(paths.depotcache / name).is_file(),
                )
            )
    else:
        # 没有 ACF（未安装）：退回到"depotcache 里按 depotid 前缀归属该 appid 的清单"
        for depotid, (gid, name) in sorted(index.items()):
            if steam.app_of_depot(depotid) == appid:
                depots.append(
                    DepotPlan(
                        depotid=depotid,
                        gid=gid,
                        manifest_name=name,
                        key=keys.get(depotid),
                        from_cache=(paths.depotcache / name).is_file(),
                    )
                )

    return InstallPlan(
        appid=appid,
        title=f"appid {appid}",
        depots=depots,
        already_owned=appid in owned or appid in installed,
    )


def cmd_update(paths, args):
    """检查/应用游戏更新"""
    from . import updater
    if getattr(args, "all", False):
        results = updater.check_all(verbose=True)
        need = [r for r in results if r.get("has_update")]
        print(f"\n{len(results)} 个游戏，{len(need)} 个有更新")
        if need and getattr(args, "apply", False):
            for r in need:
                res = updater.apply(r["appid"])
                print(f"  AppID {r['appid']}: {res.get('message')}")
        elif need:
            print("加 --apply 参数实际更新")
        return
    if not getattr(args, "appid", None):
        print("用法: python3 -m suos.cli update <appid> [--apply]")
        print("      python3 -m suos.cli update --all [--apply]")
        return 1
    appid = int(args.appid)
    if getattr(args, "apply", False):
        res = updater.apply(appid, verbose=True)
        print(f"\n{res.get('message')}")
        for s in res.get("steps", []):
            print(f"  {s}")
        if res.get("ok") and res.get("downloaded"):
            print("\n→ 重启 Steam 后自动开始下载")
    else:
        updater.check(appid, verbose=True)


def cmd_status(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    print(paths.describe())
    keys = steam.load_depot_keys(paths)
    print(f"\nconfig.vdf 里的 depot 密钥: {len(keys)} 个")
    manifests = list(paths.depotcache.glob("*.manifest")) if paths.depotcache.is_dir() else []
    print(f"depotcache 里的清单       : {len(manifests)} 个")
    print(f"已安装游戏 (ACF)          : {len(steam.installed_appids(paths))} 个")
    print(f"我们的裸清单目录          : {MANIFEST_DIR} "
          f"({len(list(MANIFEST_DIR.glob('*.manifest'))) if MANIFEST_DIR.is_dir() else 0} 个)")

    cfg = SLSConfig().load()
    print(f"\nSLSsteam 配置             : {cfg.path} ({'存在' if cfg.exists() else '不存在'})")
    print(f"  已声明拥有              : {len(cfg.owned())} 个 appid")
    sls = find_slssteam()
    print(f"SLSsteam 安装             : {'已安装' if sls['installed'] else '未安装'}")
    if sls["installed"]:
        print(f"  SLSsteam.so             : {sls['lib']}")
        print(f"  library-inject.so       : {sls['inject']}")
    else:
        print("  → 运行 python3 -m suos.cli setup-slssteam 安装（需要网络）")
    return 0


def cmd_list(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """列出本地清单能覆盖到的 appid（按 depotid 前缀归组）。"""
    keys = steam.load_depot_keys(paths)
    installed = steam.installed_appids(paths)
    index = _depot_index(paths)
    # 用"已安装游戏的 appid"当候选集合，能显著提高 depotid -> appid 的归组准确率
    candidates = steam.installed_appids(paths)
    by_app: dict[int, list[int]] = {}
    for depotid in index:
        by_app.setdefault(steam.app_of_depot(depotid, candidates), []).append(depotid)
    print(f"{'appid':>9} {'depot':>6} {'有密钥':>7} {'已安装':>7}")
    for appid in sorted(by_app):
        deps = by_app[appid]
        has_key = sum(1 for d in deps if d in keys)
        print(f"{appid:>9} {len(deps):>6} {has_key:>7} {'是' if appid in installed else '否':>7}")
    print(f"\n共 {len(by_app)} 个 appid 可以入库（清单已在本机）")
    return 0


def cmd_plan(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    plan = build_plan(paths, args.appid)
    print(f"目标 appid : {plan.appid}")
    print(f"已拥有     : {'是' if plan.already_owned else '否（将写入 SLSsteam 配置）'}")
    print(f"清单数量   : {len(plan.depots)}")
    if not plan.depots:
        print("\n⚠ 没找到该 appid 的清单。需要先从服务端拉取 .lua + .manifest（阶段 2 未接入）。")
        return 1
    print(f"\n{'depotid':>10} {'manifest gid':>20} {'密钥':>6} {'来源':>10}")
    for d in plan.depots:
        print(f"{d.depotid:>10} {d.gid:>20} {'有' if d.key else '缺':>6} "
              f"{'Steam 缓存' if d.from_cache else '待放置':>10}")
    if plan.missing_keys:
        print(f"\n注意: {len(plan.missing_keys)} 个 depot 没有解密密钥（config.vdf 里没有），"
              f"这些 depot 的文件将无法解密下载。")
    return 0


def cmd_install(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """入库某个 appid（统一走 webui_core）。不加 --apply 只预览。"""
    return _do_install(args, preview_default=True)


def cmd_setup_slssteam(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    root = Path(__file__).resolve().parent.parent
    print("SLSsteam 安装/更新脚本：")
    print(f"    bash {root}/scripts/setup-slssteam.sh")
    print("带注入重启 Steam：")
    print(f"    bash {root}/scripts/restart-steam-injected.sh")
    return 0


def cmd_fetch(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """从作者服务端拉一个 appid 的入库数据（仅下载+解析，不落盘）。"""
    from . import server_api

    cache = Path(__file__).resolve().parent.parent / "cache"
    try:
        payload = server_api.install_plan(args.appid, cache_dir=cache)
    except Exception as exc:  # 网络/协议问题都给用户看得懂的提示
        print(f"✗ 拉取失败: {exc}")
        return 1
    print(payload.describe())
    if payload.depots:
        print(f"\n{'depotid':>9} {'gid':>20} {'大小':>10} {'平台':>8} {'密钥':>4}")
        for d in payload.depots:
            size = (
                f"{d.size / 1024**3:.2f}GB"
                if d.size > 10**9
                else f"{d.size / 1024**2:.1f}MB"
            )
            print(f"{d.depotid:>9} {d.gid or 0:>20} {size:>10} "
                  f"{d.oslist or '-':>8} {'有' if d.key else '缺':>4}")
    print(f"\n原始响应已缓存到 {cache}/GetAppinfo_{args.appid}.bin")
    print("要真正入库请跑：python3 -m suos.cli install <appid> --from-server --apply")
    return 0



def cmd_list_branches(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """列出某个游戏可选的版本分支（来自 SteamUnlock 服务端 appinfo）"""
    import ast
    from . import rc4, server_api, appinfo_parse
    from pathlib import Path as _P
    appid = args.appid
    server_api.install_plan(appid, cache_dir=_P("cache"))
    f = _P("cache/GetAppinfo_%d.bin" % appid)
    if not f.is_file():
        print("✗ 拿不到 appinfo")
        return 1
    j = ast.literal_eval(rc4.rc4(rc4.RC4_KEY, f.read_bytes()).decode("utf-8", "replace"))
    dep = appinfo_parse.parse_depots(j.get("appinfo", ""))
    if not dep:
        print("✗ 解析不到 depot")
        return 1
    # 汇总所有分支及其大小
    allb = {}
    for depid, info in dep.items():
        for b, m in info.get("manifests", {}).items():
            allb.setdefault(b, []).append((depid, m["gid"], m.get("size", 0)))
    print(f"== appid {appid} 的可选版本（共 {len(allb)} 个）==")
    for b in list(allb)[:40]:
        tot = sum(x[2] for x in allb[b])
        print(f"  {b:<28} {len(allb[b])} 个 depot, {tot/1048576:.0f} MB")
    print()
    print("用法: python3 -m suos.cli prepare <appid> --branch <分支名>")
    return 0


def cmd_mh3(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """★ 一键入库（多源聚合：ManifestHub3 + 社区密钥库 + 服务端）。"""
    return _do_install(args, preview_default=False)


def cmd_mh3_check(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """检查某个 appid 在 ManifestHub3 有没有数据"""
    from . import manifesthub3 as m3
    if m3.branch_exists(args.appid):
        files = m3.list_files(args.appid)
        print(f"★ {args.appid} 有数据（{len(files)} 个文件）")
        for f in files:
            print(f"    {f['name']:<44} {f.get('size',0):>9} B")
        return 0
    print(f"✗ {args.appid} 没有数据")
    return 1


def cmd_prepare(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """★ 一键入库（统一走 webui_core）。"""
    return _do_install(args, preview_default=False)


def cmd_install_server(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """从服务端取数据并入库（统一走 webui_core）。不加 --apply 只预览。"""
    return _do_install(args, preview_default=True)


OVERLAP_INTERNAL_ERROR = 3   # 与 0/1/2（正常/警告/错误）区分，healthcheck.sh 据此计数


def _print_overlap_report(rep, limit: int) -> None:
    from suos.depot_overlap import Status

    print(f"== {rep.appid} {rep.name}")
    if rep.install_dir:
        print(f"   安装目录           : {rep.install_dir}")
    if rep.mount_order:
        print(f"   挂载顺序(末位胜出) : {' → '.join(map(str, rep.mount_order))}")
    if rep.priority:
        print(f"   期望优先级(高→低)  : {' > '.join(map(str, rep.priority))}")
    for problem in rep.problems:
        print(f"   ⚠ {problem}")
    if not rep.findings:
        print("   ✓ 单 depot，不存在重叠" if len(rep.mount_order) < 2
              else "   ✓ 没有内容不同的同名文件")
        return

    n = {s: len(rep.with_status(s)) for s in Status}
    print(f"   冲突路径 {len(rep.findings)} 个：✓ 正确 {n[Status.OK]}  ✓ 已认可 {n[Status.ACCEPTED]}"
          f"  ✗ 盖错 {n[Status.WRONG_DEPOT]}  ✗ 缺失 {n[Status.MISSING]}"
          f"  · 补丁/手改 {n[Status.MODIFIED]}")
    bad = [f for f in rep.findings if f.status in (Status.WRONG_DEPOT, Status.MISSING)]
    for f in bad[:limit]:
        if f.status is Status.MISSING:
            print(f"     ✗ 缺失  {f.path}  （期望来自 depot {f.expected_depot}）")
        else:
            print(f"     ✗ 盖错  {f.path}  期望 {f.expected_depot}，实际 "
                  f"{'/'.join(map(str, f.actual_depots))}")
    if len(bad) > limit:
        print(f"     … 另有 {len(bad) - limit} 个")

    risks = rep.order_risks
    if risks:
        print(f"   ⚠ 挂载顺序风险 {len(risks)} 个：验证完整性/更新后，这些文件会换成低优先级版本")
        if not bad:
            for f in risks[:limit]:
                print(f"     ⚠ {f.path}  按当前顺序会落 depot {f.order_winner}，期望 {f.expected_depot}")
            if len(risks) > limit:
                print(f"     … 另有 {len(risks) - limit} 个")
        print("     处理：不要验证完整性；或卸载后用新版工具重新入库再安装（重新登记挂载顺序）")
    if n[Status.WRONG_DEPOT] or n[Status.MODIFIED]:
        print(f"   提示：如果是打补丁带来的、且游戏运行正常，"
              f"运行 check-overlap {rep.appid} --accept 记下当前版本，之后不再报警")
    print("   结论: " + ("✓ 正常", "⚠ 有风险", "✗ 有文件被旧版本盖掉")[rep.severity])


def _overlap_healthcheck(reports: list) -> int:
    from suos.depot_overlap import Status

    def line(label: str, text: str) -> None:
        print(f"  {label:<52} {text}")

    shown = 0
    for rep in reports:
        label = f"{rep.appid} {rep.name}"[:40]
        if rep.severity == 2:
            wrong = len(rep.with_status(Status.WRONG_DEPOT))
            missing = len(rep.with_status(Status.MISSING))
            detail = f"{wrong} 个文件被其他 depot 盖错" + (f"，{missing} 个缺失" if missing else "")
            line(label, f"✗ {detail}（check-overlap {rep.appid} 看详情；补丁所致可 --accept）")
        elif rep.severity == 1:
            parts = []
            if rep.order_risks:
                parts.append(f"{len(rep.order_risks)} 个文件在验证完整性后会被旧版盖掉，别验证")
            parts.extend(rep.problems[:1])
            line(label, "⚠ " + "；".join(parts))
        else:
            continue
        shown += 1
    if not shown:
        line("depot 重叠覆盖", f"✓ {len(reports)} 个多 depot 游戏均正常")
    return max((r.severity for r in reports), default=0)


def cmd_check_overlap(paths: steam.SteamPaths, args: argparse.Namespace) -> int:
    """检查多个 depot 的同名文件是否落对了版本（旧 DLC 盖新 DLC 会导致游戏启动报错）。"""
    from suos import depot_overlap

    try:
        priority = ([int(x) for x in args.priority.split(",") if x.strip()]
                    if args.priority else None)
    except ValueError:
        print("✗ --priority 只能是逗号分隔的 depot ID", file=sys.stderr)
        return OVERLAP_INTERNAL_ERROR
    if not args.appids and not args.all:
        print("✗ 需要指定 appid，或用 --all 检查全部多 depot 游戏", file=sys.stderr)
        return OVERLAP_INTERNAL_ERROR
    if args.accept and not args.appids:
        print("✗ --accept 必须明确指定 appid（避免一次认可所有游戏）", file=sys.stderr)
        return OVERLAP_INTERNAL_ERROR
    try:
        appids = args.appids or depot_overlap.multi_depot_apps(paths)
        if args.accept:
            for appid in appids:
                rep = depot_overlap.check_app(paths, appid, priority, hash_all=True)
                count = depot_overlap.save_accepted(rep)
                print(f"✓ {appid} {rep.name}: 已把 {count} 个偏离期望版本的文件记为基线"
                      f" → {depot_overlap.accepted_path(appid)}")
        reports = [depot_overlap.check_app(paths, a, priority, depot_overlap.load_accepted(a))
                   for a in appids]
    except Exception as exc:  # noqa: BLE001 —— 体检不能被单个坏文件打断，如实报错即可
        print(f"✗ depot 重叠检查出错: {exc}", file=sys.stderr)
        return OVERLAP_INTERNAL_ERROR

    if args.healthcheck:
        return _overlap_healthcheck(reports)
    if not reports:
        print("✓ 没有挂载了多个 depot 的已安装游戏")
        return 0
    for rep in reports:
        _print_overlap_report(rep, max(1, args.limit))
    return max(r.severity for r in reports)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="suos", description="Steam Toolkit —— SteamUnlock 的 SteamOS 数据管道")
    parser.add_argument("--steam-root", help="手动指定 Steam 根目录")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="显示环境状态")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="列出本地有清单的游戏")
    p_list.set_defaults(func=cmd_list)

    p_plan = sub.add_parser("plan", help="演算某个 appid 的入库计划（不落盘）")
    p_plan.add_argument("appid", type=int)
    p_plan.set_defaults(func=cmd_plan)

    p_install = sub.add_parser("install", help="入库某个 appid（不加 --apply 只预览）")
    p_install.add_argument("appid", type=int)
    p_install.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    p_install.add_argument("--dlc", action="store_true", help="同时入库全部 DLC")
    p_install.set_defaults(func=cmd_install)

    p_setup = sub.add_parser("setup-slssteam", help="安装 SLSsteam 的说明")
    p_setup.set_defaults(func=cmd_setup_slssteam)

    p_fetch = sub.add_parser("fetch", help="从服务端拉取某个 appid 的入库数据（不落盘）")
    p_fetch.add_argument("appid", type=int)
    p_fetch.set_defaults(func=cmd_fetch)

    p_prep = sub.add_parser("prepare", help="★ 一键入库（等同 install-mh3，已统一）")
    p_prep.add_argument("appid", type=int, help="Steam AppID")
    p_prep.add_argument("--dlc", action="store_true", help="同时入库全部 DLC")
    p_prep.add_argument("--dry-run", action="store_true", help="只看不写")
    p_prep.set_defaults(func=cmd_prepare)

    p_mh3 = sub.add_parser("install-mh3", help="★ 一键入库（推荐，多源聚合，数据最全）")
    p_mh3.add_argument("appid", type=int)
    p_mh3.add_argument("--dlc", action="store_true", help="同时入库全部 DLC")
    p_mh3.add_argument("--dry-run", action="store_true", help="只看不写")
    p_mh3.set_defaults(func=cmd_mh3)

    p_mh3c = sub.add_parser("check-mh3", help="检查 ManifestHub3 有没有该游戏的数据")
    p_mh3c.add_argument("appid", type=int)
    p_mh3c.set_defaults(func=cmd_mh3_check)

    p_br = sub.add_parser("list-branches", help="列出游戏的可选版本（来自 SteamUnlock appinfo）")
    p_br.add_argument("appid", type=int, help="Steam AppID")
    p_br.set_defaults(func=cmd_list_branches)

    p = sub.add_parser("update", help="检查/应用游戏更新")

    p.add_argument("appid", nargs="?", default=None, help="AppID（留空配合 --all）")

    p.add_argument("--all", action="store_true", help="检查所有已入库游戏")

    p.add_argument("--apply", action="store_true", help="实际更新（下新清单+改Lua）")

    p.set_defaults(func=cmd_update)

    p_isrv = sub.add_parser("install-server", help="入库某个 appid（等同 install，已统一）")
    p_isrv.add_argument("appid", type=int)
    p_isrv.add_argument("--apply", action="store_true", help="真正写入（默认只预演）")
    p_isrv.add_argument("--dlc", action="store_true", help="同时入库全部 DLC")
    p_isrv.set_defaults(func=cmd_install_server)

    p_ov = sub.add_parser("check-overlap",
                          help="检查多 depot 同名文件是否落对版本（防止旧 DLC 盖掉新 DLC）")
    p_ov.add_argument("appids", nargs="*", type=int, help="AppID，可多个")
    p_ov.add_argument("--all", action="store_true", help="检查全部挂载了多个 depot 的已安装游戏")
    p_ov.add_argument("--priority", help="手动指定优先级（高→低），逗号分隔的 depot ID")
    p_ov.add_argument("--limit", type=int, default=20, help="每个游戏最多列几条问题（默认 20）")
    p_ov.add_argument("--healthcheck", action="store_true", help="healthcheck.sh 用的单行输出")
    p_ov.add_argument("--accept", action="store_true",
                      help="把当前偏离期望版本的文件记为基线（打完补丁、确认游戏正常后用）")
    p_ov.set_defaults(func=cmd_check_overlap)

    args = parser.parse_args(argv)
    try:
        paths = steam.find_steam(args.steam_root)
    except steam.SteamNotFound as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    return args.func(paths, args)


if __name__ == "__main__":
    raise SystemExit(main())

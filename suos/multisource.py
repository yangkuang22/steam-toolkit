"""多数据源统一安装器

数据源（按优先级）:
  ① Sudama 密钥库        api.993499094.xyz/depotkeys.json   239,946 条密钥
  ② 社区 depotkeys.json  SteamAutoCracks/ManifestHub        288,381 条密钥
  ③ ManifestHub3         steamtools-games/ManifestHub3      62,000+ 分支（密钥+清单+Lua）
  ④ SteamUnlock 服务端   auth1.caigamer.cn/GetAppinfo       密钥 + 权威 depot 列表

策略:
  · depot 全集: Steam appinfo（api.steamcmd.net，Valve 权威）
                + ManifestHub3 分支 + SteamUnlock 服务端
  · 密钥: Sudama → 社区库 → ManifestHub3 → 服务端（谁有就用谁）
  · 清单: ManifestHub3 → 按权威 gid 现取

★ 2026-11-07 修过的坑（appid 2696050 Yarimono 实证）
  ① DLC 自有 depot 不在解析范围内
     DLC 可以有【自己的 depot】（appinfo 的 hasdepotsindlc=1 / depot 的
     depotfromapp 指向 DLC app）。入库时 Lua 用 addappid 注入了 DLC 所有权，
     Steam 随即把这些 depot 加进下载集合并索要密钥。只解析本体 depot 的话
     必然缺密钥，Steam 报
         Failed to initialize depot 3593650 (Missing decryption key)
     并取消【整个】下载任务 —— 用户看到的就是"内容仍处于加密状态"。
     实证: 2696050 的 DLC 3593650 自带 depot 3593650 + 3593651，
           DLC 3171570 自带 depot 3171570 + 3171571，
           而本体 appinfo 只列出 3171570 和 3593651。
     → 现在按 dlcappid / depotfromapp / listofdlc 展开 DLC 的自有 depot。

  ② depot 列表和密钥解析范围不一致
     旧代码里 out["depots"]（给占位密钥用）包含服务端 raw_keys 的 depot，
     但密钥解析只遍历另一个更窄的列表。于是"服务端明明有密钥"的 depot
     反而拿不到密钥，接着被当成空 depot 写了【全零占位密钥】。
     实证: 2696050 的 depot 2696053 真密钥在两个库里都有，
           设备上却是 64 个 0。
     → 现在全程只有一份 depot 全集，解析和占位用同一个。

  ③ 有内容的 depot 判定依赖"清单是否下载成功"
     GitHub API 抽风导致某个 .manifest 没下来，该 depot 就从"有内容"变成
     "空 depot"，于是被写全零密钥 → Steam 解出垃圾。
     → 现在 content_depots 来自权威 appinfo 的 size>0，与下载成败无关。
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import depotkeys, manifesthub3, sudama

#: 单次 resolve 最多展开多少个 DLC 的 appinfo（防止 DLC 极多的游戏打爆请求数）
MAX_DLC_LOOKUPS = 60

#: 全零密钥 —— 只能当占位用，绝不能当成真密钥收进 keys
_ZERO_KEY = "0" * 64

#: 缓存比这个旧，且有内容的 depot 查不到密钥时，才同步刷新密钥库
MISS_REFRESH_AFTER = 15 * 60


def _refresh_stale_sources(verbose: bool) -> dict[str, dict]:
    """把不新鲜的密钥库同步刷新一次，返回 {源名: 新表}（只含真的拿到新数据的源）。

    远端没变（ETag 一致）时 refresh_if_remote_changed 返回 None —— 说明本地
    已是最新，缺的密钥确实还没被收录，不必下载。
    """
    out: dict[str, dict] = {}
    for name, mod in (("sudama", sudama), ("community", depotkeys)):
        try:
            if mod.age() < MISS_REFRESH_AFTER:
                continue
            _log(f"  {name}: 缓存 {mod.age() / 3600:.1f} 小时前，有 depot 缺密钥，"
                 f"检查远端是否有更新…", verbose)
            table = mod.refresh_if_remote_changed(verbose=verbose)
        except Exception as exc:  # noqa: BLE001
            _log(f"  {name}: 刷新失败 ({type(exc).__name__})", verbose)
            continue
        if isinstance(table, dict) and table:
            out[name] = table
    return out


def _log(msg: str, verbose: bool) -> None:
    if verbose:
        print(msg)


def _valid_key(key: object) -> bool:
    """64 位 hex 才是 Steam 认的 32 字节 AES 密钥；全零是占位，不算真密钥。

    SLSsteam 二进制实证: "addappid(...): invalid key (need 64 hex chars), ignoring"
    """
    if not isinstance(key, str) or len(key) != 64:
        return False
    k = key.lower()
    if k == _ZERO_KEY:
        return False
    return all(c in "0123456789abcdef" for c in k)


def _appinfo_depots(info: dict) -> tuple[dict[int, dict], bool, set[int]]:
    """解析一份 steamcmd appinfo。

    返回 ({depot: {"gid": str|None, "size": int}}, hasdepotsindlc, 引用到的其他 app)
    """
    depots: dict[int, dict] = {}
    refs: set[int] = set()
    node = info.get("depots")
    if not isinstance(node, dict):
        return depots, False, refs

    has_in_dlc = str(node.get("hasdepotsindlc") or "") == "1"
    for key, val in node.items():
        if not str(key).isdigit() or not isinstance(val, dict):
            continue
        pub = (val.get("manifests") or {}).get("public") or {}
        gid = pub.get("gid")
        try:
            size = int(pub.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        depots[int(key)] = {"gid": str(gid) if gid else None, "size": size}
        # dlcappid / depotfromapp 指向"这个 depot 其实归哪个 app 管"
        for field in ("dlcappid", "depotfromapp"):
            try:
                ref = int(val.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if ref:
                refs.add(ref)
    return depots, has_in_dlc, refs


def _listofdlc(info: dict) -> list[int]:
    ext = info.get("extended")
    raw = ext.get("listofdlc") if isinstance(ext, dict) else None
    out: list[int] = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out


def _steam_depot_universe(
    appid: int, verbose: bool = False
) -> tuple[dict[int, dict], dict[int, set[str]], list[int]]:
    """Valve 权威 depot 全集：本体 + DLC 自有 depot。

    返回:
      depots     {depot: {"gid": str|None, "size": int, "from_app": int}}
                 同一 depot 在本体和 DLC 里都出现时，以【本体视角】为准
                 （Steam 下载本体时用的就是本体 appinfo 的 gid）
      gid_pool   {depot: {gid, ...}} 所有见过的 gid —— 两边 gid 不一致时
                 把清单都抓到本地最保险（depotcache 里多一个清单无副作用）
      dlc_appids 展开过的 DLC appid
    """
    from . import updater

    depots: dict[int, dict] = {}
    gid_pool: dict[int, set[str]] = {}

    def _absorb(src: dict[int, dict], from_app: int, *, authoritative: bool) -> None:
        for dep, meta in src.items():
            if meta.get("gid"):
                gid_pool.setdefault(dep, set()).add(str(meta["gid"]))
            cur = depots.get(dep)
            if cur is None:
                depots[dep] = {**meta, "from_app": from_app}
            elif authoritative:
                depots[dep] = {**meta, "from_app": from_app}
            else:
                # 非权威来源只补空缺，不覆盖本体视角
                if not cur.get("gid") and meta.get("gid"):
                    cur["gid"] = meta["gid"]
                if not cur.get("size") and meta.get("size"):
                    cur["size"] = meta["size"]

    info = updater._steamcmd_info(appid)
    if not info:
        _log("  Steam appinfo: 查询失败（api.steamcmd.net）", verbose)
        return depots, gid_pool, []

    base, has_in_dlc, refs = _appinfo_depots(info)
    _absorb(base, appid, authoritative=True)

    # ★ 需要展开 DLC 的条件：appinfo 明确说 DLC 里有 depot，
    #   或者某个 depot 把归属指向了别的 app
    todo: list[int] = []
    for ref in sorted(refs):
        if ref != appid and ref not in todo:
            todo.append(ref)
    if has_in_dlc:
        for dlc in _listofdlc(info):
            if dlc != appid and dlc not in todo:
                todo.append(dlc)
    todo = todo[:MAX_DLC_LOOKUPS]

    if not todo:
        _log(f"  Steam appinfo: {len(depots)} 个 depot（无 DLC 自有 depot）", verbose)
        return depots, gid_pool, []

    def _one(sub_app: int) -> tuple[int, dict | None]:
        return sub_app, updater._steamcmd_info(sub_app)

    expanded: list[int] = []
    # 并发查询：串行会把 resolve 拖到几十秒
    with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
        for sub_app, sub_info in pool.map(_one, todo):
            if not sub_info:
                continue
            sub_depots, _, _ = _appinfo_depots(sub_info)
            if not sub_depots:
                continue
            _absorb(sub_depots, sub_app, authoritative=False)
            expanded.append(sub_app)

    new_ids = sorted(set(depots) - set(base))
    _log(f"  Steam appinfo: {len(depots)} 个 depot"
         f"（本体 {len(base)} + DLC 自有 {len(new_ids)}，展开 {len(expanded)} 个 DLC）",
         verbose)
    if new_ids:
        _log(f"    DLC 自有 depot: {', '.join(str(d) for d in new_ids[:8])}"
             + ("…" if len(new_ids) > 8 else ""), verbose)
    return depots, gid_pool, expanded


def resolve(appid: int, verbose: bool = True) -> dict | None:
    """汇总所有数据源，返回统一结构。

    返回:
      {
        "appid": int,
        "name": str | None,
        "keys": {depot: 64hex},
        "manifests": [(depot, gid, bytes)],
        "lua": str | None,
        "depots": [depot, ...]            # depot 全集（含无密钥的）
        "content_depots": [depot, ...]    # 【有可下载内容】的 depot，绝不可写占位密钥
        "steam_depots": [depot, ...]      # Valve 当前 appinfo 还认的 depot
        "manifest_pins": {depot: gid}     # 该钉固到 Lua 的权威 gid
        "manifest_candidates": {depot: [gid, ...]}  # 所有已知 gid（供预取清单）
        "dlc_appids": [appid, ...]        # 展开过自有 depot 的 DLC
        "sources": {"keys": [...], "manifests": [...], "name": [...], "failed": [...]},
      }
    """
    out: dict = {
        "appid": appid,
        "name": None,
        "keys": {},
        "manifests": [],
        "lua": None,
        "sources": {"keys": [], "manifests": [], "name": [], "failed": []},
        # ★ depot 全集。install() 用它找"无密钥 depot"写占位密钥。
        #   注意：解析密钥和写占位必须用【同一份】列表，否则会出现
        #   "某个源有密钥但没被查，结果被写了全零占位"（见模块头 ②）。
        "depots": [],
        "content_depots": [],
        "steam_depots": [],
        "manifest_pins": {},
        "manifest_candidates": {},
        "dlc_appids": [],
    }

    # ── ① Steam appinfo（Valve 权威：depot 全集 + gid + size + DLC 自有 depot）──
    try:
        steam_depots, gid_pool, dlc_apps = _steam_depot_universe(appid, verbose)
        out["dlc_appids"] = dlc_apps
    except Exception as exc:  # noqa: BLE001
        _log(f"  Steam appinfo 失败: {str(exc)[:60]}", verbose)
        out["sources"]["failed"].append("steam_appinfo")
        steam_depots, gid_pool, dlc_apps = {}, {}, []

    # ── ② ManifestHub3（清单文件 + Lua + 一部分密钥）──
    mh3 = None
    try:
        mh3 = manifesthub3.fetch_all(appid)
    except Exception as exc:  # noqa: BLE001
        _log(f"  ManifestHub3 失败: {str(exc)[:60]}", verbose)
        out["sources"]["failed"].append("manifesthub3")

    mh3_depots: list[int] = []
    if mh3:
        for dep, gid, _data in mh3.get("manifests") or []:
            if dep not in mh3_depots:
                mh3_depots.append(dep)
            gid_pool.setdefault(dep, set()).add(str(gid))
        for dep in mh3.get("keys") or {}:
            if dep not in mh3_depots:
                mh3_depots.append(dep)
        # Lua 里出现的 depot（含 addappid 和 setManifestid）
        for m in re.finditer(r"addappid\(\s*(\d+)", mh3.get("lua") or ""):
            dep = int(m.group(1))
            if dep != appid and dep not in mh3_depots:
                mh3_depots.append(dep)
        for m in re.finditer(r'setManifestid\((\d+),\s*"(\d+)"\)', mh3.get("lua") or ""):
            gid_pool.setdefault(int(m.group(1)), set()).add(m.group(2))
        if mh3_depots:
            _log(f"  ManifestHub3: {len(mh3_depots)} 个 depot, "
                 f"{len(mh3.get('manifests') or [])} 个清单", verbose)

    # ── ③ SteamUnlock 服务端（depot 列表 + 自带密钥）──
    api_config = None
    server_keys: dict[int, str] = {}
    try:
        from . import server_api
        plan = server_api.install_plan(
            appid, cache_dir=Path(__file__).parent.parent / "cache")
        if plan and plan.depots:
            api_config = plan
            # ★ 服务端自带的密钥也是一个源（旧代码只拿它的 depot 列表，白扔了密钥）
            for dep, key in (getattr(plan, "raw_keys", None) or {}).items():
                try:
                    dep_i = int(dep)
                except (TypeError, ValueError):
                    continue
                if _valid_key(key):
                    server_keys[dep_i] = key.lower()
            for d in plan.depots:
                if d.depotid and _valid_key(getattr(d, "key", None)):
                    server_keys[int(d.depotid)] = str(d.key).lower()
                if d.depotid and d.gid:
                    gid_pool.setdefault(int(d.depotid), set()).add(str(d.gid))
            _log(f"  SteamUnlock API: {len(plan.depots)} 个 depot, "
                 f"{len(server_keys)} 个自带密钥", verbose)
    except Exception as exc:  # noqa: BLE001
        _log(f"  SteamUnlock API 不可用: {str(exc)[:60]}", verbose)
        out["sources"]["failed"].append("steamunlock_api")

    # ── 组装 depot 全集（★ 唯一一份，解析密钥和写占位都用它）──
    all_depots: list[int] = []

    def _add(dep: object) -> None:
        try:
            dep_i = int(dep)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if dep_i and dep_i != appid and dep_i not in all_depots:
            all_depots.append(dep_i)

    for dep in steam_depots:
        _add(dep)
    for dep in mh3_depots:
        _add(dep)
    if api_config:
        for d in api_config.depots:
            _add(d.depotid)
        for dep in getattr(api_config, "raw_keys", None) or {}:
            _add(dep)
    for dep in server_keys:
        _add(dep)

    out["depots"] = sorted(all_depots)

    # ── 有内容的 depot：权威 appinfo 里 size>0 ──
    #   ★ 不能用"清单下载成功"来判断（见模块头 ③）
    content: set[int] = {d for d, meta in steam_depots.items() if meta.get("size")}
    if api_config:
        for d in api_config.depots:
            if d.depotid and getattr(d, "size", 0):
                content.add(int(d.depotid))
    # MH3 的 meta 里也有 size
    meta_node = ((mh3 or {}).get("meta") or {}).get("depot")
    if isinstance(meta_node, dict):
        for key, val in meta_node.items():
            if not str(key).isdigit() or not isinstance(val, dict):
                continue
            pub = (val.get("manifests") or {}).get("public") or {}
            try:
                if int(pub.get("size") or 0) > 0:
                    content.add(int(key))
            except (TypeError, ValueError):
                continue
    # 有清单文件 / Lua 里被钉固过 = 一定有内容
    for item in (mh3 or {}).get("manifests") or []:
        try:
            content.add(int(item[0]))
        except (TypeError, ValueError, IndexError):
            continue
    for m in re.finditer(r"setManifestid\((\d+),", (mh3 or {}).get("lua") or ""):
        content.add(int(m.group(1)))
    out["content_depots"] = sorted(content & set(all_depots))

    # ── ④ 密钥: Sudama → 社区库 → ManifestHub3 → 服务端 ──
    try:
        sud = sudama.load()
    except Exception:  # noqa: BLE001
        sud = {}
        out["sources"]["failed"].append("sudama")
    try:
        comm = depotkeys.load_community_keys()
    except Exception:  # noqa: BLE001
        comm = {}
        out["sources"]["failed"].append("community")

    mh3_keys = (mh3 or {}).get("keys") or {}
    lookups: list[tuple[str, object]] = [
        ("sudama", sud),
        ("community", comm),
        ("manifesthub3", mh3_keys),
        ("steamunlock_api", server_keys),
    ]

    def _lookup(deps: list[int]) -> None:
        for dep in deps:
            for src_name, table in lookups:
                if not isinstance(table, dict):
                    continue
                key = table.get(str(dep))
                if key is None:
                    key = table.get(dep)
                if not _valid_key(key):
                    continue
                out["keys"][dep] = str(key).lower()
                if src_name not in out["sources"]["keys"]:
                    out["sources"]["keys"].append(src_name)
                break

    _lookup(all_depots)

    # ── ④.5 ★ 有内容的 depot 查不到 → 强制刷新一次密钥库再查（新游戏保底）──
    #   缓存是 stale-while-revalidate：过期时先返回旧的、后台刷新，只保证
    #   "下一次"能查到。可用户此刻就在搜这个新游戏 —— 实证 4348910 发售于
    #   10-28，而设备缓存停在 10-03，旧缓存里当然没有。
    #   所以这里同步刷新，但很克制:
    #     · 只在【有内容的 depot 缺密钥】时触发（不是每次 resolve 都刷）
    #     · 缓存足够新（< MISS_REFRESH_AFTER）就不刷，防止对真没收录的游戏反复打
    #     · 先用 ETag 问远端变了没，没变就不下载（游戏确实还没被收录）
    missing = [d for d in all_depots if d in content and d not in out["keys"]]
    if missing:
        refreshed = _refresh_stale_sources(verbose)
        for name, table in refreshed.items():
            lookups[:] = [(n, table if n == name else t) for n, t in lookups]
        if refreshed:
            _lookup(missing)
            recovered = [d for d in missing if d in out["keys"]]
            _log(f"  密钥库刷新后找回 {len(recovered)}/{len(missing)} 个 depot 的密钥"
                 + (f": {', '.join(str(d) for d in recovered[:6])}" if recovered else ""),
                 verbose)
            out["sources"]["refreshed"] = sorted(refreshed)

    # ── 清单钉固信息 ──
    #   pins: 钉到 Lua 的权威 gid（本体视角优先）
    #   candidates: 所有已知 gid —— 本体和 DLC 对同一 depot 可能给不同 gid，
    #               两个清单都落到 depotcache 才不会卡在请求码
    #   ★ 只钉固 Valve 当前 appinfo 还认的 depot。ManifestHub3 的分支里常留着
    #     游戏早期的语言 depot（实证: 2696050 的 2696051-2696054 已从 appinfo
    #     移除），给它们钉固等于往 Lua 里复活死 depot —— 密钥照写无害，但钉固
    #     会让 4.95 的清单硬门槛去校验一个 Steam 根本不会下载的东西。
    out["steam_depots"] = sorted(set(steam_depots) & set(all_depots))
    #   api.steamcmd.net 挂了的时候退回"所有已知 gid 的 depot"，
    #   否则会连 setManifestid 都生成不出来，反而比改之前更差。
    live = set(out["steam_depots"]) or {d for d, g in gid_pool.items() if g}

    pins: dict[int, str] = {}
    for dep in all_depots:
        if dep not in content or dep not in live:
            continue
        gid = (steam_depots.get(dep) or {}).get("gid")
        if not gid:
            pool = gid_pool.get(dep) or set()
            gid = sorted(pool)[0] if pool else None
        if gid:
            pins[dep] = str(gid)
    out["manifest_pins"] = pins
    out["manifest_candidates"] = {
        dep: sorted(gids) for dep, gids in gid_pool.items()
        if dep in content and dep in live and gids
    }

    # ── 清单文件 ──
    if mh3 and mh3.get("manifests"):
        out["manifests"] = list(mh3["manifests"])
        out["sources"]["manifests"].append("manifesthub3")
    if mh3 and mh3.get("lua"):
        out["lua"] = mh3["lua"]
    # 名字
    if mh3 and mh3.get("meta"):
        meta = mh3["meta"]
        out["meta"] = meta
        for key in ("name", "common_name", "appname"):
            if isinstance(meta.get(key), str):
                out["name"] = meta[key]
                out["sources"]["name"].append("manifesthub3")
                break
    if not out["name"] and api_config is not None:
        nm = getattr(api_config, "name", None)
        if nm:
            out["name"] = nm
            out["sources"]["name"].append("steamunlock_api")

    # ── ManifestHub3 没 Lua 时，用权威 gid + 解析出的密钥生成 ──
    if not out["lua"] and (out["keys"] or pins):
        lines = [f"addappid({appid})"]
        for dep in all_depots:
            key = out["keys"].get(dep)
            if key:
                lines.append(f'addappid({dep},0,"{key}")')
            else:
                lines.append(f"addappid({dep})")
            if dep in pins:
                lines.append(f'setManifestid({dep},"{pins[dep]}")')
        out["lua"] = "\n".join(lines) + "\n"
        out["sources"]["manifests"].append("steam_appinfo_gids")

    _log(f"  → 最终: {len(out['keys'])}/{len(all_depots)} 个密钥, "
         f"{len(out['manifests'])} 个清单, {len(pins)} 个钉固", verbose)

    missing = [d for d in content if d not in out["keys"]]
    if missing:
        _log(f"  ⚠ {len(missing)} 个【有内容】的 depot 没有密钥: "
             + ", ".join(str(d) for d in sorted(missing)[:8]), verbose)

    return out if (out["keys"] or out["manifests"] or out["lua"]) else None

"""WebUI 业务逻辑：搜索 / 入库 / 卸载

设计原则:
  1. 任何写操作前先备份
  2. 只增不改（同 depot 才覆盖）
  3. 写完立刻校验
  4. 全程可回滚
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

# ── 路径 ──
HOME = Path.home()
STEAM = HOME / ".local/share/Steam"
CONFIG_VDF = STEAM / "config/config.vdf"
DEPOTCACHE = STEAM / "depotcache"
LUA_STEAM = STEAM / "config/lua"
SLS_DIR = HOME / ".config/SLSsteam"
LUA_SLS = SLS_DIR / "lua"
SLS_TOML = SLS_DIR / "config.toml"
BACKUP = Path(__file__).resolve().parent / "backup/webui"   # 项目目录下（不写死目录名）


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _backup(paths: list[Path], tag: str) -> Path:
    dst = BACKUP / f"{tag}-{time.strftime('%Y%m%d-%H%M%S')}"
    dst.mkdir(parents=True, exist_ok=True)
    for p in paths:
        if p.is_file():
            shutil.copy2(p, dst / p.name)
    _log(f"备份 → {dst}")
    return dst


# ══════════════════════════════════════════════════════════════
#  搜索
# ══════════════════════════════════════════════════════════════

def _steam_name(appid: int) -> str | None:
    """从 Steam 商店 API 拿游戏名（中文优先）"""
    import urllib.request
    for lang in ("schinese", "english"):
        try:
            url = f"https://store.steampowered.com/api/appdetails?appids={appid}&l={lang}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=15).read())
            info = d.get(str(appid), {})
            if info.get("success"):
                name = info["data"].get("name")
                if name:
                    return name
        except Exception:
            continue
    return None


def search(appid: int) -> dict:
    """查这个 appid 有没有可用数据。

    返回:
      {
        "found": bool,
        "appid": int,
        "name": str|None,
        "keys": {depot: key},
        "sources": {...},
        "installed": bool,
        "message": str,
      }
    """
    from suos import multisource, sudama, depotkeys

    out = {
        "found": False, "appid": appid, "name": None,
        "keys": {}, "key_sources": [], "depot_count": 0,
        "installed": False, "message": "",
    }

    # 已经在库里？
    lua = LUA_SLS / f"{appid}.lua"
    out["installed"] = lua.is_file()

    # 名字（Steam API）
    out["name"] = _steam_name(appid)

    # 数据源解析
    try:
        r = multisource.resolve(appid, verbose=False)
    except Exception as exc:  # noqa: BLE001
        r = None
        out["message"] = f"数据源查询失败: {str(exc)[:100]}"

    if r:
        out["keys"] = r.get("keys") or {}
        out["key_sources"] = r.get("sources", {}).get("keys", [])
        out["depot_count"] = len(out["keys"])
        if r.get("name") and not out["name"]:
            out["name"] = r["name"]

    # 兜底：只在【已知的 depot 列表】里查密钥。
    # 注意：绝不能靠 appid 前缀猜 depot（9999999 会匹配到任何 99999* 的 depot
    #       → 假阳性）。depot 列表只能来自 API / ManifestHub3 / Steam 的 appinfo。
    if not out["keys"]:
        try:
            from suos import server_api
            plan = server_api.install_plan(
                appid, cache_dir=Path(__file__).parent / "cache")
            if plan and plan.depots:
                for d in plan.depots:
                    if not d.depotid:
                        continue
                    for loader, label in ((sudama.load, "sudama"),
                                          (depotkeys.load_community_keys, "community")):
                        try:
                            k = (loader() or {}).get(str(d.depotid))
                        except Exception:
                            k = None
                        if k and len(k) == 64:
                            out["keys"][d.depotid] = k.lower()
                            if label not in out["key_sources"]:
                                out["key_sources"].append(label)
                            break
        except Exception:
            pass
        out["depot_count"] = len(out["keys"])

    if out["keys"] or (r and r.get("manifests")):
        out["found"] = True
        out["message"] = "找到数据"
    else:
        out["message"] = "没有这个游戏的数据（三个密钥库都没有收录）"

    return out


# ══════════════════════════════════════════════════════════════
#  DLC 支持
# ══════════════════════════════════════════════════════════════

def fetch_dlcs(appid: int) -> list[int]:
    """从 SteamUnlock API 拿这个游戏的全部 DLC AppID。

    为什么不需要 DLC 的密钥:
      服装/道具类 DLC 内容都在【本体 depot】里，它们只是"解锁标记"。
      实测（怪物猎人崛起抽查 8 个 DLC）：全部无独立 depot。

    ★ 必须过滤掉"其实是 depot 的条目" —— SteamUnlock 的 config.dlcs
      里偶尔混进本体 depot。实测:
        潜渊症(602960) 的 dlcs = [1197650, 3714450]
        但 1197650 也出现在 config.depots 里 → 它是 depot，不是 DLC
      如果不滤掉，会在 config.toml 里留下无意义的 appid。
    """
    import ast
    import urllib.request
    from suos import rc4
    try:
        url = f"http://auth1.caigamer.cn/GetAppinfo/{appid}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=30).read()
        txt = rc4.rc4(rc4.RC4_KEY, raw).decode("utf-8", "replace")
        d = ast.literal_eval(txt)
        cfg = d.get("config")
        if isinstance(cfg, str):
            cfg = json.loads(cfg)
        if not isinstance(cfg, dict):
            return []

        # 收集"这些其实是 depot"的 id
        depot_ids: set[int] = set()
        for x in (cfg.get("depots") or []):
            try:
                depot_ids.add(int(x))
            except (TypeError, ValueError):
                pass
        # Lua 里带密钥的 addappid 也都是 depot
        for m in re.finditer(r"addappid\((\d+),\s*\d+,\s*\"", str(d.get("Key", ""))):
            try:
                depot_ids.add(int(m.group(1)))
            except (TypeError, ValueError):
                pass

        out: list[int] = []
        for key in ("dlcs", "packagedlcs"):
            for x in (cfg.get(key) or []):
                try:
                    v = int(x)
                except (TypeError, ValueError):
                    continue
                if not v or v == appid:
                    continue
                if v in depot_ids:
                    continue          # ★ 是 depot，不是 DLC
                if v not in out:
                    out.append(v)
        return out
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════
#  处理记录（存本地 JSON，便于前端展示）
# ══════════════════════════════════════════════════════════════

RECORD = HOME / ".config/SteamUnlockOS/installed.json"


def _load_record() -> dict:
    try:
        if RECORD.is_file():
            d = json.loads(RECORD.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _save_record(d: dict) -> None:
    RECORD.parent.mkdir(parents=True, exist_ok=True)
    tmp = RECORD.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2))
    tmp.replace(RECORD)


def _record_add(appid: int, dlcs: list[int], sources: list, game_name: str | None) -> None:
    d = _load_record()
    d[str(appid)] = {
        "appid": appid,
        "name": game_name,
        "dlcs": dlcs,
        "dlc_count": len(dlcs),
        "sources": sources,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _save_record(d)


def _record_remove(appid: int) -> None:
    d = _load_record()
    d.pop(str(appid), None)
    _save_record(d)


def _resolve_name(appid: int) -> "str | None":
    """多源拿游戏名。全部失败返回 None（调用方不要缓存 None）。

    多源回退（Steam 商店 API 在国内常被墙）:
      ① 本地 appmanifest 的 name 字段（最快）
      ② ManifestHub3 元数据
      ③ SteamUnlock API 的 appinfo
      ④ api.steamcmd.net
      ⑤ Steam 商店 API（中文名，但可能超时）
    """
    import re as _re
    # ① 本地 ACF
    try:
        acf = STEAM / f"steamapps/appmanifest_{appid}.acf"
        if acf.is_file():
            m = _re.search(r'"name"\s+"([^"]+)"', acf.read_text(errors="replace"))
            if m and m.group(1):
                return m.group(1)
    except Exception:
        pass
    # ② ManifestHub3
    try:
        from suos import manifesthub3
        r = manifesthub3.fetch_all(appid)
        if r and r.get("meta"):
            for k in ("name", "common_name", "appname"):
                v = r["meta"].get(k)
                if isinstance(v, str) and v:
                    return v
    except Exception:
        pass
    # ③ SteamUnlock API
    try:
        import ast as _ast
        import urllib.request as _ur
        from suos import rc4 as _rc4
        raw = _ur.urlopen(_ur.Request(
            f"http://auth1.caigamer.cn/GetAppinfo/{appid}",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=15).read()
        d = _ast.literal_eval(_rc4.rc4(_rc4.RC4_KEY, raw).decode("utf-8", "replace"))
        m = _re.search(r'"name"\s+"([^"]+)"', str(d.get("appinfo", "")))
        if m and m.group(1):
            return m.group(1)
    except Exception:
        pass
    # ④ steamcmd
    try:
        import urllib.request as _ur
        d = json.loads(_ur.urlopen(_ur.Request(
            f"https://api.steamcmd.net/v1/info/{appid}",
            headers={"User-Agent": "Mozilla/5.0"}), timeout=12).read())
        v = (d["data"][str(appid)].get("common") or {}).get("name")
        if v:
            return v
    except Exception:
        pass
    # ⑤ Steam 商店
    try:
        n = _steam_name(appid)
        if n:
            return n
    except Exception:
        pass
    # ★ 所有源都失败 → 返回 None（不返回兜底字符串，避免被写进缓存）
    return None


# 内存缓存: appid → 真实名字(str)，或最近一次解析失败的时间戳(float)
_name_cache: dict = {}
# 解析失败后的冷却期：期间直接显示兜底名，不重复请求
# （5 个源全部超时时单个游戏要 1 分钟以上，不能每次刷新列表都重来一遍）
_NAME_RETRY_SEC = 300
_FALLBACK_NAME_RE = re.compile(r"^AppID \d+$")


def _is_fallback_name(name) -> bool:
    """兜底显示名（"AppID 数字"）不是真名，不能当成已知名字"""
    return not name or bool(_FALLBACK_NAME_RE.match(str(name)))


def _name_for(appid: int, db_name: str | None) -> str:
    """拿游戏名：记录里 → 内存缓存 → 磁盘名字缓存 → 多源解析

    记录 / 缓存里的兜底名（"AppID 数字"）一律视为"没有名字"，会重新解析（自愈）。
    """
    if not _is_fallback_name(db_name):
        return db_name
    key = str(appid)
    display = f"AppID {appid}"
    hit = _name_cache.get(key)
    if isinstance(hit, str):
        return hit
    # 磁盘名字缓存（避免每次调 API）
    nc = HOME / ".config/SteamUnlockOS/names.json"
    try:
        cache = json.loads(nc.read_text()) if nc.is_file() else {}
    except Exception:
        cache = {}
    if key in cache and not _is_fallback_name(cache[key]):
        _name_cache[key] = cache[key]
        return cache[key]
    # 冷却期内刚失败过 → 不重试
    if isinstance(hit, float) and time.time() - hit < _NAME_RETRY_SEC:
        return display
    # ★ 自愈：如果旧缓存里是兜底值（"AppID 数字"），重新解析
    nm = _resolve_name(appid)
    if not nm:
        # ★ 失败只记时间戳（冷却期后会重试）；原来把兜底名当成功结果缓存，
        #   一次网络抖动就锁死到 WebUI 进程重启
        _name_cache[key] = time.time()
        return display
    # ★ 只有真实结果才写永久缓存
    cache[key] = nm
    try:
        nc.parent.mkdir(parents=True, exist_ok=True)
        nc.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    except Exception:
        pass
    _name_cache[key] = nm
    return nm


def inventory(refresh_names: bool = False) -> list[dict]:
    """列出所有通过本工具入库过的游戏。

    以【磁盘上的 Lua 文件】为准（最真实），
    用记录文件补充 DLC 数量等信息。
    """
    rec = _load_record()
    out = []
    seen = set()
    for f in sorted(LUA_SLS.glob("*.lua")):
        if not f.stem.isdigit():
            continue
        appid = int(f.stem)
        seen.add(appid)
        info = rec.get(str(appid), {})
        # 从 Lua 里数 DLC（注释 -- DLC 之后的行）
        text = f.read_text(errors="replace")
        dlc_count = info.get("dlc_count")
        if dlc_count is None:
            m = re.search(r"--\s*DLC\s*\n((?:addappid\(\d+\)\s*\n?)+)", text)
            dlc_count = len(re.findall(r"addappid\(\d+\)", m.group(1))) if m else 0
        out.append({
            "appid": appid,
            "name": _name_for(appid, info.get("name")),
            "dlc_count": dlc_count,
            "sources": info.get("sources", []),
            "installed_at": info.get("installed_at"),
        })
    # 记录里有、但 Lua 已删的（异常情况）
    for k, v in rec.items():
        try:
            a = int(k)
        except Exception:
            continue
        if a not in seen:
            out.append({
                "appid": a, "name": v.get("name") or f"AppID {a}", "dlc_count": 0,
                "sources": [], "installed_at": v.get("installed_at"),
                "missing_lua": True,
            })
    out.sort(key=lambda x: -(x["appid"]))
    return out


# ══════════════════════════════════════════════════════════════
#  入库
# ══════════════════════════════════════════════════════════════

def _read_vdf_keys(text: str) -> dict:
    return dict(re.findall(
        r'"(\d+)"\s*\{\s*"DecryptionKey"\s*"([0-9a-fA-F]{64})"', text))


def write_keys_to_config(keys: dict) -> int:
    """把密钥写进 config.vdf。只增不改（同 depot 覆盖）。

    返回实际写入（新增 + 替换）的密钥数量。

    ★ 与旧版的区别: 用 re.subn 验证替换是否真的发生。
      旧版无论 re.sub 有没有匹配到都 n += 1，格式稍有不同就会
      "报告成功但实际没写"，导致用户重启 Steam 后仍然提示内容加密。
      现在替换失败会抛异常（调用方应视为致命错误）。
    """
    if not CONFIG_VDF.is_file():
        raise FileNotFoundError(f"找不到 {CONFIG_VDF}")
    text = CONFIG_VDF.read_text(errors="replace")
    existing = _read_vdf_keys(text)

    n = 0
    bad_len: list[str] = []
    replace_failed: list[str] = []

    for dep, key in keys.items():
        dep_s = str(dep)
        if len(key) != 64:
            bad_len.append(f"{dep_s}({len(key)}位)")
            continue

        if dep_s in existing:
            if existing[dep_s].lower() == key.lower():
                continue                      # 已经是同一个值，无需改动
            pattern = (r'("' + re.escape(dep_s) +
                       r'"\s*\{\s*"DecryptionKey"\s*")[0-9a-fA-F]{64}(")')
            text, cnt = re.subn(pattern, r"\g<1>" + key + r"\g<2>", text)
            if cnt == 0:
                # 格式与预期不符 —— 不能静默当成功
                replace_failed.append(dep_s)
                continue
            n += cnt
        else:
            # ★ 安全检查: 解析器读不到，不代表文件里没有这个 depot。
            #   如果文件里已有 "<id>" 块（格式不符预期），【绝不】再插一个，
            #   否则会出现重复条目 → Steam 读到哪个不确定 → 可能用旧的错密钥。
            if re.search(r'"' + re.escape(dep_s) + r'"\s*\{', text):
                replace_failed.append(f"{dep_s}(已存在但格式不符)")
                continue
            i = text.find('"depots"')
            if i < 0:
                raise ValueError("config.vdf 里找不到 depots 段")
            j = text.find("{", i)
            if j < 0:
                raise ValueError("config.vdf 的 depots 段没有 { 起始")
            block = (f'\n\t\t\t\t\t"{dep_s}"\n\t\t\t\t\t{{\n'
                     f'\t\t\t\t\t\t"DecryptionKey"\t\t"{key}"\n\t\t\t\t\t}}')
            text = text[:j + 1] + block + text[j + 1:]
            n += 1

    # 原子写（即使 n == 0 也写一次，保证返回前文件状态一致；
    # 若内容没变则内容相同，写入无害）
    tmp = CONFIG_VDF.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(CONFIG_VDF)

    if bad_len:
        raise ValueError(
            f"{len(bad_len)} 个密钥长度不是 64 位 hex（Steam 会完全忽略）: "
            + ", ".join(bad_len[:5]))
    if replace_failed:
        raise ValueError(
            f"{len(replace_failed)} 个 depot 的密钥替换失败（config.vdf 格式不符预期）: "
            + ", ".join(replace_failed[:5])
            + " —— 已写入的 {n} 个仍然有效，但这些没写进去".format(n=n))
    return n


def write_manifests(manifests: list) -> int:
    """写清单文件到 depotcache。返回写入数量。"""
    DEPOTCACHE.mkdir(parents=True, exist_ok=True)
    n = 0
    for item in manifests:
        try:
            dep, gid, data = item
        except Exception:
            continue
        if not data:
            continue
        (DEPOTCACHE / f"{dep}_{gid}.manifest").write_bytes(data)
        n += 1
    return n



def _depots_needing_placeholder(r: dict) -> list[int]:
    """找出【Valve 要求、但我们没有任何可用密钥】的 depot。

    这类 depot 必须写 32 字节全零的占位密钥，否则 Steam 会：
      1. 向 SLSsteam 的 hook 要密钥 → hook 没有 → 放行给 Valve
      2. Valve 回 "你没有这个游戏"（5439 响应）
      3. 把【整个下载任务】从队列移除
      4. 用户看到 "内容仍处于加密状态" / 下载卡住

    实测案例:
      · 紫色晶石(625960) depot 4506760（40 字节）—— 写占位后立刻能下载
      · 逆转裁判(787480) 没有这种 depot，所以不需要

    ★ 数据来源: multisource.resolve() 的 "depots" 字段
      它来自 SteamUnlock API 的 appinfo.depots（有 public 清单的）
      + config.depots。这两个才是 Valve 权威的 depot 列表。
      （不要用 ManifestHub3 的 meta.depot —— 它不包含这些小 depot！）

    ★★ 安全红线（本次修复新增）：只给【没有可下载内容】的 depot 写占位。
       全零密钥 = 用全零 AES 去解密。对于几十字节的空 marker depot 无害
       （根本没有加密内容走解密路径）；但对于有真实清单/内容的 depot，
       全零密钥会让 Steam 解出垃圾数据 → 完整性校验失败 → 下载损坏甚至崩溃。
       所以凡是出现在清单列表 / lua 的 setManifestid 里的 depot（= 有内容），
       一律不写占位，让它老实地走真密钥或如实失败。
    """
    depots = r.get("depots") or []
    keys_int = {int(k) for k in (r.get("keys") or {}) if str(k).isdigit()}

    # 有内容的 depot（有清单 = 有要解密的数据），绝不写零密钥
    content_depots: set[int] = set()
    for item in (r.get("manifests") or []):
        try:
            content_depots.add(int(item[0]))
        except (TypeError, ValueError, IndexError):
            continue
    for m in re.finditer(r'setManifestid\((\d+),', r.get("lua") or ""):
        content_depots.add(int(m.group(1)))

    out: list[int] = []
    for d in depots:
        try:
            dep = int(d)
        except (TypeError, ValueError):
            continue
        if dep in keys_int:
            continue
        if dep in content_depots:
            continue  # 有内容的 depot 写零密钥会损坏下载，跳过
        out.append(dep)

    # 再排除 config.vdf 里已经有非零密钥的（避免零密钥覆盖真密钥）
    try:
        if CONFIG_VDF.is_file():
            existing = _read_vdf_keys(CONFIG_VDF.read_text(errors="replace"))
            out = [d for d in out
                   if not (str(d) in existing and existing[str(d)].strip("0"))]
    except Exception:
        pass
    return sorted(set(out))


# 所有权只走 Lua（addappid）。config.toml 的 AppIds / AdditionalApps 不再写入：
#   · UseWhitelist=false（默认）时 AppIds 是【黑名单】，往里写等于把游戏拉黑，
#     只是碰巧被 AdditionalApps 覆盖才没出事；
#   · AdditionalApps 按 SLSsteam 官方说明只该用于"库里没有的游戏"，且它会和 Lua
#     重复注入同一批 appid。
# 旧版把每个游戏和 DLC 同时写进了这两个列表，下面只保留"移除"能力用于清理残留。

def _remove_appids_from_toml(appids) -> int:
    """从 config.toml 的 AppIds / AdditionalApps 移除这些 appid，返回移除的条目数。

    原子替换写入：SLSsteam 会监视 config.toml 并热重载，直接截断重写可能让它
    读到半个文件 → 列表瞬间变空 → 运行中的所有权被撤销。
    """
    if not SLS_TOML.is_file():
        return 0
    drop = {str(a) for a in appids}
    if not drop:
        return 0
    text = SLS_TOML.read_text()
    removed = 0
    for field in ("AppIds", "AdditionalApps"):
        m = re.search(rf"^{field} = \[([^\]]*)\]", text, re.M)
        if not m:
            continue
        items = [x.strip() for x in m.group(1).split(",") if x.strip()]
        kept = [x for x in items if x not in drop]
        if len(kept) == len(items):
            continue
        removed += len(items) - len(kept)
        text = text[:m.start()] + f"{field} = [{', '.join(kept)}]" + text[m.end():]
    if removed:
        tmp = SLS_TOML.with_suffix(".toml.tmp")
        tmp.write_text(text)
        tmp.replace(SLS_TOML)
    return removed


def cleanup_legacy_toml_lists() -> int:
    """一次性迁移：把旧版写进 AppIds / AdditionalApps、且已由我们 Lua 覆盖的 appid 移除。

    只移除 Lua 里 addappid 过的 appid（解锁本来就由 Lua 负责），不碰其他条目。
    """
    covered: set[int] = set()
    for d in (LUA_SLS, LUA_STEAM):
        if d.is_dir():
            for f in d.glob("*.lua"):
                covered.update(int(x) for x in re.findall(
                    r"addappid\(\s*(\d+)", f.read_text(errors="replace")))
    return _remove_appids_from_toml(covered)


def install(appid: int, include_dlc: bool = False) -> dict:
    """完整入库流程。include_dlc=True 时一并入库全部 DLC。

    步骤分类（决定 ok 的取值）:
      致命（任一失败 → ok=False）: 解析数据 / 备份 / 密钥 / Lua / 校验
      可选（失败只警告 → ok 仍可为 True）:
        清单（只有当 Lua 里没有 manifest 时才算致命）
        DLC 查询 / 处理记录
    所有权只由 Lua（addappid）承担，不写 config.toml。
    """
    from suos import multisource

    res = {"ok": False, "steps": [], "message": "", "dlcs": [], "fatal": []}
    lua_text = ""

    def fatal(msg: str) -> None:
        """记录一个致命失败"""
        res["fatal"].append(msg)
        res["steps"].append(f"✗ {msg}")

    def warn(msg: str) -> None:
        res["steps"].append(f"⚠ {msg}")

    # ── 1. 解析数据（致命）──
    try:
        r = multisource.resolve(appid, verbose=False)
    except Exception as exc:  # noqa: BLE001
        fatal(f"数据解析失败: {str(exc)[:120]}")
        res["message"] = "处理失败：拿不到游戏数据"
        return res

    if not r or (not r.get("keys") and not r.get("manifests") and not r.get("lua")):
        fatal("没有这个游戏的数据（三个密钥库都查不到）")
        res["message"] = "处理失败：没有这个游戏的数据"
        return res

    # ★ 拿不到名字就记 None，不能把兜底名 "AppID xxx" 写进 installed.json
    #   （否则 inventory 会一直用它，永不自愈 —— P1-1 的另一条路径）
    out_name = r.get("name") or _resolve_name(appid)

    # ── 2. 备份（致命 —— 没备份不敢写配置）──
    try:
        _backup([CONFIG_VDF, SLS_TOML], f"install-{appid}")
        res["steps"].append("✓ 已备份 config.vdf / config.toml")
    except Exception as exc:  # noqa: BLE001
        fatal(f"备份失败，为安全起见中止: {str(exc)[:80]}")
        res["message"] = "处理失败：无法创建备份"
        return res

    # ── 3. 密钥（致命）──
    try:
        n = write_keys_to_config(r.get("keys") or {})
        res["steps"].append(f"✓ 写入 config.vdf: {n} 个密钥")
    except Exception as exc:  # noqa: BLE001
        fatal(f"写密钥失败: {str(exc)[:120]}")
        res["message"] = "处理失败：密钥没写进去"
        return res

    # ── 3.5 ★ 无密钥 depot 的占位密钥（防止阻塞整个下载）──
    #   有些 depot（常见是几十字节的小 depot / 着色器 depot）所有密钥库都查不到，
    #   但 Valve 的 appinfo 要求它。Steam 要不到密钥就把【整个下载任务】取消。
    #   写 32 字节全零的占位密钥可以规避这个问题 —— 实测有效（见 docs/使用手册.md）。
    try:
        need_ph = _depots_needing_placeholder(r)
        if need_ph:
            wrote = []
            for depid in need_ph:
                try:
                    if write_keys_to_config({depid: "00" * 32}):
                        wrote.append(depid)
                except Exception:
                    pass
            if wrote:
                res["steps"].append(
                    f"✓ 占位密钥 {len(wrote)} 个（无密钥 depot，防止阻塞下载）: "
                    + ", ".join(str(d) for d in wrote[:6])
                    + ("…" if len(wrote) > 6 else ""))
    except Exception as exc:  # noqa: BLE001
        warn(f"写占位密钥失败（非致命）: {str(exc)[:60]}")

    # ── 4. 清单（条件致命：Lua 里没有 manifest 时必须成功）──
    lua_text = r.get("lua") or ""
    lua_has_manifest = bool(re.search(r"setManifestid\(", lua_text))
    try:
        n = write_manifests(r.get("manifests") or [])
        if n:
            res["steps"].append(f"✓ 写入 depotcache: {n} 个清单")
        elif not lua_has_manifest:
            fatal("没有任何清单可写（Lua 里也没有 setManifestid）")
    except Exception as exc:  # noqa: BLE001
        if lua_has_manifest:
            warn(f"写清单失败（Lua 里有内嵌清单，可继续）: {str(exc)[:60]}")
        else:
            fatal(f"写清单失败且 Lua 无内嵌清单: {str(exc)[:80]}")

    # ── 4.5 DLC（可选）──
    dlcs: list[int] = []
    if include_dlc:
        try:
            dlcs = fetch_dlcs(appid)
        except Exception:  # noqa: BLE001
            dlcs = []
        if dlcs:
            res["steps"].append(f"✓ 找到 {len(dlcs)} 个 DLC")
            res["dlcs"] = dlcs
        else:
            res["steps"].append("（这个游戏没有 DLC 或 API 查不到）")

    # ── 4.8 ★ 把清单版本对齐到 Steam 最新 ──
    #   ManifestHub3 是快照（可能几个月没更新），直接用它的 gid 会导致:
    #     · WebUI 更新检查永远报"有更新"
    #     · 用户更新后重新入库又被退回旧版
    #   所以这里用 api.steamcmd.net 的最新 gid 覆盖，并下载对应的清单。
    if lua_text:
        try:
            from suos import updater as _upd
            latest = _upd.latest_gids(appid)
            if latest:
                pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua_text))
                bumped = []
                for dep_s, gid_s in latest.items():
                    old_gid = pins.get(str(dep_s))
                    if old_gid and str(old_gid) != str(gid_s):
                        lua_text = re.sub(
                            rf'(setManifestid\({dep_s},\s*")[0-9]+(")',
                            rf'\g<1>{gid_s}\g<2>', lua_text)
                        bumped.append(str(dep_s))
                if bumped:
                    res["steps"].append(
                        f"✓ 清单版本对齐到最新（{len(bumped)} 个 depot）: "
                        + ", ".join(bumped[:5]) + ("…" if len(bumped) > 5 else ""))
                    # 把这几个的最新清单也下下来（否则 Steam 还得自己拉）
                    from suos import manifests as _mf
                    fresh = []
                    for dep_s in bumped:
                        try:
                            mr = _mf.fetch_manifest(appid, int(dep_s), int(latest[dep_s]))
                            if mr:
                                fresh.append((int(dep_s), int(latest[dep_s]), mr.data))
                        except Exception:
                            pass
                    if fresh:
                        write_manifests(fresh)
                        res["steps"].append(f"✓ 同时下载了 {len(fresh)} 个最新清单")
        except Exception as exc:  # noqa: BLE001
            warn(f"对齐最新版本失败（保留原版本）: {str(exc)[:60]}")

    # ── 4.9 清单本地化兜底（关键：绕开请求码依赖）──
    #   Steam 下载 depot 前会先查本地 depotcache/<depot>_<gid>.manifest：
    #     命中 → 直接用，不向 CDN 请求，也就不需要 manifest request code
    #     未命中 → 向 CDN 请求 → 需要 request code → 依赖 SLSsteam 的提供者
    #
    #   ★ 而 SLSsteam 的 request-code 提供者实测经常全部失效
    #     （opensteamtool 403 / wudrm 返回非清单内容 / steamrun 502），
    #     且提供者列表编译在二进制里、无法扩充。
    #   ★ 只要清单在本地，整条 request-code 路径就被绕开。
    #
    #   上面 4.8 只在 gid 变化时下载清单（"版本对齐"），
    #   但 gid 本来就最新的游戏（例：ManifestHub3 无分支的新游）
    #   会跳过下载 → depotcache 缺失 → 卡在 request code。
    #   所以这里独立检查"清单在不在本地"，与 gid 是否变化无关。
    if lua_text:
        try:
            from suos import manifests as _mf
            pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua_text))
            missing = []
            for dep_s, gid_s in pins.items():
                if not (DEPOTCACHE / f"{dep_s}_{gid_s}.manifest").is_file():
                    missing.append((int(dep_s), int(gid_s)))
            if missing:
                got, failed = [], []
                for dep_i, gid_i in missing:
                    try:
                        mr = _mf.fetch_manifest(appid, dep_i, gid_i)
                    except Exception:
                        mr = None
                    if mr:
                        got.append((dep_i, gid_i, mr.data))
                    else:
                        failed.append(str(dep_i))
                if got:
                    write_manifests(got)
                    res["steps"].append(
                        f"✓ 已补齐 {len(got)} 个本地清单（免请求码）"
                        + (f"，{len(failed)} 个失败" if failed else ""))
                else:
                    warn(f"{len(missing)} 个清单没拿到，Steam 可能需要请求码"
                         + (f"（depot: {', '.join(failed[:4])}）" if failed else ""))
        except Exception as exc:  # noqa: BLE001
            warn(f"清单本地化检查失败: {str(exc)[:60]}")

    # ── 4.95 ★ 清单可用性硬门槛（核心修复）──
    #   走到这里，lua 里每个 setManifestid 的清单都应该已经落到 depotcache。
    #   如果还有缺的，这个游戏【必定】下载失败 —— 真机 content_log 实证的死亡链：
    #     BYldRequestDepotManifest ... Failed to get manifest request code 'Access Denied'
    #     → update canceled : Failed downloading manifests (No connection)   ← "无互联网连接"
    #     → scheduler finished : removed (result Missing decryption key)      ← "内容仍处于加密状态"
    #     → 反复重试把 Steam 下载状态机搞崩溃
    #   以前这里只 warn 然后报"入库完成"，用户去重启 Steam 必然踩雷。
    #   现在：清单缺失 = 致命。宁可让用户稍后重试，也不交付一个必坏的配置。
    if lua_text:
        final_pins = dict(re.findall(r'setManifestid\((\d+),\s*"(\d+)"\)', lua_text))
        still_missing = [
            f"{dep}_{gid}"
            for dep, gid in final_pins.items()
            if not (DEPOTCACHE / f"{dep}_{gid}.manifest").is_file()
        ]
        if still_missing:
            shown = ", ".join(still_missing[:6]) + (
                f" 等 {len(still_missing)} 个" if len(still_missing) > 6 else "")
            fatal(f"清单缺失，游戏无法下载（Steam 会报无连接/内容加密）: {shown}。"
                  f"多为数据源临时不可用 —— 请稍后重试入库，现在不要重启 Steam")

    # ── 5. Lua（致命）──
    if not lua_text and r.get("keys"):
        lines = [f"addappid({appid})"]
        for dep, key in sorted(r["keys"].items()):
            lines.append(f'addappid({dep},0,"{key}")')
        lua_text = "\n".join(lines) + "\n"
    if not lua_text:
        fatal("没有可写的 Lua 内容")
    else:
        if f"addappid({appid})" not in lua_text:
            lua_text = f"addappid({appid})\n" + lua_text
        # 附加 DLC 的 addappid（只需要 AppID，不需要密钥）
        if dlcs:
            extra = "\n".join(f"addappid({d})" for d in dlcs)
            lua_text = lua_text.rstrip() + "\n\n-- DLC\n" + extra + "\n"
        # ★ 防覆盖保护: 新 Lua 的 setManifestid 数量不能少于已有的
        #   （避免数据源临时抽风时，用不完整的数据覆盖能工作的配置）
        old_lua = LUA_SLS / f"{appid}.lua"
        if old_lua.is_file():
            old_pins = len(re.findall(r"setManifestid\(", old_lua.read_text(errors="replace")))
            new_pins = len(re.findall(r"setManifestid\(", lua_text))
            if new_pins < old_pins:
                fatal(f"新 Lua 的清单数({new_pins})少于现有的({old_pins})，"
                      f"拒绝覆盖以免破坏可用配置。请重试或检查数据源")
                lua_text = old_lua.read_text(errors="replace")   # 保持原样
            else:
                # 覆盖前存一份到 backup 目录（不放在 Lua 运行目录里，避免污染）
                try:
                    bdir = BACKUP.parent / "lua"
                    bdir.mkdir(parents=True, exist_ok=True)
                    (bdir / f"{appid}.lua").write_text(old_lua.read_text(errors="replace"))
                except Exception:
                    pass

        # ★ Lua 就是所有权：有致命失败（尤其清单缺失）时一个字都不写。
        #   新游戏 → 不出现在库里，不会让用户点进必坏的下载；
        #   已有游戏 → 保留原来能用的 Lua，不被半成品覆盖。
        if res["fatal"]:
            res["steps"].append("（有致命失败，未写入/未改动 Lua，避免交付必坏配置）")
        else:
            try:
                LUA_SLS.mkdir(parents=True, exist_ok=True)
                LUA_STEAM.mkdir(parents=True, exist_ok=True)
                (LUA_SLS / f"{appid}.lua").write_text(lua_text)
                (LUA_STEAM / f"{appid}.lua").write_text(lua_text)
                res["steps"].append("✓ 写入 Lua 配置（两个目录）"
                                    + (f"，含 {len(dlcs)} 个 DLC" if dlcs else ""))
            except Exception as exc:  # noqa: BLE001
                fatal(f"写 Lua 失败: {str(exc)[:100]}")

    # ── 7. 校验（致命）──
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        from suos import vdf
        vdf.load(str(CONFIG_VDF))
        res["steps"].append("✓ config.vdf 校验通过")
    except Exception as exc:  # noqa: BLE001
        fatal(f"config.vdf 校验失败（请从 backup/webui/ 还原）: {str(exc)[:80]}")

    # ── 8. 记录（可选）──
    if not res["fatal"]:
        try:
            _record_add(appid, dlcs, r.get("sources", {}).get("keys", []), out_name)
        except Exception as exc:  # noqa: BLE001
            warn(f"写记录失败: {str(exc)[:60]}")

    # ── 汇总 ──
    res["lua"] = lua_text
    if res["fatal"]:
        res["ok"] = False
        res["message"] = (f"处理不完整（{len(res['fatal'])} 个关键步骤失败）："
                          + "；".join(res["fatal"][:2])
                          + "。详见下方步骤，配置未生效请勿重启 Steam 前重试")
    else:
        res["ok"] = True
        res["message"] = "处理完成，重启 Steam 后即可在库中管理"
    return res


def _steam_running() -> bool:
    """Steam 是否在运行"""
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                exe = os.readlink(f"/proc/{pid}/exe")
            except Exception:
                continue
            if exe.endswith("/ubuntu12_32/steam"):
                return True
    except Exception:
        pass
    return False


def clean_steam_leftovers(appid: int, dry_run: bool = True) -> dict:
    """清理 Steam 为某个 appid 留的缓存文件（Steam 自己不会清）。

    背景: 用本工具移除游戏后，这些不会自动消失，导致:
      · 库里还显示这个游戏（封面图缓存在）
      · 桌面图标还在
      · 成就相关缓存还在

    清理项:
      appcache/librarycache/<appid>/          封面图缓存
      appcache/stats/*<appid>*                成就定义/统计
      userdata/*/config/librarycache/*.json   成就列表
      steamapps/temp/<appid>/                 临时目录
      steamapps/downloading/<appid>/          下载残留
      shadercache/<appid>/                    着色器缓存
      ~/Desktop/*.desktop                     指向该 appid 的桌面图标
      steamapps/appmanifest_<appid>.acf       ★ 安装记录（只在 remove_local 时删）
      steamapps/common/<installdir>/          ★ 游戏文件（只在 remove_local 时删）
    """
    import os as _os
    steam = HOME / ".local/share/Steam"
    found: list[str] = []
    removed: list[str] = []
    skipped: list[str] = []

    def hit(desc: str, path: Path) -> None:
        if not path.exists():
            return
        found.append(f"{desc}: {path}")
        if dry_run:
            return
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(f"{desc}: {path.name}")
        except Exception as exc:  # noqa: BLE001
            skipped.append(f"{desc}: {path.name} ({exc})")

    # 缓存类（无害，随时删）
    hit("封面图缓存", steam / f"appcache/librarycache/{appid}")
    for f in (steam / "appcache/stats").glob(f"*{appid}*"):
        hit("成就缓存", f)
    for u in (steam / "userdata").glob("*"):
        hit("成就列表", u / f"config/librarycache/{appid}.json")
    hit("临时目录", steam / f"steamapps/temp/{appid}")
    hit("下载残留", steam / f"steamapps/downloading/{appid}")
    hit("着色器缓存", steam / f"shadercache/{appid}")

    # 桌面图标（按内容匹配）
    desk = HOME / "Desktop"
    if desk.is_dir():
        for f in desk.glob("*.desktop"):
            try:
                if re.search(rf"rungameid/{appid}\b", f.read_text(errors="replace")):
                    hit("桌面图标", f)
            except Exception:
                pass

    return {"found": found, "removed": removed, "skipped": skipped,
            "count": len(found)}


def remove_local_content(appid: int) -> dict:
    """删除已安装游戏的本地内容（ACF + 游戏文件）。

    ★ 安全检查:
      · Steam 必须在【关闭】状态（否则它会重建 ACF）
      · 备份 ACF 到 backup/webui/
      · 只删 ACF 和它记录的 installdir，不碰别的东西
    """
    steam = HOME / ".local/share/Steam"
    acf = steam / f"steamapps/appmanifest_{appid}.acf"
    out = {"ok": False, "steps": [], "freed": 0}

    if not acf.is_file():
        out["steps"].append("（没有 ACF，本来就没安装）")
        out["ok"] = True
        return out

    if _steam_running():
        out["steps"].append("✗ Steam 正在运行 —— 请先退出 Steam 再卸载本地内容")
        return out

    # 找 installdir
    installdir = ""
    try:
        m = re.search(r'"installdir"\s+"([^"]+)"', acf.read_text(errors="replace"))
        installdir = m.group(1) if m else ""
    except Exception:
        pass

    # 备份 ACF
    try:
        bdir = BACKUP.parent / "removed"
        bdir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(acf, bdir / acf.name)
        out["steps"].append(f"✓ ACF 已备份到 backup/removed/{acf.name}")
    except Exception as exc:  # noqa: BLE001
        out["steps"].append(f"⚠ ACF 备份失败: {str(exc)[:50]}")

    # 删游戏文件
    if installdir:
        gdir = steam / "steamapps/common" / installdir
        if gdir.is_dir():
            try:
                sz = sum(f.stat().st_size for f in gdir.rglob("*") if f.is_file())
                shutil.rmtree(gdir)
                out["freed"] = sz
                out["steps"].append(f"✓ 删除游戏文件: {installdir} （{sz / 1073741824:.2f} GB）")
            except Exception as exc:  # noqa: BLE001
                out["steps"].append(f"✗ 删游戏文件失败: {str(exc)[:60]}")
        else:
            out["steps"].append(f"（游戏目录不存在: {installdir}）")

    # 删 ACF
    try:
        acf.unlink()
        out["steps"].append("✓ 删除安装记录（ACF）—— Steam 重启后不再显示已安装")
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["steps"].append(f"✗ 删 ACF 失败: {str(exc)[:60]}")
    return out


def _dlc_appids_from_lua(appid: int) -> list[int]:
    """从 Lua 文件里读出"顺带添加的 DLC appid"。

    入库带 DLC 时，Lua 末尾会有:
        -- DLC
        addappid(1753180)
        addappid(1753181)
        ...
    卸载时用它清理旧版残留在 config.toml 里的 DLC appid
    （旧版入库会把 DLC 写进 config.toml，残留会让库里留下 DLC 孤儿）。

    只取 `-- DLC` 标记之后的裸 addappid，避免误删别的游戏。
    """
    f = LUA_SLS / f"{appid}.lua"
    if not f.is_file():
        f = LUA_STEAM / f"{appid}.lua"
    if not f.is_file():
        return []
    text = f.read_text(errors="replace")
    m = re.search(r"^--\s*DLC\s*$", text, re.M)
    if not m:
        return []
    out: list[int] = []
    for mm in re.finditer(r"addappid\((\d+)\)", text[m.end():]):
        try:
            v = int(mm.group(1))
        except (TypeError, ValueError):
            continue
        if v != appid and v not in out:
            out.append(v)
    return out


def uninstall(appid: int, remove_local: bool = False) -> dict:
    """卸载：删 Lua（所有权随之撤销，含 DLC）、清理 config.toml 旧版残留、清理 Steam 缓存残留。

    密钥保留（无害，且避免误删别的游戏共用的 depot）。

    remove_local=True 时还会:
      · 删除 ACF（安装记录）→ 库里的"已安装"状态消失
      · 删除游戏文件（释放磁盘）
      · 需要 Steam 已关闭（否则 ACF 会被重建）
    """
    res = {"ok": False, "steps": [], "leftovers": [], "local": None}
    _backup([CONFIG_VDF, SLS_TOML], f"uninstall-{appid}")

    # ★ 先读出 DLC 列表（Lua 删了就没了）
    dlcs: list[int] = []
    try:
        dlcs = _dlc_appids_from_lua(appid)
    except Exception:
        dlcs = []

    for d in (LUA_SLS, LUA_STEAM):
        f = d / f"{appid}.lua"
        if f.is_file():
            f.unlink()
            res["steps"].append(f"✓ 删除 {f}")

    # 清理旧版写进 config.toml 的残留（本体 + DLC；新版入库不再写 toml）
    try:
        n = _remove_appids_from_toml([appid, *dlcs])
        if n:
            res["steps"].append(f"✓ 清理 config.toml 旧版残留 {n} 项")
    except Exception as exc:  # noqa: BLE001
        res["steps"].append(f"⚠ 清理 config.toml 残留失败: {str(exc)[:60]}")
    if dlcs:
        res["steps"].append(f"✓ 同时移除 {len(dlcs)} 个 DLC（随 Lua 一起删除）")

    try:
        _record_remove(appid)
        res["steps"].append("✓ 删除处理记录")
    except Exception:
        pass

    # ★ 清理 Steam 缓存残留（封面图/成就/桌面图标等，Steam 不会自己清）
    try:
        lv = clean_steam_leftovers(appid, dry_run=False)
        res["leftovers"] = lv["removed"]
        if lv["removed"]:
            res["steps"].append(f"✓ 清理 Steam 缓存残留 {len(lv['removed'])} 项"
                                + ("（封面图/成就/桌面图标等）" if len(lv["removed"]) > 1 else ""))
        if lv["skipped"]:
            res["steps"].append(f"⚠ {len(lv['skipped'])} 项清理失败: " + str(lv["skipped"][:2]))
    except Exception as exc:  # noqa: BLE001
        res["steps"].append(f"⚠ 清理残留失败: {str(exc)[:60]}")

    # ★ 可选: 删除本地内容（ACF + 游戏文件）
    if remove_local:
        try:
            lc = remove_local_content(appid)
            res["local"] = lc
            for s in lc["steps"]:
                res["steps"].append(s)
        except Exception as exc:  # noqa: BLE001
            res["steps"].append(f"⚠ 删本地内容失败: {str(exc)[:60]}")

    res["ok"] = True
    res["dlcs_removed"] = dlcs
    msg = "已卸载" + (f"（含 {len(dlcs)} 个 DLC）" if dlcs else "")
    msg += "，重启 Steam 后生效"
    if remove_local and res.get("local") and res["local"].get("freed"):
        msg += f"，释放 {res['local']['freed'] / 1073741824:.2f} GB"
    res["message"] = msg
    return res

# ══════════════════════════════════════════════════════════════
#  版本更新（委托给 suos.updater）
# ══════════════════════════════════════════════════════════════

def update_check_all() -> list[dict]:
    """检查所有已添加游戏的更新（不改动任何东西）"""
    from suos import updater
    return updater.check_all()


def update_apply(appid: int) -> dict:
    """应用某个游戏的更新（server.py 已加锁）"""
    from suos import updater
    return updater.apply(appid)

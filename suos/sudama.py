"""Sudama 密钥库 —— 239,946 条 depot 解密密钥

来源发现: 读开源项目 cai-install (zhouchentao666/Fluent-Install) 的
backend/cai_backend.py 第 1838 行找到的端点。

它的优势（实测）:
  · 239,946 条，其中 26,921 条是社区库（288,381 条）没有的
  · 包含 SteamUnlock 有、而 ManifestHub3/ManifestAutoUpdate 都没有的新游戏
    （例: 命运石之门 RE:BOOT 4012811 / 杀戮尖塔2 2868841）

缓存策略: 7 天。过期后先用旧缓存返回（密钥基本不变），后台再刷新，不让搜索卡住。

★ 2026-11-06 修过的坑: 下载文件和缓存文件曾是同一个路径。下载失败时读回的是
  {timestamp, data} 信封，又被当成新数据再包一层存回去，失败几次就套娃好几层，
  load() 只看到 timestamp/data 两个键（"2 条密钥"），Sudama 独有的游戏全都搜不到。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path

URL = "https://api.993499094.xyz/depotkeys.json"
TIMEOUT = 120
CACHE_TTL = 6 * 3600       # 6 小时。新游戏天天出，7 天太长；
                           # 远端没变时只花一次 ETag 探测，不会重下 18 MB
MIN_ENTRIES = 1000         # 少于这个数不可能是完整的密钥库

_CACHE_DIR = Path(os.environ.get("SUOS_CACHE", str(Path.home() / ".cache/suos")))
_CACHE = _CACHE_DIR / "sudama_depotkeys.json"
_RAW = _CACHE_DIR / "sudama_depotkeys.raw.json"   # 下载落地文件，绝不和缓存共用路径
_FP = _CACHE_DIR / "sudama_depotkeys.fp.json"     # 远端 ETag/Last-Modified，用于"变了没"判断
_mem: dict | None = None
_refresh_lock = threading.Lock()


def _unwrap(obj: object) -> tuple[object, float, int]:
    """剥掉 {timestamp, data} 信封，返回 (内容, 最内层时间戳, 层数)。

    最内层时间戳才是数据真正下载的时间，外层都是失败时重写的。
    """
    ts = 0.0
    depth = 0
    while (isinstance(obj, dict) and set(obj) == {"timestamp", "data"}
           and isinstance(obj["data"], dict)):
        if isinstance(obj["timestamp"], (int, float)):
            ts = float(obj["timestamp"])
        obj = obj["data"]
        depth += 1
    return obj, ts, depth


def _is_key_table(data: object) -> bool:
    """是不是完整的 {depot: key} 表。错误页、残缺文件、信封都过不了。"""
    if not isinstance(data, dict) or len(data) < MIN_ENTRIES:
        return False
    sample = [k for _, k in zip(range(200), data)]
    return sum(str(k).isdigit() for k in sample) >= len(sample) * 0.9


def _save_cache(data: dict, ts: float | None = None) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"timestamp": time.time() if ts is None else ts, "data": data}))
    tmp.replace(_CACHE)


def _load_cache(allow_stale: bool = False) -> dict | None:
    if not _CACHE.is_file():
        return None
    try:
        raw = json.loads(_CACHE.read_text())
    except Exception:
        return None
    data, ts, depth = _unwrap(raw)
    if not _is_key_table(data):
        return None
    if depth > 1:          # 自愈老版本写坏的套娃缓存，保留真实下载时间
        try:
            _save_cache(data, ts)
        except OSError:
            pass
    if not allow_stale and time.time() - ts > CACHE_TTL:
        return None
    return data


def age() -> float:
    """本地缓存写入至今多少秒；没有可用缓存就返回 inf。"""
    if not _CACHE.is_file():
        return float("inf")
    try:
        raw = json.loads(_CACHE.read_text())
    except Exception:  # noqa: BLE001
        return float("inf")
    data, ts, _ = _unwrap(raw)
    if not _is_key_table(data) or ts <= 0:
        return float("inf")
    return max(0.0, time.time() - ts)


def _save_fp(fp: tuple[str, str, int]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _FP.write_text(json.dumps(
            {"etag": fp[0], "last_modified": fp[1], "size": fp[2]}))
    except OSError:
        pass


def _load_fp() -> tuple[str, str, int]:
    try:
        d = json.loads(_FP.read_text())
        return (str(d.get("etag") or ""), str(d.get("last_modified") or ""),
                int(d.get("size") or 0))
    except Exception:  # noqa: BLE001
        return ("", "", 0)


def _unchanged_remotely(verbose: bool = False) -> bool:
    """远端还是上次那份吗？只花一次 Range GET（1 字节），不下全文。

    密钥库 18 MB，TTL 到期就无条件重下太浪费；ETag/Last-Modified 一致
    说明内容没变，直接把本地缓存的时间戳刷新即可。
    """
    old = _load_fp()
    if not any(old[:2]):
        return False
    try:
        from . import fetch as _fetch
        new = _fetch.remote_fingerprint(URL)
    except Exception:  # noqa: BLE001
        return False
    if not any(new[:2]):
        return False
    same = (new[0] and new[0] == old[0]) or (new[1] and new[1] == old[1])
    if same and verbose:
        print("  Sudama: 远端未变化（ETag 一致），只刷新缓存时间戳")
    return bool(same)


def _download(verbose: bool = False) -> dict | None:
    """下载并校验。只有拿到完整的密钥表才写缓存，失败返回 None。"""
    try:
        from . import fetch as _fetch
        # min_bytes: 端点是 chunked（无 Content-Length），没有大小可校验，
        # 用"至少 1 MB"挡掉错误页和截断响应
        if _fetch.fetch_resumable(URL, _RAW, verbose=verbose, timeout=30,
                                  min_bytes=1_000_000):
            data = json.loads(_RAW.read_text())
            if _is_key_table(data):
                _save_cache(data)
                try:
                    _save_fp(_fetch.remote_fingerprint(URL))
                except Exception:  # noqa: BLE001
                    pass
                return data
            if verbose:
                print("  Sudama: 下载的内容不是完整密钥表，丢弃")
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  Sudama: 下载失败 ({type(exc).__name__})")
    return None


def _refresh_in_background() -> None:
    """旧缓存已经返回给调用方了，后台试着换新的。失败无所谓，下次再试。"""
    if not _refresh_lock.acquire(blocking=False):
        return

    def run() -> None:
        global _mem
        try:
            # 远端没变就别重下 18 MB，只把本地时间戳刷新，避免每 6 小时一次满量下载
            if _unchanged_remotely():
                cached = _load_cache(allow_stale=True)
                if cached:
                    _save_cache(cached)
                    _mem = cached
                    return
            data = _download()
            if data:
                _mem = data
        finally:
            _refresh_lock.release()

    threading.Thread(target=run, name="sudama-refresh", daemon=True).start()


def load(force: bool = False, verbose: bool = False) -> dict:
    """返回 {depot_id_str: 64hex_key}。优先缓存；过期先用旧的，后台刷新。"""
    global _mem
    if _mem is not None and not force:
        return _mem
    if not force:
        cached = _load_cache()
        if cached:
            if verbose:
                print(f"  Sudama: 用缓存（{len(cached):,} 条）")
            _mem = cached
            return cached
        stale = _load_cache(allow_stale=True)
        if stale:
            if verbose:
                print(f"  Sudama: 缓存已过期，先用旧的（{len(stale):,} 条），后台刷新")
            _mem = stale
            _refresh_in_background()
            return stale
    if verbose:
        print(f"  Sudama: 从 {URL} 下载…")
    data = _download(verbose)
    if data:
        if verbose:
            print(f"  Sudama: 下载完成（{len(data):,} 条）")
        _mem = data
        return data
    # 兜底：断点续传的所有源都失败时，再直连整包试一次。
    # ★ 2026-11-07: 这里原来的门控是 `if force or not _CACHE.is_file()` ——
    #   只要本地有缓存文件就跳过兜底。配合 fetch_resumable 对 chunked 响应
    #   恒返回 False 的 bug，等于"有缓存就永远不再更新"，缓存冻结了五周。
    #   现在只要走到这一步（说明 _download 失败了）就试一次直连。
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read().decode("utf-8"))
        if _is_key_table(data):
            _save_cache(data)
            _mem = data
            return data
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"  Sudama: 直连失败 ({type(exc).__name__})")
    stale = _load_cache(allow_stale=True)
    if stale:
        if verbose:
            print("  Sudama: 下载失败，用过期缓存兜底")
        _mem = stale
        return stale
    if verbose:
        print("  Sudama: 全部失败")
    _mem = {}
    return {}


def refresh_if_remote_changed(verbose: bool = False) -> dict | None:
    """远端变了才真下载；没变返回 None（表示本地已经是最新的）。

    给"搜不到某个新游戏"这条路径用: 先花一次 1 字节的 Range GET 判断远端
    有没有更新，没更新就说明本地已是最新、这个游戏确实还没被收录 ——
    省掉 18 MB 的无用下载。
    """
    if _unchanged_remotely(verbose):
        cached = _load_cache(allow_stale=True)
        if cached:
            _save_cache(cached)        # 内容没变，只把时间戳推到现在
        return None
    return load(force=True, verbose=verbose)


def key_for(depot_id: int) -> str | None:
    d = load()
    return d.get(str(depot_id))


def keys_for_app(appid: int) -> dict:
    """key.vdf 兼容的 {depot: key}；这里只能给出该 appid 自身的条目。"""
    d = load()
    out = {}
    if str(appid) in d:
        out[appid] = d[str(appid)]
    return out

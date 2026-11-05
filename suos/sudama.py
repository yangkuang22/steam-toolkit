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
CACHE_TTL = 7 * 24 * 3600  # 7 天（网络不稳，延长缓存）
MIN_ENTRIES = 1000         # 少于这个数不可能是完整的密钥库

_CACHE_DIR = Path(os.environ.get("SUOS_CACHE", str(Path.home() / ".cache/suos")))
_CACHE = _CACHE_DIR / "sudama_depotkeys.json"
_RAW = _CACHE_DIR / "sudama_depotkeys.raw.json"   # 下载落地文件，绝不和缓存共用路径
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


def _download(verbose: bool = False) -> dict | None:
    """下载并校验。只有拿到完整的密钥表才写缓存，失败返回 None。"""
    try:
        from . import fetch as _fetch
        if _fetch.fetch_resumable(URL, _RAW, verbose=verbose, timeout=25):
            data = json.loads(_RAW.read_text())
            if _is_key_table(data):
                _save_cache(data)
                return data
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
    # 兜底：断点续传的所有源都失败时，再直连整包试一次（首次使用、没有任何缓存时才走到这）
    if force or not _CACHE.is_file():
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

"""
depot 密钥获取：社区密钥库（SteamAutoCracks/ManifestHub，28 万条）

★ 2026-11-07 修过的坑: 缓存永不过期、也不校验内容。
  旧实现只要 ~/.cache/suos/depotkeys.json 存在就直接 json.loads 返回，
  没有 TTL、没有条目数校验、写入也不是原子的。后果:
    · 第一次下载成功后这份快照会被用到天荒地老 —— 新游戏的密钥永远查不到；
    · 写一半被打断（断电/杀进程）留下的残缺 JSON 要么解析失败，要么解出
      一个条目很少的 dict，而调用方分不出"没有这个 depot"和"缓存坏了"；
    · 每次调用都重新解析 16 MB，入库流程里被反复调用。
  现在: 7 天 TTL + 条目数/键格式校验 + 原子写 + 进程内 memo，
  下载失败时退回过期缓存（密钥基本不变，有总比没有好）。
  策略与 sudama.py 保持一致。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Dict, Optional

MANIFESTHUB_URLS = [
    "https://cdn.jsdelivr.net/gh/SteamAutoCracks/ManifestHub@HEAD/depotkeys.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/HEAD/depotkeys.json",
    "https://raw.githubusercontent.com/SteamAutoCracks/ManifestHub/HEAD/depotkeys.json",
]

_CACHE_DIR = Path(os.environ.get("SUOS_CACHE", str(Path.home() / ".cache/suos")))
CACHE = _CACHE_DIR / "depotkeys.json"
_META = _CACHE_DIR / "depotkeys.meta.json"   # 下载时间单独存，不动数据文件格式

CACHE_TTL = 7 * 24 * 3600   # 7 天
MIN_ENTRIES = 1000          # 少于这个数不可能是完整的社区密钥库
TIMEOUT = 180

_mem: Optional[Dict[str, str]] = None
_refresh_lock = threading.Lock()


def _is_key_table(data: object) -> bool:
    """是不是完整的 {depot: key} 表。错误页、残缺文件都过不了。"""
    if not isinstance(data, dict) or len(data) < MIN_ENTRIES:
        return False
    sample = [k for _, k in zip(range(200), data)]
    return sum(str(k).isdigit() for k in sample) >= len(sample) * 0.9


def _cache_age() -> float:
    """缓存写入至今的秒数；取不到就按"无限旧"处理。"""
    try:
        ts = float(json.loads(_META.read_text()).get("timestamp") or 0)
    except Exception:  # noqa: BLE001
        # 没有 meta（老版本写的缓存）就退回文件 mtime
        try:
            ts = CACHE.stat().st_mtime
        except OSError:
            return float("inf")
    if ts <= 0:
        return float("inf")
    return max(0.0, time.time() - ts)


def _read_cache() -> Optional[Dict[str, str]]:
    if not CACHE.is_file():
        return None
    try:
        data = json.loads(CACHE.read_text())
    except Exception:  # noqa: BLE001
        return None
    return data if _is_key_table(data) else None


def _write_cache(raw: bytes) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".json.tmp")
    tmp.write_bytes(raw)
    tmp.replace(CACHE)                      # 原子替换，不留半截文件
    try:
        _META.write_text(json.dumps({"timestamp": time.time()}))
    except OSError:
        pass


def _download(verbose: bool = False) -> Optional[Dict[str, str]]:
    """逐个镜像试，只有拿到完整密钥表才写缓存。"""
    for url in MANIFESTHUB_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=TIMEOUT).read()
            data = json.loads(raw)
            if not _is_key_table(data):
                if verbose:
                    print(f"  社区库: {url[:40]}… 返回的不是完整密钥表")
                continue
            _write_cache(raw)
            if verbose:
                print(f"  社区库: 下载完成（{len(data):,} 条）")
            return data
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"  社区库: {url[:40]}… 失败 ({type(exc).__name__})")
            continue
    return None


def _refresh_in_background() -> None:
    """旧缓存已经返回给调用方了，后台试着换新的。失败无所谓，下次再试。

    ★ 必须是后台: 这份表 16 MB，三个镜像各 180 秒超时，阻塞式刷新最坏能把
      入库/搜索卡五分钟以上。密钥极少变动，用一份过期的远好过让界面假死。
    """
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

    threading.Thread(target=run, name="depotkeys-refresh", daemon=True).start()


def load_community_keys(force: bool = False, verbose: bool = False) -> Dict[str, str]:
    """返回 {depot_id_str: 64hex_key}。

    优先新鲜缓存；过期时【先返回旧的】并在后台刷新，不让调用方等下载。
    完全没有缓存时才阻塞下载（首次使用）。
    """
    global _mem
    if _mem is not None and not force:
        return _mem

    cached = None if force else _read_cache()
    if cached is not None:
        age = _cache_age()
        if age <= CACHE_TTL:
            if verbose:
                print(f"  社区库: 用缓存（{len(cached):,} 条）")
        else:
            if verbose:
                print(f"  社区库: 缓存已过期（{age / 86400:.1f} 天），"
                      f"先用旧的（{len(cached):,} 条），后台刷新")
            _refresh_in_background()
        _mem = cached
        return cached

    # 没有任何可用缓存 —— 只有这种情况才阻塞下载
    fresh = _download(verbose)
    if fresh is not None:
        _mem = fresh
        return fresh

    if verbose:
        print("  社区库: 全部失败")
    _mem = {}
    return {}


def key_for(depotid: int, community: Optional[Dict[str, str]] = None) -> Optional[str]:
    """查单个 depot 的 32 字节密钥（64 hex）。返回 None 表示没有。

    全零密钥是本工具自己写的占位值，不是真密钥 —— 绝不能当成真密钥返回，
    否则会污染多源交叉校验。
    """
    if community is None:
        community = load_community_keys()
    k = community.get(str(depotid))
    if not isinstance(k, str) or len(k) != 64:
        return None
    if k.lower() == "0" * 64:
        return None
    return k


def keys_for_app(depots, community: Optional[Dict[str, str]] = None):
    """
    depots: [(depotid, gid, size), ...] 或 DepotInfo 列表
    返回 (keys, missing) —— keys: {depotid: hexkey}, missing: [depotid, ...]
    """
    if community is None:
        community = load_community_keys()
    keys, missing = {}, []
    for d in depots:
        depid = getattr(d, "depotid", None) or d[0]
        k = key_for(depid, community)
        if k:
            keys[depid] = k
        elif depid:  # 空密钥 depot（如 40 字节的小 depot）不需要
            missing.append(depid)
    return keys, missing

"""带断点续传的下载器 —— 解决国内网络慢/易断的问题

背景（实测 2026-10-03）:
  api.993499094.xyz  首字节 0.6-1.5s，但 18 MB 全量下载可能超时
  raw.githubusercontent.com  被墙（HTTP 000）
  ghproxy.net / gh-proxy.com 可用且【支持 Range 请求（HTTP 206）】

策略:
  ① 先试直连（快的话直接下完）
  ② 失败/慢 → 用代理 + 分块续传（每块 2 MB，断了自己接着下）
  ③ 全部失败 → 用本地缓存兜底
"""
from __future__ import annotations

import os
import time
import urllib.request
import urllib.error
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CHUNK = 2 * 1024 * 1024     # 每块 2 MB
MAX_TRIES = 12              # 每个 URL 最多重试次数
PROBE_TIMEOUT = 6           # 探测超时（避免被墙的源卡住）

# 国内可用的 GitHub 代理（实测可用，按速度排序）
GITHUB_PROXIES = [
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://mirror.ghproxy.com/",
]


def _reachable(url: str, timeout: int = PROBE_TIMEOUT) -> bool:
    """快速探测 URL 是否可达（避免被墙的源浪费时间）"""
    try:
        req = urllib.request.Request(url, method="HEAD", headers=UA)
        urllib.request.urlopen(req, timeout=timeout).read(1)
        return True
    except urllib.error.HTTPError as e:
        return e.code in (200, 206, 403, 405)  # 403/405 说明服务器能连上
    except Exception:
        return False


def _head_size(url: str, timeout: int = 25) -> int:
    """拿文件大小（HEAD 或 Range 0-0）"""
    for method in ("HEAD", "GET"):
        try:
            headers = dict(UA)
            if method == "GET":
                headers["Range"] = "bytes=0-0"
            req = urllib.request.Request(url, method=method, headers=headers)
            r = urllib.request.urlopen(req, timeout=timeout)
            cl = r.headers.get("Content-Length")
            if cl:
                return int(cl)
            cr = r.headers.get("Content-Range")
            if cr and "/" in cr:
                return int(cr.split("/")[-1])
        except Exception:
            continue
    return 0


def fetch_resumable(url: str, dest: Path, *, timeout: int = 20,
                    verbose: bool = False, proxies: list[str] | None = None) -> bool:
    """带断点续传的下载。成功返回 True 并保证文件完整。

    关键设计（针对国内网络）:
      · 每个候选 URL 只探测一次，不可达就直接跳过（不浪费重试）
      · 单个 URL 最多试 3 次，失败就换下一个
      · 断点记录在 .part 文件里，跨 URL 也能续传
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    pxy = proxies if proxies is not None else GITHUB_PROXIES
    candidates = [url] + [p + url for p in pxy]

    # 先拿总大小（用第一个可达的源）
    total = 0
    usable: list[str] = []
    for uri in candidates:
        if not _reachable(uri, timeout=PROBE_TIMEOUT):
            if verbose:
                print(f"    ✗ 不可达: {uri[:56]}")
            continue
        usable.append(uri)
        if not total:
            total = _head_size(uri, timeout=min(timeout, 15))
    if not usable:
        if verbose:
            print("    ✗ 所有源都不可达")
        return False
    if verbose:
        print(f"    可用源: {len(usable)} 个，总大小 {total/1048576:.1f} MB")

    for uri in usable:
        got = part.stat().st_size if part.is_file() else 0
        if total and got >= total:
            break
        for attempt in range(3):
            got = part.stat().st_size if part.is_file() else 0
            if total and got >= total:
                break
            headers = dict(UA)
            if got:
                headers["Range"] = f"bytes={got}-"
            try:
                r = urllib.request.urlopen(urllib.request.Request(uri, headers=headers),
                                           timeout=timeout)
                if got and r.status != 206:
                    part.unlink(missing_ok=True)
                    got = 0
                mode = "ab" if got else "wb"
                with open(part, mode) as f:
                    while True:
                        blk = r.read(262144)
                        if not blk:
                            break
                        f.write(blk)
                        got += len(blk)
                        if total and got >= total:
                            break
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"    中断({type(exc).__name__}) 已下 {got/1048576:.1f} MB")
                time.sleep(1)
        got = part.stat().st_size if part.is_file() else 0
        if total and got >= total:
            break
        if verbose:
            print(f"    {uri[:44]}… → {got/1048576:.1f}/{total/1048576:.1f} MB")

    got = part.stat().st_size if part.is_file() else 0
    if total and got >= total:
        part.replace(dest)
        if verbose:
            print(f"    ✓ 完成 {dest.name} ({got:,} 字节)")
        return True
    if verbose:
        print(f"    ✗ 未完成（{got:,}/{total:,} 字节）")
    return False

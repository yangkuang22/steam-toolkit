"""带断点续传的下载器 —— 解决国内网络慢/易断的问题

背景（实测 2026-10-03）:
  api.993499094.xyz  首字节 0.6-1.5s，但 18 MB 全量下载可能超时
  raw.githubusercontent.com  被墙（HTTP 000）
  ghproxy.net / gh-proxy.com 可用且【支持 Range 请求（HTTP 206）】

策略:
  ① 探测每个候选源：可达性 / 总大小 / 是否支持 Range（一次 Range GET 全拿到）
  ② 支持 Range → 分块续传（断了接着下）
  ③ 不支持 Range / 大小未知 → 单次流式全量下载（不碰已有的 .part）
  ④ 全部失败 → 调用方用本地缓存兜底

★ 2026-11-07 修过的坑（Sudama 缓存冻结五周的真因，实测定位）

  ① 大小未知就【永远】返回 False
     api.993499094.xyz 用 Transfer-Encoding: chunked，不给 Content-Length，
     所以 _head_size() 返回 0 → total=0。而收尾判断是

         if total and got >= total:
             part.replace(dest); return True
         return False

     total=0 时恒为假 —— 哪怕 18 MB 完整下完也返回 False，.part 永远不会
     提升为 dest。sudama 于是认为下载失败，而它的直连兜底又被
     `if force or not _CACHE.is_file()` 挡住（缓存文件存在就跳过），
     结果缓存永久冻结。实证: 设备缓存停在 10-03，新游戏（10-28 发售的
     appid 4348910）永远搜不到。
     → 现在大小未知时，以"流正常结束且不小于 min_bytes"为成功判据。

  ② 服务器不支持 Range 时把已下载的进度删掉
         if got and r.status != 206:
             part.unlink(missing_ok=True)   # ← 进度没了
     Sudama 端点实测对 Range 请求回 200（无 Accept-Ranges / Content-Range），
     于是每次重试都从零开始，3 次机会各要完整下 18 MB，慢链路上基本不可能
     成功。观察到 .part 从 18.5 MB 退回 9.4 MB 就是这个循环。
     → 现在"不支持 Range"是探测阶段就确定的事实，走独立的全量下载路径，
       写到另一个临时文件，绝不破坏 Range 源留下的 .part。

  ③ 给非 GitHub 的 URL 套 GitHub 代理
     候选源是 [url] + [ghproxy + url]，但那些代理只转发 github.com 的 URL。
     套在 api.993499094.xyz 上纯属浪费探测时间。
     → 现在只有 GitHub 域名的 URL 才加代理。

  ④ timeout 是 socket 超时，不是总预算
     慢而稳的 18 MB 下载不该被判失败。
     → 新增 max_seconds 墙钟预算，与 socket 超时分开。
"""
from __future__ import annotations

import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CHUNK = 262144              # 流式读取块大小
MAX_TRIES = 4               # 每个源的重试次数
PROBE_TIMEOUT = 8           # 探测超时（避免被墙的源卡住）
MAX_SECONDS = 900           # 单次 fetch_resumable 的墙钟预算

# 国内可用的 GitHub 代理（实测可用，按速度排序）
GITHUB_PROXIES = [
    "https://ghproxy.net/",
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://mirror.ghproxy.com/",
]

#: 只有这些域名的 URL 才值得套 GitHub 代理
GITHUB_HOSTS = (
    "github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
)


@dataclass
class Probe:
    """一个候选源的探测结果。"""
    ok: bool = False
    size: int = 0            # 0 = 未知（chunked 或没给 Content-Length）
    ranges: bool = False     # 是否真的支持 Range（回 206 才算）
    etag: str = ""
    last_modified: str = ""


def is_github_url(url: str) -> bool:
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in GITHUB_HOSTS


def probe(url: str, timeout: int = PROBE_TIMEOUT) -> Probe:
    """一次 `Range: bytes=0-0` 的 GET 同时拿到可达性、大小、Range 支持、缓存校验头。

    为什么不用 HEAD: 不少 CDN 对 HEAD 的响应头和 GET 不一致（尤其 chunked
    响应根本不给 Content-Length），而 Range GET 只传 1 个字节，更可靠也几乎没开销。
    """
    out = Probe()
    headers = dict(UA)
    headers["Range"] = "bytes=0-0"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(1)
            out.ok = True
            out.etag = r.headers.get("ETag") or ""
            out.last_modified = r.headers.get("Last-Modified") or ""
            cr = r.headers.get("Content-Range")
            if getattr(r, "status", 200) == 206 and cr and "/" in cr:
                tail = cr.rsplit("/", 1)[-1].strip()
                if tail.isdigit():
                    out.ranges = True
                    out.size = int(tail)
            else:
                # 回 200 = 忽略了 Range，不支持续传
                cl = r.headers.get("Content-Length")
                # 注意: 这里的 Content-Length 是整个响应体的长度（服务器
                # 忽略 Range 时就是全文件大小）；chunked 时没有这个头。
                if cl and cl.isdigit() and int(cl) > 1:
                    out.size = int(cl)
    except urllib.error.HTTPError as exc:
        # 403/405 说明服务器活着，只是不让这么请求
        out.ok = exc.code in (200, 206, 403, 405, 416)
    except Exception:  # noqa: BLE001
        out.ok = False
    return out


def remote_fingerprint(url: str, timeout: int = PROBE_TIMEOUT) -> tuple[str, str, int]:
    """返回 (etag, last_modified, size)，用于"远端变了没"的廉价判断。

    密钥库是 16-18 MB，每次 TTL 到期就重下太浪费。拿 ETag/Last-Modified
    对一下，没变就只刷新本地缓存的时间戳。
    """
    pr = probe(url, timeout)
    return pr.etag, pr.last_modified, pr.size


def _stream_to(uri: str, target: Path, *, offset: int, timeout: int,
               deadline: float, expect_206: bool) -> tuple[int, bool, str]:
    """把 uri 的内容写到 target。返回 (本次写入后的总字节数, 流是否正常结束, 备注)。

    offset>0 时发 Range 并以追加方式写；要求响应是 206，否则直接放弃
    （绝不截断已有数据 —— 让调用方改走全量路径）。
    """
    headers = dict(UA)
    mode = "wb"
    if offset:
        headers["Range"] = f"bytes={offset}-"
        mode = "ab"
    try:
        req = urllib.request.Request(uri, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if expect_206 and offset and getattr(r, "status", 200) != 206:
                return offset, False, "服务器忽略了 Range"
            got = offset
            with open(target, mode) as f:
                while True:
                    if time.monotonic() > deadline:
                        return got, False, "超出墙钟预算"
                    blk = r.read(CHUNK)
                    if not blk:
                        break
                    f.write(blk)
                    got += len(blk)
            return got, True, ""
    except Exception as exc:  # noqa: BLE001
        size = target.stat().st_size if target.is_file() else offset
        return size, False, f"{type(exc).__name__}"


def _resume(uri: str, part: Path, total: int, *, timeout: int,
            deadline: float, verbose: bool) -> bool:
    """支持 Range 的源：分块续传直到补满 total 字节。"""
    for _ in range(MAX_TRIES):
        if time.monotonic() > deadline:
            return False
        got = part.stat().st_size if part.is_file() else 0
        if got >= total:
            return True
        got, clean, note = _stream_to(uri, part, offset=got, timeout=timeout,
                                      deadline=deadline, expect_206=True)
        if note == "服务器忽略了 Range":
            if verbose:
                print("    这个源其实不支持 Range，改走全量下载")
            return False
        if verbose and not clean:
            print(f"    中断({note}) 已下 {got / 1048576:.1f}/{total / 1048576:.1f} MB")
        if got >= total:
            return True
        time.sleep(1)
    return (part.stat().st_size if part.is_file() else 0) >= total


def _full(uri: str, part: Path, expected: int, *, timeout: int, deadline: float,
          min_bytes: int, verbose: bool) -> bool:
    """不支持 Range（或大小未知）的源：单次流式全量下载。

    写到独立的 .full 临时文件，成功后才替换 .part —— 这样即使失败，
    之前某个支持 Range 的源留下的 .part 进度依然完好。
    """
    tmp = part.with_suffix(part.suffix + ".full")
    for attempt in range(MAX_TRIES):
        if time.monotonic() > deadline:
            break
        tmp.unlink(missing_ok=True)
        got, clean, note = _stream_to(uri, tmp, offset=0, timeout=timeout,
                                      deadline=deadline, expect_206=False)
        if clean and got >= max(min_bytes, 1) and (not expected or got >= expected):
            tmp.replace(part)
            return True
        if verbose:
            why = note or ("太小" if got < min_bytes else "不完整")
            print(f"    全量下载失败({why}) 第 {attempt + 1}/{MAX_TRIES} 次，"
                  f"已下 {got / 1048576:.1f} MB")
        time.sleep(1)
    tmp.unlink(missing_ok=True)
    return False


def fetch_resumable(url: str, dest: Path, *, timeout: int = 20,
                    verbose: bool = False, proxies: list[str] | None = None,
                    max_seconds: int = MAX_SECONDS, min_bytes: int = 1) -> bool:
    """下载 url 到 dest。成功返回 True 并保证 dest 是完整内容。

    参数:
      timeout      socket 单次操作超时（不是总时长）
      max_seconds  整个过程的墙钟预算
      min_bytes    大小未知时，至少要这么多字节才认为成功
                   （防止把错误页/截断响应当成完整文件）

    成功判据:
      · 服务器给了大小 → 必须下满
      · 大小未知（chunked）→ 流正常结束且 >= min_bytes
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    deadline = time.monotonic() + max_seconds

    if proxies is None:
        proxies = GITHUB_PROXIES if is_github_url(url) else []
    candidates = [url] + [p + url for p in proxies]

    probed: list[tuple[str, Probe]] = []
    for uri in candidates:
        if time.monotonic() > deadline:
            break
        pr = probe(uri, timeout=min(timeout, PROBE_TIMEOUT))
        if not pr.ok:
            if verbose:
                print(f"    ✗ 不可达: {uri[:56]}")
            continue
        probed.append((uri, pr))
        if verbose:
            size_txt = f"{pr.size / 1048576:.1f} MB" if pr.size else "大小未知"
            print(f"    ✓ 可用: {uri[:48]}… {size_txt}, "
                  f"Range={'支持' if pr.ranges else '不支持'}")

    if not probed:
        if verbose:
            print("    ✗ 所有源都不可达")
        return False

    # 支持 Range 的源排前面：它们能续传，失败代价最小
    probed.sort(key=lambda item: not item[1].ranges)

    for uri, pr in probed:
        if time.monotonic() > deadline:
            break
        ok = False
        if pr.ranges and pr.size:
            ok = _resume(uri, part, pr.size, timeout=timeout,
                         deadline=deadline, verbose=verbose)
        if not ok:
            ok = _full(uri, part, pr.size, timeout=timeout, deadline=deadline,
                       min_bytes=min_bytes, verbose=verbose)
        if ok:
            part.replace(dest)
            if verbose:
                print(f"    ✓ 完成 {dest.name} ({dest.stat().st_size:,} 字节)")
            return True

    if verbose:
        got = part.stat().st_size if part.is_file() else 0
        print(f"    ✗ 未完成（已下 {got:,} 字节）")
    return False

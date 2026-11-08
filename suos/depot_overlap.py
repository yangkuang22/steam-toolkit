"""depot 重叠覆盖检查 —— 多个 depot 带同名文件时，确认落盘的是该落的那一份。

## 为什么需要

一个 app 的多个 depot 可以包含同一个相对路径。实证 Yarimono(2696050)：本体
2696055、DLC1(3171570/3171571)、DLC2(3593650/3593651) 五个 depot 各带一份
``data/www/index.html`` 和 js 插件，内容各不相同。

Steam 落盘时同名文件只留一份，实测是【挂载顺序里最后一个含该路径的 depot】胜出：
appmanifest 的 InstalledDepots 顺序与 content_log 的 ``mounted depots`` 一致，
Windows 那台末位是 3593650 → DLC2 的文件落盘；SteamOS 那台末位是 3171571 →
DLC1 的旧文件盖掉了 DLC2 的新文件。

挂载顺序在 depot 首次加入时登记（content_log ``user config changed : added
depots ...``），取的是当时 Lua 里 addappid 的顺序，此后一直沿用。入库分几轮把
depot 追加到 Lua 末尾，就会让旧 DLC 排到新 DLC 后面。结果 plugins.js（只有新
depot 里有）是新版、插件本体却是旧版，游戏一启动就报
``ReferenceError: XXX is not defined``。

## 优先级

默认 depot ID 越大优先级越高：Steam 递增分配 depot ID，后发布的 DLC 携带的是
更新的公共文件（上面的实例里 ID 顺序与游戏版本 v2.19 < v3.08 一致）。可以用
``priority`` 参数覆盖。``lua_depot_order()`` 按同一规则给出 Lua 里 addappid
应有的顺序（优先级最高的放最后），入库和检查共用这一个定义。

## 怎么区分"Steam 盖错"和"补丁写入"

Steam 只会落【挂载顺序胜出者】的那一份。磁盘内容等于胜出者、却不是期望版本 →
Steam 盖错了（WRONG_DEPOT）；内容不是胜出者的 → 一定是别人写进去的（补丁/手动
拷贝），即使恰好等于某个旧 depot 也只算 MODIFIED。实证：该游戏的 v4.00 补丁就
自带了 7 个与本体 2696055 旧版逐字节相同的图片。

补丁也可能恰好带进"胜出者版本"，工具无从判断是否有意为之，所以提供基线：确认
游戏正常后 ``save_accepted()`` 记下当前的偏差文件，之后内容不变就算 ACCEPTED。
"""

from __future__ import annotations

import enum
import hashlib
import io
import json
import os
import struct
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from suos import steam, vdf

ACCEPT_DIR = Path.home() / ".config" / "SteamUnlockOS" / "overlap-accepted"

PAYLOAD_MAGIC = 0x71F617D0
METADATA_MAGIC = 0x1F4812BE
SIGNATURE_MAGIC = 0x1B81B817
END_MAGIC = 0x32C415AB

FLAG_DIRECTORY = 0x40          # EDepotFileFlag.Directory

_HASH_CHUNK = 1 << 20


class ManifestError(ValueError):
    """清单文件无法解析。"""


@dataclass(frozen=True)
class FileVersion:
    size: int
    sha1: str


@dataclass
class ManifestFiles:
    depot_id: int
    gid: int
    filenames_encrypted: bool
    # casefold 后的相对路径 -> (清单里的原始相对路径, 版本)
    files: dict[str, tuple[str, FileVersion]]


# ── protobuf 最小解码 ────────────────────────────────────────────────

def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise ManifestError("varint 越界")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ManifestError("varint 过长")


def _fields(buf: bytes) -> Iterator[tuple[int, int | bytes]]:
    pos, end = 0, len(buf)
    while pos < end:
        key, pos = _varint(buf, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(buf, pos)
        elif wire == 2:
            length, pos = _varint(buf, pos)
            if pos + length > end:
                raise ManifestError("长度字段越界")
            value, pos = buf[pos:pos + length], pos + length
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            if pos + width > end:
                raise ManifestError("定长字段越界")
            value, pos = buf[pos:pos + width], pos + width
        else:
            raise ManifestError(f"不支持的 wire type {wire}")
        yield num, value


def _sections(raw: bytes) -> dict[int, bytes]:
    if raw[:2] == b"PK":
        # CDN 原样格式：zip 里包着一份二进制清单
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
            if not names:
                raise ManifestError("zip 清单是空的")
            raw = zf.read(names[0])
    out: dict[int, bytes] = {}
    pos = 0
    while pos + 4 <= len(raw):
        (magic,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        if magic == END_MAGIC:
            break
        if pos + 4 > len(raw):
            raise ManifestError(f"段 {magic:#010x} 缺长度")
        (length,) = struct.unpack_from("<I", raw, pos)
        pos += 4
        if pos + length > len(raw):
            raise ManifestError(f"段 {magic:#010x} 越界")
        out[magic] = raw[pos:pos + length]
        pos += length
    if PAYLOAD_MAGIC not in out:
        raise ManifestError("没有 payload 段，不是 Steam 清单")
    return out


def _norm(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


def parse_manifest(raw: bytes) -> ManifestFiles:
    """解析 depotcache 里的 .manifest，返回每个文件的大小与内容 SHA1（目录跳过）。"""
    sections = _sections(raw)
    depot_id = gid = 0
    encrypted = False
    for num, value in _fields(sections.get(METADATA_MAGIC, b"")):
        if not isinstance(value, int):
            continue
        if num == 1:
            depot_id = value
        elif num == 2:
            gid = value
        elif num == 4:
            encrypted = bool(value)

    files: dict[str, tuple[str, FileVersion]] = {}
    for num, value in _fields(sections[PAYLOAD_MAGIC]):
        if num != 1 or not isinstance(value, bytes):
            continue
        name: str | None = None
        size = flags = 0
        sha = b""
        for fnum, fvalue in _fields(value):
            if fnum == 1 and isinstance(fvalue, bytes):
                name = fvalue.decode("utf-8", "replace")
            elif fnum == 2 and isinstance(fvalue, int):
                size = fvalue
            elif fnum == 3 and isinstance(fvalue, int):
                flags = fvalue
            elif fnum == 5 and isinstance(fvalue, bytes):
                sha = fvalue
        if not name or flags & FLAG_DIRECTORY or not sha:
            continue
        rel = _norm(name)
        files[rel.casefold()] = (rel, FileVersion(size, sha.hex()))
    return ManifestFiles(depot_id, gid, encrypted, files)


# ── 优先级（入库与检查共用）────────────────────────────────────────────

def default_priority(depots: Iterable[int]) -> list[int]:
    """优先级从高到低：depot ID 越大越优先。"""
    return sorted(set(depots), reverse=True)


def lua_depot_order(depots: Iterable[int]) -> list[int]:
    """Lua 里 addappid 应有的顺序：优先级最高的排最后（Steam 末位胜出）。"""
    return list(reversed(default_priority(depots)))


# ── 检查 ─────────────────────────────────────────────────────────────

class Status(enum.Enum):
    OK = "ok"                    # 落盘的是期望版本
    ACCEPTED = "accepted"        # 与用户记下的基线一致（补丁等有意为之）
    WRONG_DEPOT = "wrong_depot"  # 落的是挂载顺序胜出者、但不是期望版本 —— Steam 盖错了
    MODIFIED = "modified"        # Steam 不会这样落盘：补丁/手动写入，仅提示
    MISSING = "missing"          # 文件不在磁盘上


@dataclass(frozen=True)
class PathFinding:
    path: str
    status: Status
    expected_depot: int
    actual_depots: tuple[int, ...]   # 内容与磁盘一致的 depot（MODIFIED 时也可能非空）
    order_winner: int                # 按当前挂载顺序，验证完整性后会落盘的 depot
    order_risk: bool                 # order_winner 的版本 != 期望版本
    digest: str = ""                 # 磁盘文件 SHA1；大小对不上任何版本时为空


@dataclass
class AppReport:
    appid: int
    name: str = ""
    install_dir: Path | None = None
    mount_order: list[int] = field(default_factory=list)
    priority: list[int] = field(default_factory=list)
    findings: list[PathFinding] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def with_status(self, status: Status) -> list[PathFinding]:
        return [f for f in self.findings if f.status is status]

    @property
    def order_risks(self) -> list[PathFinding]:
        return [f for f in self.findings if f.order_risk]

    @property
    def severity(self) -> int:
        """0 正常 / 1 警告 / 2 错误。补丁改过的文件只算提示，不计入。"""
        if any(f.status in (Status.WRONG_DEPOT, Status.MISSING) for f in self.findings):
            return 2
        if self.problems or self.order_risks:
            return 1
        return 0


def _ci_get(node: dict, key: str):
    """VDF 键大小写不固定（Windows 的 ACF 有 universe/Universe 混用）。"""
    if key in node:
        return node[key]
    low = key.lower()
    for k, v in node.items():
        if k.lower() == low:
            return v
    return None


def read_mount_order(acf: Path) -> tuple[str, str, list[tuple[int, int]]]:
    """返回 (游戏名, installdir, [(depot, manifest gid)...])，顺序即挂载顺序。"""
    state = _ci_get(vdf.load(acf), "AppState") or {}
    depots: list[tuple[int, int]] = []
    node = _ci_get(state, "InstalledDepots") or {}
    for key, value in node.items():
        if not (key.isdigit() and isinstance(value, dict)):
            continue
        gid = str(_ci_get(value, "manifest") or "")
        if gid.isdigit():
            depots.append((int(key), int(gid)))
    return str(_ci_get(state, "name") or ""), str(_ci_get(state, "installdir") or ""), depots


def _resolve(root: Path, rel: str) -> Path | None:
    """先按原样找；找不到再逐级忽略大小写（Linux 区分大小写，清单里不一定一致）。"""
    direct = root / rel
    if direct.is_file():
        return direct
    cur = root
    for part in rel.split("/"):
        try:
            names = os.listdir(cur)
        except OSError:
            return None
        match = next((n for n in names if n.casefold() == part.casefold()), None)
        if match is None:
            return None
        cur = cur / match
    return cur if cur.is_file() else None


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def check_app(paths: steam.SteamPaths, appid: int,
              priority: list[int] | None = None,
              accepted: dict[str, str] | None = None,
              hash_all: bool = False) -> AppReport:
    """检查一个已安装 app：在 2 个以上 depot 里内容不同的路径，磁盘上落的是谁。

    accepted: load_accepted() 的结果（casefold 路径 -> SHA1）。
    hash_all: 大小对不上任何版本也算哈希（记基线时需要）。
    """
    accepted = accepted or {}
    report = AppReport(appid)
    acf = steam.acf_path(paths, appid)
    if acf is None:
        report.problems.append("未安装（找不到 appmanifest）")
        return report
    try:
        report.name, installdir, mount = read_mount_order(acf)
    except (OSError, vdf.ParseError) as exc:
        report.problems.append(f"appmanifest 解析失败: {exc}")
        return report
    report.mount_order = [d for d, _ in mount]
    if len(mount) < 2:
        return report                       # 单 depot 不存在重叠
    if not installdir:
        report.problems.append("appmanifest 里没有 installdir")
        return report
    report.install_dir = acf.parent / "common" / installdir
    if not report.install_dir.is_dir():
        report.problems.append(f"安装目录不存在: {report.install_dir}")
        return report

    manifests: dict[int, ManifestFiles] = {}
    for depot, gid in mount:
        mpath = paths.depotcache / f"{depot}_{gid}.manifest"
        if not mpath.is_file():
            report.problems.append(f"depot {depot} 缺清单 {mpath.name}，未参与检查")
            continue
        try:
            parsed = parse_manifest(mpath.read_bytes())
        except (OSError, ManifestError, zipfile.BadZipFile) as exc:
            report.problems.append(f"depot {depot} 清单解析失败: {exc}")
            continue
        if parsed.filenames_encrypted:
            report.problems.append(f"depot {depot} 清单的文件名是加密的，无法比对")
            continue
        manifests[depot] = parsed

    order = [d for d in report.mount_order if d in manifests]
    base = default_priority(order)
    wanted = [d for d in (priority or []) if d in manifests]
    report.priority = wanted + [d for d in base if d not in wanted]
    rank = {d: i for i, d in enumerate(report.priority)}

    index: dict[str, dict[int, tuple[str, FileVersion]]] = {}
    for depot, parsed in manifests.items():
        for key, entry in parsed.files.items():
            index.setdefault(key, {})[depot] = entry

    for key, per in index.items():
        if len(per) < 2 or len({v.sha1 for _, v in per.values()}) < 2:
            continue                        # 只有一份，或各 depot 内容相同：谁赢都一样
        expected = min(per, key=rank.__getitem__)
        exp_rel, exp_ver = per[expected]
        winner = next(d for d in reversed(order) if d in per)
        risk = per[winner][1].sha1 != exp_ver.sha1

        found = None
        for rel in dict.fromkeys(rel for rel, _ in per.values()):
            found = _resolve(report.install_dir, rel)
            if found is not None:
                break
        digest = ""
        if found is None:
            status, matched = Status.MISSING, ()
        else:
            try:
                size = found.stat().st_size
                candidates = {d: v for d, (_, v) in per.items() if v.size == size}
                # 大小对不上任何版本，又不用对基线时，不必算哈希
                if candidates or hash_all or key in accepted:
                    digest = _sha1(found)
            except OSError as exc:
                report.problems.append(f"读取失败 {exp_rel}: {exc}")
                continue
            matched = tuple(sorted(d for d, v in candidates.items() if v.sha1 == digest))
            if digest and digest == exp_ver.sha1:
                status = Status.OK
            elif digest and accepted.get(key) == digest:
                status = Status.ACCEPTED
            elif winner in matched:
                status = Status.WRONG_DEPOT
            else:
                status = Status.MODIFIED
        report.findings.append(
            PathFinding(exp_rel, status, expected, matched, winner, risk, digest))

    report.findings.sort(key=lambda f: f.path.casefold())
    return report


def accepted_path(appid: int, base: Path | None = None) -> Path:
    return (base or ACCEPT_DIR) / f"{appid}.json"


def load_accepted(appid: int, base: Path | None = None) -> dict[str, str]:
    """读基线。文件不存在或损坏都按"没有基线"处理（体检宁可多报，不可漏报）。"""
    try:
        data = json.loads(accepted_path(appid, base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, dict):
        return {}
    return {str(k).casefold(): v for k, v in files.items() if isinstance(v, str) and v}


def save_accepted(report: AppReport, base: Path | None = None) -> int:
    """把当前偏离期望版本的文件记为基线（原子写入），返回记录数。

    report 需由 check_app(..., hash_all=True) 产生，否则大小不匹配的文件没有哈希。
    缺失的文件没法认可，不记。
    """
    keep = (Status.WRONG_DEPOT, Status.MODIFIED, Status.ACCEPTED)
    files = {f.path: f.digest for f in report.findings if f.status in keep and f.digest}
    target = accepted_path(report.appid, base)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "appid": report.appid,
        "name": report.name,
        "accepted_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mount_order": report.mount_order,
        "files": files,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)
    return len(files)


def multi_depot_apps(paths: steam.SteamPaths) -> list[int]:
    """已安装且挂载了 2 个以上 depot 的 app（只有它们可能出现重叠）。"""
    result: list[int] = []
    for appid in sorted(steam.installed_appids(paths)):
        acf = steam.acf_path(paths, appid)
        if acf is None:
            continue
        try:
            _, _, depots = read_mount_order(acf)
        except (OSError, vdf.ParseError):
            continue
        if len(depots) >= 2:
            result.append(appid)
    return result

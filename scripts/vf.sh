#!/bin/bash
# Vapor Forge 运维工具
#   vf.sh status        现状检查（版本 / 模块是否加载 / 包装器 / 缓存）
#   vf.sh rollback      回到 SLSsteam（改回包装器，需重启）
#   vf.sh check-update  查上游最新版本
#   vf.sh upgrade       下载并安装最新版本（需重启 Steam）
set -uo pipefail
H="$HOME"
LIB="$H/.local/share/vapor-forge"
W1="$H/.local/bin/steam"
W2="$H/.local/share/SLSsteam/path/steam"
REPO="Hintay/vapor-forge"
API="https://api.github.com/repos/$REPO/releases/latest"

_ver() { cat "$LIB/VERSION" 2>/dev/null || echo "unknown"; }

case "${1:-status}" in
  status)
    echo "== 版本 =="; echo "  已装: $(_ver)"
    echo "== 模块 =="
    for f in "$LIB/i686/libvapor_forge.so" "$LIB/x86_64/libvapor_forge.so"; do
      [ -f "$f" ] && echo "  $(md5sum "$f" | cut -d' ' -f1)  $(stat -c%s "$f")  $f" || echo "  缺失 $f"
    done
    PID=$(pgrep -f '^/home/deck/.local/share/Steam/steamrt64/steam ' | head -1)
    if [ -n "${PID:-}" ] && grep -q vapor_forge "/proc/$PID/maps" 2>/dev/null; then
      echo "  ✅ 已加载进 steam（pid $PID）"
    else
      echo "  ⚠️ 未在运行中的 steam 里找到（没启动或没生效）"
    fi
    echo "== 包装器 =="
    grep -h 'LIB[12]=' "$W1" "$W2" 2>/dev/null | sed 's/^/  /'
    echo "  备份: $(ls -1 "$W1".pre-vf.bak "$W2".pre-vf.bak 2>/dev/null | wc -l) 个"
    echo "== 配置 =="
    grep -E '^(log_level|patterns_url)' "$H/.config/vapor-forge/config.toml" 2>/dev/null | sed 's/^/  /'
    echo "== 脚本 =="
    echo "  $(ls -1 "$H/.local/share/Steam/config/lua/"*.lua 2>/dev/null | wc -l) 个 lua（Steam 目录）"
    echo "  $(ls -1 "$H/.config/SLSsteam/lua/"*.lua 2>/dev/null | wc -l) 个 lua（SLSsteam 目录）"
    echo "== 前端 =="
    ss -ltn 2>/dev/null | grep -q ':8917 ' && echo "  ✅ 8917 在监听" || echo "  ⚠️ 8917 没起来（systemctl --user start steamunlock-webui）"
    ;;
  rollback)
    [ -f "$W1.pre-vf.bak" ] || { echo "找不到 $W1.pre-vf.bak"; exit 1; }
    cp -f "$W1.pre-vf.bak" "$W1"; cp -f "$W2.pre-vf.bak" "$W2"
    rm -f "$H/.SLSsteam-crash-count"
    echo "已恢复 SLSsteam 包装器（$(md5sum "$W1" | cut -d' ' -f1)）。请整机重启。"
    ;;
  check-update)
    latest=$(curl -fsS -m 20 "$API" 2>/dev/null | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
    echo "已装: $(_ver)"; echo "上游最新: ${latest:-查询失败（检查网络）}"
    ;;
  upgrade)
    latest=$(curl -fsS -m 20 "$API" | grep -o '"tag_name": *"[^"]*"' | head -1 | cut -d'"' -f4)
    url=$(curl -fsS -m 20 "$API" | grep -o '"browser_download_url": *"[^"]*linux.tar.gz"' | head -1 | cut -d'"' -f4)
    [ -n "${latest:-}" ] && [ -n "${url:-}" ] || { echo "查询失败"; exit 1; }
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    echo "下载 $latest ..."; curl -fsSL -m 300 "$url" -o "$tmp/vf.tar.gz"
    tar -xzf "$tmp/vf.tar.gz" -C "$tmp" --strip-components=1
    for a in i686 x86_64; do
      cp -f "$tmp/$a/libvapor_forge.so" "$LIB/$a/libvapor_forge.so"
      cp -f "$tmp/$a/libvapor_forge_proton_inject.so" "$LIB/$a/libvapor_forge_proton_inject.so" 2>/dev/null || true
      chmod 755 "$LIB/$a/"*.so
    done
    echo "$latest" > "$LIB/VERSION"
    echo "已安装 $latest。请整机重启（或至少重启 Steam）。"
    ;;
  *)
    echo "用法: $0 {status|rollback|check-update|upgrade}"; exit 2;;
esac

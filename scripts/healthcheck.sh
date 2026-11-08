#!/bin/bash
# SLSsteam-Plus 健康检查 —— 任何时候出问题先跑这个
# 用法: bash ~/steam-toolkit/scripts/healthcheck.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 项目根目录：从脚本位置推导，不写死目录名（新旧安装都适用）
SLSDIR="$HOME/.local/share/SLSsteam"
STEAMCFG="$HOME/.local/share/Steam/config"
CONFIGDIR="$HOME/.config/SLSsteam"
ok=0; warn=0; err=0
p() { printf "  %-52s %s\n" "$1" "$2"; }
okf() { p "$1" "✓ $2"; ok=$((ok+1)); }
warnf() { p "$1" "⚠ $2"; warn=$((warn+1)); }
errf() { p "$1" "✗ $2"; err=$((err+1)); }

echo "=============================================="
echo " SLSsteam-Plus 健康检查"
echo "=============================================="
echo
echo "--- 1. 注入库 ---"
for f in "$SLSDIR/SLSsteam.so" "$SLSDIR/library-inject.so"; do
    n=$(basename "$f")
    if [ -s "$f" ]; then
        sz=$(stat -c%s "$f")
        arch=$(file -b "$f" | grep -o "32-bit" || echo "非32位")
        okf "$n" "$sz 字节, $arch"
    else
        errf "$n" "缺失或为空"
    fi
done
echo
echo "--- 2. 动态依赖 ---"
for f in "$SLSDIR/SLSsteam.so" "$SLSDIR/library-inject.so"; do
    [ -s "$f" ] || continue
    miss=$(ldd "$f" 2>/dev/null | grep -c "not found")
    if [ "$miss" -eq 0 ]; then okf "$(basename $f) 依赖" "完整"
    else errf "$(basename $f) 依赖" "缺 $miss 个"; fi
done
echo
echo "--- 3. LD_AUDIT 符号 ---"
if [ -s "$SLSDIR/SLSsteam.so" ]; then
    n=$(nm -D "$SLSDIR/SLSsteam.so" 2>/dev/null | grep -cE "la_version|la_objopen|la_preinit")
    [ "$n" -ge 3 ] && okf "SLSsteam.so 导出" "$n 个符号" || errf "SLSsteam.so 导出" "只有 $n 个"
fi
echo
echo "--- 4. 启动器 ---"
if [ -x "$SLSDIR/path/steam" ]; then
    if grep -q "SLS_NOINJECT" "$SLSDIR/path/steam"; then
        okf "启动器" "已加固（含回退）"
    else
        warnf "启动器" "未加固（建议重新安装）"
    fi
else
    errf "启动器" "缺失"
fi
echo
echo "--- 5. 安全网（系统 Steam 未被改）---"
if [ -L /usr/bin/steam ]; then okf "/usr/bin/steam" "符号链接 -> $(readlink /usr/bin/steam)"
else warnf "/usr/bin/steam" "被改成了普通文件？"; fi
[ -x "$HOME/.local/bin/steam-noinject" ] && okf "无注入启动器" "可用" || warnf "无注入启动器" "缺失"
echo
echo "--- 6. 配置 ---"
[ -f "$CONFIGDIR/config.toml" ] && okf "config.toml" "存在" || errf "config.toml" "缺失"
nlua=$(ls "$CONFIGDIR/lua/"*.lua 2>/dev/null | wc -l)
nlua2=$(ls "$STEAMCFG/lua/"*.lua 2>/dev/null | wc -l)
[ "$nlua" -gt 0 ] && okf "Lua 配置" "$nlua 个（Steam侧 $nlua2 个）" || warnf "Lua 配置" "0 个（没配任何游戏）"
echo
echo "--- 7. depot 密钥 ---"
python3 - "$STEAMCFG/config.vdf" <<'PY' 2>/dev/null || echo "  （python 检查跳过）"
import re,sys
try:
    t=open(sys.argv[1],encoding='utf-8',errors='replace').read()
    keys=re.findall(r'"(\d+)"\s*\{\s*"DecryptionKey"\s*"([0-9a-fA-F]+)"', t)
    good=sum(1 for _,k in keys if len(k)==64)
    bad=sum(1 for _,k in keys if len(k)!=64)
    print(f"  {'config.vdf 密钥':<52} ✓ {len(keys)} 个（有效 {good}, 无效 {bad}）")
except Exception as e:
    print(f"  config.vdf                                ✗ 解析失败: {e}")
PY
echo
echo "--- 8. 清单文件 ---"
nm=$(ls "$STEAMCFG/../depotcache/"*.manifest 2>/dev/null | wc -l)
[ "$nm" -gt 0 ] && okf "depotcache" "$nm 个清单" || warnf "depotcache" "0 个清单"
echo
echo "--- 9. 最近日志（注入是否成功）---"
if [ -f "$HOME/.SLSsteam.log" ]; then
    if grep -q "Loaded successfully" "$HOME/.SLSsteam.log" 2>/dev/null; then
        okf "SLSsteam 日志" "上次启动成功"
    elif grep -q "Failed to find all patterns" "$HOME/.SLSsteam.log" 2>/dev/null; then
        warnf "SLSsteam 日志" "特征码失配（Steam 更新了？）→ 见恢复手册"
    else
        warnf "SLSsteam 日志" "无成功记录"
    fi
    miss=$(grep -oE "missing=[0-9]+" "$HOME/.SLSsteam.log" 2>/dev/null | tail -1)
    [ -n "$miss" ] && p "  最近特征码状态" "$miss"
else
    warnf "SLSsteam 日志" "不存在（还没用注入启动过）"
fi
echo
echo "--- 10. 根目录整洁度 ---"
_stray=$(ls "$HOME"/.SLSsteam.log.before-* "$HOME"/q.py "$HOME"/*.tmp "$HOME"/.SLSsteam.log.prev 2>/dev/null | wc -l)
if [ "$_stray" -eq 0 ]; then
    p "根目录残留" "✓ 干净"
else
    warnf "根目录残留" "$_stray 个临时文件（跑 scripts/archive-logs.sh 清理）"
fi
echo
echo "--- 11. 启动器日志 ---"
_launcher_log="$ROOT/logs/launcher.log"
[ -f "$_launcher_log" ] && tail -3 "$_launcher_log" | sed 's/^/  /' || p "启动器日志" "暂无"
echo
echo "--- 12. depot 重叠覆盖（多个 depot 的同名文件是否落对版本）---"
( cd "$ROOT" && python3 -m suos.cli check-overlap --all --healthcheck )
case $? in
    0) ok=$((ok+1)) ;;
    1) warn=$((warn+1)) ;;
    2) err=$((err+1)) ;;
    *) warnf "depot 重叠检查" "检查本身出错（见上方输出）" ;;
esac
echo
echo "=============================================="
echo " 结果: ✓ $ok 项正常  ⚠ $warn 项警告  ✗ $err 项错误"
echo "=============================================="
if [ "$err" -gt 0 ]; then
    echo
    echo "有错误！试试无注入启动（安全网）:"
    echo "    ~/.local/bin/steam-noinject"
    echo "恢复手册: cat $ROOT/docs/恢复手册.md"
fi

#!/bin/bash
# ============================================================
# Steam Toolkit 启动器（加固版）
#   1. 自动检查注入库是否存在且可加载
#   2. 任何异常都回退到"不带注入"启动，保证 Steam 一定能开
#   3. 设置 SLS_NOINJECT=1 可强制不注入
#   4. 崩溃保护：连续 3 次启动崩溃自动降级为不注入，
#      注入库或 Steam 更新后自动恢复注入
# ============================================================
SLSDIR="$HOME/.local/share/SLSsteam"
LIB1="$SLSDIR/library-inject.so"
LIB2="$SLSDIR/SLSsteam.so"
STEAMBIN="/usr/bin/steam"
STEAMCLIENT="$HOME/.local/share/Steam/ubuntu12_32/steamclient.so"
CONSOLE_LOG="$HOME/.local/share/Steam/logs/console-linux.txt"
MONITOR_SECS=30

# 日志放项目目录（不污染根目录）。项目目录由 install.sh 部署时写入 __SUOS_DIR__；
# 没写入/不存在就试两个候选（新安装 ~/steam-toolkit、老安装 ~/SteamUnlockOS）；
# 都不可用则降级到 /dev/null
SUOS_DIR="${SUOS_DIR:-__SUOS_DIR__}"
if [ ! -d "$SUOS_DIR" ]; then
    for _d in "${HOME:?}/steam-toolkit" "$HOME/SteamUnlockOS"; do
        if [ -d "$_d" ]; then SUOS_DIR="$_d"; break; fi
    done
fi
LOG="/dev/null"
if [ -d "$SUOS_DIR" ] && mkdir -p "$SUOS_DIR/logs" 2>/dev/null && [ -w "$SUOS_DIR/logs" ]; then
    LOG="$SUOS_DIR/logs/launcher.log"
fi

log() { echo "[$(date '+%F %T')] $*" >> "$LOG" 2>/dev/null || :; }

# 32 位 Steam 客户端是否已在运行（按 /proc/*/exe 精确匹配，不用 pkill -f）
steam_running() {
    local d
    for d in /proc/[0-9]*; do
        [ "${d#/proc/}" = "$$" ] && continue
        case "$(readlink "$d/exe" 2>/dev/null)" in
            */ubuntu12_32/steam) return 0 ;;
        esac
    done
    return 1
}

# 黑匣子：SLSsteam 每次加载都会清空 ~/.SLSsteam.log，Steam 自己的日志也会轮换，
# 偶发故障（如冷启动卡转圈）的现场下次一启动就没了。所以真正启动前先把上一次会话的
# 日志压缩存到 logs/blackbox/<时间>/，保留最近 8 份。任何失败都忽略，不影响启动。
blackbox() {
    [ -d "$SUOS_DIR/logs" ] || return 0
    local bb="$SUOS_DIR/logs/blackbox" sl="$HOME/.local/share/Steam/logs" dst f
    dst="$bb/$(date '+%Y%m%d-%H%M%S')"
    mkdir -p "$dst" || return 0
    [ -s "$HOME/.SLSsteam.log" ] && gzip -c "$HOME/.SLSsteam.log" > "$dst/SLSsteam.log.gz"
    for f in webhelper_js console_log content_log bootstrap_log webhelper connection_log; do
        [ -s "$sl/$f.txt" ] && tail -c 2097152 "$sl/$f.txt" | gzip > "$dst/$f.txt.gz"
    done
    ls -1d "$bb"/*/ 2>/dev/null | head -n -8 | xargs -r rm -rf
}

# 只在真正启动时存（转发 steam:// URL 的调用不存，否则会把现场冲掉）
steam_running || { blackbox 2>/dev/null || :; }

# 手动跳过注入
if [ "${SLS_NOINJECT:-0}" = "1" ]; then
    log "SLS_NOINJECT=1 → 不带注入启动"
    exec "$STEAMBIN" "$@"
fi

# ★ Steam 已在运行：本次调用只是把 steam:// URL / 参数转发给运行中的实例，
#   转发进程几秒内就会正常退出。不能进入下面的崩溃监控 —— 否则会被误计为崩溃，
#   点 3 次桌面快捷方式后，下次启动 Steam 就被降级成不注入（游戏显示"购买"）。
if steam_running; then
    exec "$STEAMBIN" "$@"
fi

# 检查文件存在且非空
if [ ! -s "$LIB1" ] || [ ! -s "$LIB2" ]; then
    log "注入库缺失或为空 → 回退到普通启动（Steam 仍可正常使用）"
    exec "$STEAMBIN" "$@"
fi

# 快速验证动态链接（缺依赖就不注入，避免刷屏报错）
if command -v ldd >/dev/null 2>&1; then
    if ldd "$LIB2" 2>/dev/null | grep -q "not found"; then
        log "SLSsteam.so 依赖缺失 → 回退到普通启动"
        exec "$STEAMBIN" "$@"
    fi
fi

# ★ 崩溃保护：连续崩溃 3 次就自动降级为不注入（避免一直崩）
FAILFILE="$HOME/.SLSsteam-crash-count"
if [ -f "$FAILFILE" ]; then
    # 注入库或 Steam 更新过，之前的崩溃记录就作废，重新试注入
    if [ "$LIB2" -nt "$FAILFILE" ] || [ "$STEAMCLIENT" -nt "$FAILFILE" ]; then
        log "注入库或 Steam 已更新 → 清除崩溃记录，重新尝试注入"
        rm -f "$FAILFILE" 2>/dev/null
    fi
fi
if [ -f "$FAILFILE" ]; then
    fails=$(cat "$FAILFILE" 2>/dev/null || echo 0)
    if [ "${fails:-0}" -ge 3 ]; then
        log "检测到连续 $fails 次启动崩溃 → 自动降级为【不注入】启动"
        log "  修好后删除 $FAILFILE 即可恢复注入（注入库或 Steam 更新后会自动恢复）"
        if command -v notify-send >/dev/null 2>&1; then
            notify-send -t 0 "Steam Toolkit" \
                "Steam 连续崩溃 $fails 次，已临时关闭注入，入库游戏可能显示“购买”。" 2>/dev/null &
        fi
        exec "$STEAMBIN" "$@"
    fi
fi

log "带 SLSsteam-Plus 注入启动"
export LD_AUDIT="$LIB1:$LIB2"

# 同一次启动可能同时来两个调用（日志里同一秒两条）。只让拿到锁的那个监控和计数，
# 否则一次崩溃会被算两次；另一个照常带注入启动，由 Steam 自己决定谁转发。
if command -v flock >/dev/null 2>&1; then
    exec 9>"$HOME/.SLSsteam-launcher.lock"
    if ! flock -n 9; then
        exec 9>&-
        exec "$STEAMBIN" "$@"
    fi
fi

# steam.sh 会吞掉段错误（自己退出码还是 0），退出码判断不了崩溃，所以看控制台日志里
# 这次启动之后有没有新增崩溃记录，形如:
#   steam.sh: line 995: 65827 Segmentation fault (core dumped) "$STEAMROOT/$STEAMEXEPATH" "$@"
console_offset=$(stat -c %s "$CONSOLE_LOG" 2>/dev/null || echo 0)
crashed_since_start() {
    local size
    size=$(stat -c %s "$CONSOLE_LOG" 2>/dev/null) || return 1
    # 日志轮换过（变小了）就从头看
    [ "$size" -lt "$console_offset" ] && console_offset=0
    tail -c +"$((console_offset + 1))" "$CONSOLE_LOG" 2>/dev/null |
        grep -qE 'steam\.sh: line [0-9]+: +[0-9]+ +(Segmentation fault|Aborted|Bus error|Illegal instruction)'
}

# 启动并监控：MONITOR_SECS 秒内退出就检查是不是崩溃。每秒看一次，崩了不用白等满。
"$STEAMBIN" "$@" 9>&- &
SPID=$!
waited=0
while [ "$waited" -lt "$MONITOR_SECS" ] && kill -0 "$SPID" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
done

if kill -0 "$SPID" 2>/dev/null; then
    # 还活着 → 清空失败计数
    rm -f "$FAILFILE" 2>/dev/null
    wait "$SPID"
    exit $?
fi

wait "$SPID" 2>/dev/null
rc=$?
sleep 1    # 等日志进程把最后几行写完
if [ "$rc" -eq 0 ] && ! crashed_since_start; then
    # 正常退出（例如用户很快关了 Steam，或只是转发了参数）不是崩溃
    log "Steam 在 $waited 秒内正常退出，不计为崩溃"
    exit 0
fi
fails=$(cat "$FAILFILE" 2>/dev/null || echo 0)
echo $((fails + 1)) > "$FAILFILE"
log "Steam 启动后 $waited 秒内崩溃（第 $((fails + 1)) 次），退出码 $rc"
exit "$rc"

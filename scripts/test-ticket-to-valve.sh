#!/bin/bash
# ============================================================
# 票据请求控制（切换 BlockTicketRequests 配置）
#
# 控制 SLSsteam 是否拦截发往 Valve 的认证票据请求
#
# 使用: bash scripts/test-ticket-to-valve.sh on|off|status
# ============================================================
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 项目根目录：从脚本位置推导，不写死目录名（新旧安装都适用）
CFG="$HOME/.config/SLSsteam/config.toml"
BAK="$ROOT/backup/known-good/config.toml"
LOG="$HOME/.Steam Toolkit-launcher.log"

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

case "${1:-status}" in
  on)
    # 让票据发给 Valve
    if grep -q "^BlockTicketRequests = true" "$CFG" 2>/dev/null; then
        cp "$CFG" "$ROOT/backup/config.toml.before-ticket-test"
        sed -i 's/^BlockTicketRequests = true/BlockTicketRequests = false/' "$CFG"
        log "✓ 已改为【票据发给 Valve】(BlockTicketRequests = false)"
    else
        log "（已经是 false 或没这行）"
    fi
    ;;
  off)
    # 恢复拦截（安全模式）
    if [ -f "$ROOT/backup/config.toml.before-ticket-test" ]; then
        cp "$ROOT/backup/config.toml.before-ticket-test" "$CFG"
        log "✓ 已恢复【拦截票据】(BlockTicketRequests = true)"
    else
        sed -i 's/^BlockTicketRequests = false/BlockTicketRequests = true/' "$CFG" 2>/dev/null || \
            echo "BlockTicketRequests = true" >> "$CFG"
        log "✓ 已设为拦截"
    fi
    ;;
  status)
    echo "当前配置:"
    grep -E "BlockTicketRequests|PlayNotOwnedGames|PackageInjection" "$CFG" 2>/dev/null || echo "  （读不到）"
    ;;
  *)
    echo "用法: $0 on|off|status"
    exit 1
    ;;
esac

if [ "${1}" != "status" ]; then
    echo
    echo "下一步: 重启 Steam"
    echo "  ~/.local/share/SLSsteam/path/steam"
    echo
    echo "然后试安装一个小游戏（别再拿紫色晶石试了）"
    echo "看日志: grep -E 'Dropped raw AppOwnershipTicket|Loaded local' ~/.SLSsteam.log"
fi

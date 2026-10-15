#!/bin/sh
# SLSsteam-Plus 构建环境（SteamOS）
#
# 用途: 编译 SLSsteam-Plus（需要 32 位工具链，SteamOS 默认没有）
#   32 位工具链放在  <项目目录>/buildtools/  （不需要 sudo，删了也能重装）
#
# 用法:
#   . <项目目录>/buildenv.sh
#   cd ~/slsplus-build && make -j$(nproc)
#
# ★ 项目目录不写死（老安装在 ~/SteamUnlockOS，新安装在 ~/steam-toolkit）。
#   脚本是用 `.` source 的，这时 $0 是 shell 名而不是脚本路径，所以按顺序找:
#     ① 调用方已设置的 SLSU_ROOT
#     ② bash 下用 BASH_SOURCE 定位本文件
#     ③ 兜底：哪个候选目录里有 buildtools 就用哪个

if [ -z "${SLSU_ROOT:-}" ] && [ -n "${BASH_SOURCE:-}" ]; then
    SLSU_ROOT="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
fi
if [ -z "${SLSU_ROOT:-}" ] || [ ! -d "$SLSU_ROOT/buildtools" ]; then
    for _d in "$HOME/steam-toolkit" "$HOME/SteamUnlockOS"; do
        if [ -d "$_d/buildtools" ]; then SLSU_ROOT="$_d"; break; fi
    done
fi
SLSU_TOOLS="$SLSU_ROOT/buildtools"

# ${VAR:-} 防止调用方开了 set -u 时因变量未定义而中断
export PATH="$SLSU_TOOLS/wrappers:$PATH"
export LD_LIBRARY_PATH="$SLSU_TOOLS/usr/lib:${LD_LIBRARY_PATH:-}"
export C_INCLUDE_PATH="$SLSU_TOOLS/usr/include"
export CPLUS_INCLUDE_PATH="$SLSU_TOOLS/usr/include"
export PKG_CONFIG_PATH="$SLSU_TOOLS/usr/lib/pkgconfig"

# 等价的 32 位专属变量（有些 Makefile 只看这些）
export LIBRARY_PATH="$SLSU_TOOLS/usr/lib:${LIBRARY_PATH:-}"

# ★ libm 必须链接系统自带的那份（2026-10-15 踩坑）：buildtools 里的 lib32-glibc 比 SteamOS 新，
#   链接时 fmod 会绑定到 GLIBC_2.43，SteamOS(glibc 2.41) 加载不了 → 启动器判定"依赖缺失"回退不注入。
#   gcc -m32 会先找 <LIBRARY_PATH 目录>/../lib32，所以符号链接放在 syslibm/lib32，路径写 syslibm/lib。
if [ -e /usr/lib32/libm.so.6 ]; then
    mkdir -p "$SLSU_TOOLS/syslibm/lib" "$SLSU_TOOLS/syslibm/lib32"
    ln -sf /usr/lib32/libm.so.6 "$SLSU_TOOLS/syslibm/lib32/libm.so"
    export LIBRARY_PATH="$SLSU_TOOLS/syslibm/lib:$LIBRARY_PATH"
fi

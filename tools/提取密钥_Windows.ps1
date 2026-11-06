# ============================================================
# Windows 端 depot 密钥导出（跨平台同步）
#
# 使用场景:
#   社区数据库尚未收录的游戏 → 在 Windows 端获取密钥
#   → 用本脚本导出 → 同步到 SteamOS
#
# 方法（两种策略，自动依次尝试）:
#   策略1: 监听 config.vdf 变化（直接从配置文件提取）
#   策略2: 扫描 Steam 进程内存（用 depot id 做锚点定位）
#
# 用法: 管理员 PowerShell，游戏【正在下载时】运行
# ============================================================
$ErrorActionPreference = 'Continue'

$DEPOT = Read-Host "请输入要提取的 Depot ID（例如 4012811）"
if ($DEPOT -notmatch '^\d+$') { Write-Host "必须是数字" -Fore Red; Read-Host "回车退出"; return }
$DEPOT_INT = [int]$DEPOT
$APPID_HINT = Read-Host "游戏 AppID（可留空）"

Write-Host ""
Write-Host "=== 目标: Depot $DEPOT ===" -Fore Cyan
Write-Host ""

# ── 找 Steam 路径 ──
$steamPath = $null
foreach ($p in @((Get-ItemProperty "HKCU:\Software\Valve\Steam" -Name SteamPath -EA SilentlyContinue).SteamPath,
                 "$env:ProgramFiles(x86)\Steam", "C:\Steam", "D:\Steam",
                 "$env:ProgramFiles\Steam")) {
    if ($p -and (Test-Path (Join-Path $p "steam.exe"))) { $steamPath = $p; break }
}
$configVdf = if ($steamPath) { Join-Path $steamPath "config\config.vdf" } else { $null }
Write-Host "Steam: $steamPath"

# ══════════════════════════════════════════════════════════════
#  策略 1: 监听 config.vdf
# ══════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "[策略1] 监听 config.vdf 里有没有 depot $DEPOT 的密钥..." -Fore Yellow

function Check-Vdf {
    if (-not $configVdf -or -not (Test-Path $configVdf)) { return $null }
    $t = Get-Content $configVdf -Raw -Encoding UTF8
    $m = [regex]::Match($t, '"' + $DEPOT + '"\s*\{\s*"DecryptionKey"\s*"([0-9a-fA-F]{64})"')
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}

$key = Check-Vdf
if ($key) {
    Write-Host "  ★★★ 已经在 config.vdf 里找到了！" -Fore Green
    Write-Host "  depot $DEPOT = $key" -Fore Green
} else {
    Write-Host "  config.vdf 里没有 → SteamUnlock 用的是内存注入"
    Write-Host "  开始【策略2】扫描 Steam 内存..."
}

# ══════════════════════════════════════════════════════════════
#  策略 2: 用 depot id 做锚点扫内存
# ══════════════════════════════════════════════════════════════
if (-not $key) {
    Add-Type -TypeDefinition @"
using System;using System.Runtime.InteropServices;
public static class MK{
[DllImport("kernel32.dll")]public static extern IntPtr OpenProcess(uint a,bool b,int c);
[DllImport("kernel32.dll")]public static extern bool ReadProcessMemory(IntPtr h,IntPtr a,byte[] b,int s,out IntPtr r);
[DllImport("kernel32.dll")]public static extern IntPtr VirtualQueryEx(IntPtr h,IntPtr a,out MBI m,int l);
[DllImport("kernel32.dll")]public static extern bool CloseHandle(IntPtr h);
[StructLayout(LayoutKind.Sequential)]public struct MBI{public IntPtr B;public IntPtr A;public uint AP;public IntPtr S;public uint St;public uint P;public uint T;}
}
"@

    $procs = Get-Process steam -EA SilentlyContinue
    if (-not $procs) { Write-Host "  ✗ Steam 没运行" -Fore Red; Read-Host "回车退出"; return }

    # 锚点1: depot id 的 LE uint32 字节
    $b0 = [byte]($DEPOT_INT -band 0xFF)
    $b1 = [byte](($DEPOT_INT -shr 8) -band 0xFF)
    $b2 = [byte](($DEPOT_INT -shr 16) -band 0xFF)
    $b3 = [byte](($DEPOT_INT -shr 24) -band 0xFF)
    # 锚点2: depot id 的 ASCII hex 字符串
    $anchorStr = $DEPOT

    $hits = New-Object System.Collections.ArrayList

    foreach ($proc in $procs) {
        $h = [MK]::OpenProcess(0x0410, $false, $proc.Id)
        if ($h -eq [IntPtr]::Zero) { continue }
        $mbi = New-Object MK+MBI
        $msz = [System.Runtime.InteropServices.Marshal]::SizeOf([type][MK+MBI])
        $addr = [IntPtr]::Zero
        $region = 0
        for ($i = 0; $i -lt 500000; $i++) {
            if ([MK]::VirtualQueryEx($h, $addr, [ref]$mbi, $msz) -eq [IntPtr]::Zero) { break }
            $sz = [int64]$mbi.S
            if ($sz -le 0) { break }
            $pr = $mbi.P
            $ok = ($mbi.St -eq 0x1000) -and (($pr -band 0x100) -eq 0) -and
                  (($pr -band 2) -or ($pr -band 4) -or ($pr -band 0x20) -or ($pr -band 0x40) -or ($pr -band 0x80))
            if ($ok -and $sz -le 67108864) {
                $region++
                $buf = New-Object byte[] $sz
                $rd = [IntPtr]::Zero
                if ([MK]::ReadProcessMemory($h, $mbi.B, $buf, [int]$sz, [ref]$rd)) {
                    # 找 depot id 的 LE 字节
                    for ($k = 0; $k -le $buf.Length - 4; $k++) {
                        if ($buf[$k] -eq $b0 -and $buf[$k+1] -eq $b1 -and $buf[$k+2] -eq $b2 -and $buf[$k+3] -eq $b3) {
                            # 在 ±4096 范围内找 32 字节高熵缓冲
                            $st = [Math]::Max(0, $k - 4096)
                            $en = [Math]::Min($buf.Length - 32, $k + 4096)
                            for ($j = $st; $j -le $en; $j++) {
                                # 简单熵检查: 32 字节里不同值 >= 20 且无长串零
                                $set = @{}
                                $zeros = 0
                                for ($z = 0; $z -lt 32; $z++) {
                                    $v = $buf[$j+$z]
                                    $set[$v] = 1
                                    if ($v -eq 0) { $zeros++ }
                                }
                                if ($set.Count -ge 24 -and $zeros -le 2) {
                                    $hex = -join ($buf[$j..($j+31)] | ForEach-Object { $_.ToString('x2') })
                                    [void]$hits.Add([PSCustomObject]@{ Pid=$proc.Id; Addr=$mbi.B; Off=$j; Hex=$hex })
                                }
                            }
                        }
                    }
                }
            }
            $nx = [int64]$mbi.B + $sz
            if ($nx -le [int64]$mbi.B) { break }
            $addr = [IntPtr]$nx
            if ($nx -gt 0x7FFFFFFF0000) { break }
        }
        [MK]::CloseHandle($h) | Out-Null
        Write-Host "  进程 $($proc.Id): 扫了 $region 个区域"
    }

    Write-Host ""
    if ($hits.Count -eq 0) {
        Write-Host "  ✗ 没找到候选密钥" -Fore Yellow
        Write-Host "    建议: 确认游戏【正在下载】，然后重跑本脚本" -Fore Yellow
    } else {
        Write-Host "  找到 $($hits.Count) 个候选（32 字节高熵、离 depot id 很近）:" -Fore Green
        Write-Host ""
        $uniq = $hits | Select-Object -ExpandProperty Hex -Unique
        $n = 0
        foreach ($hx in $uniq) {
            $n++
            Write-Host "    [$n] $hx"
            if ($n -ge 30) { break }
        }
        Write-Host ""
        Write-Host "  ★ 把这些发给我，我帮你确认哪个是真的（或逐个试）" -Fore Cyan
    }
}

# ── 如果策略1拿到了，也做一次内存扫描做对照 ──
if ($key) {
    Write-Host ""
    Write-Host "  提示: 这个密钥已经在 config.vdf 里，不需要扫内存了" -Fore Green
}

# ── 输出结果 ──
$outFile = Join-Path ([Environment]::GetFolderPath("Desktop")) "depotkey_$DEPOT.txt"
$lines = @()
$lines += "# Depot $DEPOT 密钥提取结果"
$lines += "# 时间: $(Get-Date)"
$lines += "# Steam: $steamPath"
$lines += ""
if ($key) {
    $lines += "config.vdf 里的密钥:"
    $lines += "$DEPOT = $key"
    $lines += ""
    $lines += "可直接粘贴到 SteamOS 的 config.vdf:"
    $lines += '"' + $DEPOT + '"'
    $lines += '{'
    $lines += '    "DecryptionKey" "' + $key + '"'
    $lines += '}'
} else {
    $lines += "候选密钥（需要人工确认）:"
    if ($hits) { $hits | Select-Object -ExpandProperty Hex -Unique | ForEach-Object { $lines += $_ } }
}
$lines += ""
$lines += "# SteamOS 上入库命令:"
$lines += "#   python3 -m suos.cli install-mh3 $APPID_HINT"
$lines | Out-File $outFile -Encoding UTF8

Write-Host ""
Write-Host "=============================================="
Write-Host "已保存: $outFile" -Fore Green
Write-Host "把它发给我" -Fore Cyan
Write-Host "=============================================="
Read-Host "回车退出"

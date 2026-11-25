# 掌机条目系统 · Master 交接指南

> 用途：Steam 更新导致失效时，把这份文件整个发给 AI，它能在 5 分钟内理解这套系统的全貌并开始排障。
> 编写时间：2026-11-25 23:15（Asia/Shanghai）
> 机器：SteamOS 掌机（`deck@192.168.31.158`，SSH 私钥在本机 `~/.ssh/ctf_deck`）
> 这份文件描述的是**当前实际运行**的系统，不是计划。

---

## 0. 30 秒速览

```
你点 Steam
  → ~/.local/bin/steam（加固包装器，设置 LD_AUDIT）
    → /usr/bin/steam → steam.sh → steamrt64/steam（64 位客户端，pressure-vessel 容器内）
      → glibc 按 ELF class 加载 audit 模块：
          32 位进程 → i686/libvapor_forge.so   （跳过 x86_64 那个）
          64 位进程 → x86_64/libvapor_forge.so （跳过 i686 那个）
            → 模块 hook 住 Steam 的所有权判定、depot 规划、清单请求码、depot 密钥加载
              → 你在库里看到的条目"可安装、可启动"
```

三条独立的东西，别混：

| 名字 | 干什么 | 现在的位置 |
|---|---|---|
| **Vapor Forge** | 运行时模块（Rust），真正让 Steam 认为"你有这个游戏" | `~/.local/share/vapor-forge/{i686,x86_64}/libvapor_forge.so` |
| **SteamUnlockOS**（工具链） | Python 工具集 + 本地网页，负责"查数据 → 写 lua → 写密钥" | `~/SteamUnlockOS/`，网页 `127.0.0.1:8917` |
| **包装器** | 唯一的注入入口，决定 Steam 加载哪个 LD_AUDIT 模块 | `~/.local/bin/steam` + `~/.local/share/SLSsteam/path/steam`（两份必须一致） |

旧的 **SLSsteam（i386）** 模块还在磁盘上但**不加载**（64 位客户端下会报 `wrong ELF class`）。它是回滚目标，不是现役。

---

## 1. 基线指纹（排障时先核对）

| 项 | 值 | 怎么查 |
|---|---|---|
| 系统 | SteamOS 3.9.5，`BUILD_ID=20261023.100` | `cat /etc/os-release` |
| 客户端 | `steamrt64/steam`（64 位），build `1794536281` | `ps -eo args \| grep steamrt64` |
| `steamclient.so` | SHA256 `dab8df0daa27ffa6594ef5d6bc8cf707f37990220628f1f249fb30f4d88ade91` | `sha256sum ~/.local/share/Steam/steamrt64/steamclient.so` |
| 模块版本 | Vapor Forge `v0.2.0`（Rust，AGPL-3.0） | `bash ~/SteamUnlockOS/scripts/vf.sh status` |
| 模块指纹 | i686 `2ffb57d28edc7b4df4cb30839cb031af` / x86_64 `cdd984eb0a3a9c4901a68c6624d51b31` | 同上 |
| 包装器指纹 | `ccb85302c528d477d382b7db3a1e8042`（两份相同） | `md5sum ~/.local/bin/steam ~/.local/share/SLSsteam/path/steam` |
| 包装器备份 | `.pre-vf.bak`，md5 `f3bf83b134031f41365839df151a2d2f`（原 SLSsteam 版） | 回滚用 |
| 配置文件 | `~/.config/vapor-forge/config.toml`（自动生成，5255 B 起） | — |
| 脚本目录 | `~/.local/share/Steam/config/lua/*.lua`（模块读这个）+ `~/.config/SLSsteam/lua/`（工具链写这个，两份同步） | `ls` |

---

## 2. 组件与职责

### 2.1 Vapor Forge（运行时模块）

上游：`https://github.com/Hintay/vapor-forge`（Rust 重写，继任已废弃的 SLSsteam-Plus）。
加载方式：`LD_AUDIT`（rtld-audit 接口，不是 `LD_PRELOAD`）。
双架构：同一份 `LD_AUDIT` 里列两个路径，glibc 自己跳过架构不匹配的那个。

它已经实现的能力（本次验证过）：

| 能力 | 日志特征 |
|---|---|
| 所有权改写（`CUser::CheckAppOwnership`） | `feat: sharing unlocked app_id=…` |
| 包内注册（pkg0 `AppIdVec` + 许可重算） | `package: app IDs appended to pkg0 injected=N` / `pkg0 functions resolved (4/4)` |
| depot 规划（`BuildDepotDependency` + 许可闸门） | `Vec Alloc …` / `Depot … for … -> …` |
| 清单钉固（per-depot gid） | `manifest: pinned depot_id=… old_gid=… new_gid=…` |
| depot 解密密钥供给 | `depot_key: provided depot_id=…` / `depot: key request depot_id=… served=true` |
| 清单请求码（5 个 provider 链） | `manifest-code: …`（成功时）/ 失败则回退 |
| UI 桥（库列表刷新、购买时间戳） | `steamui: hooks installed … library_ready=true` |
| 脚本（Lua） | `scripting: scripts loaded apps=N depot_keys=M manifests=K` |

### 2.2 SteamUnlockOS（工具链）

- 网页：`http://127.0.0.1:8917/`（只绑本机），输入 AppID → 查数据 → 写 lua/密钥 → 触发 Steam 安装。
- systemd 用户单元：`steamunlock-webui.service`（已 enable，开机自启）。
- 数据源（**重要，决定"新游戏能不能搜到"**）：

| 源 | 地址 | 本机状态 |
|---|---|---|
| Steam appinfo | `api.steamcmd.net/v1/info/<appid>` | ✅ 2.3 s |
| sudama 密钥表 | `api.993499094.xyz/depotkeys.json`（242,325 条） | ✅ 但要下 ~16 MB，约 90 s |
| 社区密钥表 | jsdelivr / ghproxy / raw.githubusercontent（288,381 条） | ❌ jsdelivr 连不上、ghproxy ~30 KB/s、raw 连不上 |
| 清单 ManifestHub3 | `api.github.com/repos/steamtools-games/ManifestHub3` | ✅ |
| DLC 列表 | `auth1.caigamer.cn/GetAppinfo/<appid>` | ✅ 0.1 s |
| Steam 商店 API | `store.steampowered.com/api/appdetails` | ❌ **完全连不通**（TCP 超时） |

### 2.3 包装器

`~/.local/bin/steam`（副本 `~/.local/share/SLSsteam/path/steam`，**两份必须字节一致**）：
- 检查模块存在、`ldd` 无缺失；
- 连续 3 次崩溃自动降级为不注入（计数文件 `~/.SLSsteam-crash-count`）；
- `export LD_AUDIT="i686/libvapor_forge.so:x86_64/libvapor_forge.so"` 后 `exec /usr/bin/steam`；
- 保存上次会话日志到 `~/SteamUnlockOS/logs/blackbox/<时间>/`。

它是被 `steam-launcher.service` 通过 PATH 找到的：`~/.config/environment.d/50-steamunlock.conf` 把 `~/.local/bin` 放进 systemd 用户 PATH。

---

## 3. 启动链（排障必备）

```
systemd --user
 └─ steam-launcher.service        ExecStart=/usr/lib/steamos/steam-launcher
     └─ exec steam "$@"            ← 从 PATH 找，PATH 首项是 ~/.local/bin
         └─ ~/.local/bin/steam     ← 包装器（LD_AUDIT 在这里设置）
             └─ /usr/bin/steam → steam.sh
                 └─ steamrt64/steam（默认 64 位；除非 sentinel 或 STEAM_FORCE_CLIENT=ubuntu12_32）
                     └─ pressure-vessel 容器（把 LD_AUDIT 翻译成 --ld-audit=）
                         └─ 容器内 rv 加载 libvapor_forge.so
```

验证点：

```bash
# 1. 包装器是否被用到
tr '\0' '\n' < /proc/$(pgrep -f '^/home/deck/.local/share/Steam/steamrt64/steam ' | head -1)/environ | grep LD_AUDIT
# 2. 模块是否真进进程
grep vapor_forge /proc/<steam-pid>/maps
# 3. 模块自己的日志（写进 Steam 控制台日志）
grep 'vapor-forge' ~/.local/share/Steam/logs/console-linux.txt | tail
# 4. systemd 侧
journalctl --user -u steam-launcher.service -b | grep -i vapor
```

---

## 4. 数据流：一次"添加新条目"发生了什么

```
① 网页输入 AppID
   ├─ Steam appinfo（api.steamcmd.net）→ depot 全集、gid、DLC 自有 depot
   ├─ sudama / 社区表 → 每个 depot 的 32 字节密钥
   ├─ ManifestHub3 → 清单 gid + 清单文件 + 现成 lua
   └─ 商店 API 拿名字（连不通就跳过，用其它源的名字）

② 写盘
   ├─ ~/.local/share/Steam/config/lua/<appid>.lua   ← 模块读这个
   │     addappid(本体)
   │     addappid(depot,0,"<64hex 密钥>")
   │     setManifestid(depot,"<gid>")
   │     addappid(DLC…)
   ├─ 同一份复制到 ~/.config/SLSsteam/lua/（工具链自己的目录）
   └─ 密钥同时写进 config.vdf 的 Depots/<id>/DecryptionKey（双保险）

③ 模块热加载 lua（目录 inotify）→ 把条目写进 pkg0 + 重算许可
   日志：`scripting: … Lua scripts reloaded` → `package: … injected`

④ Steam 里点"安装"
   ├─ 规划：BIsLicensedForDepot 放行 → depot 列表非空（日志 `Vec Alloc … Size N`）
   ├─ 取码：ContentServerDirectory.GetManifestRequestCode#1 → provider 链
   ├─ 取清单：CDN（或本地 depotcache）
   └─ 解密：LoadDepotDecryptionKey 命中 → 写 32 B → 内容落盘
   成功标志：content_log `finished update, N mounted depots`，appmanifest 里 InstalledDepots 非空
```

---

## 5. 关键路径清单

| 路径 | 作用 | 备注 |
|---|---|---|
| `~/.local/bin/steam` | 包装器（主） | 与副本必须同 md5 |
| `~/.local/share/SLSsteam/path/steam` | 包装器（副本） | 工具链也会读它 |
| `~/.local/share/vapor-forge/i686/libvapor_forge.so` | 32 位模块 | 8.86 MB |
| `~/.local/share/vapor-forge/x86_64/libvapor_forge.so` | 64 位模块 | 9.16 MB |
| `~/.local/share/vapor-forge/x86_64/libvapor_forge_proton_inject.so` | Proton 注入助手 | 733 KB |
| `~/.local/share/vapor-forge/VERSION` | 已装版本号（`v0.2.0`） | `vf.sh` 读写 |
| `~/.config/vapor-forge/config.toml` | 模块配置 | 见 §6 |
| `~/.local/share/Steam/config/lua/*.lua` | 条目定义（模块读） | 13 个 |
| `~/.config/SLSsteam/lua/*.lua` | 条目定义（工具链写） | 13 个，与上同步 |
| `~/.local/share/Steam/depotcache/*.manifest` | 已下载的清单 | 离线安装靠它 |
| `~/.local/share/Steam/config/config.vdf` | 密钥备份（`Depots/<id>/DecryptionKey`） | 工具链会写 |
| `~/.local/share/Steam/logs/console-linux.txt` | **模块日志在这**（含 `[vapor-forge]` 行） | 6-7 MB 滚动 |
| `~/.local/share/Steam/logs/content_log.txt` | Steam 的下载/安装日志 | 排 0 字节问题看这里 |
| `~/SteamUnlockOS/` | 工具链仓库（git） | 可 `git diff` 看改动 |
| `~/SteamUnlockOS/scripts/vf.sh` | **本次新增的运维脚本** | status/rollback/check-update/upgrade |
| `~/SteamUnlockOS/logs/blackbox/` | 每次启动的日志快照 | 崩溃现场 |
| `~/.config/systemd/user/steamunlock-webui.service` | 网页自启单元 | `systemctl --user status steamunlock-webui` |

备份：

| 备份 | 内容 |
|---|---|
| `~/.local/bin/steam.pre-vf.bak`、`…/path/steam.pre-vf.bak` | 原 SLSsteam 包装器 |
| `~/SteamUnlockOS/webui_core.py.pre-searchfix-*` | 搜索改造前 |
| `~/SteamUnlockOS/suos/multisource.py.pre-refreshfix-*.bak` | 刷新改造前 |
| `~/SteamUnlockOS/webui_core.py.pre-claude-*` 等 | 更早的历史备份 |
| `~/.local/share/Steam/config/lua/4012810.lua.pre-dlckey.bak` | DLC 密钥补写前 |
| `~/rollback-backup-20261125-202036/` | 本轮总体回滚包（56 MB） |

---

## 6. 模块配置要点（`~/.config/vapor-forge/config.toml`）

| 键 | 现值 | 说明 |
|---|---|---|
| `[runtime] log_level` | `warn` | **默认应该是 warn**。设 `info`/`debug` 会产生 70 行/秒的报文日志，几分钟就几 MB |
| `[runtime] patterns_url` | `""` | 置空：本机连不上 raw.githubusercontent，留着只会启动时白等并打 WARN |
| `[apps.shared] enabled` | `true` | 家庭共享并发游玩解锁 |
| `[cloud] backend` | `disabled` | 云存档交给 Steam 原生；需要云存档同步时改 `local`/`cumulus` |
| `[ticket] cache` | `disk` | 票据缓存到磁盘 |
| `[manifest] providers` | `["20770407","manifestdex","opensteamtool","wudrm","steamrun"]` | 清单请求码的取码链，按序回退 |
| `[[apps.inject]]` | 空（由 lua 提供） | 脚本里的 `addappid` 会合并进来 |

改配置后模块会热加载；个别项（日志级别）可能要到下次启动才完全生效。

---

## 7. 本次改造清单（2026-11-25）

| # | 改动 | 位置 | 目的 |
|---|---|---|---|
| 1 | 模块从 SLSsteam(i386) 换成 Vapor Forge(i686+x86_64) | 包装器 2 份 + 新增模块目录 | 让 64 位客户端能加载 |
| 2 | 包装器 `LIB1/LIB2` 指向新模块；`STEAMCLIENT` 改指 `steamrt64` | 2 份包装器 | 注入路径正确 |
| 3 | 搜索顺序调整：先本地源，名字缺失才问商店 API | `webui_core.py` | 商店 API 本机不可达，原先固定白等 31 s |
| 4 | 商店 API 超时 15s→4s，失败后 10 分钟熔断 | `webui_core.py` | 同上 |
| 5 | 解析结果缓存 10 分钟 | `webui_core.py` | 重复搜索 24 s → 0 s |
| 6 | 密钥库刷新加 6 秒上限 + 失败源 30 分钟退避 | `suos/multisource.py` | 原先会同步下 16 MB、最坏卡 9 分钟 |
| 7 | 搜不到时的提示改为"可能正在后台更新" | `webui_core.py` | 区分"真没有"与"还没刷到" |
| 8 | 启动后台预热密钥表 | `webui_core.py` + `webui/server.py` | 首次搜索少解析两张表 |
| 9 | 模块日志级别降到 `warn`、关闭在线签名热更 | `config.toml` | 日志从 70 行/秒降到 ~0.3 行/秒 |
| 10 | 4012810 三个 DLC depot 补真密钥 | 两份 `4012810.lua` | 消掉 `Missing decryption key` 的更新取消循环 |
| 11 | 网页加 systemd 自启 | `steamunlock-webui.service` | 开机即用，不用手动跑 |
| 12 | 新增运维脚本 | `~/SteamUnlockOS/scripts/vf.sh` | 一条命令查状态/回滚/升级 |

---

## 8. 诊断树（按症状）

### 8.1 库里条目全变回"购买"/"安装"按钮

```bash
bash ~/SteamUnlockOS/scripts/vf.sh status      # 先看模块是否加载
journalctl --user -u steam-launcher.service -b | grep -iE 'vapor|ELFCLASS' | tail -20
grep -E 'pattern match failed|hook-install|patterns loaded' ~/.local/share/Steam/logs/console-linux.txt | tail
```

| 看到什么 | 原因 | 处置 |
|---|---|---|
| `wrong ELF class: ELFCLASS32` 且指向 SLSsteam | 包装器被还原成旧版 | 改回 vapor-forge 路径（见 §9 或 `vf.sh` 注释） |
| 完全没有任何 `[vapor-forge]` 行 | 包装器没执行 / LD_AUDIT 没生效 | `tr '\0' '\n' < /proc/<pid>/environ \| grep LD_AUDIT`；检查 `~/.local/bin` 是否在 PATH 首位 |
| `pattern match failed hook=…` 多条 | **Steam 客户端更新了**，模块签名不匹配 | 见 §10 |
| `hook capability ready … policy=disable-feature` | 个别能力被降级（当前已知：launch-environment 两条） | 一般不影响库与安装 |
| `~/.SLSsteam-crash-count` 存在且 ≥3 | 包装器的崩溃保护把注入关掉了 | `rm -f ~/.SLSsteam-crash-count` 后重启 |

### 8.2 网页搜索一直转圈

```bash
systemctl --user status steamunlock-webui
journalctl --user -u steamunlock-webui -f      # 看是不是卡在某个源
```

- 现在最长 ~30 秒（第三方源慢），重复搜同一 AppID 是秒出；
- 若超过 60 秒：多半是 `sudama` 表正在下载（~90 s），等它完成再搜；
- 若报"没查到"：先等 1 分钟再搜（后台在刷新密钥表）；仍然没有 = 社区还没收录这个游戏。

### 8.3 搜到了，但安装 0 字节 / 立刻"完成"

看 `content_log.txt`：

| 日志 | 含义 | 处置 |
|---|---|---|
| `finished update, 0 mounted depots` | depot 规划为空 | 模块没放行 license 闸门 → 确认模块已加载且 AppID 在 lua 里 |
| `Failed to get manifest request code, 'Access Denied'` | 取码失败 | 检查 `[manifest] providers`；本机网络能否到那些站点 |
| `Failed to initialize depot … (Missing decryption key)` | 某个 depot 没有密钥 | 给该 depot 补 key（sudama 表查），写进 lua `addappid(depot,0,"…")` |
| 有 `1 mounted depots` 但大小是 0 | 清单下到了但内容没下 | 看 `depotcache/` 有无 `<depot>_<gid>.manifest` |

### 8.4 游戏启动闪退 / 提示需要购买

```bash
grep -E 'feat:|netpacket:|ticket' ~/.local/share/Steam/logs/console-linux.txt | tail -30
```

- `netpacket: answered ownership ticket request locally app_id=…` 是正常路径；
- 若该游戏走 DRM 校验，可能需要 `[steam_id]` 或 `[[apps.inject]] ticket = "delegate"`（见上游 README）。

### 8.5 网页打不开

```bash
systemctl --user restart steamunlock-webui
ss -ltn | grep 8917
```

### 8.6 掌机卡顿 / 发热 / 日志暴涨

```bash
grep -n 'log_level' ~/.config/vapor-forge/config.toml    # 必须是 warn
ls -l ~/.local/share/Steam/logs/console-linux.txt        # 正常几十 KB 到几 MB
top -H -p $(pgrep -f 'steamrt64/steam ' | head -1)
```

本次实测：模块空闲 CPU ≈ 2.6%（单核百分比），日志 ≈ 0.3 行/秒；若日志明显更快，就是 `log_level` 被改回 info/debug 了。

---

## 9. 回滚 / 升级

### 9.1 回滚到 SLSsteam（当模块出问题、想回到"不注入"状态）

```bash
bash ~/SteamUnlockOS/scripts/vf.sh rollback
# 等价于：
cp ~/.local/bin/steam.pre-vf.bak ~/.local/bin/steam
cp ~/.local/share/SLSsteam/path/steam.pre-vf.bak ~/.local/share/SLSsteam/path/steam
rm -f ~/.SLSsteam-crash-count
# 然后整机重启
```
回滚后回到"i386 模块 + 64 位客户端 = 模块不加载"的状态，条目全部显示未持有。

### 9.2 升级 Vapor Forge

```bash
bash ~/SteamUnlockOS/scripts/vf.sh check-update   # 对比已装 vs 上游最新
bash ~/SteamUnlockOS/scripts/vf.sh upgrade        # 下载 + 替换两个 .so + 记版本号
# 整机重启
```

### 9.3 改回包装器指向（如果被还原）

```bash
for f in ~/.local/bin/steam ~/.local/share/SLSsteam/path/steam; do
  sed -i 's#^LIB1=.*#LIB1="$HOME/.local/share/vapor-forge/i686/libvapor_forge.so"#' "$f"
  sed -i 's#^LIB2=.*#LIB2="$HOME/.local/share/vapor-forge/x86_64/libvapor_forge.so"#' "$f"
done
md5sum ~/.local/bin/steam ~/.local/share/SLSsteam/path/steam   # 必须一致
```

---

## 10. Steam 客户端更新后的应对流程（最重要）

Steam 每次大更新都可能改内部函数，模块靠**签名（pattern）+ vtable 名字表**定位，签名失效时表现为"所有条目变购买"。

**标准处理顺序：**

1. **确认是更新导致的**
   ```bash
   sha256sum ~/.local/share/Steam/steamrt64/steamclient.so   # 和 §1 的指纹对比
   grep -c 'pattern match failed' ~/.local/share/Steam/logs/console-linux.txt
   ```

2. **查模块有没有新版本**
   ```bash
   bash ~/SteamUnlockOS/scripts/vf.sh check-update
   ```

3. **有新版本 → 升级 → 整机重启** → 复查 §8.1

4. **已是最新版本仍失效**：说明上游还没适配，只能等。此时可以：
   - 用 `vf.sh rollback` 回到不注入（至少 Steam 本身可用）；
   - 到上游仓库看 Issues/Release 是否已提适配；
   - 把下面这组信息贴给 AI 或上游：
     ```
     客户端 build：<ps 里的 buildid 或 steamwebhelper 参数>
     steamclient.so sha256：<命令输出>
     模块版本：<vf.sh status 的已装版本>
     失败 hook 名：<grep 'pattern match failed' 的 hook=… 列表>
     ```

5. **不要做的事**：不要手动改 `.so`、不要从别处下载"修复版"、不要反复重启 gamescope 会话（会进黑屏循环，只能长按电源）。

---

## 11. 性能与资源现状（2026-11-25 实测）

| 项 | 数值 | 说明 |
|---|---|---|
| 模块空闲 CPU | ≈ 2.6%（单核） | 采样 10 s 的进程 CPU tick |
| Steam 进程常驻内存 | ≈ 550 MB | 与客户端本身相当，模块开销未单独计量 |
| 模块日志速率 | ≈ 0.3 行/秒（warn 级） | 之前 info 级是 ~70 行/秒、53 分钟 6.5 MB |
| 网页服务内存 | ≈ 160 MB | 常驻，systemd 托管 |
| 首搜耗时（新 AppID） | 20–30 s | appinfo 7 s + ManifestHub3 16 s + 可能的密钥表刷新 |
| 重复搜同一 AppID | < 1 s | 进程内缓存 10 分钟 |
| 崩溃转储 | 今晚 3 个 `assert_*.dmp` | 都在重启/安装窗口，未观察到功能影响 |

---

## 12. 已知限制与取舍

1. **社区密钥表基本不可用**：jsdelivr 连不上、ghproxy ~30 KB/s。因此"新游戏能不能搜到"主要取决于 sudama 那张表（242,325 条，实测收录了 10 月底发售的 depot）。刚发售几小时内的游戏很可能查不到——这是数据覆盖问题，不是故障。
2. **商店 API 不可达**：所以游戏名字有时来自第三方源，可能和官方译名不同；搜索必须输 AppID，不能按名字搜。
3. **首次搜索慢**（20–30 s）：两个第三方源响应慢，已用缓存缓解重复搜索。
4. **在线签名热更已关闭**：`patterns_url = ""`。Steam 更新后需要手动升级模块（§10）。
5. **旧的 SLSsteam 模块留在磁盘上但不加载**：占 ~9 MB，作为回滚备份保留。
6. **包装器的崩溃保护**：连续 3 次启动崩溃会自动不注入。调试期若"改了没效果"，先 `rm -f ~/.SLSsteam-crash-count`。
7. **不要远程重启 gamescope 会话**：会进黑屏重试循环。所有生效性改动都靠用户手动整机重启。

---

## 13. 交给 AI 的提示词模板

```
我在 SteamOS 掌机上有一套"条目注册"系统，由三部分组成：
（1）Vapor Forge 运行时模块（Rust，LD_AUDIT 注入 64 位 Steam 客户端）
（2）SteamUnlockOS 工具链（Python + 本地网页 127.0.0.1:8917，负责查数据/写 lua）
（3）一个加固包装器 ~/.local/bin/steam（设置 LD_AUDIT）

下面是这台机器的完整交接指南（包含启动链、文件路径、诊断树、回滚与升级步骤）。

现在的症状是：<在这里写症状，例如"库里全变成购买按钮">

请先按指南 §8 的诊断树给出排查命令（只读优先），
确认根因后再给修复方案；任何写操作前先说明备份方式。
不要建议重启 gamescope 会话，也不要用 LD_PRELOAD 之类的新方案替换现有注入方式。
```

---

## 14. 术语表

| 说法 | 实际指 |
|---|---|
| 条目注册 / 添加条目 | 让 Steam 认为当前账号拥有某 AppID（写 lua + 模块注入） |
| 密钥 | depot 解密用的 32 字节 AES key（64 位 hex） |
| 清单 / manifest | depot 的文件清单，含 gid 与文件哈希 |
| 请求码 | `GetManifestRequestCode`，拿到它才能从 CDN 下清单 |
| 钉固（pin） | 在 lua 里 `setManifestid(depot,gid)` 固定清单版本 |
| pkg0 | Steam 的"包 0"，所有自有条目都在它的 AppIdVec 里 |
| 闸门 | `CUser::BIsLicensedForDepot`，逐个 depot 判许可，未过则 depot 被丢弃 |

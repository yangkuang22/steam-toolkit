# Steam Toolkit

在 **SteamOS / Linux** 上管理 Steam depot 配置与 SLSsteam 运行时扩展的工具集。

聚合多个社区数据源，管理 depot 解密密钥、内容清单和 SLSsteam 配置。
配合 [SLSsteam](https://github.com/AceSLS/SLSsteam) 运行时扩展，所有改动都在用户目录内，可随时完整回滚。

---

## 一键安装

```bash
git clone https://github.com/yangkuang22/steam-toolkit.git ~/steam-toolkit
cd ~/steam-toolkit
bash install.sh
```

> `install.sh` 会：检查环境 → 拉代码 → 装启动器（含 4 道回退保护）→ 装桌面图标 → 跑健康检查

## 图形界面（推荐）

双击应用菜单里的 **「游戏库管理」**，或者：

```bash
~/steam-toolkit/start-webui.sh
```

输入 AppID → 查询数据源 → 写入配置 → 重启 Steam 生效。详见 [前端使用说明](docs/前端使用说明.md)。

## 命令行用法

```bash
cd ~/steam-toolkit
python3 -m suos.cli install-mh3 <appid>            # ★ 写入配置（推荐）
python3 -m suos.cli install-mh3 <appid> --dlc      # 包含全部 DLC depot
python3 -m suos.cli install-mh3 <appid> --dry-run  # 只预览数据，不写入
~/.local/share/SLSsteam/path/steam                 # 重启 Steam，然后库里点「安装」
```

> **所有配置命令（`install` / `install-mh3` / `prepare` / `install-server`）现在都走同一条实现**，
> 和图形界面完全一致。无论从哪个入口进来，拿到的都是同一套校验防护（见下方「技术架构」）。

---

## 技术架构

```
数据源                          处理                        配置目标
──────────────────────────     ────────────────────       ─────────────
ManifestHub3（62000+ 分支）  →  webui_core.install()     →  Steam 客户端配置
社区密钥库（28 万+ 条目）        ├─ 写 depot 解密密钥          ├─ config.vdf
Sudama 密钥库（24 万+ 条目）     │   → config.vdf             ├─ depotcache/
上游 appinfo API                ├─ 放清单文件                 └─ SLSsteam config
                                │   → depotcache/
                                ├─ 生成 Lua 配置（版本锁定）
                                │   → config/lua/
                                └─ 更新运行时配置
                                    → SLSsteam config.toml
```

**核心机制**：Steam 启动时从 `config.vdf` 预加载 depot 解密密钥。
SLSsteam 管理运行时配置（`config.toml`）并通过 Lua 脚本锁定清单版本。

**清单落盘**：Steam 操作前查本地 `depotcache/<depot>_<gid>.manifest`。
清单在本地 → 直接用；不在 → 向 Valve 请求 request code（可能被拒）。
因此配置时清单必须成功落盘——这是本工具最核心的保证（见下）。

---

## 已修复的关键问题

工具经过两轮完整体检（静态审计 + ROG Ally 真机 SSH 实证），已知问题全部修复：

### 2026-10-05 下载链路体检（真机验证）

| 问题 | 说明 | 状态 |
|---|---|---|
| 清单缺失静默失败 | 清单没下到也报"配置完成"，但实际操作报无连接/内容加密→崩溃 | ✅ 改为硬门槛：清单缺失即判失败、不写配置 |
| 清单下载零重试 | 一次网络抖动就导致永久失败 | ✅ 每源重试 3 次（指数退避）+ 最稳的源排首位 |
| 全零占位密钥损坏下载 | 给有内容的 depot 写全零密钥 → Steam 解出垃圾 → 校验失败 | ✅ 只给无内容的空 depot 写占位 |
| 密钥长度不校验 | 非 64 位密钥被 SLSsteam 静默丢弃，却让人以为写成功了 | ✅ 源头只收 64 hex |
| Lua 大小写不一致 | 小写 `setmanifestid` 让清单被更新检查/清理误删 | ✅ 全部统一大写 |

详见 [体检报告](docs/体检报告-2026-10-05.md)。

### 2026-10-03 全面审计

| 编号 | 问题 | 状态 |
|---|---|---|
| P1-1 | 游戏名缓存被污染后永不自愈 | ✅ 已修（失败不缓存 + 自愈） |
| P1-2 | 启动器日志堆到根目录 | ✅ 已修（统一到 logs/） |
| P1-3 | depotcache 堆积无用旧清单 | ✅ 已修（自动清理，省 157 MB） |
| P2-1 | 密钥写入静默失败 | ✅ 已修（写入后校验） |
| P2-2 | 关键步骤失败仍报成功 | ✅ 已修（致命/可选步骤分类） |
| P2-3 | slsconfig 读错配置文件 | ✅ 已修（读 config.toml） |
| P2-4 | 配置写入时 O(n²) 文件读写 | ✅ 已修（批量一次读写） |
| P3-1 | 测试覆盖不足 | ✅ 持续补充（现 86 个单元测试） |

详见 [BUG 清单](docs/BUG清单-待分析.md)。

### 2026-10-05 架构整理

- **三条各自残缺的配置路径合并为一条**。以前 `installer.py` / `installer2.py` / WebUI 三套独立实现，
  修了一条另两条还是旧的。现在全部统一到 `webui_core.install()`。
- 删除失效的 `config.yaml` 写入代码（SLSsteam 已改用 `config.toml`）。

---

## 完整技术文档

**[项目全貌与维护指南](docs/项目全貌与维护指南.md)** —— 完整文档，包含系统架构、核心技术原理、
环境部署、故障排查、维护升级指南。

| 文档 | 内容 |
|---|---|
| [体检报告](docs/体检报告-2026-10-05.md) | **最新**：下载链路根因分析 + 修复 + 架构整理 |
| [使用手册](docs/使用手册.md) | 怎么用（命令、原理、限制） |
| [恢复手册](docs/恢复手册.md) | 出问题怎么救（Steam 更新、回滚） |
| [前端使用说明](docs/前端使用说明.md) | 图形界面操作 |
| [构建说明](docs/构建说明.md) | 怎么重新编译 SLSsteam |
| [BUG 清单](docs/BUG清单-待分析.md) | 2026-10-03 审计记录（已全部修复） |

---

## 常用命令

```bash
# 健康检查（出问题先跑这个）
bash scripts/healthcheck.sh

# 检查某游戏有没有数据（不写入）
python3 -m suos.cli install-mh3 <appid> --dry-run

# 看可选的历史版本
python3 -m suos.cli list-branches <appid>

# 检查/应用游戏更新
python3 -m suos.cli update --all
python3 -m suos.cli update <appid> --apply

# 安全网：不带注入启动 Steam
~/.local/bin/steam-noinject
```

---

## 项目结构

```
steam-toolkit/
├── webui_core.py           ★ 核心配置引擎（search / install / uninstall / inventory）
├── webui/                  图形界面（HTTP 服务 + 前端）
├── suos/                   核心 Python 模块
│   ├── multisource.py      ★ 多数据源聚合（depot key + manifest + Lua config）
│   ├── manifesthub3.py     ManifestHub3 数据源
│   ├── manifests.py        清单下载（多 provider + 重试）
│   ├── sudama.py           Sudama 密钥库
│   ├── depotkeys.py        社区密钥库
│   ├── server_api.py       SteamUnlock 服务端接口
│   ├── updater.py          游戏更新检查
│   ├── depotcache_clean.py 旧清单自动清理
│   ├── slsconfig.py        SLSsteam config.toml 只读查询
│   ├── cli.py              命令行入口（入库命令统一调用 webui_core）
│   └── steam.py / vdf.py / rc4.py / appinfo*.py   底层
├── scripts/                安装 / 健康检查 / 启动器脚本
├── tests/                  单元测试（86 个）
├── docs/                   文档
└── backup/                 所有备份（每次配置写入前自动备份）
```

---

## 一个必知的点

**无密钥的空 depot 会阻塞下载**——某些游戏在 Valve 的 appinfo 里有几十字节的"空 depot"，
社区密钥库里没有它的密钥，Steam 一遇到就取消整个更新。

工具已自动处理：**只给这类真正没有内容的空 depot 写占位密钥**（有内容的 depot 绝不写，
否则会损坏下载）。无需手动操作。

---

## 状态

- ✅ 端到端验证通过（真机 ROG Ally / SteamOS）
- ✅ 数据链路核心故障已修复（清单缺失不再静默失败）
- ✅ 三条配置路径已合并为一条，统一校验覆盖所有入口
- ✅ 启动器已加固（4 道回退，Steam 一定开得起来）
- ✅ 系统文件未被修改（可完全回滚）
- ✅ 86 个单元测试全部通过
- ⚠️ 社区未收录的新游戏（通常是发售几个月内）暂时无数据
```

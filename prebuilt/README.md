# prebuilt —— 预编译的 SLSsteam-Plus 库

这两个文件是**必须**的注入库。没有它们，游戏不会出现在库里。

| 文件 | 大小 | 作用 |
|---|---|---|
| `SLSsteam.so` | 9.5 MB | 实际的 hook 逻辑（所有权构造、密钥提供、版本控制） |
| `library-inject.so` | 25 KB | 把 SLSsteam.so 注入到 32 位 Steam 客户端 |

**来源**：从 `Hintay/SLSsteam-Plus` 源码编译，主要补丁：
1. `Makefile` —— 链接标志（不用 pkg-config）
2. `res/patterns.toml` —— 特征码适配，部分标记 `optional`（防止 Steam 更新后 abort）
3. `src/feats/requestcode.cpp` —— 崩溃修复（jobid 去重 + depot==app 规避）
4. `src/sdk/*.hpp`、`src/hooks.cpp` —— 适配 Steam 1791918634
5. `src/memhlp.cpp` —— 特征码扫描越界修复（SteamOS 3.9.5 后随机段错误）

补丁内容见 `scripts/slsplus.patch`。

当前的 `SLSsteam.so`（sha256 `aba7572c…`）已在真机验证，第 5 项是直接改二进制的 2 个字节，
详情和原因见 `docs/稳定性与更新.md`。下次重新编译时，源码补丁会自动带上这项修复。

**什么时候需要重新编译**：
- Steam 大版本更新导致特征码失配 → 跑 `bash scripts/check-after-steam-update.sh --fix`
- 上游 SLSsteam-Plus 有重要更新 → `bash scripts/build-slsteam-plus.sh`

**编译环境**：32 位工具链在 `buildtools/`（162 MB，脚本会自动下载）。
SteamOS 默认不能编译 32 位代码，所以需要这个。

**注意**：这两个 .so 是针对特定 Steam 版本的特征码。
如果 Steam 更新后注入失效，用 `check-after-steam-update.sh` 体检。

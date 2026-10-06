# hiksemi-sub 移植到飞牛OS (fnOS) 方案

> 目标：把「小彩屏客户端」跑在飞牛OS 上，先做到**界面能显示**。  
> 前提：机器有同款 2.86 寸 DSI 小屏（R1 / 同平台机型）。  
> 版本：hiksemi-sub v1.1.12，Electron 30，Linux x86_64。

---

## 一、先看清障碍：三个层级，难度递增

| 层级     | 内容                      | 飞牛OS 是否具备     | 难度    |
| ------ | ----------------------- | ------------- | ----- |
| L1 运行时 | Electron 跑起来、窗口显示       | 需自装 X11 + 依赖库 | ★★    |
| L2 屏幕  | 小屏作为 DSI 输出被识别          | **取决于内核驱动**   | ★★★★  |
| L3 数据源 | 127.0.0.1:20443 REST 服务 | **完全没有**      | ★★★★★ |

**结论先说**：飞牛OS 上跑这个程序，**最大的坎不是 Electron，是它背后那套设备服务**。程序本身只是个「显示终端」，真正的数据（CPU 温度、硬盘、风扇、灯效）都来自海康固件里跑在本机 20443 端口的 REST 服务。刷了飞牛，那套服务就不存在了。

---

## 二、硬件环境核对

| 项    | 海康 R1（原厂）                                                  | 飞牛OS 侧                                 |
| ---- | ---------------------------------------------------------- | -------------------------------------- |
| CPU  | Intel N100（x86_64）                                         | ✅ 同硬件，架构一致                             |
| 屏幕   | 2.86" TFT 触摸屏，**960×376**，60Hz，300nit                      | 需内核识别为 DRM 输出                          |
| 屏幕标识 | 程序内称 `DSI1`                                                | 需 `xrandr` 可见 DSI-1                    |
| 亮度   | `/sys/class/backlight/intel_backlight/brightness`（0–96000） | 需该 sysfs 节点存在                          |
| 系统   | HIKSEMI OS（定制 Debian）                                      | Debian 12 bookworm，内核 6.6.38 / 6.12.18 |
| 桌面   | 有（小屏专用 kiosk）                                              | **默认无桌面**，需自装                          |

> 屏幕分辨率在程序里写成 `width:376 height:960`，而硬件标称 `960×376`——说明**小屏是旋转 90° 竖装的**，需要靠 `xrandr --rotate` 或面板参数处理。

---

## 三、L1：让 Electron 跑起来（最容易，先做这步）

### 3.1 系统准备

飞牛OS 默认无桌面，两个选择：

**方案 A：装轻量 X11（推荐，先验证用）**

```bash
sudo apt update
sudo apt install -y xinit x11-xserver-utils xserver-xorg-video-intel
sudo apt install -y --no-install-recommends openbox
# 允许 root/无 tty 用户连接 X
sudo dpkg-reconfigure x11-common   # 选 "Anybody"
```

**方案 B：装完整桌面（KDE/GNOME，重）**

```bash
sudo apt install -y kde-full xinit
# 注意：需配置显示管理器延迟启动，否则开机卡屏
```

### 3.2 Electron 依赖库

关键：**NAS 系统是最小化安装，Chromium 依赖基本都缺**。

```bash
sudo apt update
sudo apt install -y \
  libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
  libdrm2 libgbm1 libgtk-3-0 libasound2 libxss1 libxcomposite1 \
  libxdamage1 libxrandr2 libxkbcommon0 libpango-1.0-0 libcairo2 \
  libgdk-pixbuf-2.0-0 libgconf-2-4 libx11-xcb1 libxcb-dri3-0 \
  libxtst6 libfuse2 zlib1g
```

> `libfuse2` 是 AppImage 运行必需（FUSE 挂载）；当前解包目录方式运行可不需要。

**⚠️ 包名易错点（实测踩坑）：**

| 库文件名                     | Debian 包名            | 常见错误                         |
| ------------------------ | -------------------- | ---------------------------- |
| `libXss.so.1`            | **`libxss1`**（全小写）   | 敲成 `libXss1` 会「无法定位软件包」      |
| `libnspr4.so`            | `libnspr4`           | —                            |
| `libXtst.so.6`           | `libxtst6`           | —                            |
| `libgtk-3.so.0`          | `libgtk-3-0`         | Debian 13 起叫 `libgtk-3-0t64` |
| `libasound.so.2`         | `libasound2`         | Debian 13 起叫 `libasound2t64` |
| `libatk-bridge-2.0.so.0` | `libatk-bridge2.0-0` | Debian 13 起加 `t64`           |

> 规律：**库文件名大小写混合，包名几乎全小写**。装不上就用 `apt-file search libXss.so.1` 反查。

**最可靠的依赖检查方式：用 `ldd` 而不是 `ldconfig -p`**

```bash
cd /opt/hiksemi-sub
ldd ./hiksemi-sub | grep "not found"
```

> 不要用 `ldconfig -p | grep libxss` 这种子串匹配——`ldconfig` 输出大小写敏感，  
> 库里实际叫 `libXss.so.1`，用小写 `libxss` 查会**误报缺失**。  
> 一键检查：`ldd ./hiksemi-sub | grep "not found"`（**只认带 `not found` 的行**）。

**⚠️ `linux-vdso.so.1` 是假缺失，别去装**

`ldd` 输出里经常能看到 `linux-vdso.so.1 => (0x00007ffd...)`：

- 它是**内核内置的虚拟动态库**（vDSO），用于加速 `gettimeofday` 等系统调用
- **不在文件系统上**，是内核 mmap 到每个进程的
- 永远 `apt install` 不到，也不需要
- `ldd` 把它列出来是正常现象

**判据只有一个：只看带 `not found` 的行。** 例如：

```
linux-vdso.so.1 (0x00007ffd8c5f9000)          ← 正常，忽略
libXtst.so.6 => not found                      ← 真缺，要装
libXss.so.1 => not found                       ← 真缺，要装
```

这两个 X11 扩展库是 Chromium 的硬依赖，缺任一个都会 SIGTRAP：

| ldd 名字         | apt 包名     | 作用                     |
| -------------- | ---------- | ---------------------- |
| `libXtst.so.6` | `libxtst6` | X Test 扩展，合成输入事件       |
| `libXss.so.1`  | `libxss1`  | X Screen Saver 扩展，屏保控制 |

```bash
sudo apt update && sudo apt install -y libxtst6 libxss1
```

### 3.3 启动（⚠️ SSH 会话专用，别用 startx）

**重要：通过 SSH 登录时，`startx` 用不了。** 原因：  
`startx` 是 `xinit` 的前端，它会尝试在**当前 tty 前台**接管显卡。SSH 会话没有真实 tty、也不是控制台，所以会直接报 `unable to open display` 或 `xauth: file .Xauthority does not exist`。

**正确做法：用 `nohup` 后台起 Xorg，显式指定 vt。**

```bash
# ── 步骤 1：后台启动 X server（关键：指定 vt1，否则会和 getty 抢控制台）──
sudo nohup Xorg :0 vt1 -nolisten tcp > /var/log/xorg-manual.log 2>&1 &
# ── 步骤 2：等 3 秒，确认 X socket 起来了 ──
sleep 3
ls /tmp/.X11-unix/          # 期望看到 X0
# 若没有 X0 → 看日志：tail -40 /var/log/xorg-manual.log

# ── 步骤 3：让 root 能用 xhost（X server 由 root 启动时需处理授权）──
export DISPLAY=:0
export XAUTHORITY=/root/.Xauthority
sudo xhost +local:           # 允许本地任意用户连接

# ── 步骤 4：运行程序 ──
DISPLAY=:0 ./hiksemi-sub --no-sandbox
```

程序自带的 `.desktop` 里 `Exec=AppRun --no-sandbox %U`，说明**原厂就依赖禁用沙箱**（NAS 上无法做 user namespace 隔离）。

#### 一次性把 X 和程序都拉起来的 kiosk 写法

```bash
# 让 openbox + 程序在 X 启动后自动跑（适合无人值守）
sudo nohup startx /usr/bin/openbox-session -- :0 vt1 \
  > /var/log/xorg-kiosk.log 2>&1 &
```

注意 `startx` 后面跟的**是客户端命令**（`openbox-session`），`--` 之后才是 X 参数。这样 `startx` 就不是"纯启动 X"，而是"启动 X 并运行指定客户端"，不会因为 SSH 会话结束而退出（有 `nohup` 保护）。

### 3.4 常见报错对照

| 报错                                                | 原因                                       | 解决                                                                     |
| ------------------------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------- |
| `unable to open display ":0"`                     | **X server 没在跑**（不是 DISPLAY 写错）          | 先 `ls /tmp/.X11-unix/` 确认有 `X0`；没有就先起 Xorg                             |
| `xauth: file /root/.Xauthority does not exist`    | SSH 会话无 X 授权文件                           | `sudo Xorg :0 vt1` 启动后，X 会自动生成；或 `export XAUTHORITY=/root/.Xauthority` |
| `startx` 无反应 / 立即退出                               | SSH 会话不能用 startx                         | 改用 `nohup Xorg :0 vt1 &`                                               |
| **`Trace/breakpoint trap (core dumped)`**         | **Chromium 沙箱校验失败自杀（SIGTRAP=133）**，见下方专章 | 加 `--no-sandbox` 或用方案 2/3                                              |
| `error while loading shared libraries: libXXX.so` | 缺依赖库                                     | 按文件名 apt 搜索安装（`_` 换成 `-`）                                              |
| `Failed to connect to the bus`                    | dbus 未启动                                 | `sudo service dbus start`                                              |
| 黑屏无窗口                                             | 窗口尺寸 376×960 超出屏幕                        | 见 L2，需先解决分辨率                                                           |
| `Cannot open virtual console 1`                   | vt1 被占用或权限不足                             | 换 vt2：`Xorg :0 vt2`，或确认不在容器里                                           |

> 诊断脚本：
>
> - `ldd ./hiksemi-sub | grep "not found"` —— 依赖检查（只认带 `not found` 的行）
> - `tools/diag.sh` —— ★ 唯一诊断脚本（链路分段：X→进程→Vue挂载→中间件→数据→日志，  
>   自动解密 `/tmp/subLog`；支持 `--chain` / `--logs` / `--net` / `--quiet`）  
>   其中段 0「X 服务」覆盖 X 进程 / socket / DRM 输出 / 背光 / 触摸检查，  
>   段 5「程序日志」是排查 `Trace/breakpoint trap` 的关键入口

### 3.5 专章：`Trace/breakpoint trap` 怎么解

**这不是崩溃，是 Chromium 主动自杀。**

退出码 **133 = 128 + 5（SIGTRAP）**。Chromium 在启动时发现自己的安全沙箱不可用，为了不「无沙箱裸奔」，它就主动触发陷阱把自己杀掉。所以看到这个反而是好消息——**说明 X 已经通了**，只是沙箱没配好。

#### 三个必要条件（满足任一即可）

Chromium 沙箱需要下面**至少一个**成立：

| 条件                                | 检查                                                   | 满足方式     |
| --------------------------------- | ---------------------------------------------------- | -------- |
| ① `chrome-sandbox` 有 SUID 且属 root | `ls -l chrome-sandbox` 看是否 `-rwsr-xr-x root root`    | 方案 2     |
| ② 内核允许非特权 user namespace          | `cat /proc/sys/kernel/unprivileged_userns_clone` = 1 | 方案 3     |
| ③ 明确禁用沙箱                          | —                                                    | 方案 1 / 4 |

**NAS / 容器环境通常三个都不满足**，所以必挂。

> **✅ 实机验证结论（2026-10-04，飞牛OS）**：本机 `chrome-sandbox` 已是 `-rwsr-xr-x root root`、`/opt` 挂载也无 `nosuid`，但**普通用户跑 `--no-sandbox` 仍然 133**；改用 `sudo` 后**一次成功**（成功读到 mock 服务数据）。
>
> **真实原因不在 chrome-sandbox 权限**，而在 **X server 的授权链路**：X 是 `sudo Xorg` 起的，跑在 root 下，普通用户既读不到 X socket / `.Xauthority`，SUID 提权链路也不完整。Electron 起来第一步要连 X，连不上就在极早期 SIGTRAP。
>
> **`sudo` 一次同时解决 X 授权 + 沙箱提权两件事** —— 这才是 133 的真正解法。
>
> **可复现的最短启动方式**：
>
> ```bash
> cd /opt/hiksemi-sub
> sudo DISPLAY=:0 ./hiksemi-sub --no-sandbox
> ```
>
> 更干净的做法（让普通用户免 sudo，利于 systemd 自启）：
>
> ```bash
> DISPLAY=:0 xhost +local:$USER      # 授权当前用户（或 xhost + 全放开，仅内网）
> DISPLAY=:0 ./hiksemi-sub --no-sandbox
> ```
>
> ⚠️ 排查提示：**SIGTRAP 不等于「一定是沙箱问题」**。若 SUID 权限、nosuid、`--no-sandbox`、`ELECTRON_DISABLE_SANDBOX` 全部满足仍报 133，优先怀疑 ① X 授权（本案例）② GPU 初始化（加 `--disable-gpu`）③ `/dev/shm` 过小（加 `--disable-dev-shm-usage`）④ 主程序之外的 `.so`/`.node` 缺依赖（`ldd` 主程序查不出）。

#### 修复方案

```bash
# ── 方案 1：直接禁用沙箱（最快，先用它验证能否启动）──
DISPLAY=:0 ./hiksemi-sub --no-sandbox
# 注意：本程序自带的 .desktop 里就是 Exec=AppRun --no-sandbox，
#       说明原厂本就是禁沙箱运行的，所以这不算"降级"，是回归原状。

# ── 方案 2：修 chrome-sandbox 权限（保留沙箱，最规范）──
cd /opt/hiksemi-sub           # 你的实际目录
sudo chown root:root chrome-sandbox
sudo chmod 4755 chrome-sandbox
ls -l chrome-sandbox          # 期望 -rwsr-xr-x root root

# ── 方案 3：开启内核 user namespace ──
sudo sysctl kernel.unprivileged_userns_clone=1
# 永久：
echo 'kernel.unprivileged_userns_clone=1' | sudo tee /etc/sysctl.d/00-local-userns.conf
sudo sysctl -p /etc/sysctl.d/00-local-userns.conf

# ── 方案 4：全局禁用（对机器上所有 Electron 程序生效）──
echo 'export ELECTRON_DISABLE_SANDBOX=1' >> ~/.bashrc
export ELECTRON_DISABLE_SANDBOX=1

# ── 方案 5：若报错与 GPU 有关（无显卡/驱动缺失）──
DISPLAY=:0 ./hiksemi-sub --no-sandbox --disable-gpu

# ── 方案 6：dbus 未启动（报 "Failed to connect to the bus"）──
sudo service dbus start
```

#### 为什么 NAS 上必踩

- **飞牛OS 是容器化架构**：若你的 shell 在容器里，`/proc/1/cgroup` 会显示 docker/lxc，user namespace 被禁
- **AppImage 挂载重置权限**：AppImage 每次挂到 `/tmp/.mount_xxx/`，SUID 位不保留，所以方案 2 对 AppImage 无效（对当前解包目录有效）
- **最小化安装无 dbus**：报错可能混合出现


#### 一条命令排查清单

```bash
# 1) 沙箱权限对不对
ls -l chrome-sandbox
# 2) 内核 userns 开没开
cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null
# 3) 是否在容器里
cat /proc/1/cgroup | head -3
# 4) 直接看真实报错（关键：加 --no-sandbox 对比）
DISPLAY=:0 ./hiksemi-sub --no-sandbox 2>&1 | head -30
```

> 一键诊断：`tools/diag.sh` 会检查沙箱权限、userns、容器环境、依赖库，  
> 并提供 `--logs`（解密程序日志，SIGTRAP 的真实原因常在这里）与 `--net`（数据链路）分段视角。
>
> SIGTRAP 专项排查方向（按优先级）：① X 授权链路（本案例）  
> ② GPU 初始化（加 `--disable-gpu`）③ `/dev/shm` 过小（加 `--disable-dev-shm-usage`）  
> ④ 主程序之外的 `.so`/`.node` 缺依赖（`ldd` 主程序查不出，需逐个 `ldd`）。

---

## 四、L2：小屏点亮（最大不确定性）

程序主进程逻辑（逆向所得）：

```js
xrandr | grep '*+' | awk '{print $1}'        // 探测所有活动输出
→ 若输出 > 1 个（有 HDMI），调 xrandr 把 DSI1 放 HDMI1 右边
→ 窗口 setPosition(大屏宽度, 0)，即贴到右侧
→ 若无 HDMI，窗口放 (0,0)
```

**依赖两个前提**：

1. `xrandr` 能看到 `DSI1` 这个输出（内核 DRM 驱动已绑定小屏）
2. 无 HDMI 时窗口定位到 (0,0)，尺寸正好铺满小屏

### 可行性判断

| 情况                             | 结果                               |
| ------------------------------ | -------------------------------- |
| 内核自带 `panel-simple`/DSI 驱动识别到屏 | ✅ 大概率可用，`xrandr` 能看到输出           |
| 屏幕走的是私有 MIPI-DSI 时序 / 需专用驱动    | ❌ 飞牛内核无对应驱动，屏幕不亮，需自编译 DT overlay |
| 屏幕通过 USB 转接                    | ⚠️ 需另装驱动，且不会是 DSI1 标识            |

### 排查步骤

> ⚠️ **`card0` 只是举例，不要死认编号。** `cardN` 是内核枚举顺序，与功能无关。  
> 多显卡/NAS 上常见 `card0` 是虚拟输出（HDMI/eDP），带 DSI 的可能是 `card1`。  
> **始终用通配符 `*-DSI-*` 判断，不看 card 编号。**

```bash
# 1. 列出所有 DRM 设备，看清楚每个 card 是什么
ls -l /sys/class/drm/
#   → 关注两种条目：
#       card0 / card1              裸卡（无连接器后缀）
#       card0-HDMI-A-1 / card1-DSI-1 ...  带连接器的输出

# 2. 通配符找 DSI（关键判据，不关心 cardN）
for d in /sys/class/drm/*-DSI-*; do
    [ -e "$d" ] || continue
    echo "== $d"
    echo "   status  = $(cat $d/status)"    # 期望 connected
    echo "   enabled = $(cat $d/enabled)"   # 期望 enabled
    echo "   modes   = $(cat $d/modes)"     # 期望有 960x376
done
# 没有任何输出 → 内核没识别到小屏

# 3. 辅助：看驱动绑定情况
for c in /sys/class/drm/card[0-9]; do
    echo "== $c  →  $(readlink -f $c/device/driver 2>/dev/null)"
done
# 期望看到 i915（Intel 核显）驱动
# 若某个 card 驱动为空或指向 vkms/virtual → 那是虚拟显示，不是真屏

# 4. 看背光节点
ls /sys/class/backlight/      # 期望有 intel_backlight

# 5. 看 xrandr（需 X 已启动）
DISPLAY=:0 xrandr --query     # 期望列出 DSI-1
xrandr --query --verbose | grep -A2 DSI    # 看它绑在哪个 card

# 6. 触摸设备
ls /dev/input/ ; libinput list-devices
```

### 如何解读结果

| 观察到的现象                                | 含义                            |
| ------------------------------------- | ----------------------------- |
| 有 `cardN-DSI-1`，status=connected      | ✅ 内核认屏，L2 可通过，继续调 xrandr      |
| 有 `cardN-DSI-*` 但 status=disconnected | ⚠️ 驱动在但没检测到屏，可能时序/供电问题        |
| 完全无 `*-DSI-*`                         | ❌ 内核无该屏驱动，L2 卡住，需移植 DT/内核补丁   |
| 有多个 card，其中 card0 无驱动或 virtual        | 正常现象，`card0` 是虚拟输出，真屏在别的 card |

如果 `/sys/class/drm/` 里**通配符 `*-DSI-*` 一个都匹配不到**，说明内核没驱动这块屏，L2 就卡住了——需要移植原厂的内核补丁/设备树，这是**极高难度**工作。

---

## 五、L3：数据源缺失（决定能不能「有内容」）

程序显示的所有数据都来自本地 REST：

```
https://127.0.0.1:20443/rest/1.x/...?access_token=md5(...)&action=...&cmdfrom=devsub
```

刷成飞牛OS 后，**20443 服务不存在**。表现：

- 首页时钟/农历**能显示**（本地 dayjs 计算，不依赖设备）
- 主题背景动画**能显示**（本地 PAG 文件）
- 设备页 / 硬盘页 → 一直转圈或显示「未接入」
- 控制中心所有操作 → 全部失败

### 三个应对方向

**方向 1：Mock 服务（早期验证 UI 用，现已被 middleware 取代）**

自己写一个假服务，监听 20443，返回固定数据。

> ⚠️ **本节为演进过程中的历史方案**：mock 只能返回固定假数据。  
> 现已由 `tools/middleware/middleware.py`（读真实 /proc、hwmon、sysfs）取代，  
> 该 mock 脚本已从仓库移除，下述代码仅作原理参考。

```python
# mock_server.py —— 最小可用
import json, ssl, hashlib, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

SAFE = {
    "get_sys_info":  {"code":0,"product_model":"HS-AFS-R1","mac":"00:11:22:33:44:55","is_active":1},
    "get_admin_accountid": {"code":0,"accountid":"1"},
    "get_eth_info_v2": {"code":0,"eth":[{"port":1,"status":"up","speed":"2500M"}]},
    "get_network_mode": {"code":0,"mode":1},
    "get_off_time": {"code":0,"xp_time":5},
    "get_atmo_light": {"code":0,"light_mode":1000},
    "get_light_theme": {"code":0,"mode":1},
    "space_manage": {"code":0,"diskList":[
        {"port":1,"disk_state":"active","smart_status":"normal","disk_cap":4000,"disk_form":"HDD"}]},
    "get_space_list": {"code":0,"space_list":[{"space_index":1,"space_path":"/vol1","space_state":"clean"}]},
    "get_update_version_desc": {"code":-2099,"msg":"no new version"},
    "opt_screen_theme": {"code":0},
    "set_power": {"code":0}, "set_hardware":{"code":0}, "set_brightness":{"code":0},
    "set_off_time": {"code":0}, "set_atmo_light":{"code":0}, "set_atmo_color":{"code":0},
    "set_light_theme": {"code":0}, "set_update":{"code":0}, "set_ezviz_server":{"code":0},
}

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        act = (q.get("action") or [""])[0]
        body = SAFE.get(act, {"code": 0})
        # 长连接动作返回一帧
        if act in ("get_hw_resource_info",):
            body = {"code":0,"cpu":"12%","memory":"34%","upload":"0KB/s",
                    "download":"0KB/s","Fanmode":1,"cpuTemp":45,"devTemp":38}
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_POST(self): self.do_GET()

ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain("cert.pem","key.pem")     # 自签即可，客户端不校验证书
HTTPserver = HTTPServer(("127.0.0.1",20443), H)
HTTPserver.socket = ctx.wrap_socket(HTTPserver.socket, server_side=True)
print("mock on https://127.0.0.1:20443")
HTTPserver.serve_forever()
```

生成自签证书：

```bash
openssl req -x509 -newkey rsa:2048 -nodes -keyout key.pem -out cert.pem -days 3650 -subj "/CN=127.0.0.1"
```

> 关键：客户端是 `rejectUnauthorized: false`，**任何自签证书都能过**。

**方向 2：接真机数据（进阶）**

把飞牛OS 的 `procfs`/sysfs 映射成接口：`/proc/stat` → CPU、`/sys/class/hwmon/` → 温度、`/sys/class/backlight/` → 亮度、`smartctl`/`lsblk` → 硬盘。工作量中等，但值回票价。

**方向 3：接海康云（绕过本地服务）**

程序里有云网关逻辑（`api.hiksemi.cn`）。可以改成走云，但云接口有设备绑定/MAC 校验，且只提供固件升级、主题下载等有限能力，**拿不到本地硬件数据**。不建议。

---

## 六、推荐实施路线

```
阶段 0  备份原系统（dd 整个 eMMC），确认能回滚
          ↓
阶段 1  装 X11 + Electron 依赖，跑起程序
         （⚠️ 关键：用 sudo 起，解决 X 授权 + 沙箱双重问题）
         验证点：Electron 能跑、窗口能出
         ✅ 已完成（2026-10-04）：sudo DISPLAY=:0 ./hiksemi-sub --no-sandbox 成功启动
          ↓
阶段 2  跑 mock_server.py，重启程序
         目标：首页时钟 + 主题粒子动画正常，页面能滑动
         验证点：UI 完整可用（除硬件数据为假）
         ✅ 已部分完成：程序已能读取 mock 服务数据
          ↓
阶段 3  确认 /sys/class/drm/ 里有没有 DSI-1
         有 → xrandr 调旋转/位置，让窗口铺满小屏
         无 → 停在此处，需内核级移植（另立项）
          ↓
阶段 4  mock 替换为真实数据采集（/proc、hwmon、smartctl）
         目标：界面显示真实 CPU/温度/硬盘
          ↓
阶段 5  触摸校准 + 开机自启（systemd service）
```

**阶段 1 和 2 是「能跑起来看界面」，投入约 1–2 小时，成功率高。**  
**阶段 3 是分水岭**，取决于内核是否认屏。

### 已实机验证的启动命令（阶段 1）

```bash
# ① 起 X（SSH 会话专用）
sudo nohup Xorg :0 vt1 -nolisten tcp > /var/log/xorg-manual.log 2>&1 &

# ② 起程序（sudo 是必须的，见 3.5 节）
cd /opt/hiksemi-sub
sudo DISPLAY=:0 ./hiksemi-sub --no-sandbox

# ③ 起 mock 数据服务（另开终端）
# （历史方案，脚本已移除；现用 middleware.py，见部署指南）
```

> 环境备忘：程序解包在 `/opt/hiksemi-sub`（**不要放在 `/vol1` 等 zfs 存储池**，虽实测 `/vol1` 非 nosuid，但 `/opt` 更稳妥）。

---

## 七、开机自启（阶段 5 用）

**正确做法：用 `xinit` 把「X + 窗口管理器 + 程序」当一个整体拉起**，而不是先起 X 再注入程序。这样 systemd 只管一个进程，生命周期一致，也避免 SSH 那套授权问题。

```ini
# /etc/systemd/system/hiksemi-sub.service
[Unit]
Description=HIKSEMI Sub Kiosk Display
After=network.target systemd-user-sessions.service

[Service]
Type=simple
User=root
# 关键：让 xinit 在 vt1 上起 X(:0)，并执行我们的 kiosk 脚本
ExecStart=/usr/bin/xinit /opt/hiksemi-sub/kiosk.sh -- :0 vt1 -nolisten tcp
# 崩溃后重启
Restart=always
RestartSec=5
# 不占用控制台登录，避免和 getty 抢 tty
StandardInput=tty-fail
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes

[Install]
WantedBy=multi-user.target
```

配套的 `/opt/hiksemi-sub/kiosk.sh`：

```bash
#!/bin/bash
# 由 xinit 在 X 起来后执行，此时 DISPLAY 已就绪
export DISPLAY=:0
# 清屏、隐藏光标
xsetroot -solid black 2>/dev/null
unclutter -idle 0 -root 2>/dev/null &
# 拉起窗口管理器（如装了 openbox）
openbox-session &
sleep 1
# 小彩屏程序本体
exec /opt/hiksemi-sub/hiksemi-sub --no-sandbox
```

加载：

```bash
sudo chmod +x /opt/hiksemi-sub/kiosk.sh
sudo systemctl daemon-reload
sudo systemctl enable --now hiksemi-sub.service
sudo systemctl status hiksemi-sub.service
```

> **不要**在 systemd 里用 `ExecStartPre=/usr/bin/startx ...`：`startx` 是阻塞式前台程序，会一直卡住 `ExecStartPre`，导致服务永远起不来。

---

## 八、替代方案（如果只是想「用起来」）

如果目标不是「研究移植」而是「让这块屏有用」，还有两条更省事的路：

| 方案     | 做法                                | 优劣                  |
| ------ | --------------------------------- | ------------------- |
| 保留海康系统 | 不动固件，飞牛装在别处                       | 屏幕原生可用，但 NAS 系统不是飞牛 |
| 自写小屏程序 | 飞牛OS 上写个简单 PyQt/GTK 程序，读 /proc 显示 | 几百行代码，完全可控，**推荐**   |
| 双系统引导  | eMMC 装海康，SSD 装飞牛，切换启动             | 屏幕归海康，飞牛归数据         |

> **实务建议**：如果只是想要「飞牛OS + 有个小屏显示状态」，**自己写一个**比移植这个 Electron 程序简单得多。原程序的价值在于主题动画和 UI，但那套 UI 深度绑定 20443 服务，移植成本远高于重写。

---

## 九、风险提示

1. **刷机风险**：飞牛OS 会覆盖 eMMC，原厂 HIKSEMI OS 和 20443 服务一起丢失，**务必先完整备份 eMMC**。
2. **内核驱动未知**：小屏能否点亮取决于面板型号和内核支持，**这是整个方案最大的不确定性**，建议先做阶段 3 的探针再投入。
3. **触摸屏**：即使显示成功，触摸也需要 evdev 驱动 + X 输入映射，可能需手动校准。
4. **无官方支持**：本方案基于静态逆向和公开资料推断，未经该机型实机验证。
5. **亮度过低/无法调**：若 `intel_backlight` 节点不存在（走 PWM 私有通道），亮度不可控。

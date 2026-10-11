# hiksemi-sub 飞牛OS 部署指南（纯操作版）

> 从零把「小彩屏 + 服务中间件」部署到飞牛OS。只有操作步骤，不含调试过程。
>
> 前提：机器带 2.86" DSI 触摸小屏（海康 R1 同款硬件），系统为飞牛OS（Debian 12 派生）。

## ★ 关于「程序目录」——本指南不写死路径

下文统一用 **`$APP`** 代表你的程序目录。**每条命令里的 `$APP` 都要换成你自己机器上的真实路径**，不要照抄。

```bash
# 先按你机器上的实际情况设定一次，后续所有命令直接复用（同一终端会话内有效）
APP=/usr/local/hiksemi-sub        # ← 改成你自己的路径
# 其它常见选择：/opt/hiksemi-sub  /srv/hiksemi-sub  /vol1/1001/hiksemi-sub
```

> **不要**把程序放在 zfs 存储池（`/vol1` 等）之外仍有 nosuid 的挂载点上；
>
> 判断方法：`findmnt -no OPTIONS --target "$APP"` 输出里**不能**有 `nosuid`。
>
> 所有**脚本**（`kiosk.sh` / `install-autostart.sh`）都会**自动探测**程序目录
> （含常见位置搜索，判据=目录内有可执行 `hiksemi-sub`），通常无需改任何脚本内容；
>
> 自动探测不中时用 `--dir <路径>` 显式指定（见 **§2.1**）。只有本文档里的命令需要你替换 `$APP`。

### 1.0 目录布局：程序目录 vs 工具目录

程序本体若来自 **`HistorSub-Linux-*.AppImage`**，解包/挂载目录是**易失**的
（每次运行路径可能变化），因此本项目把「工具」与「程序」解耦：

| 角色 | 默认位置 | 说明 |
| --- | --- | --- |
| **程序目录** `APP_DIR` | AppImage 解包目录 | 含 `hiksemi-sub` 二进制、`resources/`、`chrome-sandbox` |
| **工具目录** `TOOLS_DIR` | ★ **`/screen/tools`** | 持久目录，含 `kiosk.sh`、`middleware/`、诊断脚本 |

> `/screen` 目录已被程序占用（`/screen/lang.config` + `/screen/themes/`），
> 工具放在其子目录 `/screen/tools/` 下，**三者共存互不冲突**。

`TOOLS_DIR` 解析优先级（安装器与 `kiosk.sh` 一致）：

```
① --tools-dir <路径>        安装器命令行显式指定
② $HIKSEMI_TOOLS            环境变量
③ $APP_DIR/tools            程序自带（向后兼容）
④ /screen/tools             默认（推荐）
```

安装器会把 `TOOLS_DIR` 通过 `Environment=HIKSEMI_TOOLS=...` 写进 systemd 服务，
由 `kiosk.sh` 读取，从而**路径唯一确定**，不依赖运行时探测。

---

## 1. 安装系统包

SSH 登录飞牛OS，执行（一条命令装齐）：

```bash
sudo apt update
sudo apt install -y xinit x11-xserver-utils xserver-xorg \
    xserver-xorg-video-intel xserver-xorg-input-libinput \
    libxtst6 libxss1 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libgbm1 libgtk-3-0 libasound2 \
    libxcomposite1 libxdamage1 libxrandr2 libxkbcommon0 \
    libpango-1.0-0 libcairo2 openssl
sudo apt install -y --no-install-recommends openbox
```

---

## 2. 放置程序

把解包后的 hiksemi-sub 目录整体传到 NAS（scp / samba / U 盘均可）：

```bash
sudo mv /vol1/1001/hiksemi-sub "$APP"        # 从当前位置挪过去（按需改源路径）
cd "$APP"
sudo chown root:root chrome-sandbox && sudo chmod 4755 chrome-sandbox
ls -l chrome-sandbox        # 必须看到 -rwsr-xr-x
file hiksemi-sub            # 必须看到 ELF 64-bit
```

> 整目录搬迁必须保留 `resources/`、`locales/`、`*.pak`、`libffmpeg.so` 等全部文件。

### 2.1 安装器如何定位程序目录（★ 报 `主程序不存在` 时看这里）

`install-autostart.sh` 按以下顺序确定 `APP_DIR`：

1. **`--dir <路径>` 显式指定**（最可靠，推荐）
2. **自动搜索常见位置**：`/usr/local`、`/opt`、`/srv`、`/screen`、`/root`（含一级子目录）
   以及飞牛OS 存储池 `/vol*`（含两级）—— **判据 = 目录内存在可执行文件 `hiksemi-sub`**
3. 脚本所在 `tools/` 的上一级（**旧布局**：工具放在程序目录内时才成立）

> ★ 本项目推荐「程序与工具分离」（程序在 `/usr/local/hiksemi-sub`、工具在 `/screen/tools`），
> 此时第 3 条**必然不命中**，靠第 2 条自动搜索或第 1 条显式指定。
> 自动搜索**刻意不含** AppImage 挂载点 `/tmp/.mount_*` —— 那是易失目录，装自启无意义。

> ★ **在工具目录内就地执行是允许的**（`cd /screen/tools && sudo bash install-autostart.sh`）。
> 此时「脚本所在目录」== 「工具目录」，安装器**跳过自我复制**，不会再出现
> `cp: '/screen/tools/kiosk.sh' and '/screen/tools/kiosk.sh' are the same file`。
> 中间件 / `coolercontrol/` 同理（源与目标同一目录时直接跳过拷贝）。

**报 `[XX] 主程序不存在：<路径>` / `[XX] 程序目录不存在：<路径>` 时**，按序执行：

```bash
# ① 只预演：看安装器探测到了什么（不落盘、不改系统）
sudo bash /screen/tools/install-autostart.sh --dry-run
# ② 全盘定位主程序（AppImage 解包目录里那个 ELF）
sudo find / -xdev -name hiksemi-sub -type f 2>/dev/null
# ③ 程序还是 .AppImage 没解包？先解到持久目录（勿用 /tmp 挂载点，重启即失效）
chmod +x HistorSub-Linux-*.AppImage
./HistorSub-Linux-*.AppImage --appimage-extract      # 得到 squashfs-root/
sudo mv squashfs-root /usr/local/hiksemi-sub
# ④ 用 --dir 明确指定后重跑
sudo bash /screen/tools/install-autostart.sh --dir /usr/local/hiksemi-sub
```

---

## 3. 放置服务中间件

**标准位置：`$TOOLS/middleware/`，即 `$TOOLS`（默认 `/screen/tools`）下的 `middleware/` 子目录。**

> 这一点与早期文档不同：中间件**跟随工具目录**，不再固定在程序根目录。
> 原因见 §1.0（AppImage 路径易失）。若你的部署仍在 `$APP/middleware/`，
> 脚本兼容，**无需搬动**。

中间件随仓库可能出现在多个位置之一，先确认：

```bash
ls "$TOOLS/middleware/" 2>/dev/null && echo "[OK] 已在标准位置" \
  || { ls "$APP/tools/middleware/" 2>/dev/null && echo "[!!] 还在程序 tools 下，安装时会自动拷贝到 $TOOLS"; }
```

> ★ **无需手动搬**：`install-autostart.sh` 会按 `TOOLS_DIR` 定点同步中间件
> （**内容比对同步**：不同才覆盖，覆盖前备份 `*.bak-install-*`；
> 用户可能手调过的 `data-mapping.json` 默认不覆盖，源版本另存 `.new`）。
> 手动命令仅作为兜底：

```bash
sudo mkdir -p "$TOOLS" && sudo cp -rf "$APP/tools/middleware" "$TOOLS/"
```

> 脚本对三处都兼容（`kiosk.sh` 与安装器会按 `HIKSEMI_TOOLS` → `$APP/tools` → `/screen/tools` 顺序探测），
> 但**标准位置是 `$TOOLS/middleware/`**，docs/ 的路径引用均以此为准。
> 安装器还会打印**中间件版本号**、并留存 `middleware.py.orig` 便于回滚。

确认文件齐全：

```bash
ls "$TOOLS/middleware/"
# 应看到: middleware.py  data-mapping.json
```

生成 HTTPS 自签证书（客户端不校验证书，任意证书均可）：

```bash
cd "$TOOLS/middleware"
sudo openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout key.pem -out cert.pem -days 3650 -subj "/CN=127.0.0.1"
```

按需修改数据映射配置（默认已可用，可跳过）：

```bash
sudo vi "$TOOLS/middleware/data-mapping.json"
```

常用修改点：

| 想改什么         | 改哪里                                                                |                      |
| ------------ | ------------------------------------------------------------------ | -------------------- |
| 主网卡名（MAC 取值） | `get_sys_info` → `mac`（**默认自动探测，一般不用改**；要固定就加 `"iface": "enp2s0"`） |                      |
| CPU 温度来源     | `get_hw_resource_info` → `cpu_temp` → `chip`（**列表**，按顺序优先：默认 `coretemp`→`x86_pkg_temp`→`k10temp`→`zenpower`→`auto` 兜底）。★ 不要写成单个不存在的字面名 |
| 机箱/设备温度     | `temperature` → `chip`（默认 `i915`/核显优先，其次 `it8613`/`nct*`/`acpitz`）+ `exclude` 排除 CPU 芯片名，防止两个温度取到同一个传感器 |
| 盘位数/匹配规则     | `space_manage` → `pattern`（默认 \`sd[a-z]                            | nvme[0-9]+n[0-9]+\`） |
| 存储空间挂载点      | `get_space_list` → `mounts`（留空自动探测 `/vol*`）                        |                      |
| 排除的网卡        | `get_eth_info_v2` → `exclude`                                      |                      |
| 长连接推送间隔      | 顶层 `stream` → `interval`（默认 2s）                                    |                      |

> **★ 网卡名不用手改（自动探测）**：飞牛OS 上**没有 `eth0`**，物理网卡通常是 `enp2s0` / `enp3s0`
>
> （开启 OVS 时会带 `-ovs` 后缀，如 `enp2s0-ovs`，关闭 OVS 后后缀消失）。
>
> 中间件 `mac` 字段用 `source: "mac"` **自动挑选主网卡**，优先级：
>
> ① 有默认路由的网卡 → ② 首个 `up` 状态的物理网卡 → ③ 首个物理网卡；
>
> 带 `-ovs` 后缀的接口优先跳过（OVS 虚拟口）。因此换网卡名/开关 OVS 都无需改配置。
>
> 需要固定时：`{"source":"mac","iface":"enp2s0"}`。
>
> **改配置无需重启**：中间件按 mtime 自动热重载，保存即生效。
>
> ⚠️ **移动目录后必须重启服务**：`sudo systemctl restart hiksemi-sub`。移动目录不会影响运行中的旧进程（其配置路径已固定），重启后才会用新路径。

---

## 4. 安装开机自启

```bash
cd "$APP/tools"  # 或你解包后的仓库 tools/ 目录
sudo bash install-autostart.sh
```

脚本自动完成：权限检查 → SUID 修复 → 证书检查 → 安装 kiosk.sh → 注册 systemd 服务 → 启用开机自启。最后询问「立即启动」时选 `y`。

**默认行为**：`kiosk.sh` 与 `middleware/` 会安装到 **`/screen/tools/`**（见 §1.0）。

若指定其他程序目录：`sudo bash install-autostart.sh --dir /你的路径`

若指定其他**工具目录**：`sudo bash install-autostart.sh --tools-dir /你的工具路径`

> 安装器会在 `[5/6]` 打印实际生效的 `ExecStart=` / `Environment=` 行，
> 可据此确认 `HIKSEMI_TOOLS` 指向的是期望目录。

卸载：`sudo bash install-autostart.sh uninstall`

---

## 5. 验证

```bash
# 1) 服务状态（应为 active (running)）
sudo systemctl status hiksemi-sub --no-pager

# 2) 普通接口（一次性 JSON，验证中间件在跑）
curl -sk "https://127.0.0.1:20443/rest/1.1/config?cmdfrom=devsub&action=get_sys_info"

# 3) 长连接接口（设备状态，multipart 分帧；curl 会持续输出不退出）
#    看到 --myboundary 分帧即正确
timeout 5 curl -skN "https://127.0.0.1:20443/rest/1.2/config?cmdfrom=devsub&action=get_hw_resource_info"

# 4) 小屏上应显示：界面正常铺满、首页 CPU/内存/网速为真实变化数据
```

> **⚠️ 关键：设备状态走的是长连接分帧，不是普通 JSON。**
>
> - 普通接口：`/rest/1.1/config?action=get_sys_info` 等 → 一次性 JSON；
> - **长连接**：`/rest/1.2/config?action=get_hw_resource_info`（设备状态）与 `/rest/1.0/event?action=get_event`（事件）→ **multipart `--myboundary` 分帧持续推送**；
> - 若长连接未实现分帧，程序收不到帧 → **curl 有数据但屏幕不更新**。中间件已实现（见 `data-mapping.json` 顶层 `stream` 配置）。

三个日志文件（统一目录 `/var/log/hiksemi-sub/`，出问题再看）：

```bash
sudo tail -20 /var/log/hiksemi-sub/kiosk.log        # kiosk 启动日志
sudo tail -20 /var/log/hiksemi-sub/middleware.log   # 中间件日志（应看到 STREAM ... 开始推送）
sudo tail -20 /var/log/hiksemi-sub/xorg.log         # X server 日志
sudo journalctl -u hiksemi-sub -n 50 --no-pager     # systemd 侧日志
```

> 日志目录不可写时（非 root 运行），kiosk.sh 自动回退到 `<程序目录>/logs/`。

#### 中间件日志级别（★ 控制日志量）

中间件默认只记录**启动信息 / 硬件探测结论 / 错误 / 写入操作**，
**只读轮询请求已合并计数**（首次打印 + 每 100 次摘要一行），
日志量比旧版**降低 90% 以上**（实测 108 分钟从 4800+ 行降到约 400 行）。

用环境变量 `HIKSEMI_LOG` 控制三档：

| 级别 | 内容 | 适用 |
| --- | --- | --- |
| `quiet` | 启动/错误/写入操作 | 长期稳定运行，日志最少 |
| `normal` | 上面 + 轮询首次与每 100 次摘要（**默认**） | 日常运行 |
| `debug` | 每个请求/响应完整流水 | **排障时临时开** |

改法（systemd 服务）：
```bash
sudo systemctl edit hiksemi-sub        # 或直接改 /etc/systemd/system/hiksemi-sub.service
# 把 Environment=HIKSEMI_LOG=normal 改成 debug
sudo systemctl daemon-reload && sudo systemctl restart hiksemi-sub
```

> 同类错误（如高频路径的 I2C 读取失败）也会**自动去重**：
> 首次完整报出，之后每 50 次汇总一行。

#### ★ 散热风扇外部应用通道（可选）

中间件已实现风扇 4 档的**加载 / 修改 / 保存**（状态文件 `/var/lib/hiksemi-sub/fan.json`）。
**默认不落地硬件**（飞牛OS 上真实调速通道待真机复查，见 `fan-control.md` §6）。
有三种落地方式，**按优先级**尝试（上层接口完全不动）：

| 优先级 | 环境变量 | 作用 | 示例 |
| --- | --- | --- | --- |
| ① | `HIKSEMI_FAN_APPLY_CMD` | 档位变化时执行外部命令（`{mode}`/`{name}` 占位） | `/usr/local/bin/pwm-set {mode}` |
| ② | `HIKSEMI_FAN_COOLERCONTROL` | 启用**内置 CoolerControl 适配器**，调用 `coolercontrol/apply-mode.sh {mode}` | `1`（可配 `HIKSEMI_FAN_COOLERCONTROL_SCRIPT` 覆盖脚本路径） |
| ③ | `HIKSEMI_FAN_PWM_PATH` | 直接写 sysfs PWM 节点（低速 64/标准 140/强劲 255；智能档不写） | `/sys/class/hwmon/hwmon3/pwm1` |

> **推荐方式 ②**：CoolerControl 是成熟的风扇控制守护进程，配置全部落在
> `/etc/coolercontrol/{config.toml,modes.json}`，界面 4 档正好对应它的 4 个 Mode。
> 完整的映射表、部署步骤、契约要点与真机验证清单见 **`coolercontrol.md`**；
> 配套文件在 **`tools/coolercontrol/`**（模板 + 渲染脚本 + 落地脚本 + 探测脚本）。

**★ 一键部署（安装器已接管，无需手工拷文件）**：

```bash
# 自动探测：机器上装了 CoolerControl 就启用，没装就跳过
sudo bash install-autostart.sh --tools-dir /screen/tools

# 强制启用 / 强制关闭
sudo bash install-autostart.sh --fan-coolercontrol on
sudo bash install-autostart.sh --fan-coolercontrol off

# 先看要做什么，不落盘不改系统
sudo bash install-autostart.sh --fan-coolercontrol on --dry-run
```

安装器会：把 `tools/coolercontrol/` 同步到 `$TOOLS/coolercontrol/`（**内容比对同步**，
不同才覆盖，旧的备份为 `*.bak-install-*`）→ 给脚本补可执行位 → 把
`Environment=HIKSEMI_FAN_COOLERCONTROL=1 HIKSEMI_FAN_COOLERCONTROL_SCRIPT=$TOOLS/coolercontrol/apply-mode.sh`
注入 service → 校验注入结果。切换回 `off` 时该行会被**自动清除**。

> 装机后先确认服务里**确实生效**（模板里带有被注释的示例行，会干扰 grep）：
> ```bash
> grep '^Environment=.*HIKSEMI_FAN_COOLERCONTROL' /etc/systemd/system/hiksemi-sub.service
> sudo grep -i 'fan' /var/log/hiksemi-sub/kiosk.log     # kiosk 会上报三种通道的生效情况
> ```

> ★ **模板未渲染 = 不生效**：`tools/coolercontrol/config.toml` 里含 `@DEVICE_UID@`
> 等占位符，必须先用真机 uid 渲染（`probe-coolercontrol.sh` → `render-config.py`）。
> 安装器检测到占位符仍在时会**明确警告**并打印后续命令。

改法（systemd 服务，与 `HIKSEMI_LOG` 同方式，手工场景）：
```bash
sudo systemctl edit hiksemi-sub
# [Service]
# Environment=HIKSEMI_FAN_COOLERCONTROL=1
# 或（方式③）
# Environment=HIKSEMI_FAN_PWM_PATH=/sys/class/hwmon/hwmon3/pwm1
sudo systemctl daemon-reload && sudo systemctl restart hiksemi-sub
```
启动日志的「功能就绪摘要」会显示
`风扇档位 : ✅ 可落地（CoolerControl / 外部命令 / sysfs PWM）` 或 `⚠ 仅保存/回显`。

### 5.1 ★ 程序自身日志（排查「小屏没数据」最关键）

程序内核 `@semi/nas-log` 会把**每一个 REST 请求与响应、绑定状态判断、长连接帧**写进文件日志。

**日志路径**：`{app.getPath("temp")}/subLog/`，在 Linux 上就是 **`/tmp/subLog/`**（渲染进程日志 = `HSLog_sub.log`；主进程日志同名，且另有 `/tmp/screenApp.log`）。

> ⚠ 常见误区：日志**不在** `/tmp/HSLOG_main_xxx/` 下——`HSLOG_main` 只是**文件名前缀**，目录是 `temp/subLog`。

> 🔐 **关键：日志是加密的！** 程序用 `@semi/nas-log` 写日志且 `isEncode = true`，每条消息体走
>
> **AES-128-ECB + base64**（key 硬编码 `[45/*YUIdse..e;]`，无 IV）。
>
> **直接 `grep` / `tail` 看到的是密文，搜任何中文关键字都搜不到**——这就是「日志里没有 openinfo」的原因。
>
> 必须先用 `tools/decode-hslog.py` 解密：

```bash
# 看日志目录（不存在 = 渲染进程没跑起来，本身就是关键线索）
sudo ls -lt /tmp/subLog/ 2>&1

# ★ 解密后阅读（纯标准库实现，目标机无需装任何库）
sudo python3 tools/decode-hslog.py /tmp/subLog/HSLog_sub.log | tail -100
# 只看某关键字
sudo python3 tools/decode-hslog.py /tmp/subLog/*.log --grep 设备状态

# 兜底全盘搜
sudo find /tmp /var/tmp /root /home -maxdepth 3 \
     \( -name "HSLog*" -o -name "screenApp.log" -o -type d -name subLog \) 2>/dev/null
```

解密后，重点看这几类关键字：

| 日志关键字                             | 含义                                                      |
| --------------------------------- | ------------------------------------------------------- |
| `获取设备是否绑定url` / `获取设备是否绑定结果`      | 绑定检测（`/rest/1.1/openinfo`）——**程序启动的第一道关卡**              |
| `[getDeviceStatus]设备状态-已绑定`       | 通过绑定检测，会进入主页                                            |
| `[getDeviceStatus]设备状态-获取异常`      | **失败 → 卡在「启动中」页**（表现：屏幕一直显示「启动中」）                       |
| `[llx-getDeviceStatus]设备状态：{...}` | `sessionStorage.deviceInfo` 全文（看 `model` 字段有没有值）        |
| `设备状态长链接Url` / `设备状态长链接结果`        | 设备状态长连接是否在收帧                                            |
| `渲染进程--当前语言配置文件中无内容`              | 走了 `/screen/lang.config` 缺失分支（会尝试 `curl ipinfo.io` 猜语言） |
| `获取设备不在使用中` / `返回设备未绑定错误码`        | 被判定为未绑定/停用                                              |

> **`/tmp/subLog/` 完全不存在**意味着：Vue 应用根本没有挂载完成（`App.vue` 的 setup 都没执行），
>
> 而应用挂载被 `initThemeConfigInNas().then(() => createApp(App)...mount())` 阻塞，
>
> 也可能是更早的 X 阶段就失败了。此时优先查 Xorg 日志与 X 是否在跑（见 §7）。

### 5.2 程序启动流程（理解卡点）

```
应用启动（Vue mount 完成）
  └─ 启动页 index-22438822.js
       n = async () => {
         if (await getDeviceStatus())                       ← 返回假就重试
           switch (DEVICE_BIND_STATUS.value) {
             case 0: push('/bind'); break;                  ← 绑定页
             case 1: push('/index'); break;                 ← 主页
             default: setTimeout(n, 2000); break;
           }
         else setTimeout(n, 2000);                          ← 每 2 秒重试
       }
       onMounted 后 1 秒开始循环调用

getDeviceStatus()  （index-be27a12a.js:20794）
  ├─ getDevIsActive() → GET /rest/1.1/openinfo?action=get_sys_info
  │    ├─ a.is_active === 1                → s.active = true → DEVICE_BIND_STATUS = 1
  │    ├─ a.product_model 为真时            → s.model = a.product_model（写进 sessionStorage.deviceInfo）
  │    └─ 返回 s（即 sessionStorage.deviceInfo）
  ├─ active 时 closeHeart() + startHeart()
  └─ return ((n?.model?.indexOf("R1-A")) < 0 && (fsp 读/写 /screen/lang.config 的 Promise 链))
       ├─ model 非空 且 不含 "R1-A"  → 走 fsp 链，返回 Promise（truthy）→ 进页面 ✅
       ├─ model 非空 且 含 "R1-A"    → && 左侧 false → 返回 false → ❌ 卡启动页
       └─ model 为 undefined/null    → 左侧 undefined<0 = false → 返回 false → ❌ 卡启动页
  ※ 任何一步抛异常 → catch → 写「[getDeviceStatus]设备状态-获取异常：xxx」→ return false（!1）
```

> **注意**：CPU/内存等设备状态**只在「设备状态」页（DEVICE）激活时才拉取**，首页（HOME）不显示这些值——
>
> 用手指在小屏上左右滑动切页，切到设备页才会有数据，这是**正常设计**，不是故障。
>
> 另外程序还会读写绝对路径 **`/screen/lang.config`** 与 **`/screen/themes/`**；
>
> 该目录缺失时可能导致语言初始化异常、主题加载失败。`install-autostart.sh` 与 `kiosk.sh` 都会自动创建 `/screen/`。

#### ★ 如何从「openinfo 被反复请求」反推故障

`openinfo` 是 `getDeviceStatus()` 发的**一次性请求**。正常路径下它只会发 **1~2 次**（启动时 + 可能与 `getDevStart` 配合）。

如果你在中间件日志里看到 openinfo **持续反复出现**（例如 5 次、10 次…间隔约 2 秒），说明：

> **`getDeviceStatus()` 一直返回 false → 启动页在每 2 秒重试 → 屏幕永远停在「启动中」。**

返回 false 只有三种可能，按此顺序查：

| # | 原因                                                               | 怎么确认                                              | 怎么修                                                      |
| - | ---------------------------------------------------------------- | ------------------------------------------------- | -------------------------------------------------------- |
| 1 | **`product_model` 缺失/为空** → `s.model` 未被赋值 → `model` 为 undefined | 看日志里 `sessionStorage.deviceInfo` 内容有没有 `model` 字段 | 中间件 `get_sys_info` 必须有非空 `product_model`（默认 `HS-AFS-R1`） |
| 2 | **`product_model` 含 `"R1-A"`**（海外版型号）                            | 看返回值是否形如 `R1-A-xxx`                               | 改成**不含 `R1-A`** 的型号，如 `HS-AFS-R1`                        |
| 3 | **`getDeviceStatus` 抛异常**                                        | 日志里有 `[getDeviceStatus]设备状态-获取异常：xxx`             | 按异常原文处理（常见：`/screen` 不可写、`semiLogWrite` 失败）              |

> ⚠️ 第 2 条是**反直觉**的：型号里带 `R1-A`（海外版）反而进不去页面。因为源码是 `indexOf("R1-A") < 0 && (…)`——
>
> 「不是海外版」才走正常分支。**中间件默认值 `HS-AFS-R1` 是对的**（不含 `R1-A`，判定通过）。

一键采集：`sudo bash tools/diag.sh`

> ★ **唯一诊断脚本**，按链路分段（X → 进程 → Vue挂载 → 中间件 → 数据 → 日志），  
> 每段自证通断，并自动调用 `tools/decode-hslog.py` 解密 `/tmp/subLog`  
> （程序日志是 AES-128-ECB 加密的，直接 tail 只会看到乱码）。  
> 另有 `--chain`（链路，默认）/`--i2c`（灯控）/`--screen`（息屏）/`--power`（电源）/
> `--logs`（只看日志）/`--net`（只验数据链路）/`--quiet`（只出结论）子命令。


## 7. 故障速查

| 现象                                | 处理                                                                                                                                                                                                                        |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 服务起不来                             | `sudo journalctl -u hiksemi-sub -n 50`；确认 `PAMName=login` 在 service 里                                                                                                                                                     |
| **重启后服务 failed，但手动 restart 就好** | ★ **真根因（2026-10-05，两轮修正后的最终结论）：logind 动态拉起 `autovt@tty1`（=getty@tty1）抢 tty1**。我们的 xinit 激活 VT1 恰好触发 logind 的「VT 无属主则 spawn getty」；getty 一起来，其启动事务就因 `Conflicts=` 反向停掉本服务（journal 里 `Stopping` 排在 `Main process exited` 之前是铁证；显式 stop 不触发 `Restart=always`）。**它不在开机事务里 ⇒ `After=` 无从等待，`mask` 才是主修**：`sudo systemctl mask getty@tty1`（安装器已自动做，卸载自动 unmask）。次要保留：`Conflicts+After` 惯用法 + kiosk.sh 的 `HIKSEMI_X_WAIT_SEC` X 等待。验证：`journalctl -b \| grep -iE "autovt\|getty"`，若 getty@tty1 的 Started 落在本服务 Started 与 Stopping 之间即实锤 |
| **kiosk.log 出现「程序本体前置检查未通过」** | `hiksemi-sub` 二进制缺失/不可执行（多为移动目录后 `HIKSEMI_DIR` 指错）。日志里有「修复指引」4 条；`tools/diag.sh` 段 5 会直接给结论 |
| **重启后飞牛 Web UI 的 CPU / 网络流量 / 存储读写【全部无数据】** | ★ 装了本服务后才出现。根因：fnOS 的 `resmon_service` / `sysinfo_service` 开机时要「**挂载设备接口**」（GPU/DRM 等），而我们的 `xinit` 同一时刻 open `/dev/dri/card0` 并抢 DRM master ⇒ 它们**静默降级**：服务仍是 `active`，但所有指标全空（官方口径即「资源监控未挂上设备接口」），**必须 restart 才恢复**。**已内置三重处理**：①`[Unit]` 写 `After=…resmon_service.service sysinfo_service.service`，显式排到它们**之后**启动；②起 X 前先跑 `kiosk.sh --pre-delay`，开机窗口内（uptime<300s）等 `HIKSEMI_BOOT_DELAY_SEC`（默认 8s）让它们先挂设备；③兜底：**启动程序前【同步】执行一次 `systemctl restart resmon_service sysinfo_service`**（= 手工命令的自动化；★ 必须是**同步**、且用 **`restart`** 而非 `try-restart` —— 旧版写成「后台 sleep 60s」会随主进程退出被 cgroup 杀掉，开机早期 kiosk 常被重拉 ⇒ 永远跑不到）。临时恢复：`sudo systemctl restart resmon_service sysinfo_service`；自检：`journalctl -b -u resmon_service \| grep -i "挂\|probe"`，并看 `/var/log/hiksemi-sub/kiosk.log` 的「♻ 已补挂」行 |
| 退出码 133 (SIGTRAP)                 | 确认 `chrome-sandbox` 为 `-rwsr-xr-x root root` 且目录无 `nosuid`                                                                                                                                                                |
| 界面黑屏但服务 active                    | `ls /tmp/.X11-unix/X0` 确认 X 在；`DISPLAY=:0 xrandr` 看输出                                                                                                                                                                     |
| 程序无数据                             | 先确认**用的哪个地址**：设备状态必须访问 `/rest/1.2/config`（长连接），普通 JSON 地址不推送帧；查 `/var/log/hiksemi-sub/middleware.log` 有无 `STREAM` 字样                                                                                                      |
| **小屏一直显示「启动中」**                   | 绑定检测没过。看程序日志 `/tmp/subLog/*.log` 里 `获取设备是否绑定结果` / `[getDeviceStatus]设备状态-获取异常`；确认 `/rest/1.1/openinfo?action=get_sys_info` 返回 `is_active: 1`                                                                              |
| **中间件日志里 openinfo 反复出现（每 2 秒一次）** | ★ **`getDeviceStatus()` 一直返回 false，启动页在重试**。三种原因按序查：①`product_model` 缺失→`model` 为 undefined；②`product_model` **含 `"R1-A"`**（海外版型号，反直觉地进不去）；③`getDeviceStatus` 抛异常（看日志 `设备状态-获取异常`）。跑 `sudo bash tools/diag.sh` 看段 4/5 |

| **`/tmp/subLog/` 目录不存在** | 说明 Vue 应用**根本没挂载完成**（App.vue 的 setup 未执行）。依次查：① X 是否在跑（`ls /tmp/.X11-unix/X0`、`xorg.log`）；② `/screen` 是否存在可写（主题初始化会阻塞 mount）；③ 程序进程是否还活着（`ps aux \| grep hiksemi-sub`）。跑 `tools/diag.sh` 看段 0 与段 5 的 app.log |

| **小屏显示绑定页（二维码）** | `/rest/1.1/openinfo` 返回的 `is_active` 不为 1 → 中间件 `get_sys_info` 里 `is_active` 必须为 `1` |

| **某页有数据、某页没有** | 各页数据按需拉取：CPU/内存/温度**只在设备状态页（DEVICE）激活时**才请求。左右滑屏切页验证 |

| **界面能显示但数值空白** | 按顺序查：① `/screen` 目录是否存在（`ls -ld /screen /screen/lang.config`）；② 程序日志里长连接是否收到帧；③ 中间件日志 `STREAM get_hw_resource_info 首帧` 内容是否合理 |

| **屏幕不更新（curl 却有数据）** | 两个原因按顺序查：①**字段名/类型不符**——设备状态帧必须是 `cpu`(数值)/`memory`(数值)/`upload`(数值 KB/s)/`download`(数值)/`fanmode`(小写)/`cpu_temp`/`temperature`/`disk_manage`，带 `%`、`KB/s` 的字符串会被客户端 `r.cpu >= 0` 判false丢弃整帧；②**长连接未走分帧**——确认 `stream.actions` 含 `get_hw_resource_info` |

| **中间件日志文件都没有** | 依次排查：`ls -l /var/log/hiksemi-sub/` 看目录权限；`ls "$APP/logs/"`（无 root 时的回退位置）；`cat /var/log/hiksemi-sub/kiosk.log` 看有没有 `✗` 行（脚本已把「找不到 python3 / 缺证书 / 启动即退出」都写进去）；`command -v python3` 确认 Python 存在 |

| **想直接确认日志能否生成** | 手动前台跑一次：`python3 "$TOOLS/middleware/middleware.py" --check` — 自检模式会打印完整环境（Python 路径/配置/证书/端口）后退出，不启动服务 |

| `curl` 连不上 20443 | 中间件没起：`sudo -u root python3 "$TOOLS/middleware/middleware.py"` 手动跑看报错 |

| 长连接 curl 卡住不输出 | 正常现象（流式持续推送）：用 `timeout 5 curl -skN ...` 限制时长 |

| 日志报 `配置文件不存在` / `热重载失败` | 配置文件被移动或目录改名 → 中间件进程仍在旧路径找配置。**重启服务即可**：`sudo systemctl restart hiksemi-sub` |

| 移动过目录后配置不生效 | 移动目录必须重启服务（旧进程 cwd 仍指向旧位置）。启动自检会在配置缺失时直接报 `[FATAL]` 退出 |

| 硬盘页无数据 | `space_manage` 返回必须是嵌套结构 `space_list[].disk_info[]`，且 `disk_cap` 为**字节数**（客户端自行换算） |

| 主题页存储空间列表为空 | `get_space_list` 的 `space_state` 只认 `clean`/`degraded`/`syncing`，返回 `normal` 会被过滤掉 |

| 某字段数值不对 | 改 `data-mapping.json` 对应字段的 `source`/`chip`/`path`，保存即生效 |

| tty1 无法登录 | 服务占用了 tty1：先 `sudo systemctl stop hiksemi-sub` |

| 温度恒定不变 / 一直显示 45、38 | ★ 先跑 `sudo bash probe.sh temp`（列出真实芯片名 + 中间件实际取到的值 + 是否命中 default）。**历史 bug（教训 89）**：配置写 `"chip": "auto"` 被解析器当成字面芯片名做子串匹配 ⇒ 所有设备被跳过 ⇒ 恒返回 `default`。现已修好（`auto` = 自动挑选 + 芯片列表优先）。若真机芯片名不在默认列表里，把 `probe.sh temp` 显示的 name 填进 `chip` 列表（末尾保留 `"auto"`） |

| 日志刷屏 `采样异常` | 已做抑制：首次完整报出 + 每 10/100/1000 次提示 + 恢复时提示。若持续异常，说明本机 `/proc` 不可读（中间件应在 Linux 运行） |

---

## 8. 目录结构（最终形态）

> **程序目录与工具目录分离**：程序本体来自 AppImage（路径易失），
> 工具默认落到持久的 **`/screen/tools/`**（见 §1.0）。
> 下图 `$APP` 以 `/usr/local/hiksemi-sub` 为例，`$TOOLS` = `/screen/tools`。

### 8.1 程序目录 `$APP`

```
$APP/                        # ← AppImage 解包/挂载目录（或手动解包目录）
├── hiksemi-sub              # Electron 主程序
├── chrome-sandbox           # SUID 4755 root:root
├── resources/ ...           # 前端资源（整目录搬迁必须保留）
├── tools/                   # （程序自带，可选）安装器会优先采用；无则用 /screen/tools
└── docs/                    # 文档
    ├── port-to-fnos.md      # 移植方案（完整版）
    ├── deploy-guide.md      # 本文档（纯操作版）
    ├── local-rest-api.md    # REST 接口完整规范（逆向，含 LED/屏幕控制接口详解）
    ├── led-control.md       # LED 灯控制逻辑（逆向 + I2C 实测定论）
    ├── off-time-control.md  # 自动息屏控制逻辑（★ 背光优先屏控定论）
    ├── power-control.md     # 关机 / 重启控制逻辑
    └── fan-control.md       # 散热风扇控制逻辑（4 档 → set_hardware 链路）
```

### 8.2 工具目录 `$TOOLS`（默认 `/screen/tools`）

```
$TOOLS/                      # ★ 默认 /screen/tools（持久目录）
├── kiosk.sh                 # kiosk 启动脚本（由 service 的 ExecStart 调用）
├── install-autostart.sh     # 自启安装器（自动探测程序目录；--tools-dir 改工具目录；
│                            #   --fan-coolercontrol auto|on|off 接 CoolerControl；--dry-run 预演）
├── hiksemi-sub.service      # systemd 单元源（@APP_DIR@ / @TOOLS_DIR@ / @KIOSK@ 占位）
├── diag.sh                  # ★ 唯一诊断脚本（--chain 链路 / --i2c 灯控 / --screen 息屏 / --power 电源）
├── probe.sh                 # ★ 唯一硬件探测脚本（env 能力快照 / i2c-led 交互 / enable-i2c / fan）
├── test.sh                  # ★ 唯一测试入口（middleware/_test_*.py + _test_coolercontrol.py + _test_installer.sh）
├── decode-hslog.py          # ★ 程序日志 AES 解密器（日志是加密的！）
├── _test_installer.sh       # 安装器回归测试实现（100 断言，由 test.sh 调用）
├── _test_coolercontrol.py   # CoolerControl 渲染器测试（37 断言：双风扇/降级/校验，由 test.sh 调用）
├── coolercontrol/           # ★ 风扇 4 档 → CoolerControl 落地（安装器按需自动部署）
│   ├── config.toml          # 模板：profiles / functions / device-settings（含 @占位符@，双风扇 pwm1/pwm2）
│   ├── modes.json           # 模板：4 个 Mode 快照（hs-smart/low/standard/high，均双通道）
│   ├── render-config.py     # 渲染模板 + 契约自校验（真机 uid 渲染后才可写入；支持单/双风扇与降级）
│   ├── apply-mode.sh        # ★ 中间件调用的落地脚本（{5|2|3|4} → 切 Mode + 重启 daemon）
│   ├── probe-coolercontrol.sh  # 只读探测：安装形态 / 真实 device_uid / 双路 pwm 通道 / 温度源
│   └── README.md            # 使用说明
├── cert.pem / key.pem       # 自签证书（中间件用；首次自检时生成）
└── middleware/              # ★ 服务中间件（目录内容）
    ├── middleware.py        # 服务主体（纯 Python 标准库）
    ├── middleware.py.orig   # 原始副本（安装器留存，便于回滚）
    ├── data-mapping.json    # ★ 数据映射配置（改这里）
    ├── _test_eth_builder.py # 网卡/盘位 builder 单元测试（桩模拟，离线跑）
    ├── _test_fan.py         # 散热风扇 4 档单元测试（含 CoolerControl 适配器分支）
    ├── _test_led_*.py       # LED 相关单元测试（i2c / state / e2e）
    ├── _test_off_time.py    # 自动息屏单元测试
    ├── _test_set_power.py   # 关机重启单元测试
    └── cert.pem / key.pem   # 自签证书（首次启动自动生成）
```

> **兼容说明**：若程序自带 `$APP/tools/`，安装器会优先采用它（向后兼容）；
> 若都没有，才回退 `/screen/tools`。旧部署（工具在程序目录内）**无需改动**。

> **回归测试**：改动 `middleware.py` / `data-mapping.json` / `install-autostart.sh` /
> `coolercontrol/*` 后，在工具目录跑 `bash test.sh`
> （= 中间件 347 + CoolerControl 渲染器 37 + 安装器 119 = **503 断言**，
> 改完必跑；Windows 开发机可直接跑，无需真机）。
> 子命令：`middleware` / `coolercontrol` / `installer` 可只跑其中一类。

> 上图中的 `$APP/` 是**示意**，实际目录名由你决定；
>
> 所有脚本都按「脚本自身位置 / systemd unit / 运行中进程」自动定位程序目录，不依赖固定路径。

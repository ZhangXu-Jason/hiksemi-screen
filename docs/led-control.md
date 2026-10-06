# LED 灯控制逻辑（逆向还原）

> 目标：让**小屏的 LED 设置**真正作用到**操作系统 LED 硬件**。
> 客户端（屏幕端）只发 REST 请求，中间件负责把它翻译成 sysfs 写入。

---

## 一、客户端 LED 控制链路（逆向自 `index-be27a12a.js`）

### 1.1 五个 REST 接口（全部 `/rest/1.1/config`）

| JS 函数 | action | 参数 | 方向 |
|---|---|---|---|
| `getLightEffectStatus` | `get_atmo_light` | — | 读氛围灯状态 |
| `setupLightEffectStatus` | `set_atmo_light` | `light_mode={n}` | 写氛围灯开关 |
| `setupThemeLigtEffect` | `set_light_theme` | `mode={n}` | 写主题灯效模式 |
| `setupLightEffectColor` | `set_atmo_color` | **POST JSON body** | 写 RGB 颜色（retry count=3） |
| `getLightEffect` | `get_light_theme` | — | 读当前灯效 |

### 1.2 ★ LightEffect 枚举（客户端权威定义）

```js
const LightEffect = {
  LightEffectDefault:      0,     // 默认
  LightEffectTurnOff:      999,   // 关闭
  LightEffectMatchTheme:   1000,  // 随主题（= 1e3）
  LightEffectDynamicColor: 1001,  // 动态色
  LightEffectBlueColor:    1002,  // 蓝
  LightEffectWhiteColor:   1003,  // 白
  LightEffectYellowColor:  1004,  // 黄
  LightEffectReserved:     9999,  // 保留
};
```

### 1.3 ★★ 枚举 → RGB 映射（`setLightEffect` 内，**原样透传，勿"纠正"**）

| 枚举 | color_id | R | G | B | 备注 |
|---|---|---|---|---|---|
| 1000 随主题 | 0 | 123 | 123 | 123 | 占位值，实际跟随主题 |
| 1001 动态色 | 1 | 123 | 123 | 123 | 占位值，实际动态 |
| **1002 蓝** | 2 | **30** | **180** | **25** | ★ 见下方警告 |
| 1003 白 | 3 | 255 | 255 | 255 | |
| 1004 黄 | 4 | 255 | 90 | 0 | |

> ⚠️ **反直觉警告**：客户端把「蓝色」编码成 **RGB(30,180,25)** —— 这是**绿色**。
> 几乎可以确定是源码 bug（从绿色分支复制后忘改），但**必须原样透传**：
> 屏幕 UI 选「蓝」时，硬件收到的就是这个值。我们若"修正"它，会导致
> 屏幕显示与实际灯光不一致。**保持 bug 兼容。**

### 1.4 写流程（`setLightEffect` 完整分支）

```
setLightEffect(枚举 e):
│
├─ e === 999 (关闭)
│    └→ setupLightEffectStatus(0)                    // light_mode=0，直接关
│
└─ 其他
     ├─ 按 e 组装 {color_id, color_r, color_g, color_b}
     └→ getLightEffectStatus()                       // ★ 先读状态
          ├─ 若 status === 0（灯当前是开的）
          │    └→ setupLightEffectColor(rgb)         // 直接设颜色
          │         └─ code===0 则 setupLightEffectStatus(9)   // ★ 再置 light_mode=9
          │
          └─ 若 status !== 0（灯当前是关的）
               ├─ e === 1000 → setupThemeLigtEffect(0)  // 随主题：mode=0
               └─ 其他       → setupThemeLigtEffect(1)  // 其他：mode=1
                    └─ code===0 则 setupLightEffectColor(rgb)   // 再设颜色
```

**读流程**（`getLightEffect`，即 `get_light_theme` 的响应解析）：

```js
if (resp.code === 0) {
    if (resp.status === 0)            → LightEffectTurnOff (999)
    else if (resp.mode === 0)         → LightEffectMatchTheme (1000)
    else                              → resp.color_id + 1000    // 1000~1004
}
else → -1
```

> ★ 注意：`get_light_theme` 返回 `{code, status, mode, color_id}`。
> `color_id + 1000` 反解回枚举 —— 即 `color_id=2 → 1002(蓝)`。

---

## 二、★ 关键认知：「设置数据」不是「硬件状态」

客户端发来的 **是"设置意图"，不是"当前灯光状态"**：

| 客户端发来 | 含义 | 中间件该做什么 |
|---|---|---|
| `set_atmo_light&light_mode=9` | 想让灯亮（模式 9） | 点亮 LED |
| `set_atmo_light&light_mode=0` | 想关灯 | 熄灭 LED |
| `set_atmo_color` POST `{color_id:2, r:30,g:180,b:25}` | 想设成这个颜色 | **只写颜色，不动开关**（见下） |

而 `get_atmo_light` / `get_light_theme` 应该返回**硬件的真实当前状态**，
这样屏幕 UI 打开时能正确回显。

> ★★ 这是本任务的核心设计点：**读接口必须反映硬件真实状态，
> 写接口必须落地到硬件**。否则屏幕显示与灯光实际不一致。

### ★★★ 坑：`set_atmo_color` 绝不能改变开关状态

客户端 `setLightEffect(e)` 的流程是**「先设颜色、再开灯」两连击**：

```js
getLightEffectStatus().then(s => {
  s.status === 0                                  // 当前是「关灯」态
    ? setupLightEffectColor(r)                    // ① 先发颜色（此时 status 仍是 0）
        .then(a => a.code === 0
          ? setupLightEffectStatus(9) ...         // ② 再发 light_mode=9 真正开灯
```

真机日志实录（旧实现照 status 关灯，导致"点颜色先关灯"）：
```
01:48:30  [led] I2C → 关灯 (0x90=0x0)          ← set_atmo_color 时 status=0，误关灯
01:48:30  [led] I2C → 随主题/彩虹 (0x90=0x2)   ← 紧接着 set_atmo_light 才开灯
```

**正确语义**：`set_atmo_color` 只管颜色四元组，**开关由 `set_atmo_light` 决定**。
实现上强制保留当前 `status`，不因 body/上下文中可能存在的 `status=0` 而关灯：

```python
elif generic == "set_atmo_color":
    ...
    if kw:
        kw["status"] = int(led.get().get("status", 1))   # ★ 保留当前开关
        led.update(**kw)
```

这样「先设色 → 再开灯」序列中间那步不会误关灯，也避免了两次 I2C 模式写入的抖动。

---

## 三、操作系统侧（实测结果）

### 3.0 ★★★ 实测结论：`/sys/class/leds` 里的 6 个"LED"不是氛围灯！

**实测环境**：飞牛OS，主机 `EHOME-NAS01`，型号 **HS-AFS-R1**（海康 4 盘位 NAS）

```
igc-0200-led0 / led1 / led2      max_brightness=1    全部可写
igc-0300-led0 / led1 / led2      max_brightness=1    全部可写
```

**命名解析**：

| 字段 | 含义 |
|---|---|
| `igc` | **Intel Gigabit Controller** 驱动名（i225/i226 网卡） |
| `0200` / `0300` | PCI 设备地址 `02:00.0` / `03:00.0`（两块网卡） |
| `led0/1/2` | 网口上的 3 个状态灯 |

**结论：这 6 个全部是【网口 RJ45 指示灯】，与氛围灯/主题灯效无关。**

证据：
1. 驱动 `igc` = Intel 网卡驱动，与 `enp2s0-ovs`/`enp3s0-ovs` 的 PCI 地址对应
2. `max_brightness=1` → **单色开关灯**，做不了 RGB
3. `trigger` 全是通用 LED 触发器（`kbd-*` / `disk-activity` / `cpu*`）
4. hwmon 里**没有任何灯控芯片**（只有温度：acpitz/nvme/i915/drivetemp/coretemp）
5. 无厂商私有 LED 节点、无字符设备

> ⚠️ **不要往网口灯里写值**：网卡 PHY 会立刻覆盖；且会破坏"看灯判断网络状态"
> 这个正常功能。**这部分不是我们的目标。**

### 3.1 真正的氛围灯在哪？（★ 前提已更正，重开排查）

> **⚠️⚠️ 重大前提更正（用户实测反馈）**
>
> 用户确认两个关键事实，**推翻了我此前"硬件无通道"的结论**：
> 1. **海康原厂小屏运行时，氛围灯【能亮】** → 硬件存在，控制通道**必然存在**，是我没找到
> 2. **我们的 App 主进程只 `require` electron / child_process / path / nas-log**
>    → **原厂控灯不是 App 直控硬件，而是走 REST（20443）**
>
> **推论**：硬件驱动在**系统侧的某个服务**里，它接收 REST 请求后去点灯。
> **找它的方法是：定位 20443 的真正服务端，看它怎么实现 `set_atmo_light`。**
>
> 此处此前"13 条通道穷尽"的结论**作废**——当时的排查有盲区（见 3.1.5）。

#### 3.1.5 ★ 此前排查的盲区（为什么漏了）

| 盲区 | 说明 |
|---|---|
| **串口查得太窄** | 只查了 `/dev/ttyS0~S3`，**没查 ttyS4+、ttyUSB*、ttyACM*、/dev/serial/** |
| **"空闲"≠"不存在"** | MCU 可能"按需打开"串口，平时无占用进程 → 之前据此排除串口是**错误推理** |
| **没查 20443 服务端身份** | 一直在 `middleware.py`（我们自己写的）里找答案，**忘了问"原厂是谁在提供这些 REST"** |
| **没搜大二进制字符串** | `/usr/trim/bin/` 里那些 10MB+ 的二进制没做 `strings | grep atmo` |

**修正后的排查方向（优先级）**：
1. ★★★ **20443 服务端真身** —— 原厂谁在跑？它的 `/dev` fd 就是硬件通道
2. ★★ **串口全量扫描**（ttyS0~S99 + USB + ACM + /dev/serial）
3. ★★ **`/usr/trim/bin/` 大二进制 strings 搜 atmo/light**

**当时的探测手段**（已完成使命 —— 原 `probe-rest-server.sh` / `probe-serial-scan.sh`
等一次性脚本已归档，读取项并入 `probe.sh env` 的设备能力快照）：

```bash
sudo bash tools/probe.sh env    # 设备硬件能力快照（只读）
```

---

### ★★★ 3.1.9 最终突破：氛围灯走 **I2C**（用户实测 + 原厂源码双重确认）

> **结论已锁定，不再有悬念。** 用户实测 + 飞牛固件包 `cmd/` 里的原厂源码，一致指向 I2C。

#### （1）用户实测证据链

用户在飞牛OS 上实测以下命令**全部生效**：

```bash
apt-get update && apt-get install -y i2c-tools
i2cdetect -y 1                  # 总线上发现 0x26 从设备
i2cset -f -y 1 0x26 0x90 0x00   # → 氛围灯【关闭】
i2cget -f -y 1 0x26 0x90        # → 读回模式寄存器
```

用户明确确认：**`0x90` 就是开关**；**氛围灯是 RGB 多色灯带**。

#### （2）原厂源码佐证（`cmd/config_callback` 的 `set_rgb_mode()`）

用户提供的飞牛固件包 `D:/home/Desktop/新建文件夹/cmd/config_callback`（9726B）
内含**原厂控灯实现**，完整协议如下：

| I2C 项 | 值 | 说明 |
|---|---|---|
| **从设备地址** | `0x26` | RGB 灯带控制器 |
| **总线号** | **动态探测** | 不硬编码；判据=能读到 `0x26` 的 `0x90` 寄存器 |
| `0x90` | **模式寄存器** | `0x0` 关 / `0x1` 静态单色常亮(solid) / `0x2` 彩虹(rainbow) / `0x3` 呼吸(breathing) |
| `0x92` | **R**（0~255） | 红色分量 |
| `0x93` | **G**（0~255） | 绿色分量 |
| `0x94` | **B**（0~255） | 蓝色分量 |

**★ 设置顺序铁律：先写 RGB（0x92/0x93/0x94），最后写模式（0x90）。**
原厂注释明确：若先写模式再写颜色，颜色不生效。

**原厂颜色预设库**（`config_callback` 内 `COLOR_PRESETS`）：

```
red    = (255, 0,   0)      green  = (0,   255, 0)
blue   = (0,   0,   255)    yellow = (255, 255, 0)
purple = (255, 0,   255)    cyan   = (0,   255, 255)
orange = (255, 165, 0)      white  = (255, 255, 255)
```

#### （3）为什么之前 5 轮探测全都没发现？

**根因：`/dev/i2c-*` 设备节点不存在 —— 不是因为没硬件，而是 `i2c-dev` 内核模块没加载。**

```bash
lsmod | grep i2c_dev        # 无输出
ls /dev/i2c-*               # No such file or directory
```

原厂 `install_init` 里有一句 `modprobe i2c_dev` —— 装上 i2c-tools 并 `modprobe i2c_dev`
后，`/dev/i2c-1` 才出现，`0x26` 才可访问。

> **⚠️ 这是本项目第 N 次踩「不存在 vs 未启用」的坑。**
> 之前 `trim_miniscreen.service`（disabled）、`board.json`（不存在）、
> `i2c-dev`（未加载）——三次同一类误判。
> **教训已固化为排查铁律：在把某个通道标记为"不存在"之前，
> 必须先确认"它是否只是没被启用/加载/安装"。**

#### （4）枚举 → 硬件模式的映射设计（我们的中间件）

客户端只认 `LightEffect` 枚举，硬件只认 `0x90` 模式 + RGB。中间件的映射桥接：

| 客户端枚举 | 语义 | → 硬件 `0x90` | RGB |
|---|---|---|---|
| `status=0`（关） | 关闭 | `0x0` off | — |
| `1000` | 随主题 | `0x2` rainbow（动态流动） | — |
| `1001` | 动态色 | `0x3` breathing（呼吸渐变） | — |
| `1002` | 蓝（源码 bug 实为绿） | `0x1` solid | `(30,180,25)` ★原样透传 |
| `1003` | 白 | `0x1` solid | `(255,255,255)` |
| `1004` | 黄 | `0x1` solid | `(255,90,0)` |

> **反向读回**：`0x90=0x2→1000`、`0x3→1001`、`0x1`+RGB→按最近邻匹配回 `1002/1003/1004`、
> `0x0→关闭`。即使读到客户端 bug 色 `(30,180,25)` 也能正确识回 `1002`。

#### （5）★★ 探测鲁棒性加固（真机首轮反馈后）

**真机首轮反馈**：中间件日志仍报 `未探测到 I2C 灯控 → 后端=none`，点灯不生效。
诊断出旧探针的三个脆弱点并逐一加固：

| 脆弱点 | 后果 | 加固 |
|---|---|---|
| 只认「**读** 0x90 成功」 | 若寄存器只写/设备忙 → 误判"无设备" | **分级探测**：先 open+选中；读失败则**回退用 1 字节指针写**（无副作用）判定 |
| 日志只说"无 0x26" | 分不清「模块没加载 / 权限不足 / 选中失败」 | `verbose` 逐总线打失败原因；open 失败 `errno==13` 专标"权限不足" |
| 不自动加载 `i2c_dev` | 服务启动时模块没加载 → 永远探测不到 | `_ensure_i2c_dev_module()` 启动时自动 `modprobe i2c_dev`（照抄原厂） |

**新增诊断入口**：

```bash
# ① 中间件自带的独立探测（推荐，输出最直观）
python3 /usr/local/hiksemi-sub/middleware/middleware.py --led-probe

# ② 一键系统级诊断脚本（模块/总线/权限/Python 视角/复现用户命令）
sudo bash tools/diag.sh --i2c
```

`--led-probe` 会打印：`fcntl` 有无、当前 `euid`、`/dev/i2c-*` 列表、
模块是否可加载、逐总线命中结果、命中后当前 `0x90` 模式值。

**真机排错顺序**：
1. `ls /dev/i2c-*` —— 没有就先 `sudo modprobe i2c_dev`
2. `sudo i2cdetect -l` —— 确认总线号（中间件会自动探测，不必手动指定）
3. `sudo i2cget -f -y <bus> 0x26 0x90` —— 确认能访问
4. `middleware.py --led-probe` —— 看中间件视角是否一致
5. 若「root 能用、中间件不能用」→ **权限问题**：让中间件以 root 运行，
   或加 udev 规则放宽 `/dev/i2c-N` 权限。

#### （6）★★★ 根因已确诊：`i2c_dev` 模块未加载（真机诊断输出为证）

真机 `diag.sh --i2c` 输出**锁定根因**：

```
【1】i2c-dev 内核模块是否加载？
    ❌ i2c_dev 未加载
    ❌ /dev/i2c-* 不存在
【2】i2c 总线列表（i2cdetect -l）        ← 空（无总线）
【5】Python 视角：os.geteuid() = 0 (0=root)
    glob('/dev/i2c-*') = []
【6】复现用户命令：
    i2cget -f -y 1 0x26 0x90 → No such file or directory
```

**结论**：不是权限（euid=0）、不是总线号、不是只写寄存器 ——
就是 **`i2c_dev` 内核模块没加载**，导致 `/dev/i2c-*` 根本不存在，
所以连用户此前"能生效"的 `i2cget/i2cset` 此刻也一起失败了（模块被卸载/重启后未自动加载）。

**修复（三步，已工具化）**：

```bash
# ① 立即启用（加载模块 + 持久化 + 验证 0x26）
sudo bash tools/probe.sh enable-i2c

# ② 确认中间件视角
python3 /usr/local/hiksemi-sub/middleware/middleware.py --led-probe

# ③ 重启中间件生效
```

**已固化的自动加载**：
- 中间件 `_ensure_i2c_dev_module()`：启动时 `/dev/i2c-*` 不存在就自动 `modprobe i2c_dev`。
- `install-autostart.sh`：安装时加载并写 `/etc/modules-load.d/i2c-dev.conf`（**重启后依然生效**）。
- 修正了旧探测脚本里的模块名笔误（`i2c-dev` → 统一为 `i2c_dev`）。

### 3.2 旧的"无通道"定论（★ 已作废，保留备查）

#### 3.1.1 官方 LED 框架全景（已 100% 逆向，但本机完全不生效）

**三个服务全部依赖 `/boot/board.json`，而该文件确认不存在：**

| 服务 | 脚本 | 大小 | 状态 |
|---|---|---|---|
| `led-set.service` | `/usr/trim/bin/system_setled.sh` | 10747B / **396 行** | oneshot，**跳过** |
| `set_gpio-init.service` | `/usr/trim/bin/system_setgpio.sh` | 2670B | oneshot，**跳过** |
| `pwm-fancontrol.service` | `/usr/trim/bin/pwm-fancontrol.sh` | 2363B | **跳过** |

**`system_setled.sh` 全文已拿到（396 行）**，能力边界非常清楚：

```
led_color_set()  写 /sys/class/leds/<sysfs>/multi_intensity     ← 多色
load_led()       写 brightness + trigger + 各 trigger 子参数
led_turnoff()    遍历 /sys/class/leds/*/ → brightness=0        ← 全部灯！
led_turnon()     遍历 /sys/class/leds/*/ → brightness=max      ← 全部灯！
led_blink()      遍历 /sys/class/leds/*/ → trigger=timer       ← 全部灯！
led_restore()    从 /var/run/led.state 恢复
led_list()       列出所有 leds
start()          读 board.json → 逐条 load_led
CLI: {start|stop|restart|status|turnon|turnoff|blink|restore|list}
```

> ⚠️ **安全红线**：`led_turnoff/turnon/blink` 会**无差别操作 `/sys/class/leds/*` 下所有灯**。
> 本机那 6 个是**网口灯**，盲目调用会破坏"看灯判断网络状态"。**绝不能直接调这些子命令。**

> ★★ **关键事实**：全文 396 行里，**没有任何一处 GPIO / WMI / 串口 / I2C / 字符设备直控**。
> 它是一个纯粹的**标准 sysfs LED 批量配置器**，完全依赖 board.json。
> → **它不可能是氛围灯控制入口。此路彻底堵死。**

**board.json 协议格式**（从三个脚本的 `jq` 取值反推）：

```jsonc
{
  "led": [{
    "name": "blue", "sysfs": "led-name",
    "trigger": "netdev eth0 link tx rx | timer | heartbeat | none | default-on | gpio | transient | pattern",
    "brightness": 255, "delayon": 500, "delayoff": 500, "interval": 50,
    "inverted": 0, "default": 1,
    "color": { "red":255, "green":0, "blue":0 }
  }],
  "gpio": [{ "name":"...", "pin":123, "value":1, "delay":0 }],
  "fan":  [{ "name":"...", "tsysfs":"...", "fsysfs":"...", "start_speed":64,
             "start_temp":40, "max_speed":255, "temp_div":1, "verbose":true }]
}
```

**多色 LED 正确姿势**（`led_color_set()` —— 将来若可用必照抄）：
```bash
[ -e multi_intensity ] && [ -e multi_index ] || return 0
max_b="$(cat max_brightness)"
colors="$(cat multi_index | tr ' ' '\n')"     # ★ 读 multi_index 定通道顺序
for c in $colors; do
    value=$(echo "$color" | jq -r ".${c} // 0")   # 按通道名取 red/green/blue
    [ "$value" -gt "$max_b" ] && value="$max_b"
    multi_intensity="$multi_intensity$value "
done
echo "${multi_intensity% }" > /sys/class/leds/<sysfs>/multi_intensity
```
> ★ **绝不能假设通道顺序是 R G B**，必须读 `multi_index` 逐通道拼装。

#### 3.1.2 ★ 官方小屏服务：存在但未启用，脚本缺失

```ini
# /etc/systemd/system/trim_miniscreen.service
[Unit]    Description=Trim Miniscreen Service
          After=trim_wayland.service
[Service] Type=simple
          ExecStart=sh /usr/trim/bin/start_miniscreen.sh   ← ★ 该文件不存在！
          Restart=on-failure
[Install] WantedBy=multi-user.target
```

| 事实 | 结论 |
|---|---|
| `trim_miniscreen.service` | **disabled + inactive (dead)** —— 未启用 |
| `trim_wayland.service` | **disabled + inactive (dead)** —— 未启用 |
| `/usr/trim/bin/start_miniscreen.sh` | **全盘搜索：不存在** |
| `/usr/trim/bin/start_wayland.sh` | **不存在** |
| `/usr/trim/bin/` 下 `start_*.sh` | **一个都没有**（`for f in start_*.sh` 匹配 0 个） |

> **★★ 重大结论：飞牛OS 自己没有启用官方小屏方案**（服务 disabled，脚本缺失）。
> 小屏幕是留给**厂商定制应用**（即我们的 `hiksemi-sub`）使用的 ——
> 这是**我们应用的专属屏幕**，旁证了移植路线的正确性。
> 同时说明：**指望从官方小屏服务里"抄"到灯控逻辑 —— 此路不通**（脚本根本不存在）。

#### 3.1.3 实测进程实况（第 4 段）

```
PID 189298  Xorg :0 vt1                          ← 占用 tty1 + /dev/dri/card1
PID 230505  ./hiksemi-sub --no-sandbox           ← 我们的应用主进程
PID 230540  hiksemi-sub --type=gpu-process       ← 占用 /dev/dri/card1 + renderD128
PID 230557  hiksemi-sub --type=renderer
无任何进程持有 /sys/class/leds/* 的 fd            ← ★ 现场没有任何进程在管 LED
```

**全部 6 个 LED 的实时状态**：
```
igc-0200-led0~2   brightness=0   trigger=none   multi_intensity=无
igc-0300-led0~2   brightness=0   trigger=none   multi_intensity=无
```

> ★ 三点确认：① **无进程持有 LED 设备** → 没有任何厂商守护进程在管灯；
> ② 6 个网口灯 `trigger=none`；③ **没有 `multi_intensity`** → 连网口灯都不是多色。

#### 3.1.4 已排除通道总表（5 轮穷举，13 条）

| # | 通道 | 轮次 | 实测结果 | 判定 |
|---|---|---|---|---|
| 1 | `/sys/class/leds` 有无氛围灯 | 1/4/5 | 只有 6 个 `igc-*` 网口灯，`max_brightness=1` | ✗ |
| 2 | 串口 `/dev/ttyS*` | 2 | ttyS0~S3 全空闲，无占用进程 | ✗ |
| 3 | I2C | 2 | 无 `/dev/i2c-*` 节点 | ✗ |
| 4 | PWM `/sys/class/pwm` | 2 | 空 | ✗ |
| 5 | hwmon 灯控芯片 | 2 | 仅温度（acpitz/nvme/i915/drivetemp/coretemp） | ✗ |
| 6 | 厂商私有 ko | 2 | 无 hiksemi 私有模块 | ✗ |
| 7 | `/boot/board.json` | 4 | **确认不存在**（4 候选 + 搜 9 个根） | ✗ |
| 8 | `/usr/trim/bin/` 专用工具 | 4 | 100 个文件，**无 led/atmo 专用工具** | ✗ |
| 9 | `system_setled.sh` 隐藏分支 | 5 | 全文 396 行，**无 GPIO/WMI/串口直控**，纯 sysfs | ✗ |
| 10 | 官方小屏服务 `trim_miniscreen` | 5 | **服务 disabled，脚本不存在** | ✗ |
| 11 | 有无进程管 LED | 5 | **无任何进程持有 LED 设备** | ✗ |
| **12** | **GPIO 直控**（gpiochip0 / INTC1057:00，360 线） | 4 | 仅 gpio-337 被占（TPL1 IRQ）；pin3 `GPPC_B_3` 已 GPIO 模式 | **⚠️ 仅剩** |
| **13** | **ACPI WMI 厂商 GUID** | 4 | 3 个非标准 method 型 GUID（见下） | **⚠️ 仅剩** |

**WMI 厂商 GUID（唯一带厂商特征的线索）**：

| GUID | object_id | driver | 性质 |
|---|---|---|---|
| `05901221-D566-11D1-B2F0-00A0C9062910` | CC | wmi-bmof | 标准 MS（BIOS 数据块） |
| **`1F13AB7F-6220-4210-8F8E-8BB5E71EE969`** | TE | method | ★ 非标准 → 厂商自定义 |
| **`2BC49DEF-7B15-4F05-8BB7-EE37B9547C0B`** | DE | method | ★ 非标准 → 厂商自定义 |
| **`A6FEA33E-DABF-46F5-BFC8-460D961BEC9F`** | — | method | ★ 非标准 → 厂商自定义 |

> WMI 厂商 GUID 需**反解 ACPI DSDT 表**才能知道每个 method 的参数含义，
> 属**深度逆向**，成本高、收益不确定。**除非有强烈需求，不建议走这条路。**

### 3.2b ★ 对中间件实现的影响（务实结论）

**现状**：氛围灯**没有可用的软件控制入口**。

**因此中间件对 LED 接口的正确处理策略**：

| 接口 | 策略 |
|---|---|
| `get_atmo_light` / `get_light_theme` | **返回内部持久化状态**（读不到硬件真值，故读内部） |
| `set_atmo_light` / `set_light_theme` / `set_atmo_color` | **写内部状态文件**（如 `/var/lib/hiksemi-sub/led.json`），**保证 UI 回显一致 + 重启不丢** |

**为什么这是对的**：
1. 屏幕端需求本质是"设置能存住、关闭再打开回显一致"，**不要求真的点亮硬件**
2. 硬件层面本机氛围灯不可控，**强行写 sysfs 只会破坏网口灯**
3. 若将来找到硬件通道（GPIO/WMI），只需在 `apply_to_hardware()` 补一层映射，**上层完全不动**

**预留硬件落地钩子**（`middleware.py` 里，**现已被 I2C 后端取代** —— 见 3.1.9）：
```python
def apply_to_hardware(state):
    """★ 把 LED 内部状态落地到硬件。
    I2C 后端可用时 → _apply_i2c()；否则空实现（仅应用级状态）。
    """
    if LED_HW_BACKEND == "i2c":
        return _apply_i2c(state)          # ★ 已实现：写 0x92/93/94 + 0x90
    if LED_HW_BACKEND == "sysfs_multi":
        write_multi_intensity(state)
    return True
```

### 3.3 标准 LED sysfs 操作规范（★ 备用：仅当 I2C 不可用时）

```
/sys/class/leds/<led_name>/brightness      # 0 / 1 或 0~max_brightness
/sys/class/leds/<led_name>/trigger         # none / timer / heartbeat / ...
/sys/class/leds/<led_name>/max_brightness
/sys/class/leds/<led_name>/multi_index     # ★ 颜色通道顺序
/sys/class/leds/<led_name>/multi_intensity # ★ 多色亮度（按 multi_index 顺序）
```

**写入三原则**：
1. **必须先 `echo none > trigger`**，否则触发器会覆盖手动亮度值
2. **多色必须读 `multi_index` 定顺序**，逐通道拼装，不可假设 RGB 顺序
3. 权限：通常需 root（中间件若 systemd 运行，需确认 User/权限）

> ⚠️ **不要往网口灯 `igc-*` 里写值**：网卡 PHY 会立刻覆盖；且会破坏"看灯判断网络状态"
> 这个正常功能。**这部分不是我们的目标。**

---

## 四、验收标准（★ 已按 I2C 通道打通更新）

| # | 场景 | 期望 | 状态 |
|---|---|---|---|
| 1 | 屏幕点「氛围灯 → 蓝」 | UI 立即回显"蓝" | ✅ 内部状态 |
| 2 | 屏幕点「关闭」 | UI 立即回显"关闭" | ✅ 内部状态 |
| 3 | 关闭屏幕 UI 再打开 | LED 设置回显与上次一致 | ✅ 持久化 |
| 4 | 重启中间件 | 状态不丢失 | ✅ 持久化 |
| 5 | **屏幕点「关闭」** | **硬件氛围灯熄灭（0x90=0x0）** | ✅ **I2C 已实现** |
| 6 | **屏幕选「白/黄/蓝」** | **硬件灯带变对应颜色（RGB+0x90=0x1）** | ✅ **I2C 已实现** |
| 7 | **屏幕选「随主题/动态色」** | **硬件彩虹/呼吸动态** | ✅ **I2C 已实现** |
| 8 | **写入后读接口回显** | **与硬件一致（硬件真值优先）** | ✅ **I2C 已实现** |

> **自动化测试**（Windows 开发机无需真机，全部通过 123/123）：
> - `_test_led_i2c.py`（17）：I2C 探测/寄存器映射/写序/回读反解
> - `_test_led_state.py`（37）：内部状态/枚举/持久化/异常健壮性
> - `_test_led_e2e.py`（20）：HTTP 语义全链路（写→硬件→读回）
> - `_test_eth_builder.py`（49）：网卡 builder 回归（防止连带破坏）
>
> **真机验证待办**：交付到飞牛OS 后，让用户在屏幕 UI 上逐个点选，
> 核对硬件灯效是否与 UI 一致（尤其 `1000→彩虹` / `1001→呼吸` 的动态效果）。

---

## 五、相关文件

| 文件 | 作用 |
|---|---|
| `tools/probe.sh env` | 设备能力快照（只读；合并了原 5 个 recon 脚本的读取项） |
| `tools/probe.sh i2c-led` | ★ I2C 协议交互探测（辅助验证，会实际点灯） |
| `tools/middleware/middleware.py` | 中间件（**已含 I2C 后端 + LedState + builder/resolver**） |
| `tools/middleware/data-mapping.json` | 数据映射配置（**已含 5 个 LED action**） |
| `tools/middleware/_test_led_i2c.py` | I2C 桩测试（18 断言） |
| `tools/middleware/_test_led_state.py` | LED 状态测试（43 断言） |
| `tools/middleware/_test_led_e2e.py` | LED 全链路测试（20 断言） |

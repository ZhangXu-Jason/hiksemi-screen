# 散热风扇控制逻辑（逆向 + 实测）

> 本文档分析原程序（`HistorSub-Linux-1.1.12.AppImage`）对飞牛OS 中
> **硬件散热风扇**的控制：界面上 4 档（智能 / 低速 / 标准 / 强劲），
> 具体经哪些组件、哪些 REST 参数落到系统。
>
> 结论一句话：**客户端只发一个 `set_hardware?fan_mode=` 的 GET 请求；
> 飞牛OS 侧对应服务是空的（board.json 不存在），所以风扇控制需要中间件自己实现。**
>
> **★ 现状（2026-10-05）**：中间件已实现「加载 / 修改 / 保存」全链路，
> 并预留外部应用落地钩子（见 §5）。真机 sysfs 调速通道待复查（§6）。

---

## 1. 界面四档 → fan_mode 取值（程序内实测）

来源：`resources/app/dist/assets/index-be27a12a.js` @610765（`FAN_LIST` 定义）

```js
FAN_LIST = ref([
  { name: t("智能"), id: 5, icon: "auto",   animate: "autoGif",   size: "middle" },
  { name: t("低速"), id: 2, icon: "low",    animate: "lowGif",    size: "middle" },
  { name: t("标准"), id: 3, icon: "middle", animate: "middleGif", size: "middle" },
  { name: t("强劲"), id: 4, icon: "high",   animate: "highGif",   size: "middle" },
])
```

| UI 档位 | `id`（= 发给后端的 fan_mode） | 图标资源 |
| --- | --- | --- |
| **智能** | **5** | `fan_auto-c6da5254.gif` |
| **低速** | **2** | `fan_low-41ba0ee2.gif` |
| **标准** | **3** | `fan_middle-2a15520c.gif` |
| **强劲** | **4** | `fan_high-8a59c1f5.gif` |

> ⚠️ **注意取值不连续**：`5 / 2 / 3 / 4`，**没有 1**。
> 这与旧文档 `local-rest-api.md` 记的 `fan_mode={1|2|3}`（注释"1自动/2低/3中/4高"）
> **不符** —— 以本文档实测为准。

---

## 2. 写链路（设置档位）

### 2.1 唯一接口：`set_hardware`

来源：`index-be27a12a.js` @461588（`setupFanMode`）

```js
setupFanMode = function (e) {
  return new Promise((r, n) => {
    const i = encodeURI(
      `${httpHeader}/rest/1.1/config?access_token=${makeToken()}&action=set_hardware&fan_mode=${e}&cmdfrom=devsub`
    );
    semiLogWrite({ msg: `设置风扇模式接口 url:${i}` });
    httpGet(i, timeOut)
      .then((s) => {
        semiLogWrite({ msg: `设置风扇模式接口 res:${JSON.stringify(s)}` });
        s && s.code === 0 ? r(s.code) : r(-1);
      })
      .catch(...);
  });
};
```

**请求特征**：

| 项 | 值 |
| --- | --- |
| 方法 | **GET**（走 `httpGet`，非 POST） |
| 路径 | `/rest/1.1/config` |
| 必带参数 | `access_token`（`makeToken()`）、`cmdfrom=devsub` |
| 业务参数 | **`fan_mode`** = `5 \| 2 \| 3 \| 4` |
| 目标 | `https://127.0.0.1:20443`（`httpHeader` 写死；`rejectUnauthorized:false`） |
| 超时 | 10 秒；`retry.limit = 2` |
| 成功判据 | 响应 JSON 的 **`code === 0`** |

### 2.2 调用者：`setFan`

来源：`index-be27a12a.js` @611516

```js
const setFan = async (e) => {
  if ((await setupFanMode(e).catch(...)) === 0) {   // 成功
    showFan.value = !1;                              // 关闭选择弹层
    for (let n = 0; n < FAN_LIST.value.length; n++)
      if (e === FAN_LIST.value[n].id) {              // 按 id 找回档位对象
        deviceFan.value = FAN_LIST.value[n];         // 更新界面显示
        return;
      }
  } else {
    Cover.top({ message: t("设置失败") });            // 弹"设置失败"
  }
};
```

要点：
- `set_hardware` **返回 `code:0`** 才会更新界面并关闭弹层；
- 非 0（或网络异常）→ 弹「设置失败」，界面**不回退**。

---

## 3. 读链路（档位回显）

**客户端没有对应的读接口**（无 `get_hardware` / `get_fan`）。
回显完全依赖**设备状态帧**里的 `fanmode` 字段。

### 3.1 字段来源（主进程，长连接）

来源：`index-be27a12a.js` @451203（`devStatusInit`）

```js
devStatusInit = function () {
  deviceStatus.getInstance().EventConnection((r) => {
    r && r.cpu && r.cpu >= 0 && (
      devSta.code = 0,
      devSta.cpu = `${r.cpu}`,
      ...
      devSta.Fanmode = r.fanmode,        // ★ 小写 fanmode → 大驼峰 Fanmode
      ...
      ipcRenderer.send("eventDev", devSta)
    );
  }, ...);
};
```

- 设备状态走 **`/rest/1.2/config?action=get_hw_resource_info` 长连接**
  （multipart `--myboundary` 分帧，详见 `deploy-guide.md` §5）；
- 帧内字段名是 **`fanmode`（全小写）**；
- 主进程映射成 `Fanmode` 后经 IPC 发给渲染进程。

### 3.2 ★★ 匹配逻辑（发现一个原程序缺陷）

来源：`index-be27a12a.js` @617285

```js
if (e.Fanmode && e.Fanmode > 0) {
  for (let r = 0; r < FAN_LIST.value.length; r++)
    if (e.Fanmode === FAN_LIST.value[r].id) {     // ★ 严格相等
      deviceFan.value = FAN_LIST.value[r];
      break;
    }
} else {
  deviceFan.value = { name: t("智能"), id: 5, icon: "auto", ... };  // 回退
}
```

**问题**：`FAN_LIST` 的 `id` 只有 `5/2/3/4`，而真机状态帧返回的是 **`fanmode: 1`**（见下），
`1` 与任何一个都**不相等** ⇒ 循环走完无匹配，`deviceFan` **保持原值**。

真机日志实证（2026-10-04）：

```
STREAM get_hw_resource_info 首帧: {"code": 0, "cpu": 15, "memory": 18,
  "upload": 4.2, "download": 3.2, "fanmode": 1, "cpu_temp": 61,
  "temperature": 42, "disk_manage": [...]}
```

> **结论**：真机 `fanmode` 恒为 `1`，UI 的 4 档 id（5/2/3/4）永远匹配不上。
> 要么原厂后端另有一套编码（`1` = 某个默认档，未在 UI 暴露），
> 要么这是原程序的遗留 bug。
> **移植到飞牛OS 时，只要后端（中间件）返回的 `fanmode` 落在 `5/2/3/4` 内，UI 就能正确回显。**

---

## 4. 飞牛OS 侧：原厂服务是空的

程序里**只有 REST 调用**，没有底层实现 —— 真正的 PWM 写入由飞牛OS 侧服务完成。

已在飞牛OS 排查到的原厂组件：

| 服务 / 文件 | 路径 | 结论 |
| --- | --- | --- |
| `pwm-fancontrol.service` | `/usr/trim/bin/pwm-fancontrol.sh`（2363B） | **依赖 `/boot/board.json` 的 `fan` 段**，而该文件**不存在** ⇒ 空跑 |
| `led-set.service` | `/usr/trim/bin/system_setled.sh`（396 行） | 同样依赖 board.json ⇒ 空跑 |
| `set_gpio-init.service` | `/usr/trim/bin/system_setgpio.sh` | 同上 ⇒ 空跑 |
| `trim_miniscreen.service` | `/usr/trim/bin/start_miniscreen.sh` | **脚本不存在**，服务 disabled |

### 4.1 原厂 `pwm-fancontrol.sh` 期望的 board.json 结构（从 jq 取值反推）

```jsonc
{
  "fan": [{
    "name":        "...",     // 风扇名
    "tsysfs":      "...",     // 温度 sensor 的 sysfs 路径
    "fsysfs":      "...",     // 风扇 PWM 的 sysfs 路径
    "start_speed": 64,        // 启动 PWM 值
    "start_temp":  40,        // 起始温度（℃）
    "max_speed":   255,       // 最大 PWM 值
    "temp_div":    1,         // 温度分档除数
    "verbose":     true
  }]
}
```

> 这是**风扇 PID 调速**所需的最小参数集：
> 读 `tsysfs` 温度 → 按 `(temp - start_temp) / temp_div` 映射到
> `[start_speed, max_speed]` → 写 `fsysfs`。
> **对移植的意义**：如果本机能找到对应的 temperature / pwm sysfs 节点，
> 就可以直接照这套逻辑在中间件里实现智能调速，无需原厂服务。

### 4.2 已排除的通道（历史排查）

`led-control.md` §3.1.4 的穷举结论中，与风扇相关的两条：

| 通道 | 实测 |
| --- | --- |
| `/sys/class/pwm` | **当时为空** |
| `hwmon` | 仅温度芯片，无风扇控制芯片 |

> ⚠️ 但那次排查是**灯控专项**，未必穷尽风扇路径。
> 风扇可能藏在：`/sys/class/hwmon/*/pwm*`、`/sys/class/thermal/cooling_device*`、
> 或需要 `modprobe` 特定模块（如 `nct6775`、`it87`、或飞牛私有 ko）。
> **需真机复查**（见 §6）。

---

## 5. 移植实现：中间件已实现（2026-10-05）

> **状态：已落地。** 中间件版本 `2026.10.05-g`。

原缺口（已补齐）：

| 缺口 | 原先 | 现在 |
| --- | --- | --- |
| `set_hardware` action | **不存在** ⇒ 点档位弹「设置失败」 | ✅ 已新增 resolver |
| 状态帧 `fanmode` | 硬编码 `1` ⇒ UI 恒不回显 | ✅ 动态读 `FanState`（返回 5/2/3/4） |

### 5.1 三件事：加载 / 修改 / 保存

中间件里新增 **`FanState`** 状态类（`middleware.py`），与 `LedState` / `OffTimeState` 同构：

| 环节 | 实现 |
| --- | --- |
| **加载** | 启动时 `FanState()` 从状态文件读回上次档位；无效/损坏 → 默认「智能」5 |
| **修改** | `set_hardware` → `resolve_set_hardware()` → `_normalize_fan_mode()` 规范化 → `fan.update()` |
| **保存** | 原子落盘（tmp + `os.replace`），线程安全锁，mtime 感知外部人工改动 |

**四档定义集中在一处**（`FAN_MODES`，同时是合法白名单）：

```python
FAN_MODES = { 5: "智能", 2: "低速", 3: "标准", 4: "强劲" }   # ★ 不连续，无 1
FAN_MODE_DEFAULT = 5
```

**状态文件**：`/var/lib/hiksemi-sub/fan.json`（首选），兜底 `<脚本目录>/.fan-state.json`

```json
{ "fan_mode": 5 }
```

**规范化规则**（`_normalize_fan_mode`）：

| 输入 | 结果 | 说明 |
| --- | --- | --- |
| `5 / 2 / 3 / 4` | 原值 | 合法档 |
| `"4"`（字符串数字） | `4` | 容忍 query 传参 |
| `1 / 0 / 6 / 99 / -1` | `5` | ★ **不夹取**（四档无大小关系），回默认 |
| `2.9`（非整数） | `5` | 拒绝截断误判 |
| `"abc" / None / ""` | `5` | 无法解析 → 默认 |

> ★ **为什么不"最近邻夹取"**：`5` 不是最大档（顺序是 5→2→3→4），
> 数值大小与档位高低无关，夹取必然产生错误档位。故非法值一律回默认「智能」。

### 5.2 写链路 `resolve_set_hardware`

```python
# data-mapping.json
"set_hardware": {
  "code": { "source": "set_hardware" }
}
```

- 入参 `fan_mode`（缺省取 `mode`）→ 规范化 → `FanState.update()` → 返回 `code=0`
- ★ 返回**标量 0**（字段级 resolver 约定），避免 `{"code":{"code":0}}` 嵌套
- 即使 `FanState` 缺失也返回 `0`，**不让界面弹「设置失败」**

### 5.3 读链路 `fanmode` 动态返回

```python
# data-mapping.json  get_hw_resource_info
"fanmode": "state"     # ★ 从 FanState 读当前档位
```

`build_dev_status()` 的取值优先级：

1. 配置写死数字（如 `5`）→ 用配置（临时排障用）
2. 配置 `{ "source": "static", "value": n }` → 用该值
3. 否则 → `FanState.mode()`（**正常运行走这里**）
4. 兜底 → 默认 `5`

返回值必定是 **int** 且落在 `5/2/3/4`，与 UI `FAN_LIST[].id` **严格相等**匹配 ⇒ 回显正常。

### 5.4 ★ 外部应用落地钩子（预留）

飞牛OS 上风扇真实调速通道**待真机复查**（见 §6）。为此预留统一钩子
`apply_to_external_app(prev_mode, state)`，**默认空实现**（仅保存 + 回显）。
日后确定通道后，三选一即可启用，**上层接口完全不动**：

| 优先级 | 方式 | 配置 | 行为 |
| --- | --- | --- | --- |
| ① | **外部命令** | `HIKSEMI_FAN_APPLY_CMD="/usr/local/bin/pwm-set {mode}"` | 每次档位变化执行命令；`{mode}`/`{name}` 占位替换 |
| ② | **CoolerControl** | `HIKSEMI_FAN_COOLERCONTROL=1` | 调用内置适配器 `coolercontrol/apply-mode.sh {mode}`，把档位翻译成 `/etc/coolercontrol/{config.toml,modes.json}`。**推荐**，详见 `coolercontrol.md` |
| ③ | **sysfs PWM** | `HIKSEMI_FAN_PWM_PATH="/sys/class/hwmon/hwmon3/pwm1"` | 按 `FAN_MODE_PWM` 写 PWM 值（低速 64 / 标准 140 / 强劲 255） |

```python
# ④ 都没有 → 空实现，仅保存档位（日志提示"未配置外部应用通道"）
```

`FAN_MODE_PWM`（预留映射，8-bit 占空比）：

| 档位 | PWM |
| --- | --- |
| 低速（2） | 64 |
| 标准（3） | 140 |
| 强劲（4） | 255 |
| 智能（5） | — ★ 不直接写死，交外部/内核自调 |

> ★ 任何钩子异常都**不向上抛**，避免影响 REST 响应（界面不会因此报错）。
> ★ `update()` 无论档位是否变化都会重放一次钩子，保证系统状态与 UI 一致。
>
> ★ **方式 ② 已可一键部署**：`sudo bash install-autostart.sh --fan-coolercontrol on`
> 会自动把 `tools/coolercontrol/` 装到 `$TOOLS/coolercontrol/` 并注入上述环境变量
> （安装器 `auto` 模式会在探测到 CoolerControl 时自动启用，`off` 则清除该行）。
> 判断是否真的生效：`grep '^Environment=.*HIKSEMI_FAN_COOLERCONTROL' /etc/systemd/system/hiksemi-sub.service`
> —— service 模板里带**被注释的示例行**，不能用宽松 grep。

### 5.5 真机复查后如何补齐

按 §6 命令确定通道后：

- **能写 `pwm1`** → 设 `HIKSEMI_FAN_PWM_PATH`，或把 `FAN_MODE_PWM` 调成更合适的分级
- **只有 `cooling_device`（`cur_state` 0..N）** → 用 `HIKSEMI_FAN_APPLY_CMD` 包一层脚本写 `cur_state`
- **需要 PID 智能调速** → 照 §4.1 的 board.json 参数集，在外部脚本里实现（读温度 → 算 PWM → 写 node）
- **想直接复用成熟方案** → 用 **CoolerControl**（见 `coolercontrol.md`），
  四档映射成它的 4 个 Mode，无需自己写调速逻辑

---

## 6. 真机复查清单（待执行）

以下命令用于在飞牛OS 上定位风扇的真实 sysfs 通道：

```bash
# ① 风扇 RPM / PWM（最可能）
ls -l /sys/class/hwmon/*/                      # 找 fan1_input / pwm1 / pwm1_enable
cat  /sys/class/hwmon/*/name                   # 芯片名
for f in /sys/class/hwmon/*/fan*_input; do echo "$f = $(cat $f 2>/dev/null)"; done

# ② 内核 thermal 框架（现代 x86 常用 cooling_device 控风扇）
ls -l /sys/class/thermal/                      # cooling_device*
cat  /sys/class/thermal/cooling_device*/type
cat  /sys/class/thermal/cooling_device*/cur_state
cat  /sys/class/thermal/cooling_device*/max_state

# ③ 直接 PWM
ls -l /sys/class/pwm/ 2>/dev/null
ls -l /sys/devices/platform/*/pwm* 2>/dev/null

# ④ 原厂脚本本体（确认它到底读哪些路径）
cat /usr/trim/bin/pwm-fancontrol.sh
systemctl status pwm-fancontrol.service --no-pager

# ⑤ 有无其他控风扇的进程/服务
ps aux | grep -iE "fan|pwm|thermal"
systemctl list-units --all | grep -iE "fan|thermal|trim"
```

**判定要点**：
- 若能读到 `fan1_input`（转速）→ 说明有转速传感器；
- 若能写 `pwm1` → **风扇可直接调速**；
- 若只有 `cooling_device`（`cur_state` 0..N）→ 走内核 thermal governor，写入档位数而非 PWM 值；
- 若全都为空 → 风扇由 **EC/BIOS 自动控制**，OS 无法干预
  （此时"智能/低/中/高"只能做**假实现**：记录状态并回显，不改实际转速）。

---

## 7. 小结

| 环节 | 结论 |
| --- | --- |
| **UI 四档** | 智能=5、低速=2、标准=3、强劲=4（`FAN_LIST` 实测） |
| **写接口** | `GET /rest/1.1/config?action=set_hardware&fan_mode={5\|2\|3\|4}&cmdfrom=devsub` |
| **成功判据** | 响应 `{"code":0}` |
| **读接口** | **无**；靠设备状态帧 `fanmode` 字段回显 |
| **回显匹配** | `fanmode === FAN_LIST[].id` 严格相等；真机 `fanmode=1` ⇒ **匹配失败**（原程序缺陷） |
| **飞牛OS 侧** | `pwm-fancontrol.service` 依赖不存在的 `board.json` ⇒ **空跑** |
| **中间件实现** | ✅ **已完成**（`2026.10.05-g`）：`FanState` 加载/修改/保存 + `set_hardware` resolver + `fanmode` 动态返回 |
| **外部应用** | ✅ **已预留**：`HIKSEMI_FAN_APPLY_CMD`（命令）/ `HIKSEMI_FAN_PWM_PATH`（sysfs），未配置则仅保存+回显 |
| **待办** | 真机复查风扇 sysfs 通道（见 §6），确定后用外部钩子接通真实调速 |

# HIKSEMI 本地 REST 服务规范（逆向还原）

> 来源：`hiksemi-sub` v1.1.12 打包产物 `resources/app/dist/assets/index-be27a12a.js`  
> 逆向方式：静态提取 URL 模板 + HTTP 封装实现 + 枚举常量，未经真机抓包验证。

---

## 1. 服务基本信息

| 项        | 值                                                           |
| -------- | ----------------------------------------------------------- |
| Base URL | `https://127.0.0.1:20443`                                   |
| 协议       | HTTPS（证书自签，所有请求 `rejectUnauthorized: false`）                |
| 方法       | **GET**（读/设置均用 GET query 传参），仅 `set_atmo_color` 用 POST JSON |
| 编码       | URL 全部经 `encodeURI()`                                       |
| 超时       | 普通接口 `10000ms`；主题下载等 `timeout` 独立变量                         |
| 重试       | GET `retry.limit = 2`；POST 由调用方传 `count`                    |

### 1.1 鉴权 Token

```js
makeToken()       = md5("HISTORLOCALREQ" + unixSec) + ":" + unixSec + ":100000"
makeOpenInfoToken() = md5("HISTOROPENSDK" + unixSec) + ":" + unixSec
```

- `unixSec` = `Math.floor(Date.now() / 1000)`
- 参数名统一为 `access_token`，通过 query string 传递
- 普通业务接口用 `makeToken()`；**仅** `/rest/1.1/openinfo` 用 `makeOpenInfoToken()`
- 常量前缀字面量：`HISTORLOCALREQ`、`HISTOROPENSDK`，后缀固定 `100000`
- 失效返回：`-1004` + `{"msg":"Access_token invalid!"}`

### 1.2 公共 Query 参数

| 参数               | 说明                  |
| ---------------- | ------------------- |
| `access_token`   | 见上，必填               |
| `action`         | 业务动作名，必填            |
| `cmdfrom=devsub` | 固定值，标识调用方为「小彩屏 App」 |

---

## 2. 通用响应封装

### 2.1 结构

```json
{
  "code": 0,
  "msg": "…",
  "<业务字段>": "…"
}
```

- `code === 0` 为成功（枚举 `HSStatusCodeOptSuccess = 0`）
- 非 0 见第 6 节错误码表
- 客户端额外注入 `urlParam`（原始 URL）与 `costTime`（耗时 ms）

### 2.2 客户端处理规则（httpGet）

1. 响应体为空 → 抛 `"res.body为空"`
2. `JSON.parse` 失败 → 抛 `formatError`，并把原文挂在 `err.result`
3. 成功 → 附加 `urlParam` 后返回

`wrapperResHandle()` 会在此处旁路识别：`TokenInvalid(-1004)`、`UnactiveDev(-1100)`、`CGICannotUse(-2605)`、`Unuser(-1111)`、`DiskIsExpaning`、`DiskIsMigrating`。

---

## 3. 接口清单（按 REST 版本分组）

### 3.1 `/rest/1.0`

| action         | 说明                      | 关键参数 | 返回                                     |
| -------------- | ----------------------- | ---- | -------------------------------------- |
| `get_event`    | **事件长连接**（SSE/流式，非普通请求） | 无    | 见 §6                                   |
| `get_sys_info` | 获取设备是否已启动 / 系统信息        | 无    | `product_model`, `mac`, `is_active`, … |

```
GET /rest/1.0/config?access_token=…&action=get_sys_info&cmdfrom=devsub
GET /rest/1.0/event?access_token=…&action=get_event&cmdfrom=devsub
```

### 3.2 `/rest/1.1`

```
GET /rest/1.1/config?access_token=…&action=<action>&cmdfrom=devsub
```

| action                | 附加参数                                        | 说明                     |
| --------------------- | ------------------------------------------- | ---------------------- |
| `set_power`           | `power_mode={0\|1}`                         | 0 关机 / 1 重启            |
| `get_admin_accountid` | —                                           | 获取管理员 accountId        |
| `set_update`          | `auto_opt=0`                                | 触发固件升级                 |
| `get_eth_info_v2`     | —                                           | 网口信息，响应取 `.eth`        |
| `get_network_mode`    | —                                           | 网络聚合模式                 |
| `set_hardware`        | `fan_mode={5\|2\|3\|4}`                     | 风扇模式（**5 智能/2 低速/3 标准/4 强劲**；成功判据 `code===0`；详见 `fan-control.md` §5，落地到 CoolerControl 见 `coolercontrol.md`） |
| `set_brightness`      | `brightness={0-100}&mode=1&set_flag={0\|1}` | 屏幕亮度（详见 §5）            |
| `set_off_time`        | `xp_time={30\|60\|120\|300}`（秒）             | 息屏时间（详见 §5）            |
| `get_off_time`        | —                                           | 返回 `.xp_time`（详见 §5）    |
| `get_atmo_light`      | —                                           | 氛围灯状态（详见 §4）            |
| `set_atmo_light`      | `light_mode={0\|9}`                         | 氛围灯模式（详见 §4）            |
| `set_light_theme`     | `mode={0\|1}`                               | 主题灯效（详见 §4）             |
| `set_atmo_color`      | **POST JSON body**                          | 氛围灯颜色（详见 §4）            |
| `get_light_theme`     | —                                           | 当前主题灯效（详见 §4）           |
| `opt_screen_theme`    | `opt=1&id={主题id}`                           | 切换彩屏主题（详见 §5）           |

```
GET /rest/1.1/update?access_token=…&action=<action>&cmdfrom=devsub
```

| action                    | 附加参数           | 说明             |
| ------------------------- | -------------- | -------------- |
| `get_update_version_desc` | `debug={0\|1}` | 固件最新版本描述       |
| `set_ezviz_server`        | `ser_opt={n}`  | 切换萤石服务器（正式/测试） |

```
GET /rest/1.1/openinfo?access_token=<OPENSDK_TOKEN>&action=get_sys_info&cmdfrom=devsub
```

| action         | 说明     | 返回                      |
| -------------- | ------ | ----------------------- |
| `get_sys_info` | 设备是否绑定 | `is_active === 1` → 已绑定 |

### 3.3 `/rest/1.2`

| action                 | 路径                 | 附加参数                                   | 说明                |
| ---------------------- | ------------------ | -------------------------------------- | ----------------- |
| `get_hw_resource_info` | `/rest/1.2/config` | —                                      | **设备状态长连接**（§6.2） |
| `space_manage`         | `/rest/1.2/config` | —                                      | 磁盘/存储空间信息         |
| `get_space_list`       | `/rest/1.2/quota`  | `dlna=1&mode=1&private=1&disk_check=1` | 可用空间列表            |

`get_space_list` 返回 `space_list[]`，每项按以下条件过滤：

- `space_index > 0`
- `space_path` 非空
- `space_state ∈ {"clean","degraded","syncing"}`

---

## 4. LED（氛围灯 / 主题灯效）接口详解

> 逆向来源：`index-be27a12a.js` 中 `getLightEffectStatus` / `setupLightEffectStatus` /
> `setupThemeLigtEffect` / `setupLightEffectColor` / `getLightEffect` 五个函数。
> 服务端落地（I2C 协议）见 `led-control.md`。

### 4.1 客户端 `LightEffect` 枚举（权威定义）

```js
const LightEffect = {
  LightEffectDefault:      0,     // 默认
  LightEffectTurnOff:      999,   // 关闭
  LightEffectMatchTheme:   1000,  // 随主题
  LightEffectDynamicColor: 1001,  // 动态色
  LightEffectBlueColor:    1002,  // 蓝（★ 源码实为绿色，见 §4.6）
  LightEffectWhiteColor:   1003,  // 白
  LightEffectYellowColor:  1004,  // 黄
  LightEffectReserved:     9999,  // 保留
};
```

枚举 → `(color_id, R, G, B)` 映射（`setLightEffect` 内组装，**原样透传，勿"纠正"**）：

| 枚举 | 语义 | color_id | R | G | B |
| --- | --- | --- | --- | --- | --- |
| 1000 | 随主题 | 0 | 123 | 123 | 123 |
| 1001 | 动态色 | 1 | 123 | 123 | 123 |
| 1002 | 蓝（★实为绿） | 2 | **30** | **180** | **25** |
| 1003 | 白 | 3 | 255 | 255 | 255 |
| 1004 | 黄 | 4 | 255 | 90 | 0 |

### 4.2 接口一览

| action | 方法 | 参数 | 客户端函数 | 说明 |
| --- | --- | --- | --- | --- |
| `get_atmo_light` | GET | — | `getLightEffectStatus` | 读氛围灯状态（开关） |
| `set_atmo_light` | GET | `light_mode={0\|9}` | `setupLightEffectStatus` | 写氛围灯开关 |
| `set_light_theme` | GET | `mode={0\|1}` | `setupThemeLigtEffect` | 写主题灯效模式 |
| `set_atmo_color` | **POST** | JSON body | `setupLightEffectColor` | 写 RGB（retry count=**3**） |
| `get_light_theme` | GET | — | `getLightEffect` | 读当前完整灯效 |

### 4.3 读接口 `get_atmo_light` / `get_light_theme`

**请求**

```
GET /rest/1.1/config?access_token=<TOKEN>&action=get_atmo_light&cmdfrom=devsub
GET /rest/1.1/config?access_token=<TOKEN>&action=get_light_theme&cmdfrom=devsub
```

**响应字段**

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `code` | int | `0` = 成功（非 0 时客户端一律返回 `-1`） |
| `status` | int | `0` = 关灯；非 0 = 开灯 |
| `mode` | int | `0` = 随主题；其它 = 一般模式 |
| `color_id` | int | `0~4`，对应 `LightEffect - 1000` |

**响应示例**

```json
{ "code": 0, "status": 1, "mode": 1, "color_id": 2 }
```

**客户端解析规则**

```js
// getLightEffect() 的判定顺序（顺序敏感，不可颠倒）
if (res.code !== 0)                   → -1
else if (res.status === 0)            → LightEffectTurnOff   (999)
else if (res.mode === 0)              → LightEffectMatchTheme(1000)
else                                  → res.color_id + 1000  // → 1000~1004
```

> ★ `color_id + 1000` 是反向解码：`color_id=2` → `1002`（蓝）。
>
> ★★ **`get_atmo_light` 与 `get_light_theme` 请求同一份真值**，只是
> `get_atmo_light` 客户端不做枚举解码（原样拿到对象后自行读 `.status`），
> `get_light_theme` 才做上面的三态解码。二者响应体结构相同。
>
> ★★★ **服务端必须返回「硬件真实状态」而非「最近一次设置」**：
> 中间件在 I2C 后端可用时先 `led_read_hardware()` 回读 `0x90/0x92/0x93/0x94`，
> 反解出 `status/mode/color_id`（硬件真值优先）；读不到硬件才退回内部持久化状态。
> 否则屏幕显示与灯带实际不一致（详见 `led-control.md` §二）。

**硬件回读反解**（I2C 后端）

| 硬件 `0x90` | → 响应 |
| --- | --- |
| `0x0` off | `status=0`（其余取内部状态） |
| `0x2` rainbow | `status=1, mode=0, color_id=0`（→ 随主题 1000） |
| `0x3` breathing | `status=1, mode=1, color_id=1`（→ 动态色 1001） |
| `0x1` solid | `status=1`，按硬件 RGB **最近邻** 反查 `color_id` |

### 4.4 写接口 `set_atmo_light`（氛围灯开关）

```
GET /rest/1.1/config?access_token=<TOKEN>&action=set_atmo_light&light_mode=9&cmdfrom=devsub
```

| 参数 | 取值 | 语义 |
| --- | --- | --- |
| `light_mode` | `0` | **关灯** |
| `light_mode` | `9` | **开灯**（客户端固定用 9，非 1） |

**响应**：`{"code": 0}` 即成功；客户端判定 `code === 0`，否则视为失败。

**服务端落地**：`0` → `0x90 = 0x0`（off）；非 `0` → 按当前颜色重放（见 §4.7）。

> ★ 客户端「关闭」路径是**唯一直接调它**的场景（`setLightEffect(999)` → `setupLightEffectStatus(0)`）；
> 开灯路径不单独调它，而是「先设色、再调它置 9」的两连击（见 §4.6）。

### 4.5 写接口 `set_light_theme`（主题灯效模式）

```
GET /rest/1.1/config?access_token=<TOKEN>&action=set_light_theme&mode=1&cmdfrom=devsub
```

| 参数 | 取值 | 语义 |
| --- | --- | --- |
| `mode` | `0` | 随主题（对应 `1000`） |
| `mode` | `1` | 其它（对应 `1001` 动态色及固定色） |

**响应**：`{"code": 0}` 即成功。

**调用时机**：仅在「当前灯是**关闭**状态（`status !== 0` 判定为假）」时使用，属
「灯关着，用户先选模式」的开灯前置步骤（见 §4.6）。

### 4.6 写接口 `set_atmo_color`（氛围灯颜色，POST）

**请求**

```
POST /rest/1.1/config?access_token=<TOKEN>&action=set_atmo_color&cmdfrom=devsub
Content-Type: application/json

{ "color_id": 2, "color_r": 30, "color_g": 180, "color_b": 25 }
```

| body 字段 | 类型 | 语义 |
| --- | --- | --- |
| `color_id` | int | `0~4`，枚举 `LightEffect - 1000` |
| `color_r` / `color_g` / `color_b` | int | `0~255` RGB 分量 |

**客户端行为**：`got.post()` + `json: body`；`retry.limit = 3`（`count = 3`），
超时 10000ms；成功后附加 `costTime` 返回。

**响应**：`{"code": 0}`。

★★★ **服务端必须遵守的语义（本任务核心坑点）**：

```
set_atmo_color 【只改颜色，绝不改变开关状态】。
开关只由 set_atmo_light 决定。
```

原因：客户端 `setLightEffect(e)` 是 **「先设色 → 再开灯」两连击**，中间那步
`set_atmo_color` 发出时 `status` 可能仍是 `0`（灯关着），若服务端照 `status=0`
去关灯，硬件会被先写 `0x90=0x0`，表现为「用户点颜色反而先关灯」。

| 客户端分支 | 序列 |
| --- | --- |
| **灯当前关着**（`status === 0`） | ① `set_atmo_color(rgb)` → ② `code===0` 则 `set_atmo_light(9)` |
| **灯当前开着**（`status !== 0`） | ① `set_light_theme(1000→0 / 其它→1)` → ② `code===0` 则 `set_atmo_color(rgb)` |

> ★ 服务端实现强制保留当前 `status`：`kw["status"] = int(led.get()["status"])`
> 后再落盘，避免被 body 中隐含的关灯意图带偏。

**硬件落地**：固定色（`color_id 2/3/4`）→ **先写 `0x92`(R)、`0x93`(G)、`0x94`(B)，再写 `0x90=0x1`(solid)**
（顺序铁律，反了颜色不生效）；`color_id 0/1` 交由模式通道处理。

### 4.7 服务端 I2C 落地映射（写链路）

| 客户端状态 | 硬件 `0x90` | RGB 寄存器 |
| --- | --- | --- |
| `status == 0`（关灯） | `0x0` off | — |
| `color_id == 0`（随主题） | `0x2` rainbow | — |
| `color_id == 1`（动态色） | `0x3` breathing | — |
| `color_id ∈ {2,3,4}`（固定色） | `0x1` solid | 先 `0x92/0x93/0x94`，再 `0x90` |

- 从设备地址 `0x26`，总线**动态探测**（不硬编码）
- 写寄存器统一走 `i2c-dev`（需 `modprobe i2c_dev`，中间件启动时自动加载）
- 后端优先级：`i2c` > `sysfs_multi` > `none`（`none` 时仅应用级状态，不碰硬件）

---

## 5. 屏幕控制接口详解

> 逆向来源：`setScreenBrightness` / `getScreenBrightness` / `setScreenBrightnessWithCmd` /
> `setCloseScreenPeriod` / `getCloseScreenPeriod` / `setScreenTheme`。
> 熄屏动作与帧缓冲选屏详见 `off-time-control.md`。

### 5.0 屏幕控制相关接口一览

| action | 方法 | 参数 | 客户端函数 | 说明 |
| --- | --- | --- | --- | --- |
| `set_brightness` | GET | `brightness={0-100}&mode=1&set_flag={0\|1}` | `setScreenBrightness` | 屏幕亮度（REST 通道） |
| `set_off_time` | GET | `xp_time={30\|60\|120\|300}` | `setCloseScreenPeriod` | 自动息屏档位 |
| `get_off_time` | GET | — | `getCloseScreenPeriod` | 读回 `xp_time` |
| `opt_screen_theme` | GET | `opt=1&id={主题id}` | `setScreenTheme` | 切换彩屏主题 |

> ★ **亮度双通道**：`set_brightness`（REST）与 `setScreenBrightnessWithCmd`（直接 exec
> `echo … > /sys/class/backlight/intel_backlight/brightness`）**并存**，客户端拖动亮度
> 时**两条都发**。移植时缺失任一条都可能表现不一致（见 §5.1、§6）。

### 5.1 `set_brightness`（屏幕亮度）

```
GET /rest/1.1/config?access_token=<TOKEN>&action=set_brightness&brightness=60&mode=1&set_flag=0&cmdfrom=devsub
```

| 参数 | 取值 | 语义 |
| --- | --- | --- |
| `brightness` | `0~100` | 亮度百分比（客户端 UI 滑杆值） |
| `mode` | `1` | **固定值 1**，无其它取值 |
| `set_flag` | `0` | 普通设置（客户端所有调用点都传 `0`） |
| `set_flag` | `1` | 保留（源码中未使用，语义未知） |

**响应**：`{"code": 0}`；客户端判定 `a.code === 0 ? a.code : -1`。

**服务端落点**（双通道并存）

| 通道 | 实现 | 值域换算 |
| --- | --- | --- |
| REST | `set_brightness?mode=1` | 服务端把 `0~100` 换算为 `96000 * percent / 100` 后写 sysfs |
| exec | `echo $((96000*percent/100)) > /sys/class/backlight/intel_backlight/brightness` | 客户端**主进程直接执行**，不经 REST |

- 背光 sysfs 值域 **0–96000**
- 读回换算：`value * 100 / 96000`
- 中间件对背光节点做**自动发现**（`/sys/class/backlight/*/brightness`），不硬编码 `intel_backlight`

### 5.2 `set_off_time` / `get_off_time`（自动息屏档位）

**写**

```
GET /rest/1.1/config?access_token=<TOKEN>&action=set_off_time&xp_time=60&cmdfrom=devsub
```

| 参数 | 取值 | 语义 |
| --- | --- | --- |
| `xp_time` | `30` | 30 秒 |
| `xp_time` | `60` | 1 分钟 |
| `xp_time` | `120` | 2 分钟 |
| `xp_time` | `300` | 5 分钟 |

**响应**：`{"code": 0}`；客户端判定 `s.code === 0 ? s.code : -1`。

**读**

```
GET /rest/1.1/config?access_token=<TOKEN>&action=get_off_time&cmdfrom=devsub
```

**响应**

```json
{ "code": 0, "xp_time": 60 }
```

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `code` | int | `0` = 成功 |
| `xp_time` | int | **标量秒数**（`30/60/120/300`） |

> ★★★ **契约坑**：客户端取的是 `res.xp_time`（**标量**）。若服务端返回
> `{"off_time":[]}`（数组/错误字段名），客户端拿到 `undefined` → UI 永远显示 `--`，
> 用户看到「设置不生效」。**必须返回 `xp_time` 标量。**
>
> ★ 服务端对非法值做**夹取**：非 `30/60/120/300` 的值夹到最近合法档位，非数字则取默认 `120`。
> 且**写档位后立即重置空闲计时**（否则刚设完 30s 会立刻熄屏）。
>
> ★★ **客户端只发偏好，不做熄屏动作**。真正「到点黑屏」必须由服务端完成：
> 空闲监视 → 背光 `brightness=0` + `bl_power=4`（本机 X/DRM 环境下 fb `blank` 无效）。
> 详见 `off-time-control.md`。

### 5.3 `opt_screen_theme`（切换彩屏主题）

```
GET /rest/1.1/config?access_token=<TOKEN>&action=opt_screen_theme&opt=1&id=<主题id>&cmdfrom=devsub
```

| 参数 | 取值 | 语义 |
| --- | --- | --- |
| `opt` | `1` | **固定值 1**（源码中无其它取值） |
| `id` | int | 主题 id；`-1` = 默认主题（`DEFAULT_THEME_ID`） |

**响应**：任意 JSON 对象（客户端附加 `costTime` 后直接 resolve，**不校验 `code`**）。

**调用时机**：`useTheme(id)` 里当主题 id 变化（`currentThemeId !== id`）且
`u === true` 时调用；即**用户主动切换彩屏主题**时通知设备侧。

> ★ 主题资源本身走 `/screen/` 目录 + 客户端本地主题列表（`themeListConfig.inUse`），
> `opt_screen_theme` 只负责**通知设备「当前生效的主题 id」**。设备侧可根据该 id
> 播放对应彩屏内容；服务端无该 action 时返回 `{"code": 0}` 亦不影响客户端切主题。
>
> ★ 事件侧另有 `themeCheck`（事件码 `2096`，`subinfo.theme.id`）用于设备**主动**
> 反向通知（见 §6.3），与本接口方向相反。

---

## 6. 长连接（流式）规范

两条长连接均使用 `got.stream()`，`Connection: keep-alive`，响应为 **multipart 分帧**。

### 6.1 分帧格式（StreamDataParser）

```
--myboundary\r\nContent-type: application/json\r\nContent-Length: <n>\r\n\r\n<n 字节 JSON 体>
```

正则：`/--myboundary\r\nContent-type: application\/json\r\nContent-Length: (\d+)\r\n\r\n/`

- 按 `Content-Length` 切分 JSON 帧，逐帧 `JSON.parse` 回调
- 首帧不含 boundary 会触发 `onerror`

### 6.2 设备状态长连接

```
GET /rest/1.2/config?access_token=…&action=get_hw_resource_info&cmdfrom=devsub
```

- 心跳：**60 秒**无数据则销毁重连
- 错误重连间隔：**10 秒**，最多重试 3 次（`repeateTime < 3`）
- 帧结构：设备硬件资源快照，客户端映射为 `devSta`：

```json
{
  "code": 0,
  "cpu": "…", "memory": "…",
  "upload": "000.0KB/s", "download": "000.0KB/s",
  "fanmode": 5, "cpu_temp": 0, "temperature": 0
}
```

> ★ 帧内字段名是**全小写 `fanmode`**（客户端映射成 `Fanmode`）。
> 取值必须是 `5/2/3/4`（与 UI `FAN_LIST[].id` **严格相等**匹配），
> 否则 UI 不回显档位。中间件已改为动态返回（见 `fan-control.md` §5.3）。

- 磁盘明细字段（`diskList[]`）：`port`、`disk_state`（`sleep`=休眠）、`smart_status`（`warning`/`error`）、`disk_cap`、`disk_form`（`SSD`/`HDD`）

### 6.3 事件长连接

```
GET /rest/1.0/event?access_token=…&action=get_event&cmdfrom=devsub
```

帧体 `{ code: <PowerStatusCode>, brief?: <string> }`，`code` 映射表：

| code        | 事件                 | 主进程 IPC                               |
| ----------- | ------------------ | ------------------------------------- |
| 2021        | 服务器拉取日志            | → `UploadLog`                         |
| 2042        | 关机倒计时开始            | —                                     |
| 2043        | 手动关机               | `close`                               |
| 2044        | 自动关机               | `close`                               |
| 2045        | 取消关机               | —                                     |
| 2079 / 2080 | 磁盘扩容开始 / 结束        | `expandStart` / `expandend`           |
| 2081        | 设备升级               | `update`                              |
| 2082        | 设备重启               | `reStart`                             |
| 2084 / 2085 | 创建空间开始 / 完成        | `createSpaceStart` / `createSpaceend` |
| 2089 / 2090 | 移机开始 / 结束          | `devMoveStart` / `devMoveEnd`         |
| 2091        | 创建空间失败             | `createSpacefaild`                    |
| 2094 / 2095 | 网口接入 / 断开          | `netConnect` / `netDisconnect`        |
| 2096        | 主题检查               | `themeCheck`                          |
| 2100-2103   | 磁盘/USB-C/USB/SD 插入 | `usbConnect` 等                        |
| 2105-2107   | 磁盘/USB-C/USB/SD 拔出 | `usbDisconnect` 等                     |
| 2510 / 2511 | 亮屏 / 息屏            | `screenLighton` / `screenturnoff`     |
| 2521        | 用户解绑               | `devUnbind`                           |
| 2530 / 2531 | HDMI 插入 / 拔出       | `hdmiConnect` / `hdmiDisconnect`      |
| 2541        | 设备重置               | `devReset`                            |
| -1004       | token 有效心跳         | 忽略                                    |

---

## 7. 非 REST 旁路（屏幕与 LED 相关）

| 功能     | 实现                                                           | 说明                                  |
| ------ | ------------------------------------------------------------ | ----------------------------------- |
| 屏幕亮度写入 | `echo <v> > /sys/class/backlight/intel_backlight/brightness` | v = `96000 * percent / 100`，直接 exec（见 §5.1） |
| 亮度读取   | 读取同路径（范围 0–96000）                                            | 换算 `value * 100 / 96000`            |
| 分辨率/双屏 | `xrandr`（主进程）                                                | `HDMI1` / `DSI1`                    |
| 亮度接口   | `set_brightness?mode=1`                                      | 与 sysfs 写入并存，双通道                    |
| 灯控硬件   | I2C `/dev/i2c-N` 从设备 `0x26`（寄存器 `0x90/0x92/0x93/0x94`）      | 见 §4.7 与 `led-control.md`           |

---

## 8. 错误码（HSStatusCode 摘录）

### 成功 / 通用

| 值   | 名                      | 含义     |
| --- | ---------------------- | ------ |
| 0   | OptSuccess             | 成功     |
| -1  | OptFailure             | 失败     |
| 200 | HttpSuccess            | —      |
| 101 | ErrorParam             | 参数错误   |
| 102 | DevUnLink              | 设备未连接  |
| 300 | ServerSessionIdInvalid | 会话失效   |
| 301 | LocalOffline           | 本地离线   |
| 302 | DeviceNetworkErr       | 设备网络错误 |
| 401 | ProcessNotExist        | 进程不存在  |
| 500 | ServerSystemError      | 服务器错误  |
| 513 | DevRegistError         | 设备注册失败 |

### 鉴权 / 绑定

| 值     | 名                   | 含义           |
| ----- | ------------------- | ------------ |
| -1004 | TokenInvalid        | token 失效     |
| -1100 | UnactiveDev         | 设备未绑定        |
| -1103 | PasswordError       | 密码错误         |
| -1104 | DevLock             | 设备锁定         |
| -1105 | ServerTokenInvalid  | 服务端 token 失效 |
| -1111 | Unuser              | 用户不存在        |
| 407   | DevNotregistered    | 未注册          |
| 608   | DevUnbound          | 已解绑          |
| 610   | NOT_SET_SECRET_CODE | 未设置密钥        |
| 611   | PSWD_NOT_BE_SAME    | 密码不一致        |

### 磁盘 / 文件

| 值     | 名                              | 含义      |
| ----- | ------------------------------ | ------- |
| -2004 | ErrorAction                    | 动作错误    |
| -2005 | UrlPathNotExist                | 路径不存在   |
| -2009 | FileNoExist / PathFileNotExist | 文件不存在   |
| -2040 | DiskSpaceFull                  | 磁盘满     |
| -3008 | LocalFileNotExist              | 本地文件不存在 |
| -3009 | DeviceFileNotExist             | 设备文件不存在 |
| -5201 | DiskAlreadyNotExist            | 磁盘不存在   |
| -5202 | DiskIsFormatting               | 磁盘格式化中  |
| -5205 | DiskFileSysError               | 文件系统错误  |
| 2059  | CopyProQueryFail               | 复制查询失败  |


### 传输 / 同步

| 值             | 名                                 | 含义         |
| ------------- | --------------------------------- | ---------- |
| -4001 ~ -4007 | Transmit*Reset/Cancel/Pause       | 传输重置/取消/暂停 |
| -4055         | LocalDiskFull                     | 本地磁盘满      |
| -6116         | ExceedSyncLimit / CreateFileError | 超同步上限      |
| -9002         | ResponseNoBody                    | 响应无 body   |
| -1000 ~ -1003 | SyncDownload*Failed               | 同步下载失败     |

### 电源 / 系统

| 值             | 名                                            | 含义               |
| ------------- | -------------------------------------------- | ---------------- |
| 2042          | StartTimer                                   | 关机倒计时            |
| 2043 / 2044   | Manually/AutomaPowerOff                      | 手动/自动关机          |
| 2045          | CancelShutDown                               | 取消关机             |
| 2046          | EventOver                                    | 事件结束             |
| 2047 / 2048   | AutoPowerOffSuccess / NoSurportWakeup        | 自动关机成功 / 不支持唤醒   |
| 2049-2051     | ManuallyPowerOff*                            | 手动关机成功/失败        |
| 2056-2058     | DevPowerOff / DevSystemError / UuidRctIsNull | 设备关机/系统错误/UUID 空 |
| -2100 / -2101 | UpdateIntelnetFaild / YsFaild                | 升级网络失败 / 萤石失败    |
| -2099 / -2250 | Update2099 / Update2250                      | 无新版本             |
| -3            | SelectSystemFolder                           | 选择系统目录           |
| -2605         | CGICannotUse                                 | CGI 不可用          |
| -8001         | SystemSleep                                  | 系统休眠             |
| -2            | DevWakeuping                                 | 唤醒中              |

### 分享 / 权限

| 值                              | 名                     |
| ------------------------------ | --------------------- |
| -1005                          | PermissionNotmatch    |
| -1118                          | NoDiskAuthority       |
| -1110                          | NoUserAccess          |
| 405 / 406 / 1405 / 1407 / 1600 | Share*Fail            |
| -2971 ~ -2975                  | ShareDir/Authority 相关 |
| -2950                          | CreatShareFlieFail    |

---

## 9. 复刻 / 对接要点

1. **Token 是唯一门槛**：`md5("HISTORLOCALREQ"+ts)+":"+ts+":100000"`，无需密钥交换，可直接复刻。
2. **服务须监听 127.0.0.1:20443 且为 HTTPS**，客户端强制 `rejectUnauthorized:false`，自签证书即可。
3. **一切走 GET**，除 `set_atmo_color` 为 POST JSON。
4. **两条长连接**必须实现 multipart `--myboundary` 分帧协议，否则设备状态/事件推送收不到。
5. 亮度同时存在 REST（`set_brightness`）与 sysfs 两条路径，缺失任一路可能表现不一致。
6. `cmdfrom=devsub` 建议原样保留，服务端可能据此区分调用来源。
7. **LED 三个写接口必须落地到硬件且互不越权**：开关只由 `set_atmo_light` 决定，
   `set_atmo_color` 只改颜色、**绝不改开关**（否则「点颜色先关灯」）。
8. **`get_off_time` 返回标量 `xp_time`**，不是数组；`get_*` 系列读接口应反映**硬件真值**优先。
9. **屏幕熄屏动作由服务端承担**（背光通道优先），客户端只发偏好，不做黑屏。

---

## 10. 风险提示

- 本规范由**打包产物静态逆向**得出，字段名与取值范围（如 `power_mode`、`fan_mode`）来自调用侧推断，未经真机验证，实际以设备响应为准。
  > 例外 1：`fan_mode` 已由调用侧代码实测确认（`5/2/3/4`），详见 `fan-control.md`。
  > 例外 2：LED I2C 协议（`0x26` / `0x90/0x92/0x93/0x94`）与背光熄灭通道已由真机实测确认，详见 `led-control.md`、`off-time-control.md`。
- `secret`（云接口用）与 `access_token`（本地）均为 **md5 弱校验 + 时间戳**，无签名防重放，属内部局域网信任模型，**不应暴露到公网**。
- 服务端可能与固件版本绑定（`minversion: 20241107`），跨版本字段可能有差异。
- `set_flag` 在 `set_brightness` 中的 `=1` 分支**源码中从未使用**，语义未知，不要臆测。
- LED 枚举 `1002`（蓝）在客户端源码里被编码为 `RGB(30,180,25)`（实为绿色），
  属**源码 bug**，**必须原样透传**，否则屏幕显示与实际灯光不一致（详见 `led-control.md`）。

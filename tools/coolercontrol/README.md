# tools/coolercontrol —— 风扇 4 档落地到 CoolerControl

把海康小彩屏（HS-AFS-R1）界面上的 **智能 / 低速 / 标准 / 强劲** 四档，
映射成 [CoolerControl](https://gitlab.com/coolercontrol/coolercontrol) 的
配置文件（`config.toml` + `modes.json`），从而在飞牛OS 上真正驱动风扇转速。

## 档位映射

本机 **it8613 超级 I/O 暴露两路 PWM**，采用**双风扇**方案：
两路各自按自己的温度曲线独立调速，档位由中间件统一切换。

| 通道 | 风扇 | 温度源 | 智能档 Profile |
| --- | --- | --- | --- |
| `pwm1` | CPU 风扇 | CPU 核心（i3-N305） | `hs-cpu-graph` |
| `pwm2` | 机箱风扇 | 核显 i915 | `hs-case-graph` |

| 界面档位 | `fan_mode` | CoolerControl Mode | 落地形态（两路） |
|---|---|---|---|
| 智能 | `5` | `hs-smart` | 两条 Graph 曲线 |
| 低速 | `2` | `hs-low` | Fixed 25% |
| 标准 | `3` | `hs-standard` | Fixed 55% |
| 强劲 | `4` | `hs-high` | Fixed 100% |

> 探测不到温度源时自动降级：CPU 侧 → `hs-smart-fallback`（Fixed 35%）、
> 机箱侧 → `hs-case-fallback`（Fixed 40%）；由 `render-config.py` 自动处理。
> 单风扇机器用 `--single-channel`。

## 数据流

```
界面点击「强劲」
   │  GET /rest/1.1/config?action=set_hardware&fan_mode=4&cmdfrom=devsub
   ▼
middleware.py  resolve_set_hardware()
   │  ctx["fan"].update(4)   ← 保存 /var/lib/hiksemi-sub/fan.json
   ▼
apply_to_external_app()
   │  HIKSEMI_FAN_APPLY_CMD="/screen/tools/coolercontrol/apply-mode.sh {mode}"
   ▼
apply-mode.sh 4
   │  ① modes.json: current_active_mode = "hs-high"（原子写）
   │  ② 重启 coolercontrold（systemd 或 docker）
   ▼
CoolerControl daemon 启动 → activate_mode("hs-high")
   │  把 Mode 的 Setting 写回 config.toml 的 [device-settings] 并应用引擎
   ▼
风扇转速 = 100%
```

## 文件清单

| 文件 | 作用 |
|---|---|
| `config.toml` | **模板**。Profiles + Functions + device-settings + settings，含 `@DEVICE_UID@` 等占位符 |
| `modes.json` | **模板**。4 个 Mode 快照（均双通道），含占位符 |
| `render-config.py` | 占位符渲染 + TOML/JSON 契约自校验 + 原子写 + 备份；支持双风扇/单风扇/降级 |
| `apply-mode.sh` | **中间件调用的落地脚本**。`apply-mode.sh {5\|2\|3\|4}` |
| `probe-coolercontrol.sh` | 只读探测真机：安装形态、端口、真实 device_uid / **双路 pwm 通道** / CPU 与机箱温度源 |
| `README.md` | 本文档 |

## 快速开始

```bash
# ── 1. 部署文件到 /screen ──────────────────────────────────
#    ★ 推荐懒人路线：sudo bash install-autostart.sh --fan-coolercontrol on
#      （自动部署本目录 + 注入中间件环境变量 + 校验，无需手工敲下面三行）
sudo mkdir -p /screen/tools/coolercontrol
sudo cp -rn tools/coolercontrol/* /screen/tools/coolercontrol/
sudo chmod +x /screen/tools/coolercontrol/*.sh /screen/tools/coolercontrol/*.py

# ── 2. 探测真机，拿 device_uid / 双路通道 / 温度源 ─────────
sudo /screen/tools/coolercontrol/probe-coolercontrol.sh

# ── 3. 渲染配置到 CoolerControl 配置目录 ───────────────────
#    ★ 推荐：机型预设一键渲染（已内置 HS-AFS-R1 实测参数）
sudo /screen/tools/coolercontrol/render-config.py --host hs-afs-r1 \
      --out-dir /etc/coolercontrol
#    列出内置机型：render-config.py --list-hosts
#    ⇒ 默认 = 双风扇：pwm1→CPU 曲线、pwm2→机箱(i915)曲线

# 渲染开关：
#   --single-channel    单风扇机器（只渲染一路）
#   --no-case-graph     双通道但机箱侧走 Fixed 40%（不用曲线）
#   --no-graph          两路智能档都走 Fixed 兜底

# 等价手工写法（其它机型参照）：
# sudo /screen/tools/coolercontrol/render-config.py \
#       --device-uid <64位hex> \
#       --cpu-channel pwm1 --case-channel pwm2 \
#       --cpu-temp-name "Intel(R) Core(TM) i3-N305" \
#       --cpu-temp-device-uid <64位hex> \
#       --case-temp-name "temp1" \
#       --case-temp-device-uid <64位hex> \
#       --out-dir /etc/coolercontrol

# ── 4. 手工验证一次 ────────────────────────────────────────
sudo /screen/tools/coolercontrol/apply-mode.sh 4      # 切强劲
sudo /screen/tools/coolercontrol/apply-mode.sh --status
sudo /screen/tools/coolercontrol/apply-mode.sh 5      # 回智能

# ── 5. 接通中间件（改 systemd 环境变量）────────────────────
#    ★ 推荐：走内置适配器（安装器 --fan-coolercontrol on 会写入这一行）
sudo systemctl edit hiksemi-sub
#   [Service]
#   Environment=HIKSEMI_FAN_COOLERCONTROL=1
#   （可选，路径已能自动定位，仅在自定义布局时才需要）
#   Environment=HIKSEMI_FAN_COOLERCONTROL_SCRIPT=/screen/tools/coolercontrol/apply-mode.sh
sudo systemctl daemon-reload && sudo systemctl restart hiksemi-sub

# 确认真的生效（service 里有被注释的示例行，grep 时要匹配行首 Environment=）：
grep '^Environment=.*HIKSEMI_FAN_COOLERCONTROL' /etc/systemd/system/hiksemi-sub.service
sudo grep -i 'fan' /var/log/hiksemi-sub/kiosk.log
```

> 也可以反过来用 `HIKSEMI_FAN_APPLY_CMD="/screen/tools/coolercontrol/apply-mode.sh {mode}"`
> —— 效果等价（① 优先级高于 ②），但通常没必要，内置适配器会把路径和参数都处理好。

## ★ 为什么必须先用 probe 拿 device_uid

`device_uid` 是 CoolerControl 依据设备属性算出的 **64 位 sha256**，
`channel` 名来自 hwmon 的 `pwmN`/`fanN` —— 两者都无法凭空生成，
**填错不会报错，只是静默不生效**，所以本目录采用「模板 + 渲染」而非写死。

最省事的获取方式：先在 CoolerControl Web UI（`https://<NAS>:11987`）
里随便给风扇设一次转速，让 daemon 把 `[device-settings]` 段写出来，
再跑 `probe-coolercontrol.sh` 第 3 节就能直接读到真实 uid 与 channel。

## 契约要点（源码级已确认，踩坑记录）

- `[device-settings.<uid>]` 的通道值**必须是内联表** `{ ... }`，
  写成 `[device-settings.uid.pwm1]` 子表 daemon 读不到。
- **双通道写在同一个内联表**里，用逗号分隔：
  `"pwm1" = { ... }, "pwm2" = { ... }`。
  ⚠ 不能写成两个独立的 `[device-settings.<uid>]` 段 —— TOML 会报重复表。
- `speed_fixed` 与 `profile_uid` **互斥**，设置其一会清空另一个。
- `temp_source` 是**内联表** `{ temp_name, device_uid }`；
  `temp_name` 是**该设备内**的传感器名（两路曲线可能都叫 `"temp1"`，
  但 device_uid 不同，因此是各自设备内的 temp1）。
- `temp_min` / `temp_max` 必须是**浮点**（写 `30.0` 而非 `30`）。
- **Graph 曲线约束**：`speed_profile` 的温度点必须**单调递增**，
  **首点温度必须等于 `temp_min`**（daemon 以 temp_min 为曲线左端）；
  占空比是 0–100 整数。`render-config.py` 会强制校验这三条。
- Graph profile 的 `function_uid` 必须指向**真实存在的** `[[functions]]`，
  否则 daemon 拒绝应用（`render-config.py` 会校验）。
- `bypass_min_at_extremes = true` 是「让曲线首点 0% 真正停转」的关键：
  否则温度低于曲线最左点时会被抬到 `duty_minimum`。
- Functions 的键名一律**蛇形**：`duty_minimum` / `duty_maximum` /
  `step_size_*` / `response_delay`（不是连字符）。
- `ExponentialMovingAvg` 类型已在 4.4.0 移除，读到会静默转 `Identity`。
- 保留 uid：Profile `"0"` = Unmanaged，Function `"0"` = Default Function；
  缺失时 daemon 会自动补写。
- `activate_mode()` 会把 Mode 的 Setting 写回 `config.toml` 并对
  **Mode 未覆盖的 channel 做 Reset**，因此改 `modes.json` 的
  `current_active_mode` 后重启 daemon 即生效。
- `apply_on_boot = true` 时，daemon 启动会重新应用 `[device-settings]`。

## 注意事项

- **重启代价**：切换档位会重启 daemon（< 2s），风扇会短暂回到开机默认后
  立刻应用新档。若不能接受，可改用 CoolerControl REST API（需处理 token）。
- **多风扇（本机默认双风扇）**：本机 it8613 有 `pwm1`(CPU) + `pwm2`(机箱)，
  两条曲线各管一路。渲染时用 `--cpu-channel` / `--case-channel` 指定；
  **每个 Mode 必须覆盖全部通道**，否则未覆盖的会被 Reset（渲染器会强制校验）。
  只有一路 PWM 的机器加 `--single-channel`，否则会多写一路不存在的 pwm2。
- **接线方向**：无法从 sysfs 判断哪个风扇接了哪路，默认按「pwm1=CPU、pwm2=机箱」
  推断。若实际相反，跑渲染时交换 `--cpu-channel` / `--case-channel` 即可
  （两条曲线的温度源与阈值是分开的，交换通道号不影响曲线本身）。
- **降级链路**：未安装 CoolerControl 时，`apply-mode.sh` 会告警并以
  非零退出，中间件会退回「仅保存 + 回显」，不影响 REST 响应。

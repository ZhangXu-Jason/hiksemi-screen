# 风扇 4 档 → CoolerControl 落地

> 关联文档：`fan-control.md`（逆向与链路）、`deploy-guide.md` §5（部署与启动方式）、
> `local-rest-api.md`（REST 契约）。
> 配套代码：`tools/coolercontrol/`（配置模板 + 脚本）、`tools/middleware/middleware.py`
> 的 `apply_to_external_app()`。

## 1. 为什么选 CoolerControl

飞牛OS（fnOS）自带的 `pwm-fancontrol.service` 依赖不存在的 `/boot/board.json`，
**是空跑**（见 `fan-control.md` §4）；而 fnOS 又没有暴露风扇控制 API。
CoolerControl 是 Linux 上成熟的风扇控制守护进程：

- 通过 **hwmon** 直接读写 `pwm*` 通道，兼容绝大多数主板；
- 配置全部落在**文本文件**（`config.toml` / `modes.json`），可版本化、可脚本生成；
- 自带 **Mode（模式）** 概念 —— 一组「设备通道 → 转速」的具名快照，
  天然对应界面上的「智能 / 低速 / 标准 / 强劲」四档；
- 支持 Docker 与原生两种部署，fnOS（Debian 12 派生）两者都可用。

⇒ 因此**不需要给中间件加任何风扇控制代码**，只需把档位翻译成
CoolerControl 的配置文件即可。

## 2. 档位映射

本机 **it8613 超级 I/O 暴露两路可控 PWM**，因此采用**双风扇**方案：
两路各自按自己的温度曲线独立调速，但**档位（Mode）由中间件统一切换**。

| 通道 | 风扇 | 温度源 | 对应 Profile |
| --- | --- | --- | --- |
| `pwm1` | CPU 风扇 | CPU 核心（Intel Core i3-N305） | 智能→`hs-cpu-graph` |
| `pwm2` | 机箱风扇 | 核显 i915（机箱热源，转码时升温） | 智能→`hs-case-graph` |

| 界面档位 | `fan_mode` | CoolerControl Mode | Profile（两路） | 落地形态 |
| --- | --- | --- | --- | --- |
| 智能 | `5` | `hs-smart` | `hs-cpu-graph` + `hs-case-graph` | 两条 Graph 曲线 |
| 低速 | `2` | `hs-low` | `hs-low`（两路共用） | Fixed **25%** |
| 标准 | `3` | `hs-standard` | `hs-standard`（两路共用） | Fixed **55%** |
| 强劲 | `4` | `hs-high` | `hs-high`（两路共用） | Fixed **100%** |

> ★ **每个 Mode 必须同时覆盖 pwm1 与 pwm2**。`activate_mode()` 会把
> Mode **未覆盖**的通道 Reset 掉，只写一路会导致另一路转速失控。`render-config.py`
> 的 `validate_modes()` 会强制校验这一点。

> **智能档曲线设计**（两条曲线都挂 Function `hs-fan-fn`）：
> - **CPU 曲线 `hs-cpu-graph`**（源=i3-N305 核心，temp_min 30 / temp_max 95）：
>   ≤45℃ 恒定 22%（低载静音底线）→ 60℃ 40% → 70℃ 65% → 80℃ 88% → ≥88℃ 100%。
>   低载段做成**水平平台**（30→45℃ 都是 22%），避免温度小幅抖动引起转速起伏；
>   88℃ 拉满给 N305 的 TJmax（≈105℃）留足余量。
> - **机箱曲线 `hs-case-graph`**（源=i915 核显，temp_min 0 / temp_max 100）：
>   ≤42℃ **0%（停转）** → 47℃ 40% → ≥55℃ 100%。
>   机箱风扇服务的是 6 块机械大盘与整体风道，对高温最敏感，所以**起调点比 CPU 曲线低、
>   上限直接打到 100%**；核显空闲时停转以彻底消除噪声。
> - **整形函数 `hs-fan-fn`**（Standard）：`bypass_min_at_extremes = true` 是关键 ——
>   它允许曲线最左端**不抬到 `duty_minimum`**，这样 pwm2 首点的 0% 才能真正停转。
>   `step_size_max_*` + `response_delay = 2.0` 限制单次调整幅度，防转速抖动。
>
> **兜底**：探测不到温度源时自动降级 —— CPU 侧 → `hs-smart-fallback`（Fixed 35%）、
> 机箱侧 → `hs-case-fallback`（Fixed 40%）；两路都探测不到则整体退化为 Fixed。
> 由 `render-config.py` 自动处理（见 §5 的 `--no-graph` / `--no-case-graph` / `--single-channel`）。

## 3. 数据流

```
界面点「强劲」
   │  GET /rest/1.1/config?action=set_hardware&fan_mode=4&cmdfrom=devsub
   ▼
middleware.py  resolve_set_hardware()          ← 只认 fan_mode，返回 {"code":0}
   │  ctx["fan"].update(4)                     ← 状态落 /var/lib/hiksemi-sub/fan.json
   ▼
apply_to_external_app(prev, state)             ← 优先级：CMD > CoolerControl > sysfs PWM
   │  环境变量 HIKSEMI_FAN_COOLERCONTROL=1
   │  执行 coolercontrol/apply-mode.sh 4
   ▼
apply-mode.sh 4
   │  ① 原子改 modes.json: current_active_mode = "hs-high"
   │      （同时把旧值写进 previous_active_mode）
   │  ② 重启 coolercontrold（systemd 或 docker，自动探测）
   ▼
CoolerControl daemon 启动
   │  读 modes.json → activate_mode("hs-high")
   │  把该 Mode 的 Setting 写回 config.toml 的 [device-settings] 并应用引擎
   ▼
风扇 PWM = 100%（新档位在 < 2s 内生效）
```

**回读**：档位回显走的是状态帧 `fanmode` 字段，由 `FanState.mode()` 提供，
与 CoolerControl 无关 —— 也就是说**即使 CoolerControl 未安装**，
界面上「看起来」也能正常切换、只是硬件不动（启动日志会明确标注 ⚠）。

## 4. 文件清单

| 文件 | 角色 | 是否含占位符 |
| --- | --- | --- |
| `config.toml` | **模板**：`[devices]` / `[device-settings]` / `[[profiles]]` / `[[functions]]` / `[settings]` | 是（`@DEVICE_UID@`、`@CPU_CHANNEL@`、`@CASE_CHANNEL@`、`@CPU_TEMP_*@`、`@CASE_TEMP_*@`、`@CPU_SMART_PROFILE@`、`@CASE_SMART_PROFILE@`） |
| `modes.json` | **模板**：4 个 Mode 快照（均**双通道**）+ `order` + `current_active_mode` | 是 |
| `render-config.py` | 渲染占位符 + 契约自校验（TOML/JSON）+ 原子写 + 自动备份；支持双风扇/单风扇/降级 | — |
| `apply-mode.sh` | **中间件实际调用的入口**：`apply-mode.sh {5\|2\|3\|4}` | — |
| `probe-coolercontrol.sh` | 只读探测真机：安装形态 / 端口 / 真实 uid / **双路 pwm 通道** / CPU 与机箱温度源 | — |

> **为什么用「模板 + 渲染」而不是直接写死**：
> `device_uid` 是 CoolerControl 依据设备 sysfs 属性算出的 **64 位 sha256**，
> `channel` 名来自 hwmon 的 `pwmN`/`fanN`。二者**无法凭空生成**，
> 且填错时 daemon **不报错、只是静默不生效** —— 这是本项目最容易踩的坑。
> 详见 §6。

## 5. 部署步骤

> ★ **懒人路线**：第 1 步和第 5 步已被安装器接管 —— 只要机器上装了 CoolerControl，
> 执行 `sudo bash install-autostart.sh` 就会**自动**把本目录部署到
> `$TOOLS/coolercontrol/` 并注入 `HIKSEMI_FAN_COOLERCONTROL=1`（`--fan-coolercontrol off`
> 可关闭，`--dry-run` 可先预演）。你只需要做第 2~4 步：探测 → 渲染 → 验证。
> 下面列出手工流程以便脱离安装器单独使用。

```bash
# ── 0. 确认 CoolerControl 已安装并跑起来 ──────────────────
# Docker：
docker run -d --name coolercontrol --privileged \
  -p 11987:11987 -v /etc/coolercontrol:/etc/coolercontrol \
  ghcr.io/guniv/coolercontrol-docker
# 或原生：直接 systemctl enable --now coolercontrold

# ── 1. 部署本目录到 /screen/tools/coolercontrol ───────────
#    ★ 推荐：sudo bash install-autostart.sh --fan-coolercontrol on
sudo mkdir -p /screen/tools/coolercontrol
sudo cp -rn tools/coolercontrol/* /screen/tools/coolercontrol/
sudo chmod +x /screen/tools/coolercontrol/*.sh /screen/tools/coolercontrol/*.py

# ── 2. 探测真机 ───────────────────────────────────────────
sudo /screen/tools/coolercontrol/probe-coolercontrol.sh
#    输出第 3 节给你 device_uid；第 5 节列出所有 pwm 通道（双风扇取 pwm1+pwm2）；
#    第 6 节给你 CPU 温度传感器名。

# ── 3. 渲染配置 ───────────────────────────────────────────
# ★ 推荐：用机型预设一键渲染（内置 HS-AFS-R1 的实测 uid/双通道/双温度源）
sudo /screen/tools/coolercontrol/render-config.py --host hs-afs-r1 \
      --out-dir /etc/coolercontrol
#    查看内置机型：render-config.py --list-hosts
#    ⇒ 默认渲染为【双风扇】：pwm1→CPU 曲线、pwm2→机箱(i915)曲线

# 渲染模式的几种开关（按需）：
#   --single-channel     单风扇机器：只渲染一路（默认把 case 通道当成 pwm2，
#                        单风扇时必须加，否则会多写一路不存在的 pwm2）
#   --no-case-graph      保留双通道但机箱侧不用曲线（pwm2 智能档走 Fixed 40%）
#   --no-graph           两路智能档都走 Fixed 兜底（测试/无温度传感器时用）

# 等价的手工写法（预设的数字来源，便于其它机型参照）：
#     风扇设备 it8613      = 71b063a3…8742
#     CPU 温度 i3-N305     = 336d28e0…c7bd
#     机箱温度 i915        = 58cea05e…4818
#     CPU 通道（hwmon）    = pwm1        机箱通道 = pwm2
# sudo /screen/tools/coolercontrol/render-config.py \
#       --device-uid 71b063a3…8742 \
#       --cpu-channel pwm1 --case-channel pwm2 \
#       --cpu-temp-name "Intel(R) Core(TM) i3-N305" \
#       --cpu-temp-device-uid 336d28e0…c7bd \
#       --case-temp-name "temp1" \
#       --case-temp-device-uid 58cea05e…4818 \
#       --out-dir /etc/coolercontrol
#    原文件会自动备份为 *.bak-render
#    ★ 其它机型：先用 probe-coolercontrol.sh 取真实 uid/channel 再替换
#    ★ 风扇设备认准 hwmon 的 name = it8613 / nct67xx 这类『超级 I/O』芯片；
#      CPU 温度认准 name = coretemp / Intel(R) Core(TM) … / acpitz
#    ★ 若两路风扇接线相反（pwm1 接的是机箱扇），用 --cpu-channel pwm2 --case-channel pwm1 交换。
#    ★ 渲染后务必核对：两路 pwm 是否真的在动（见第 4 步）。

# ── 4. 手工验证（重启 daemon 后风扇应立刻变档）────────────
sudo /screen/tools/coolercontrol/apply-mode.sh 4     # 强劲（风扇应明显加速）
sudo /screen/tools/coolercontrol/apply-mode.sh --status
sudo /screen/tools/coolercontrol/apply-mode.sh 5     # 回智能

# ── 5. 接通中间件 ─────────────────────────────────────────
#    ★ 推荐：sudo bash install-autostart.sh --fan-coolercontrol on
#      （会自动写入下面这行并校验；安装器输出的「实际生效配置」里能看到）
sudo systemctl edit hiksemi-sub
#   [Service]
#   Environment=HIKSEMI_FAN_COOLERCONTROL=1 HIKSEMI_FAN_COOLERCONTROL_SCRIPT=/screen/tools/coolercontrol/apply-mode.sh
sudo systemctl daemon-reload && sudo systemctl restart hiksemi-sub
```

> 生效性确认（service 模板里有**被注释的示例行**，直接 grep 会误判）：
> ```bash
> grep '^Environment=.*HIKSEMI_FAN_COOLERCONTROL' /etc/systemd/system/hiksemi-sub.service
> sudo grep -i 'fan' /var/log/hiksemi-sub/kiosk.log    # kiosk 启动时会上报三种通道状态
> ```


启动后看 `hiksemi-sub` 日志的「功能就绪摘要」：

```
风扇档位   : ✅ 可落地（CoolerControl），当前 5=智能
```

若显示 `⚠ 仅保存/回显（外部通道未配置）`，说明环境变量没生效或
`apply-mode.sh` 没找到（日志里会打印搜索过的候选路径）。

## 6. ★ 契约要点（源码级已确认的踩坑记录）

以下每一条都可能是「配置写了但不生效」的原因：

| # | 要点 | 说明 |
| --- | --- | --- |
| 1 | `[device-settings.<uid>]` 通道值**必须是内联表** | `pwm1 = { profile_uid = "..." }` ✅<br>`[device-settings.<uid>.pwm1]` 子表 ❌（daemon 读不到） |
| 2 | `speed_fixed` 与 `profile_uid` **互斥** | 设置其一会自动清空另一个；多键共存时优先级 `SpeedFixed → Profile → Lighting → Lcd` |
| 3 | `temp_source` 是**内联表** | `{ temp_name = "...", device_uid = "..." }`，两个字段都必需 |
| 4 | `temp_min` / `temp_max` 必须是**浮点** | 写 `30.0`，写 `30` 会被判类型错 |
| 5 | Functions 键名一律**蛇形** | `duty_minimum` / `duty_maximum` / `step_size_min_decreasing` / `response_delay`，**不是连字符** |
| 6 | `ExponentialMovingAvg` 已移除 | 4.4.0 起读到会**静默转成** `Identity`，不要再用 |
| 7 | 保留 uid | Profile `"0"` = `Unmanaged`；Function `"0"` = `Default Function`；缺失时 daemon 自动补写 |
| 8 | `activate_mode()` 的副作用 | 会把 Mode 的 Setting **写回** `config.toml`，并对 **Mode 未覆盖的 channel 做 Reset** |
| 9 | `apply_on_boot = true` | daemon 启动时重新应用 `[device-settings]` 已存设置 |
| 10 | ★★ **`[legacy690]` 段必须存在（可为空表）** | daemon 启动 `check_readable()` 第一个调用 `legacy690_ids()` 就**硬索引** `["legacy690"]`——toml_edit 对不存在的键直接 panic `index not found`（真机实测：`config.rs:480` 启动即崩）。官方默认配置含此空表（Asetek 690LC 兼容开关），普通设备留空即可。**daemon 认定完整段集 = devices / legacy690 / device-settings / profiles / functions / settings**，模板与渲染校验均已锁定 |

### 6.1 反序列化方式（为什么键是蛇形）

daemon **不使用** `serde` derive 直接反序列化 `config.toml`，
而是用 `toml_edit::DocumentMut` **手工逐键读取**，
键名即源码里的字段名（蛇形）。连字符形态只出现在**序列化**路径
（如 `serde(tag = "p_type")` 的变体名）。

### 6.2 路径常量（`paths.rs`）

| 常量 | 默认值 |
| --- | --- |
| `CONFIG_DIR` | `/etc/coolercontrol` |
| `DATA_DIR` | `/var/lib/coolercontrol` |
| `CONFIG_FILE` | `<CONFIG_DIR>/config.toml` |
| `MODE_CONFIG_FILE` | `<CONFIG_DIR>/modes.json` |
| `UI_CONFIG_FILE` | `<CONFIG_DIR>/config-ui.json` |
| `BACKUPS_DIR` | `<CONFIG_DIR>/backups` |

> 若 CoolerControl 跑在 Docker 里且**没有挂载** `/etc/coolercontrol`，
> 配置会随容器销毁而丢失 —— 务必按 §5 步骤 0 挂载。

### 6.3 `modes.json` 结构

```jsonc
{
  "modes": [
    {
      "uid": "hs-high",
      "name": "hs 强劲 100%",
      "all_device_settings": {
        "<device_uid>": {
          "pwm1": { "channel_name": "pwm1", "profile_uid": "hs-high" }
        }
      }
    }
  ],
  "order": ["hs-smart", "hs-low", "hs-standard", "hs-high"],
  "current_active_mode": "hs-smart",
  "previous_active_mode": null
}
```

`Setting` 是**平铺对象**（`#[serde(untagged)] SettingKind` + `flatten`），
即 `{ channel_name, profile_uid }` 或 `{ channel_name, speed_fixed }`
直接同级，**没有嵌套的 kind 包装**。

## 7. 为什么用「改文件 + 重启」而不是调 REST API

CoolerControl 的 REST API 需要处理 token / 会话，且路径随版本变动，
对中间件来说是额外的耦合。而：

- 切换风扇档位是**低频操作**（人工在 2.86" 小屏上点一下）；
- 改 `modes.json` + 重启是**幂等、可离线回放、可手工复现**的路径；
- daemon 启动耗时 < 2s，风扇短暂回到开机默认后立刻应用新档，体感可接受。

⇒ 因此 `apply-mode.sh` 走文件路径。若日后确实需要免重启切换，
可在此基础上补一个调 API 的分支，**中间件侧接口不需要任何改动**。

## 8. 降级与容错

| 场景 | 行为 |
| --- | --- |
| CoolerControl 未安装 / 容器名不同 | `apply-mode.sh` 告警并以非零退出 → 中间件记 ⚠ 日志，**不影响 REST 响应**（`update()` 始终返回 `(state, changed)`，resolver 恒返回 `{"code":0}`） |
| `HIKSEMI_FAN_COOLERCONTROL=1` 但 `apply-mode.sh` 不存在 | 中间件返回 `False` 并打印候选路径，**不静默当成功** |
| 显式设了 `HIKSEMI_FAN_APPLY_CMD` | **优先级最高**，CoolerControl 分支不被调用 |
| 只设了 `HIKSEMI_FAN_PWM_PATH` | 作为 CoolerControl 之后的**兜底**（低速 64 / 标准 140 / 强劲 255；智能档不写） |
| 三个都没配 | 空实现：仅保存 + 回显，不改硬件（启动日志标注 ⚠） |

## 9. 真机验证清单

**部署阶段：**

- [ ] `sudo bash install-autostart.sh --fan-coolercontrol on --dry-run` 输出的「实际生效配置」里
      能看到 `Environment=HIKSEMI_FAN_COOLERCONTROL=1 ...SCRIPT=/screen/tools/coolercontrol/apply-mode.sh`
- [ ] 正式安装后 `grep '^Environment=.*HIKSEMI_FAN_COOLERCONTROL' /etc/systemd/system/hiksemi-sub.service` 有**非注释**命中
- [ ] 安装输出**没有**警告 `config.toml 仍是模板（含 @DEVICE_UID@ 占位符）`
- [ ] `sudo grep -i 'fan' /var/log/hiksemi-sub/kiosk.log` 显示通道 ② 生效且 `apply-mode.sh` 可执行

**功能阶段：**

- [ ] `probe-coolercontrol.sh` 能打印出 64 位 `device_uid`
- [ ] `probe-coolercontrol.sh` 第 5 节能看到 `pwm1`（或实际通道名）
- [ ] `render-config.py --check` 无报错，profiles 数为 6（含兜底）
- [ ] `render-config.py --out-dir /etc/coolercontrol` 写入成功且原有文件已备份
- [ ] `apply-mode.sh 4` 后**风扇转速明显上升**（听感 / `cat /sys/class/hwmon/*/pwm1`）
- [ ] `apply-mode.sh 5` 后转速回落，且随 CPU 负载变化
- [ ] CoolerControl Web UI（`https://<NAS>:11987`）能看到 4 个 Mode
- [ ] 界面点四档 → 风扇实际变档（端到端）
- [ ] 重启 NAS 后仍保持上次档位（`apply_on_boot` 生效）

## 10. 小结

- **界面 4 档 = CoolerControl 4 个 Mode**，映射关系写在 `config.toml` / `modes.json` 里；
- 中间件**零风控代码**，只通过 `apply-mode.sh {mode}` 一个入口落地；
- 全部配置**可版本化、可脚本重建**（模板 + 渲染，杜绝写死假 uid）；
- 未安装 CoolerControl 时**安全降级**，不影响小屏其他功能；
- 唯一真机未知量是 `device_uid` / `channel`，用 `probe-coolercontrol.sh` 一次解决。

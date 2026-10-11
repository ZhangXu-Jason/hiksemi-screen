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
| `fan3` | CPU 风扇 | CPU 核心（i3-N305，**传感器名 `temp1`**） | 智能→`hs-cpu-graph` |
| `fan2` | 机箱风扇 | 核显 i915（传感器名 `temp1`） | 智能→`hs-case-graph` |

> ★★★ **通道名是 `fanN`，不是 sysfs 里的 `pwmN`！**
> CoolerControl 对 hwmon 设备一律用 `fan1…fanN` 作通道名；真机 it8613 暴露
> fan1..fan5，其中 **fan4/fan5 已在 UI 禁用**，实际用 fan3(CPU)/fan2(机箱)。
> **写错通道名与 UID 漂移症状完全一样** —— UI 里 Mode 的「设备/通道/设置」一片空白。
> 别手抄，用 `--auto-channels` / `--auto` 从真机 `[device-settings]` 段取真值。

| 界面档位 | `fan_mode` | CoolerControl Mode | Profile（两路） | 落地形态 |
| --- | --- | --- | --- | --- |
| 智能 | `5` | `hs-smart` | `hs-cpu-graph` + `hs-case-graph` | 两条 Graph 曲线 |
| 低速 | `2` | `hs-low` | `hs-low`（两路共用） | Fixed **25%** |
| 标准 | `3` | `hs-standard` | `hs-standard`（两路共用） | Fixed **55%** |
| 强劲 | `4` | `hs-high` | `hs-high`（两路共用） | Fixed **100%** |

> ★★ **Mode uid 不是写死的**：在 Web UI 里自己建的 Mode，uid 是随机 UUID，
> 档位靠 `hs-fan-mode-map.json` 绑定（可 `--bind`，也可按名字自动认领）。
> 详见 **§6.5**（屏幕与 CoolerControl 不同步）。

> ★ **每个 Mode 必须同时覆盖两个通道（fan3+fan2）**。`activate_mode()` 会把
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
>   它允许曲线最左端**不抬到 `duty_minimum`**，这样机箱曲线首点的 0% 才能真正停转。
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
| `apply-mode.sh` | **中间件实际调用的入口**：`apply-mode.sh {5\|2\|3\|4}`；另有 `--scan` / `--bind` / `--current` / `--status`（见 §6.5） | — |
| `fan-mode-map.py` | ★ 档位 ↔ Mode 映射解析器：`--scan` / `--bind` / `--resolve` / `--reverse`（**自建 Mode 也不会失去同步**） | — |
| `hs-fan-mode-map.json` | 渲染时生成的绑定文件（4 档 → `hs-*` uid），`--bind` 会改写它 | — |
| `probe-coolercontrol.sh` | 只读探测真机：安装形态 / 端口 / 真实 uid / **真实通道名 fanN** / CPU 与机箱温度源；`modes` 子命令 = 诊断「Mode 在 UI 里空白」（见 §6.4） | — |

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
#    ★ 输出 §3 给你 device_uid；§5.1 给你【真实通道名 fanN】（★ 不是 pwmN）；
#    第 6 节给你 CPU 温度传感器名。

# ── 3. 渲染配置 ───────────────────────────────────────────
# ★ 推荐：用机型预设一键渲染（内置 HS-AFS-R1 的实测 uid/双通道/双温度源）
sudo /screen/tools/coolercontrol/render-config.py --host hs-afs-r1 \
      --out-dir /etc/coolercontrol
#    查看内置机型：render-config.py --list-hosts
#    ⇒ 默认渲染为【双风扇】：fan3→CPU 曲线、fan2→机箱(i915)曲线
#
# ★ 更推荐（UID 与通道名都按真机真值对齐，不手抄）：
# sudo render-config.py --host hs-afs-r1 --auto --config-dir /etc/coolercontrol
#      --fixed-pct 30,60,100     保留手调过的三档转速（默认 25,55,100）
#      --fallback-pct 30,35      保留手调过的兜底转速（默认 35,40）

# 渲染模式的几种开关（按需）：
#   --single-channel     单风扇机器：只渲染一路
#                        （默认会写第二路，单风扇时必须加，否则多写一路不存在的通道）
#   --no-case-graph      保留双通道但机箱侧不用曲线（机箱通道智能档走 Fixed 40%）
#   --no-graph           两路智能档都走 Fixed 兜底（测试/无温度传感器时用）

# 等价的手工写法（预设的数字来源，便于其它机型参照）：
#     风扇设备 it8613      = 71b063a3…8742
#     CPU 温度 i3-N305     = 336d28e0…c7bd
#     机箱温度 i915        = 58cea05e…4818
#     CPU 通道（CoolerControl）= fan3     机箱通道 = fan2
# sudo /screen/tools/coolercontrol/render-config.py \
#       --device-uid 71b063a3…8742 \
#       --cpu-channel fan3 --case-channel fan2 \
#       --cpu-temp-name "Intel(R) Core(TM) i3-N305" \
#       --cpu-temp-device-uid 336d28e0…c7bd \
#       --case-temp-name "temp1" \
#       --case-temp-device-uid 58cea05e…4818 \
#       --out-dir /etc/coolercontrol
#    原文件会自动备份为 *.bak-render
#    ★ 其它机型：先用 probe-coolercontrol.sh 取真实 uid/channel 再替换
#    ★ 风扇设备认准 hwmon 的 name = it8613 / nct67xx 这类『超级 I/O』芯片；
#      CPU 温度认准 name = coretemp / Intel(R) Core(TM) … / acpitz
#    ★ 若两路接线相反（fan3 实际接的是机箱扇），把 --cpu-channel/--case-channel 交换即可。
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
| 1 | `[device-settings.<uid>]` 通道值**必须是内联表** | `fan3 = { profile_uid = "..." }` ✅<br>`[device-settings.<uid>.fan3]` 子表 ❌（daemon 读不到） |
| 2 | `speed_fixed` 与 `profile_uid` **互斥** | 设置其一会自动清空另一个；多键共存时优先级 `SpeedFixed → Profile → Lighting → Lcd` |
| 3 | `temp_source` 是**内联表** | `{ temp_name = "...", device_uid = "..." }`，两个字段都必需 |
| 4 | `temp_min` / `temp_max` 必须是**浮点** | 写 `30.0`，写 `30` 会被判类型错 |
| 5 | Functions 键名一律**蛇形** | `duty_minimum` / `duty_maximum` / `step_size_min_decreasing` / `response_delay`，**不是连字符** |
| 6 | `ExponentialMovingAvg` 已移除 | 4.4.0 起读到会**静默转成** `Identity`，不要再用 |
| 7 | 保留 uid | Profile `"0"` = `Unmanaged`；Function `"0"` = `Default Function`；缺失时 daemon 自动补写 |
| 8 | `activate_mode()` 的副作用 | 会把 Mode 的 Setting **写回** `config.toml`，并对 **Mode 未覆盖的 channel 做 Reset** |
| 9 | `apply_on_boot = true` | daemon 启动时重新应用 `[device-settings]` 已存设置 |
| 10 | ★★ **`[legacy690]` 段必须存在（可为空表）** | daemon 启动 `check_readable()` 第一个调用 `legacy690_ids()` 就**硬索引** `["legacy690"]`——toml_edit 对不存在的键直接 panic `index not found`（真机实测：`config.rs:480` 启动即崩）。官方默认配置含此空表（Asetek 690LC 兼容开关），普通设备留空即可。**daemon 认定完整段集 = devices / legacy690 / device-settings / profiles / functions / settings**，模板与渲染校验均已锁定 |
| 11 | ★★ **device UID 会漂移** | UID 是 daemon 按设备属性算的 sha256，**daemon 升级 / 内核 hwmon 变化都会让它变**。漂移后 `modes.json` 的 `all_device_settings` 键对不上任何已知设备 ⇒ Web UI 打开 Mode 时「设备/通道/设置」**一片空白**（JSON 语法完全正确，最容易误判成模板错）；`[device-settings]` 同样失效 ⇒ 风扇不受控。daemon 每次启动都会用真值重写 `config.toml` 的 `[devices]` 段 ⇒ 那是唯一权威来源，用 `render-config.py --auto-uid` 按设备名重新对齐即可（详见 6.4） |
| 12 | ★★★ **通道名是 `fanN`，不是 sysfs 的 `pwmN`** | CoolerControl 对 hwmon 设备一律用 `fan1…fanN` 命名通道。本机 it8613 实际可控通道 = **`fan3`(CPU) / `fan2`(机箱)**（fan4/fan5 已禁用）。**按 `pwm1/pwm2` 写死 ⇒ UI 里 Mode 内容空白，症状与 UID 漂移一模一样**（真机踩过）。通道名的唯一权威来源是 `[device-settings.<uid>]` 段，用 `--auto-channels` 自动取（详见 §2 / §6.4） |
| 13 | ★★ **`temp_name` 是传感器名，不是设备显示名** | 填 `temp1`（对应 hwmon 的 `temp1_input`），填设备名（如 `Intel(R) Core(TM) i3-N305`）会导致温度源匹配不上。两个设备的传感器可能都叫 `temp1`，靠 `device_uid` 区分 |
| 14 | ★ **Mode 的 uid 可能被换成随机 UUID** | 在 Web UI 里手工新建的 Mode，uid 是 `1a700658-…` 这种 UUID，不是 `hs-smart`。`apply-mode.sh` 找不到目标 uid 时会**按名字前缀回退匹配**（`hs 智能/低速/标准/强劲`），但推荐直接用渲染产物的语义 uid |

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
          "fan3": { "channel_name": "fan3", "profile_uid": "hs-high" }
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

### 6.4 ★ Mode 在 UI 里「设备 / 通道 / 设置」一片空白

**症状**：Mode 列表能看到（hs-smart / hs-low / hs-standard / hs-high），
点进去却看不到任何设备、通道和设置项。

**先给结论：不是模板结构问题。** 渲染产物完全符合 daemon 契约 ——

```
Mode { uid, name, all_device_settings: { <device_uid>: { <channel>: Setting } } }
Setting = { channel_name, profile_uid }        # 与 modes.rs / api/modes.rs 一致
```

空白只有两种解释（**都是「键/名对不上」，JSON 本身没错**）：

1. **`all_device_settings` 的键不是 daemon 当前认识的设备** ⇒ device UID 漂移；
2. **通道名写错** ⇒ ★★ 真机实测最多的一种：CoolerControl 对 hwmon 设备的
   通道名是 **`fanN`，不是 sysfs 里的 `pwmN`**。本机 it8613 实际是
   **`fan3`(CPU) / `fan2`(机箱)**（fan4/fan5 已在 UI 禁用）。
   UI 拿「设备表 + 通道表」做 join，两层任一对不上就不显示。

一步诊断（只读，不改任何文件）：

```bash
sudo /screen/tools/coolercontrol/probe-coolercontrol.sh modes
# 配置目录不在 /etc/coolercontrol 时：
#   sudo CC_DIR=/path/to/dir /screen/tools/coolercontrol/probe-coolercontrol.sh modes
```

它把 **daemon 真值设备表**（`config.toml` 的 `[devices]` 段）与
`modes.json` 引用的 UID / 通道 / profile 三方对比，直接告诉你属于哪一种：

| 诊断结论 | 含义 | 处置 |
| --- | --- | --- |
| **引用了真机不存在的通道** | ★ 通道名写错（如写了 `pwm1/pwm2`，真机是 `fan3/fan2`） | 按提示跑 `--auto-channels` |
| UID 与真值对不上 | **UID 漂移** | 按提示跑 `--auto-uid` |
| 设备键不是 64 位 hex | `modes.json` **没渲染过**（还是 `@DEVICE_UID@`） | 先渲染再重启 daemon |
| 引用了不存在的 profile | `config.toml` 与 `modes.json` 版本不匹配 | 重新渲染两份（render 是一起写的） |
| 全部命中 | 内容层面没问题 | 查 docker 挂载路径 / 有没有重启 daemon / UI 硬刷新（Ctrl+F5，前端会缓存设备表） |

修一条命令（`--auto` = UID + 通道名全部按真机真值对齐）：

```bash
sudo /screen/tools/coolercontrol/render-config.py --host hs-afs-r1 --auto \
      --fixed-pct 30,60,100 --fallback-pct 30,35     # 可选：保留手调过的转速
sudo systemctl restart coolercontrold          # 或 docker restart coolercontrol
sudo /screen/tools/coolercontrol/probe-coolercontrol.sh modes   # 复查应全绿
```

**`--auto-uid` 做了什么**：读 `/etc/coolercontrol/config.toml` 的 `[devices]` 段
（daemon 每次启动用真值重写 = 唯一权威来源），按**设备名**反查 UID：

| 槽位 | 设备名 / 来源 | 影响 |
| --- | --- | --- |
| `device_uid` | `it8613` | modes.json 的键 + `[device-settings]` |
| CPU 温度 | `Intel(R) Core(TM) i3-N305` | `hs-cpu-graph` 的 `temp_source.device_uid` |
| 机箱温度 | `i915` | `hs-case-graph` 的 `temp_source.device_uid` |
| **`cpu_channel` / `case_channel`** | **`[device-settings.<uid>]` 段里 daemon 实际写入的通道** | modes.json 与 `[device-settings]` 的通道名（`fan3` / `fan2`） |

通道角色怎么认：`--auto-channels` 看每个通道**当前挂的 profile 名**来推断
（`hs-cpu-graph` → CPU 风扇、`hs-case-graph` → 机箱风扇），
并自动跳过 `[settings.<uid>.channel_settings]` 里 `disabled = true` 的通道。
推断不出来时只提示、**不擅自改**，需手工指定。

三个槽位同时对齐，`[devices]` 段也会同步改写（否则产物内部自相矛盾：
`[devices]` 是旧 UID、`[device-settings]` 是新 UID，排查脚本会再次误报）。
**风扇设备找不到会直接报错退出**（绝不用旧值蒙混）；
温度源找不到只告警，该路智能档退化成 Fixed 兜底。

> 换机器/设备名不同：`--fan-device-name` / `--cpu-temp-device-name` /
> `--case-temp-device-name` 指定 `[devices]` 段里的显示名即可。

### 6.5 ★★ 屏幕与 CoolerControl 不同步（新建模式后切档失效）

**现象**：在 CoolerControl Web UI 里自己新建/改了 Mode 之后：

- 小屏上切档 → CoolerControl 没反应（或报 "目标 Mode 不在 modes.json 中"）；
- 在 UI 里手动切 Mode → 小屏仍显示旧档位。

**根因**：旧实现把「档位 → Mode」的 uid 写死成
`hs-smart / hs-low / hs-standard / hs-high`。而**在 Web UI 里自己建的 Mode，
uid 是随机 UUID**（`1a700658-89d7-4d03-…`），不是 `hs-smart` ⇒ 屏幕发过来的
档位找不到目标 Mode，链路断开。

**现在的做法：绑定外置 + 自动认领**（`tools/coolercontrol/fan-mode-map.py`），
解析优先级从高到低：

| 优先级 | 规则 | 例子 |
| --- | --- | --- |
| ① | **显式绑定**（`hs-fan-mode-map.json`，值可以是 uid 或 Mode 名字） | `--bind 5=1a700658-…` |
| ② | 默认 uid | `hs-smart` / `hs-low` / `hs-standard` / `hs-high` |
| ③ | 名字前缀 | `"hs 智能…"` / `"hs 低速…"` / `"hs 标准…"` / `"hs 强劲…"` |
| ④ | 名字含关键词 | `智能\|smart` / `低速\|low\|静音` / `标准\|standard` / `强劲\|high\|性能` |

⇒ **在 UI 里随便建模式也行，名字里带档位关键词（智能/低速/标准/强劲）就能自动认领**；
名字完全无关（比如"我的自定义模式"）时，绑定一次即可，不必重渲染、不必改 uid。

```bash
# ① 看清楚有哪些 Mode、每档当前绑定到谁（只读）
sudo /screen/tools/coolercontrol/apply-mode.sh --scan

# ② 绑定：用 uid 或 Mode 名字都行（可一次绑多个档位）
sudo /screen/tools/coolercontrol/apply-mode.sh --bind 5=1a700658-89d7-4d03-8443-c9260d1604d9
sudo /screen/tools/coolercontrol/apply-mode.sh --bind 2="hs 低速 25%" 4="性能模式"

# ③ 解除绑定（回到自动认领）
sudo /screen/tools/coolercontrol/apply-mode.sh --bind 2=
```

绑定结果写在 `hs-fan-mode-map.json`（默认与 `modes.json` 同目录，
`render-config.py` 渲染时会一并生成，内容就是四个 `hs-*` uid）。

**反向同步（CoolerControl → 小屏）**：中间件在生成状态帧时会读
`modes.json` 的 `current_active_mode` 反查档位，在 UI 里手动切了模式，
小屏回显会自动跟上（只在文件 mtime 变化时才解析，几乎无开销）。
★ 这里**不会**反向写回 CoolerControl，否则两边互相改会形成死循环。

```bash
# 看当前激活的 Mode 对应屏幕哪一档
sudo /screen/tools/coolercontrol/apply-mode.sh --current
sudo /screen/tools/coolercontrol/apply-mode.sh --status
```

| 排查动作 | 命令 |
| --- | --- |
| 屏幕切了但 CC 没变 | `apply-mode.sh --scan` → 看该档位是不是"未匹配"；是就 `--bind` |
| CC 切了但屏幕没变 | `apply-mode.sh --current` 能否识别；识别不出就是名字无关，需 `--bind` |
| 切过去完全不生效 | `--scan` 里有没有 `@DEVICE_UID@` 非法键（模板占位符残留）⇒ 重新渲染 |
| 都不行 | `probe-coolercontrol.sh modes`（UID/通道/profile 三方对比） |

> ★ 每次切档都要**重启 daemon**（`systemctl restart coolercontrold`），
> 这是 6.x 之前的既定路径（Mode 是启动时读 `current_active_mode` 激活的）。

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
- [ ] `probe-coolercontrol.sh` §5.1 能打印出 `[device-settings]` 里的真实通道名（fanN）
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

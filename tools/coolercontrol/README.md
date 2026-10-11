# tools/coolercontrol —— 风扇 4 档落地到 CoolerControl

把海康小彩屏（HS-AFS-R1）界面上的 **智能 / 低速 / 标准 / 强劲** 四档，
映射成 [CoolerControl](https://gitlab.com/coolercontrol/coolercontrol) 的
配置文件（`config.toml` + `modes.json`），从而在飞牛OS 上真正驱动风扇转速。

## 档位映射

本机 **it8613 超级 I/O 暴露两路 PWM**，采用**双风扇**方案：
两路各自按自己的温度曲线独立调速，档位由中间件统一切换。

| 通道 | 风扇 | 温度源 | 智能档 Profile |
| --- | --- | --- | --- |
| `fan3` | CPU 风扇 | CPU 核心（i3-N305，传感器名 `temp1`） | `hs-cpu-graph` |
| `fan2` | 机箱风扇 | 核显 i915（传感器名 `temp1`） | `hs-case-graph` |

> ★★ **通道名是 `fanN`，不是 sysfs 里的 `pwmN`！** CoolerControl 对 hwmon 设备
> 一律用 `fan1…fanN` 作通道名（真机 it8613 暴露 fan1..fan5，fan4/fan5 已禁用）。
> 写错通道名 = UI 里 Mode 的「设备/通道/设置」一片空白（与 UID 漂移症状相同）。
> 用 `--auto-channels`（或 `--auto`）自动取真机真值，别手抄。

| 界面档位 | `fan_mode` | CoolerControl Mode | 落地形态（两路） |
|---|---|---|---|
| 智能 | `5` | `hs-smart` | 两条 Graph 曲线 |
| 低速 | `2` | `hs-low` | Fixed 25% |
| 标准 | `3` | `hs-standard` | Fixed 55% |
| 强劲 | `4` | `hs-high` | Fixed 100% |

> ★★ **Mode uid 不是写死的**：在 Web UI 里自己新建的 Mode，uid 是随机 UUID，
> 档位靠 `hs-fan-mode-map.json` 绑定（显式 `--bind`，或按 Mode **名字**自动认领）。
> 详见下面「★ 档位 ↔ Mode 绑定」。

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
   │  ① 解析档位 → Mode uid（fan-mode-map.py：绑定 > 默认 uid > 名字）
   │  ② modes.json: current_active_mode = <解析出的 uid>（原子写）
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
| `apply-mode.sh` | **中间件调用的落地脚本**。`apply-mode.sh {5\|2\|3\|4}`（★ v3：直接写 `config.toml`，**默认不重启** daemon；`--restart` 兜底） |
| `fan-tier.py` | ★★ **档位规则的唯一权威来源**（其他脚本都 import 它，规则只此一份）。可当 CLI：`--uids` / `--keys` / `--tier N` / `--self-test` |
| `fan-mode-map.py` | 档位 ↔ Mode 映射解析的**薄封装**（规则委托 `fan-tier.py`）：`--scan` / `--bind` / `--resolve` / `--reverse` |
| `hs-fan-mode-map.json` | 渲染时一并生成的绑定文件（4 档 → `hs-*` uid）；`--bind` 改写它 |
| `probe-coolercontrol.sh` | 只读探测真机：安装形态、端口、真实 device_uid / **真实通道名 fanN** / CPU 与机箱温度源 |
| `README.md` | 本文档 |

> ★★ **改档位规则只改 `fan-tier.py` 一处**。`render-config.py` / `fan-mode-map.py` /
> `middleware.py` / `apply-mode.sh` 全部委托它；历史上因规则被复制 4~5 份且结构不一致，
> 出现过「屏幕显示 A、CC 实际跑 B」。

## ★ 档位 ↔ Mode 绑定（自建 Mode 也不会失去同步）

在 Web UI 里**自己新建**的 Mode，uid 是随机 UUID（`1a700658-…`），不是 `hs-smart`。
若档位写死 uid，屏幕切档就找不到目标 Mode ⇒「屏幕和 CoolerControl 不同步」。

解析优先级（`fan-tier.py` 的 `resolve()`，正/反向同规则）：

1. **显式绑定** `hs-fan-mode-map.json`（值可以是 uid，也可以是 Mode 名字）
2. 默认 uid：`hs-smart` / `hs-low` / `hs-standard` / `hs-high`
3. 名字前缀：`"hs 智能…"` / `"hs 低速…"` / `"hs 标准…"` / `"hs 强劲…"`
4. 名字含关键词：`智能|smart` / `低速|low|静音` / `标准|standard` / `强劲|high|性能`

★ 全表**先跑前缀轮、再跑关键词轮**（两轮内都按档位顺序 5→2→3→4），结果确定可预期；
★ `claimed` 排除：某档已被显式绑定/默认 uid 占用时，别的 Mode 名字再像也不能认领该档
（防两档抢同一个 Mode）。

```bash
sudo ./apply-mode.sh --scan                     # 有哪些 Mode？每档绑定到谁？
sudo ./apply-mode.sh --bind 5=<uid 或 Mode 名>  # 绑定（名字无关的模式必须绑）
sudo ./apply-mode.sh --bind 2=                  # 解除，回到自动认领
sudo ./apply-mode.sh --current                  # 当前激活的 Mode 对应哪一档
```

**反向同步**：中间件读 `modes.json` 的 `current_active_mode` 反查档位，
在 UI 里手动切了模式，小屏回显会自动跟上（**绝不**反向写回，避免死循环）。

★ **防自我回滚（无标记、无时间窗）**：`FanState.update()` 是**先**更新本地档位、
**再**落地到 CC；`mode()` 反向同步只做 `if ext is not None and ext != 本地档位`。
自己写下去的回波必然 `ext == 本地` ⇒ 天然不会被回滚；只有两者确实不同
（＝用户在 CC UI 真手动切了）才同步。因此**不需要**任何"自写标记 / 时间窗"。
抓现行：`sudo ./probe-coolercontrol.sh watch 30`（正常 `3→2`；自我回滚 `3→2→3`）。

## ★ 「选了低速，跑出来却是 60%」= Mode 名字与内容不符

daemon 只认 Mode 的**内容**（`profile_uid`），名字纯展示。真机踩过：
一个叫 `hs 低速 25%` 的 Mode 里挂的却是 `hs-standard`(60%) ⇒ 屏幕切「低速」实际跑 60%。

```bash
sudo ./fan-mode-map.py --scan     # 每档会打印「内容=…」，不符时标红
sudo ./render-config.py --host hs-afs-r1 --auto \
     --merge-modes /etc/coolercontrol/modes.json --rename-modes
sudo systemctl restart coolercontrold        # 或 docker restart <容器名>
```

`--merge-modes` 按**名字**认领档位（名字优先于内容，才能把内容纠正回来），
继承现有 Mode 的 uid/名字/顺序；`--rename-modes` 把名字重写成与实际转速一致。

## 快速开始

```bash
# ── 1. 部署文件到 /screen ──────────────────────────────────
#    ★ 推荐懒人路线：sudo bash install-autostart.sh --fan-coolercontrol on
#      （自动部署本目录 + 注入中间件环境变量 + 校验，无需手工敲下面三行）
sudo mkdir -p /screen/tools/coolercontrol
sudo cp -f  tools/coolercontrol/* /screen/tools/coolercontrol/
sudo chmod +x /screen/tools/coolercontrol/*.sh /screen/tools/coolercontrol/*.py

# ── 2. 探测真机，拿 device_uid / 双路通道 / 温度源 ─────────
sudo /screen/tools/coolercontrol/probe-coolercontrol.sh

# ── 3. 渲染配置到 CoolerControl 配置目录 ───────────────────
#    ★ 推荐：机型预设一键渲染（已内置 HS-AFS-R1 实测参数）
sudo /screen/tools/coolercontrol/render-config.py --host hs-afs-r1 \
      --out-dir /etc/coolercontrol
#    列出内置机型：render-config.py --list-hosts
#    ⇒ 默认 = 双风扇：fan3→CPU 曲线、fan2→机箱(i915)曲线
#
#    ★ 更推荐（不手抄任何真机参数）：
# sudo render-config.py --host hs-afs-r1 --auto --config-dir /etc/coolercontrol
#      --auto = --auto-uid + --auto-channels（UID 与通道名都按真机真值对齐）
#      --merge-modes /etc/coolercontrol/modes.json   ★ 保留你在 UI 里自建的 Mode
#                                                    （只纠正内容，uid/名字/顺序不丢）
#      --rename-modes              ★ 名字改成与实际转速一致（不再「名字 25%、实际 60%」）
#      --fixed-pct 25,55,100       三档固定转速（默认 25,55,100）
#      --fallback-pct 30,35        智能档兜底转速（默认 35,40）
#
# ★ 屏幕切档没反应 / 「选了低速却是 60%」→ 先诊断：
#     sudo ./fan-mode-map.py --scan        # 每档绑定到谁 + 内容与档位是否相符
#     sudo ./probe-coolercontrol.sh sync   # 六环取证（工具版本/绑定/反向/重启通道/中间件/自我回滚防护）
#     sudo ./probe-coolercontrol.sh watch 30   # ★ 实时盯 modes.json，抓「切了又弹回」
#         （正常 3→2 停在 2；自我回滚 3→2→3，见 docs/coolercontrol.md §6.8）

# 渲染开关：
#   --single-channel    单风扇机器（只渲染一路）
#   --no-case-graph     双通道但机箱侧走 Fixed 40%（不用曲线）
#   --no-graph          两路智能档都走 Fixed 兜底

# 等价手工写法（其它机型参照）：
# sudo /screen/tools/coolercontrol/render-config.py \
#       --device-uid <64位hex> \
#       --cpu-channel fan3 --case-channel fan2 \
#       --cpu-temp-name "temp1" \   # ★ 传感器名，不是设备显示名
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
  写成 `[device-settings.uid.fan3]` 子表 daemon 读不到。
- **双通道写在同一个内联表**里，用逗号分隔：
  `"fan3" = { ... }, "fan2" = { ... }`。
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
- `activate_mode()` 会把 Mode 的 Setting 写回 `config.toml`，并对
  **Mode 未覆盖的 channel 做 Reset**，因此改 `modes.json` 的
  `current_active_mode` 后重启 daemon 即生效。
- ★★ **v3（2026-10-11）：切档不再依赖重启。** `apply-mode.sh` 落地时
  **自己把 Mode 的 `all_device_settings` 写进 `config.toml` 的 `[device-settings]`**
  ——这正是 daemon「激活 Mode」时做的事，中间件自己做 ⇒ 改完立即一致（probe ⑦ 必绿），
  daemon 只需热重载。这消除了旧路径的三个毛病：每次切档重启 daemon／依赖 daemon
  启动时 `activate_mode` 环境正确／两进程竞争写同一文件。
  ★ 兜底：daemon 若不热重载配置，用 `apply-mode.sh --restart <mode>`
  （或 `HIKSEMI_FAN_CC_RESTART_ON_APPLY=1`）。
- `apply_on_boot = true` 时，daemon 启动会重新应用 `[device-settings]`。
  ★ **必须在 `[settings]` 段内**（放文件头 = 顶层键 = daemon 读不到）；
  `apply-mode.sh` 落地前会用 `ensure_apply_on_boot()` 自动确保它在正确位置。
  ★ 它是【断电/重启后】恢复档位的保险（与切档当下是否生效无关）。
- `apply-mode.sh` 落地后回读 `config.toml`：`config_matches_mode()` 逐通道比对
  当前 Mode 内容；不符则再写一次，仍不符以 **`exit 5`** 告警
  （指向 `probe-coolercontrol.sh sync` 的 ⑦ 环）。

**退出码**：`0`=成功 / `2`=参数或文件问题 / `5`=`config.toml` 与 Mode 内容不符。
（旧的 `3`=无重启通道 / `4`=被改回 已废弃 —— 切档不再依赖重启。）

## 注意事项

- **切换代价（v3 起极低）**：切档 = 写 `modes.json` + 写 `config.toml`，
  由 daemon 热重载，**不重启、风扇不中断**（< 1s）。仅 `--restart` 兜底路径才会重启
  （< 2s，风扇短暂回开机默认后立刻应用新档）。
- **多风扇（本机默认双风扇）**：本机 it8613 的可控通道是 `fan3`(CPU) + `fan2`(机箱)，
  两条曲线各管一路。渲染时用 `--cpu-channel` / `--case-channel` 指定；
  **每个 Mode 必须覆盖全部通道**，否则未覆盖的会被 Reset（渲染器会强制校验）。
  只有一路风扇的机器加 `--single-channel`，否则会多写一路不存在的通道。
- **接线方向**：无法从 sysfs 判断哪个风扇接了哪路，默认按「fan3=CPU、fan2=机箱」
  推断。若实际相反，跑渲染时交换 `--cpu-channel` / `--case-channel` 即可
  （两条曲线的温度源与阈值是分开的，交换通道号不影响曲线本身）。
- **降级链路**：未安装 CoolerControl 时，`apply-mode.sh` 会告警并以
  非零退出，中间件会退回「仅保存 + 回显」，不影响 REST 响应。

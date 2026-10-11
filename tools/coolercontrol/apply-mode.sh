#!/usr/bin/env bash
# =============================================================================
# apply-mode.sh —— 把海康小彩屏界面的风扇档位落到 CoolerControl
# =============================================================================
# 调用方：中间件 middleware.py 的 apply_to_external_app()
#         （环境变量 HIKSEMI_FAN_APPLY_CMD="/screen/tools/coolercontrol/apply-mode.sh {mode}"）
#
# 用法：
#     apply-mode.sh <fan_mode>        # fan_mode ∈ {5,2,3,4}
#     apply-mode.sh --list            # 列出档位 → Mode 映射
#     apply-mode.sh --status          # 打印当前 CoolerControl 激活的 Mode
#     apply-mode.sh --current         # ★ 打印当前激活 Mode 对应的【档位号】（反向）
#     apply-mode.sh --scan            # ★ 列出所有 Mode + 每档当前绑定到谁
#     apply-mode.sh --bind 5=<uid|名字> 2=…   # ★ 显式绑定档位→Mode
#     apply-mode.sh --dry-run 3       # 只打印将要做的动作，不落盘
#     apply-mode.sh --restart 3       # ★ 无 stop/start 通道时的兜底（整条 restart）
#
# ★★ 切档如何生效（v4：stop → 写 config.toml → start）：
#   ★ 官方文档明确：config.toml 是 daemon 的「所有物」，**运行时改会被覆盖**；
#     必须「stop → 改 → start」。（docs.coolercontrol.org/daemon/config-files）
#     ① stop daemon（它把内存态落盘后退出，此后没人会覆盖我们的写）；
#     ② 写 modes.json 的 current_active_mode（"模式"层，UI/反向同步用）；
#     ③ 写 config.toml 的 [device-settings.<uid>]（★"生效"层：daemon 启动读它）；
#     ④ start daemon（按新 [device-settings] 应用）。
#   ★ 为什么写 config.toml 而不只是 modes.json：daemon 启动时应用的是
#     config.toml 的 [device-settings]（modes.rs `handle_settings_at_boot`
#     → "Applying all saved device settings"），**不是** modes.json 的
#     current_active_mode（那只在 UI/REST 切模式时才被读）。
#   ★ 前提：apply_on_boot = true（否则 daemon 启动根本不应用 [device-settings]）。
#   ★ --restart：无 stop/start 通道时的兜底（整条 restart）。
#
# 档位映射（与界面一致）：
#     5 智能 → hs-smart      （CPU 通道=CPU 曲线, 机箱通道=机箱曲线）
#     2 低速 → hs-low        （Fixed 25%，两路）
#     3 标准 → hs-standard   （Fixed 55%，两路）
#     4 强劲 → hs-high       （Fixed 100%，两路）
#
# ★★ 重要：uid 不再是写死的绑定关系！
#   用户在 CoolerControl Web UI 里【自己新建】的 Mode，uid 是随机 UUID
#   （1a700658-89d7-…），不是 hs-smart —— 写死 uid 会导致切换直接失败、
#   「屏幕和 CoolerControl 不同步」。现在解析交給 fan-mode-map.py：
#     ① 显式绑定（--bind 5=<uid 或 Mode 名字>，存 hs-fan-mode-map.json）
#     ② 默认 uid：hs-smart / hs-low / hs-standard / hs-high
#     ③ 名字前缀："hs 智能…" / "hs 低速…" / "hs 标准…" / "hs 强劲…"
#     ④ 名字包含关键词：智能|smart / 低速|low / 标准|standard / 强劲|high
#   ⇒ 用户在 UI 里随便建模式、随便改名，只要名字里带档位关键词就能认领；
#     名字完全不相关时，跑一次 `--bind 5=<那个 Mode 的 uid>` 即可。
#
# 原理（源码级已确认 activate_mode / handle_settings_at_boot 语义）：
#     CoolerControl 的 Mode 就是 config.toml 里 [device-settings] 的具名快照
#     （modes.json 存 Mode 定义 + current_active_mode）。
#     ★ 激活语义：UI/REST 调 activate_mode() 会把该 Mode 的 Setting 写回
#       config.toml 的 [device-settings] 并应用到引擎。
#     ★ 启动语义：daemon 启动时走 modes.rs `handle_settings_at_boot`
#       → "Applying all saved device settings"，应用的是 **config.toml 的
#       [device-settings]**（不是 modes.json 的 current_active_mode）。
#     ⇒ 因此「写 config.toml 的 [device-settings] + 写 modes.json 的
#       current_active_mode，再 start daemon」才能既生效又被 UI 认到。
#
# ★ 为什么 stop→写→start，而不是调 REST API / 只写文件：
#     ① REST API 需要 daemon 内部 token/会话，且版本间路径会变；
#     ② config.toml 是 daemon 的「所有物」，**运行时改会被它在退出/保存时覆盖**
#        （官方文档原话）⇒ 必须 stop 后再写；
#     ③ stop→写→start 稳定、幂等、可离线回放。切档是低频操作，
#        stop/start 各约 1-2s（风扇短暂回到开机默认后立刻应用新档）可接受。
#
# ★★ 重启通道是模糊匹配的（systemd 服务名 / docker 容器名都可能被改）：
#     HIKSEMI_FAN_CC_RESTART='docker restart my-cc'      # 自定义整条命令
#     找不到重启通道时会【非 0 退出】并打印排查指引 —— 因为「写文件成功但没人
#     重启」等于档位没生效，而症状恰好就是「屏幕切档、CoolerControl 没反应」。
#
# 退出码：0=成功 / 2=参数或文件问题 / 5=config.toml 与 Mode 内容不符
#   （旧的 3/4 "无重启通道/被改回" 已不再使用 —— 切档不再依赖重启）
# =============================================================================
set -uo pipefail

# 路径可用环境变量覆盖（本地测试 / Docker 挂载路径不同时用）
MODE_CONFIG="${CC_MODE_CONFIG:-/etc/coolercontrol/modes.json}"
CONFIG_TOML="${CC_CONFIG_TOML:-/etc/coolercontrol/config.toml}"
# ★ 必须 export：fan-mode-map.py 通过这两个环境变量定位文件
export CC_MODE_CONFIG="$MODE_CONFIG"
export CC_CONFIG_TOML="$CONFIG_TOML"

DRY_RUN=0
# ★ v4：切档固定「stop → 写 → start」（官方顺序）。
#   RESTART=1 仅作为【无独立 stop/start 通道】时的兜底（整条 restart 代替）。
RESTART=0
[ "${HIKSEMI_FAN_CC_RESTART_ON_APPLY:-0}" = "1" ] && RESTART=1

log()  { printf '[fan-apply] %s\n' "$*" >&2; }
warn() { printf '[fan-apply] WARN: %s\n' "$*" >&2; }
die()  { printf '[fan-apply] ERROR: %s\n' "$*" >&2; exit 1; }

# ── 档位 → Mode uid ─────────────────────────────────────────────────────────
# ★ 解析**只**交给 fan-mode-map.py（其规则又来自唯一权威 fan-tier.py）。
#   本脚本刻意【不再内嵌】默认 uid 表或名字前缀表——历史上那份副本规则更弱
#   （只认 "hs X" 前缀），一旦 fan-mode-map.py 缺失就会给出与其它组件不同的
#   结果，导致「屏幕显示 A、CC 实际 B」。缺脚本就明确报错，不猜。
MAP_PY="${CC_FAN_MODE_MAP_PY:-$(cd "$(dirname "$0")" && pwd)/fan-mode-map.py}"

resolve_uid_for() {
    case "$1" in
        5|2|3|4) : ;;                       # 先校验档位合法
        *) return 1 ;;
    esac
    [ -f "$MAP_PY" ] || die "档位解析需要 $MAP_PY，但没找到（请重跑安装器同步 tools/coolercontrol/）"
    local u err
    # ★ 保留 stderr：解析失败时应把它打印出来（含"绑定失效/无匹配"的明确原因），
    #   不再静默吞掉 —— 否则用户只看到"切档失败"却不知为何。
    u="$(python3 "$MAP_PY" --resolve "$1" 2>/tmp/.fan-apply-resolve.err)" || {
        err="$(cat /tmp/.fan-apply-resolve.err 2>/dev/null)"; rm -f /tmp/.fan-apply-resolve.err
        [ -n "$err" ] && log "档位 $1 解析说明：$err"
        return 1
    }
    rm -f /tmp/.fan-apply-resolve.err
    [ -n "$u" ] || return 1
    echo "$u"
}
mode_name_for() {
    case "$1" in
        5) echo "智能" ;; 2) echo "低速" ;; 3) echo "标准" ;; 4) echo "强劲" ;;
        *) echo "未知" ;;
    esac
}

cmd_list() {
    cat <<'EOF'
界面档位   fan_mode   CoolerControl Mode   Fixed/曲线（双路 fan3=CPU / fan2=机箱）
--------   --------   ------------------   --------------------------------------
智能            5      hs-smart             CPU 通道→CPU 曲线 + 机箱通道→机箱曲线
低速            2      hs-low               Fixed 25%（两路）
标准            3      hs-standard          Fixed 55%（两路）
强劲            4      hs-high              Fixed 100%（两路）

★ Mode uid 不是写死的：若你在 Web UI 里自己建了 Mode（uid 是随机 UUID），
  用 --scan 看看有哪些，再 --bind 5=<uid 或 Mode 名字> 绑定一次即可；
  名字里含「智能/低速/标准/强劲」的 Mode 会自动认领，连绑定都不用。
EOF
}

# ── 选择可用的重启方式 ───────────────────────────────────────────────────────
have_root() { [ "$(id -u)" -eq 0 ]; }

# ★★ 真机上 CoolerControl 的安装形态五花八门：
#    · systemd 服务：coolercontrold / coolercontrol（有些发行版叫别的名）
#    · docker 容器：官方镜像 coolercontrol/coolercontrol，容器名可能被编排工具
#      改过（coolercontrol-1 / feiniu-coolercontrol / compose 前缀……）
#    旧实现只精确匹配两个名字 ⇒ 编排改名后「配置文件写成功、但没人重启 daemon」
#    ⇒ 切档【不生效】⇒ 症状与「屏幕和 CoolerControl 不同步」完全一样。
#    这里改成【模糊匹配】+ 支持自定义重启命令：
#      HIKSEMI_FAN_CC_RESTART='docker restart my-cc'
#      HIKSEMI_FAN_CC_RESTART='systemctl restart coolercontrol.service'
CC_CTL_KIND=""
CC_CTL_NAME=""

cc_detect_ctl() {
    [ -n "$CC_CTL_KIND" ] && return 0
    if [ -n "${HIKSEMI_FAN_CC_RESTART:-}" ]; then
        CC_CTL_KIND="custom"; CC_CTL_NAME="$HIKSEMI_FAN_CC_RESTART"
        return 0
    fi
    if command -v systemctl >/dev/null 2>&1; then
        local u
        u="$(systemctl list-unit-files --type=service --plain --no-legend 2>/dev/null \
             | awk '{print $1}' | grep -i -m1 'coolercontrol' || true)"
        if [ -n "$u" ]; then
            CC_CTL_KIND="systemd"; CC_CTL_NAME="${u%.service}"
            return 0
        fi
    fi
    if command -v docker >/dev/null 2>&1; then
        local c
        c="$(docker ps -a --format '{{.Names}}' 2>/dev/null \
             | grep -i -m1 'coolercontrol' || true)"
        if [ -z "$c" ]; then   # 容器名改了 → 退一步按镜像名找
            c="$(docker ps -a --format '{{.Names}}\t{{.Image}}' 2>/dev/null \
                 | grep -i 'coolercontrol' | head -1 | cut -f1 || true)"
        fi
        if [ -n "$c" ]; then
            CC_CTL_KIND="docker"; CC_CTL_NAME="$c"
            return 0
        fi
    fi
    return 1
}

cc_ctl_run() {     # $1 = restart | stop | start
    local act="$1"
    case "$CC_CTL_KIND" in
        custom)
            # 自定义命令只能整条执行；stop/start 无法拆分 ⇒ 只在 restart 时用
            [ "$act" = "restart" ] || return 0
            sh -c "$CC_CTL_NAME" ;;
        systemd) systemctl "$act" "$CC_CTL_NAME" ;;
        docker)  docker "$act" "$CC_CTL_NAME" ;;
        *)       return 1 ;;
    esac
}

cc_ctl_desc() {
    case "$CC_CTL_KIND" in
        custom)  echo "自定义命令：$CC_CTL_NAME" ;;
        systemd) echo "systemd 服务 $CC_CTL_NAME" ;;
        docker)  echo "docker 容器 $CC_CTL_NAME" ;;
        *)       echo "（未探测到）" ;;
    esac
}

restart_daemon() {
    if [ "$DRY_RUN" = "1" ]; then
        log "[dry-run] 将重启 CoolerControl daemon"
        return 0
    fi
    if ! cc_detect_ctl; then
        warn "未找到 CoolerControl 的 systemd 服务 / docker 容器（模糊匹配也没命中）。"
        warn "  ⇒ 配置已写入，但【没人重启 daemon ⇒ 档位不会生效】。"
        warn "     这正是「屏幕切档、CoolerControl 侧没反应」最常见的原因。"
        warn "  修：把重启命令放进环境变量（service 的 Environment= 里）——"
        warn "      HIKSEMI_FAN_CC_RESTART='docker restart <容器名>'"
        warn "      或直接用 HIKSEMI_FAN_APPLY_CMD 指定整条命令。"
        return 1
    fi
    log "重启 CoolerControl（$(cc_ctl_desc)）"
    if cc_ctl_run restart; then
        return 0
    fi
    warn "重启失败：$(cc_ctl_desc)"
    return 1
}

cmd_status() {
    [ -f "$MODE_CONFIG" ] || die "找不到 $MODE_CONFIG（CoolerControl 未安装或路径不同）"
    python3 - "$MODE_CONFIG" <<'PY'
import json, re, sys
p = sys.argv[1]
try:
    with open(p, encoding="utf-8") as f:
        d = json.load(f)
except Exception as e:
    print(f"读取失败: {e}"); sys.exit(1)
cur = d.get("current_active_mode")
prev = d.get("previous_active_mode")
name = next((m.get("name") for m in d.get("modes", []) if m.get("uid") == cur), None)
print(f"当前激活 Mode : {cur} ({name})")
print(f"上一个 Mode   : {prev}")
print(f"可选 Mode     : " + ", ".join(m.get('uid','?') for m in d.get("modes", [])))
# 非法键（占位符残留）检查
bad = [k for m in d.get("modes", [])
       for k in (m.get("all_device_settings") or {})
       if not re.fullmatch(r"[0-9a-f]{64}", str(k))]
if bad:
    print(f"⚠ 非法设备键   : {bad[:3]}（模板占位符残留 ⇒ 该 Mode 不会生效）")
PY
    # 反向：当前激活的 Mode 对应屏幕哪一档（在 UI 里手动切了模式时看这个）
    if [ -f "$MAP_PY" ]; then
        m="$(python3 "$MAP_PY" --reverse 2>/dev/null)"
        if [ -n "$m" ]; then
            echo "对应屏幕档位 : $m（$(mode_name_for "$m" 2>/dev/null || echo 未知)）"
        else
            echo "对应屏幕档位 : 识别不出（Mode 名字不含 智能/低速/标准/强劲，"
            echo "               跑 --scan 后用 --bind 绑定一次即可）"
        fi
    fi
}

# ── 主流程 ───────────────────────────────────────────────────────────────────
MODE_ARG=""
BIND_ARGS=()
for a in "$@"; do
    case "$a" in
        --list)        cmd_list; exit 0 ;;
        --status)      cmd_status; exit 0 ;;
        --dry-run|-n)  DRY_RUN=1 ;;
        --restart)     RESTART=1 ;;
        --no-restart)  RESTART=0 ;;
        --scan)
            [ -f "$MAP_PY" ] || die "找不到 $MAP_PY"
            exec python3 "$MAP_PY" --scan ;;
        --current)
            [ -f "$MAP_PY" ] || die "找不到 $MAP_PY"
            exec python3 "$MAP_PY" --reverse ;;
        --bind)
            [ -f "$MAP_PY" ] || die "找不到 $MAP_PY"
            shift
            [ $# -gt 0 ] || die "--bind 需要参数，例：--bind 5=1a700658-89d7-4d03-8443-c9260d1604d9"
            exec python3 "$MAP_PY" --bind "$@" ;;
        *=*)           BIND_ARGS+=("$a") ;;   # 也允许 apply-mode.sh 5=<uid> 直连绑定
        *)             MODE_ARG="$a" ;;
    esac
done

if [ -z "$MODE_ARG" ] && [ "${#BIND_ARGS[@]}" -eq 0 ]; then
    cmd_list
    die "缺少档位参数（5/2/3/4）"
fi

# 允许顺手绑定：apply-mode.sh 5=<uid或Mode名>  （等价于先 --bind 再切档）
if [ "${#BIND_ARGS[@]}" -gt 0 ]; then
    [ -f "$MAP_PY" ] || die "绑定需要 $MAP_PY，但没找到"
    python3 "$MAP_PY" --bind "${BIND_ARGS[@]}" || die "绑定失败"
    # 只绑定、不切档时到此为止（不给"缺少档位参数"的假报错）
    if [ -z "$MODE_ARG" ]; then
        log "已更新绑定，未切档。复查：$(basename "$0") --scan"
        exit 0
    fi
fi

MODE_UID="$(resolve_uid_for "$MODE_ARG")" || die "非法档位 '$MODE_ARG'（只接受 5/2/3/4）"
MODE_NAME="$(mode_name_for "$MODE_ARG")"

if [ ! -f "$MODE_CONFIG" ]; then
    die "找不到 $MODE_CONFIG —— 请先运行 render-config.py 生成配置"
fi
if ! have_root; then
    warn "当前非 root，写 $MODE_CONFIG 与重启 daemon 可能失败"
fi

# 原子更新 modes.json：current_active_mode / previous_active_mode
# ★ resolve_uid_for 已经用 fan-mode-map.py（→ fan-tier.py）解析出真正要激活的
#   uid；若它在 modes.json 里不存在，说明映射与实际不同步（需 --scan/--bind），
#   这里【只报错】、不再内嵌一套更弱的名字匹配（避免与权威规则不一致）。
TMP="$(mktemp)"
NEW_UID="$(python3 - "$MODE_CONFIG" "$TMP" "$MODE_UID" "$MODE_ARG" <<'PY'
import json, re, sys
path, tmp, target, fan_mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(path, encoding="utf-8") as f:
    d = json.load(f)
modes = d.get("modes", [])
uids = [m.get("uid") for m in modes]
if target not in uids:
    print(f"目标 Mode {target!r} 不在 modes.json 中（可选: {uids}）",
          file=sys.stderr)
    print("★ 若 Mode 是你在 Web UI 里新建的（uid 为随机 UUID），执行：",
          file=sys.stderr)
    print("    sudo ./apply-mode.sh --scan            # 看有哪些 Mode / 每档解析到谁",
          file=sys.stderr)
    print(f"    sudo ./apply-mode.sh --bind {fan_mode}=<uid 或 Mode 名字>",
          file=sys.stderr)
    sys.exit(1)
# ★ 目标 Mode 里若混有未渲染的占位符键（@DEVICE_UID@），切过去也不会生效
bad = [k for m in modes if m.get("uid") == target
       for k in (m.get("all_device_settings") or {})
       if not re.fullmatch(r"[0-9a-f]{64}", str(k))]
if bad:
    print(f"[fan-apply] WARN: Mode {target} 含非法设备键 {bad} "
          "（模板占位符残留）⇒ 该 Mode 可能不生效，请重新渲染 modes.json",
          file=sys.stderr)
cur = d.get("current_active_mode")
if cur != target:
    d["previous_active_mode"] = cur
    d["current_active_mode"] = target
with open(tmp, "w", encoding="utf-8") as f:
    f.write(json.dumps(d, ensure_ascii=False, indent=2))
print(target)
PY
)" || { rm -f "$TMP"; die "更新 modes.json 失败"; }
[ -n "$NEW_UID" ] || { rm -f "$TMP"; die "更新 modes.json 失败（未拿到 mode uid）"; }
[ "$NEW_UID" = "$MODE_UID" ] || MODE_UID="$NEW_UID"

read_back() {
    python3 -c "import json;print(json.load(open('$MODE_CONFIG',encoding='utf-8')).get('current_active_mode',''))" 2>/dev/null
}

# ★ 回读 config.toml：当前激活 Mode 的期望设置有没有真正落进 [device-settings]。
#   返回 0=一致 / 1=不一致 / 2=无法判定（缺文件/缺 python）。
#   ★ 为什么需要它：daemon 只有在 apply_on_boot=true 时才会在【启动时】应用
#     active mode。若为 false（或被 daemon 重写掉了），modes.json 改了、
#     config.toml 却纹丝不动 ⇒ 症状正是「模式改了但配置没变」。
config_matches_mode() {
    [ -f "$MODE_CONFIG" ] || return 2
    [ -f "$CONFIG_TOML" ] || return 2
    python3 - "$MODE_CONFIG" "$CONFIG_TOML" "$MODE_UID" <<'PY' && return 0
import json, re, sys
mod_p, cfg_p, target = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    mj = json.load(open(mod_p, encoding="utf-8"))
except Exception:
    sys.exit(1)
cm = next((m for m in mj.get("modes", []) if m.get("uid") == target), None)
if not cm:
    sys.exit(1)
want = {}
for dev, chans in (cm.get("all_device_settings") or {}).items():
    for c, st in (chans or {}).items():
        want[(dev, c)] = (st.get("profile_uid") or
                          (f"Fixed {st.get('speed_fixed')}%" if "speed_fixed" in st else "?"))
if not want:
    sys.exit(1)                      # 空 Mode：激活它没有意义 ⇒ 视为不匹配
try:
    text = open(cfg_p, encoding="utf-8", errors="replace").read()
except Exception:
    sys.exit(1)
# ★ 段头两种写法都要认：`[device-settings."uid"]`（带引号）与 `[device-settings.uid]`
#   ⇒ 统一去掉首尾引号再比较，否则 key 永远对不上（表现为"明明一致却判不一致"）。
# ★ 通道行是 `"fan3" = { ... }`（键带引号），正则必须允许可选引号。
got = {}
for m in re.finditer(r'^\[device-settings\.([^\]]+)\][ \t]*$(.*?)(?=^\[|\Z)',
                     text, flags=re.M | re.S):
    uid = m.group(1).strip().strip('"').strip("'")
    body = m.group(2)
    for line in body.splitlines():
        mm = re.match(r'^\s*["\']?(fan\w+)["\']?\s*=\s*\{(.*)\}\s*$', line)
        if not mm:
            continue
        kv = mm.group(2)
        pu = re.search(r'profile_uid\s*=\s*"([^"]*)"', kv)
        sf = re.search(r'speed_fixed\s*=\s*(\d+)', kv)
        got[(uid, mm.group(1))] = (pu.group(1) if pu else
                                   (f"Fixed {sf.group(1)}%" if sf else "?"))
sys.exit(0 if all(got.get(k) == v for k, v in want.items()) else 1)
PY
}

# ★ 确保 config.toml 的 【[settings] 段内】 有 apply_on_boot = true。
#   否则 daemon 启动时不会应用 active mode ⇒ modes.json 改了 config.toml 不变。
#
#   ★★ 关键：apply_on_boot 属于 [settings] 段（不是顶层键！）。若把它写到文件头
#      （第一个 [section] 之前），TOML 会当成顶层键 ⇒ CoolerControl 读不到 ⇒
#      「改是改了，但根本没生效」的假修复。所以必须：
#        ① [settings] 段内已有该键 → 就地改成 true；
#        ② 有 [settings] 段但无该键 → 插到 [settings] 头之后（段内首行）；
#        ③ 连 [settings] 段都没有 → 在文件末尾追加一个 [settings] 段。
#   返回 0=已确认，1=无法判定或改动失败。
ensure_apply_on_boot() {
    [ -f "$CONFIG_TOML" ] || return 1
    # 快路径：段内（或任意处）已有 true —— 用 Python 精确定位，避免误判注释行
    python3 - "$CONFIG_TOML" <<'PY' && return 0
import re, sys
t = open(sys.argv[1], encoding="utf-8").read()
sys.exit(0 if re.search(r'^[ \t]*apply_on_boot[ \t]*=[ \t]*true[ \t]*(#.*)?$',
                        t, flags=re.M) else 1)
PY
    python3 - "$CONFIG_TOML" <<'PY' || return 1
import re, sys
p = sys.argv[1]
try:
    t = open(p, encoding="utf-8").read()
except Exception:
    sys.exit(1)

lines = t.splitlines(keepends=True)

def find_sec(name):
    """返回段体起止行号 [start, end)（start 指向段头行的下一行）。"""
    pat = re.compile(r'^\[%s\][ \t]*$' % re.escape(name))
    start = None
    for i, ln in enumerate(lines):
        if pat.match(ln.rstrip("\n")):
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start, len(lines)):
        if re.match(r'^\[', lines[j]):
            end = j
            break
    return (start, end)

# ① 任意位置的字面键（非注释）→ 就地置 true
for i, ln in enumerate(lines):
    if re.match(r'^[ \t]*apply_on_boot[ \t]*=', ln):
        lines[i] = re.sub(r'^([ \t]*apply_on_boot[ \t]*=).*?(\r?\n?)$',
                          r'\1 true\2', ln)
        open(p, "w", encoding="utf-8").write("".join(lines))
        sys.exit(0)

sec = find_sec("settings")
if sec:
    start, _ = sec
    # ② 插到 [settings] 头之后（段内首行），保持可读
    lines.insert(start, "apply_on_boot = true\n")
else:
    # ③ 没有 [settings] 段 → 末尾补一个（保证前面的最后一行以换行结尾）
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append("\n[settings]\napply_on_boot = true\n")
open(p, "w", encoding="utf-8").write("".join(lines))
PY
}

# ── ★★ 关键：把 Mode 的 all_device_settings 写进 config.toml ────────────────
#   ★★ 官方文档明确（docs.coolercontrol.org/daemon/config-files）：
#      「Stop the daemon before editing any of these by hand. The daemon owns these
#        files and writes them out again on shutdown, so an edit made while it is
#        running will be overwritten and lost.」
#      ⇒ config.toml 是 daemon 的「所有物」，**运行时写会被覆盖**；
#        正确顺序永远是 stop → 写 → start（本脚本 apply_once 已遵守）。
#   ★ 为什么写 config.toml 而不是只写 modes.json：
#      daemon 启动时应用的是 config.toml 的 [device-settings]
#      （modes.rs: handle_settings_at_boot → "Applying all saved device settings"），
#      **不是** modes.json 的 current_active_mode（那只在 UI/REST 切模式时才读）。
#      ⇒ 要让"重启后档位还在"，唯一可靠的是把设置落进 config.toml。
#   ★ 顺带清理模板占位符段 [device-settings."@DEVICE_UID@"]（渲染残留会导致两段并存）。
write_device_settings() {
    [ -f "$CONFIG_TOML" ] || return 2
    python3 - "$MODE_CONFIG" "$CONFIG_TOML" "$MODE_UID" <<'PY'
import json, re, sys
mod_p, cfg_p, target = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    mj = json.load(open(mod_p, encoding="utf-8"))
    cm = next((m for m in mj.get("modes", []) if m.get("uid") == target), None)
    if not cm:
        print(f"Mode {target} 不在 modes.json", file=sys.stderr); sys.exit(3)
    ds = cm.get("all_device_settings") or {}
    if not ds:
        print(f"Mode {target} 的 all_device_settings 为空", file=sys.stderr); sys.exit(3)
    txt = open(cfg_p, encoding="utf-8").read()
except Exception as e:
    print(f"读取失败: {e}", file=sys.stderr); sys.exit(2)

def render_chan(st):
    """把 {profile_uid: X} 或 {speed_fixed: N} 渲染成内联表。"""
    if not isinstance(st, dict):
        return None
    parts = []
    if st.get("profile_uid") not in (None, ""):
        parts.append(f'profile_uid = "{st["profile_uid"]}"')
    elif st.get("speed_fixed") is not None:
        parts.append(f'speed_fixed = {int(st["speed_fixed"])}')
    return "{ " + ", ".join(parts) + " }" if parts else None

# ★ 先删除【未渲染的占位符段】（@DEVICE_UID@ 等）——它们是渲染残留，
#   daemon 读到会当成一个"不存在的设备"，且会与真 uid 段并存造成混乱。
#   段体 = 段头行 + 其后的非 [ 开头行（到下一个段头为止）。
txt = re.sub(r'^\[device-settings\.[ \t]*"[^"]*@[^"]*"\][ \t]*\n(?:[^\[]*)',
             "", txt, flags=re.M)

for dev, chans in ds.items():
    rows = []
    for ch, st in (chans or {}).items():
        body = render_chan(st)
        if body is None:
            continue
        rows.append(f'"{ch}" = {body}\n')
    if not rows:
        continue
    # 段体匹配：到「下一个段头」或「连续空行」为止（保留段间的分隔空行）
    sec_re = re.compile(
        r'^\[device-settings\.(?:"%s"|\'%s\'|%s)\][ \t]*\n(.*?)(?=\n\[|\Z)'
        % (re.escape(dev), re.escape(dev), re.escape(dev)),
        flags=re.M | re.S)
    m = sec_re.search(txt)
    new_sec = f'[device-settings."{dev}"]\n' + "".join(rows)
    if m:
        txt = txt[:m.start()] + new_sec + txt[m.end():]
    else:
        if txt and not txt.endswith("\n"):
            txt += "\n"
        txt += "\n" + new_sec

open(cfg_p, "w", encoding="utf-8").write(txt)
PY
}

# ── 单次落地：★ 严格按官方顺序 stop → 写(两个文件) → start ─────────────────
#   ★★ 为什么必须 stop 在前：config.toml 是 daemon 的「所有物」，
#      运行时写会被它退出/保存时覆盖掉（官方文档明写）。所以：
#        stop（daemon 把内存态落盘并退出）→ 我们覆盖写 → start（按新内容应用）。
#   ★ 只 stop/start 不了（无重启通道）时也【照写】—— 至少 modes.json 是好的；
#      但 config.toml 的改动可能被覆盖，由后面的回读判据报出来。
apply_once() {
    STOPPED=0
    if cc_detect_ctl; then
        if cc_ctl_run stop >/dev/null 2>&1; then
            STOPPED=1
            sleep 2                      # 等 daemon 真正退出并完成落盘
        else
            warn "stop 失败（继续写；config.toml 改动可能被 daemon 覆盖）"
        fi
    fi
    # ★ 写 modes.json（"模式"层：UI 一致性 + 反向同步）
    cat "$TMP" > "$MODE_CONFIG"
    log "已设置 current_active_mode = ${MODE_UID}（档位 ${MODE_ARG}=${MODE_NAME}）"
    # ★★ 写 config.toml 的 [device-settings]（"生效"层：daemon 启动就是读它）
    if [ -f "$CONFIG_TOML" ]; then
        if write_device_settings; then
            log "已把档位内容写入 config.toml 的 [device-settings]"
        else
            warn "写 config.toml 的 [device-settings] 失败（模式可能未真正落地）"
        fi
    fi
    # ★ start（让 daemon 按新的 [device-settings] 应用）
    if [ "$STOPPED" = "1" ]; then
        if cc_ctl_run start >/dev/null 2>&1; then
            log "已启动 CoolerControl（按新配置应用）"
        else
            warn "start 失败：$(cc_ctl_desc)"
            RC=1
        fi
    elif [ "$RESTART" = "1" ]; then
        restart_daemon || RC=1
    fi
    return 0
}

if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] 将 stop → 写 current_active_mode=${MODE_UID}（${MODE_NAME}）"
    log "[dry-run]      → 写 config.toml 的 [device-settings] → start"
    log "[dry-run] 完成（未改动任何文件）"
    rm -f "$TMP"
    exit 0
fi

RC=0
# ★ 确保 apply_on_boot = true —— daemon 启动时【必须】应用 [device-settings]，
#   否则我们写进去的档位在下次重启后不会被应用。这是整个方案的前提！
if [ -f "$CONFIG_TOML" ]; then
    if ensure_apply_on_boot; then
        log "apply_on_boot = true 已确认（daemon 启动会应用 [device-settings]）"
    else
        warn "无法确认/设置 apply_on_boot=true（config.toml 只读？）"
    fi
fi

apply_once; RC=$?
sleep 2
NOW="$(read_back)"

# ★★ 判据核心：config.toml 的真实设置是否【已等于】当前 Mode 内容。
#    这是「模式改了但配置没变」的直接判据 —— 只看 modes.json 不算数。
CFG_OK=2
if [ -f "$CONFIG_TOML" ]; then
    config_matches_mode; CFG_OK=$?
    if [ "$CFG_OK" = "1" ]; then
        warn "config.toml 与当前 Mode【不一致】→ 再落地一次"
        apply_once; RC=$?
        sleep 2
        NOW="$(read_back)"
        config_matches_mode; CFG_OK=$?
    fi
fi
rm -f "$TMP"

if [ "$NOW" = "$MODE_UID" ]; then
    log "确认：current_active_mode = $NOW"
else
    warn "回读值 ${NOW:-（空）} ≠ ${MODE_UID}"
fi

# ─── 简洁判据 ───────────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_TOML" ]; then
    warn "未找到 $CONFIG_TOML（CoolerControl 未安装？）—— 已只写 modes.json"
    exit 0
fi
if [ "$CFG_OK" = "0" ]; then
    log "档位切换完成：${MODE_ARG}(${MODE_NAME}) → ${MODE_UID}（config.toml 已同步）"
    exit 0
fi
# CFG_OK=1/2 ⇒ config.toml 没跟上
warn "★ config.toml 与 Mode 内容不符（档位可能没真正生效）。"
warn "  ① 检查是否有 @DEVICE_UID@ 占位符残留段 ⇒ render-config.py --auto 重渲"
warn "  ② 确认 Mode 的通道名是 daemon 认识的（fanN，非 pwmN）"
warn "  ③ 确认能 stop/start CoolerControl（写 config.toml 需要先停 daemon）"
warn "  排查：bash $(dirname "$0")/probe-coolercontrol.sh sync   # 看 ⑦ 环"
exit 5

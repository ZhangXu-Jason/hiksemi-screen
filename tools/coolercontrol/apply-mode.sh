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
#     apply-mode.sh --dry-run 3       # 只打印将要做的动作，不落盘
#
# 档位映射（与界面一致）：
#     5 智能 → hs-smart      （pwm1=CPU 曲线, pwm2=机箱曲线）
#     2 低速 → hs-low        （Fixed 25%，两路）
#     3 标准 → hs-standard   （Fixed 55%，两路）
#     4 强劲 → hs-high       （Fixed 100%，两路）
#
# 原理（源码级已确认 activate_mode 语义）：
#     CoolerControl 的 Mode 就是 config.toml 里 [device-settings] 的具名快照。
#     激活一个 Mode 会把它的 Setting 写回 config.toml 并应用到引擎。
#     daemon 在【启动时】读取 modes.json 的 current_active_mode 并激活它，
#     因此「改 modes.json → 重启 daemon」即可完成切换，无需调用 API。
#
# ★ 为什么用重启而不是调 API：
#     REST API 需要 daemon 内部 token/会话，且版本间路径会变；
#     重启是稳定、幂等、可离线回放的路径。切换风扇档位是低频操作，
#     重启代价（< 2s，风扇短暂回到开机默认后立刻应用新档）可接受。
# =============================================================================
set -uo pipefail

# 路径可用环境变量覆盖（本地测试 / Docker 挂载路径不同时用）
MODE_CONFIG="${CC_MODE_CONFIG:-/etc/coolercontrol/modes.json}"
CONFIG_TOML="${CC_CONFIG_TOML:-/etc/coolercontrol/config.toml}"

# 容器名 / 服务名候选（按序尝试）
SERVICE_CANDIDATES=(
    "coolercontrold"
    "coolercontrol"
)
CONTAINER_CANDIDATES=(
    "coolercontrol"
    "coolercontrold"
)

DRY_RUN=0

log()  { printf '[fan-apply] %s\n' "$*" >&2; }
warn() { printf '[fan-apply] WARN: %s\n' "$*" >&2; }
die()  { printf '[fan-apply] ERROR: %s\n' "$*" >&2; exit 1; }

# ── 档位 → Mode uid / 名称 ───────────────────────────────────────────────────
mode_uid_for() {
    case "$1" in
        5) echo "hs-smart"    ;;
        2) echo "hs-low"      ;;
        3) echo "hs-standard" ;;
        4) echo "hs-high"     ;;
        *) return 1 ;;
    esac
}
mode_name_for() {
    case "$1" in
        5) echo "智能" ;; 2) echo "低速" ;; 3) echo "标准" ;; 4) echo "强劲" ;;
        *) echo "未知" ;;
    esac
}

cmd_list() {
    cat <<'EOF'
界面档位   fan_mode   CoolerControl Mode   Fixed/曲线（双路 pwm1=CPU / pwm2=机箱）
--------   --------   ------------------   --------------------------------------
智能            5      hs-smart             pwm1→CPU 曲线 + pwm2→机箱曲线
低速            2      hs-low               Fixed 25%（两路）
标准            3      hs-standard          Fixed 55%（两路）
强劲            4      hs-high              Fixed 100%（两路）
EOF
}

# ── 选择可用的重启方式 ───────────────────────────────────────────────────────
have_root() { [ "$(id -u)" -eq 0 ]; }

restart_daemon() {
    if [ "$DRY_RUN" = "1" ]; then
        log "[dry-run] 将重启 CoolerControl daemon"
        return 0
    fi
    # ① systemd 服务（原生安装）
    for svc in "${SERVICE_CANDIDATES[@]}"; do
        if command -v systemctl >/dev/null 2>&1 \
           && systemctl list-unit-files 2>/dev/null | grep -q "^${svc}\.service"; then
            log "systemctl restart ${svc}"
            systemctl restart "$svc" && return 0
            warn "systemctl restart ${svc} 失败，继续尝试其他方式"
        fi
    done
    # ② Docker 容器
    if command -v docker >/dev/null 2>&1; then
        for c in "${CONTAINER_CANDIDATES[@]}"; do
            if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
                log "docker restart ${c}"
                docker restart "$c" && return 0
                warn "docker restart ${c} 失败"
            fi
        done
    fi
    warn "未找到 CoolerControl 服务/容器；配置已写入，需手工重启后生效"
    return 1
}

cmd_status() {
    [ -f "$MODE_CONFIG" ] || die "找不到 $MODE_CONFIG（CoolerControl 未安装或路径不同）"
    python3 - "$MODE_CONFIG" <<'PY'
import json, sys
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
PY
}

# ── 主流程 ───────────────────────────────────────────────────────────────────
MODE_ARG=""
for a in "$@"; do
    case "$a" in
        --list)        cmd_list; exit 0 ;;
        --status)      cmd_status; exit 0 ;;
        --dry-run|-n)  DRY_RUN=1 ;;
        *)             MODE_ARG="$a" ;;
    esac
done

[ -n "$MODE_ARG" ] || { cmd_list; die "缺少档位参数（5/2/3/4）"; }

MODE_UID="$(mode_uid_for "$MODE_ARG")" || die "非法档位 '$MODE_ARG'（只接受 5/2/3/4）"
MODE_NAME="$(mode_name_for "$MODE_ARG")"

if [ ! -f "$MODE_CONFIG" ]; then
    die "找不到 $MODE_CONFIG —— 请先运行 render-config.py 生成配置"
fi
if ! have_root; then
    warn "当前非 root，写 $MODE_CONFIG 与重启 daemon 可能失败"
fi

# 原子更新 modes.json：current_active_mode / previous_active_mode
TMP="$(mktemp)"
if ! python3 - "$MODE_CONFIG" "$MODE_UID" > "$TMP" <<'PY'
import json, sys
path, target = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    d = json.load(f)
uids = [m.get("uid") for m in d.get("modes", [])]
if target not in uids:
    print(f"目标 Mode {target!r} 不在 modes.json 中（可选: {uids}）", file=sys.stderr)
    sys.exit(1)
cur = d.get("current_active_mode")
if cur != target:
    d["previous_active_mode"] = cur
    d["current_active_mode"] = target
print(json.dumps(d, ensure_ascii=False, indent=2))
PY
then
    rm -f "$TMP"
    die "更新 modes.json 失败"
fi

# 原样写回（保留缩进 2）
if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] 将把 current_active_mode 设为 ${MODE_UID}（${MODE_NAME}）"
    rm -f "$TMP"
else
    cat "$TMP" > "$MODE_CONFIG" && rm -f "$TMP"
    log "已设置 current_active_mode = ${MODE_UID}（档位 ${MODE_ARG}=${MODE_NAME}）"
fi

restart_daemon
RC=$?

if [ "$DRY_RUN" = "1" ]; then
    log "[dry-run] 完成（未改动任何文件）"
    exit 0
fi

# 尝试回读确认（daemon 已启动的情况下）
sleep 1
if [ -f "$MODE_CONFIG" ]; then
    NOW="$(python3 -c "import json;print(json.load(open('$MODE_CONFIG',encoding='utf-8')).get('current_active_mode',''))" 2>/dev/null)"
    if [ "$NOW" = "$MODE_UID" ]; then
        log "确认：current_active_mode = $NOW"
    else
        warn "回读值 $NOW ≠ $MODE_UID（daemon 可能重写了该文件）"
    fi
fi

# 成功判据：配置已写入即视为成功（重启失败仅告警，交由 daemon 下次启动生效）
[ -f "$CONFIG_TOML" ] || warn "未找到 $CONFIG_TOML（可能尚未安装）"
log "档位切换完成：${MODE_ARG}(${MODE_NAME}) → ${MODE_UID}"
exit 0

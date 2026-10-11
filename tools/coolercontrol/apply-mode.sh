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
# ★ 必须 export：fan-mode-map.py 通过这两个环境变量定位文件
export CC_MODE_CONFIG="$MODE_CONFIG"
export CC_CONFIG_TOML="$CONFIG_TOML"

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

# ── 档位 → Mode uid ─────────────────────────────────────────────────────────
# ★ 解析交给 fan-mode-map.py（支持 --bind 显式绑定 / 默认 uid / 名字匹配）。
#   该脚本不存在时退回内嵌的写死 uid（功能不降级，只是不支持自定义绑定）。
MAP_PY="${CC_FAN_MODE_MAP_PY:-$(cd "$(dirname "$0")" && pwd)/fan-mode-map.py}"

hardcoded_uid_for() {
    case "$1" in
        5) echo "hs-smart"    ;;
        2) echo "hs-low"      ;;
        3) echo "hs-standard" ;;
        4) echo "hs-high"     ;;
        *) return 1 ;;
    esac
}

resolve_uid_for() {
    hardcoded_uid_for "$1" || return 1      # 先校验档位合法
    if [ -f "$MAP_PY" ]; then
        local u
        if u="$(python3 "$MAP_PY" --resolve "$1" 2>/dev/null)" && [ -n "$u" ]; then
            echo "$u"
            return 0
        fi
    fi
    hardcoded_uid_for "$1"
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
# ★ python 负责：把新 JSON 写进 $TMP，并把【最终采用的 mode uid】打印到 stdout。
#   为什么要把 uid 回传：真机上 Mode 可能是用户在 Web UI 里手工建的，
#   uid 是随机 UUID（1a700658-…）而不是我们的 hs-smart；此时按【名字前缀】
#   回退匹配，否则界面切档会直接失败「目标 Mode 不在 modes.json 中」。
TMP="$(mktemp)"
NEW_UID="$(python3 - "$MODE_CONFIG" "$TMP" "$MODE_UID" "$MODE_ARG" <<'PY'
import json, re, sys
path, tmp, target, fan_mode = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(path, encoding="utf-8") as f:
    d = json.load(f)
modes = d.get("modes", [])
uids = [m.get("uid") for m in modes]
if target not in uids:
    # 兜底：MAP_PY 不在时的【名字前缀】回退（hs-smart → "hs 智能…"）
    prefix = {"hs-smart": "hs 智能", "hs-low": "hs 低速",
              "hs-standard": "hs 标准", "hs-high": "hs 强劲"}.get(target)
    alt = next((m.get("uid") for m in modes
                if prefix and (m.get("name") or "").startswith(prefix)), None)
    if not alt:
        print(f"目标 Mode {target!r} 不在 modes.json 中（可选: {uids}）",
              file=sys.stderr)
        print("★ 若 Mode 是你在 Web UI 里新建的（uid 为随机 UUID），执行：",
              file=sys.stderr)
        print(f"    sudo ./apply-mode.sh --scan            # 看有哪些 Mode",
              file=sys.stderr)
        print(f"    sudo ./apply-mode.sh --bind {fan_mode}=<uid 或 Mode 名字>",
              file=sys.stderr)
        sys.exit(1)
    print(f"[fan-apply] 注意: 未找到 uid={target}，按名字匹配到 {alt}",
          file=sys.stderr)
    target = alt
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

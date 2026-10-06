#!/usr/bin/env bash
# =============================================================================
# probe-coolercontrol.sh —— 真机探测：CoolerControl 安装情况 + device-uid/channel
# =============================================================================
# 只读脚本，不修改任何配置。目的：为 render-config.py 取得必需的参数。
#
# 用法：
#     sudo ./probe-coolercontrol.sh            # 完整探测
#     ./probe-coolercontrol.sh --short         # 只打印可直接复制的一行命令
# =============================================================================
set +e

SHORT=0
[ "${1:-}" = "--short" ] && SHORT=1

SEC() { printf '\n\033[1;36m【%s】\033[0m\n' "$*"; }
OK()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
NG()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
INFO(){ printf '  · %s\n' "$*"; }

CC_CONF_DIR=""
DEVICE_UID=""
CHANNEL=""
CASE_CHANNEL=""
CPU_TEMP_NAME=""
CPU_TEMP_DEVICE_UID=""
CASE_TEMP_NAME=""
CASE_TEMP_DEVICE_UID=""

# ── 1. 定位 CoolerControl 安装形态 ───────────────────────────────────────────
SEC "1. CoolerControl 安装形态"

if command -v coolercontrol >/dev/null 2>&1; then
    OK "CLI: $(command -v coolercontrol)"
    coolercontrol --version 2>/dev/null | sed 's/^/    /'
else
    NG "未找到 coolercontrol CLI"
fi

if systemctl list-unit-files 2>/dev/null | grep -q '^coolercontrold\.service'; then
    OK "systemd 服务 coolercontrold.service 已注册"
    systemctl is-active coolercontrold 2>/dev/null | sed 's/^/    active: /'
else
    INFO "无 coolercontrold.service"
fi

if command -v docker >/dev/null 2>&1; then
    CT="$(docker ps -a --format '{{.Names}}\t{{.Image}}\t{{.Status}}' 2>/dev/null \
          | grep -i coolercontrol)"
    if [ -n "$CT" ]; then
        OK "Docker 容器："
        printf '%s\n' "$CT" | sed 's/^/    /'
    else
        INFO "无 coolercontrol 容器"
    fi
fi

# 配置目录（容器可能挂载在别处）
for d in /etc/coolercontrol /var/lib/coolercontrol; do
    if [ -d "$d" ]; then
        CC_CONF_DIR="$d"
        OK "配置目录存在: $d"
        ls -la "$d" 2>/dev/null | sed 's/^/    /'
    fi
done
[ -n "$CC_CONF_DIR" ] || NG "未找到 /etc/coolercontrol —— CoolerControl 未安装或未启动过"

# ── 2. Web UI / API 端口 ─────────────────────────────────────────────────────
SEC "2. Web UI / API 端口（默认 11987）"
if command -v ss >/dev/null 2>&1; then
    ss -lntp 2>/dev/null | grep -E ':(11987|11988)\b' | sed 's/^/  /' || INFO "11987/11988 未监听"
elif command -v netstat >/dev/null 2>&1; then
    netstat -lntp 2>/dev/null | grep -E ':(11987|11988)\b' | sed 's/^/  /' || INFO "11987 未监听"
fi

if command -v curl >/dev/null 2>&1; then
    R="$(curl -sk --max-time 3 -o /dev/null -w '%{http_code}' https://127.0.0.1:11987/ 2>/dev/null)"
    case "$R" in
        200|302|301|401) OK "https://127.0.0.1:11987 响应 HTTP $R" ;;
        *) INFO "https://127.0.0.1:11987 无响应（code=$R）；若用了 HTTP 试 11987 http" ;;
    esac
fi

# ── 3. 从现有 config.toml 反推 device_uid / channel ─────────────────────────
SEC "3. 现有 config.toml 中的 device-settings（最可靠的 uid 来源）"
if [ -n "$CC_CONF_DIR" ] && [ -f "$CC_CONF_DIR/config.toml" ]; then
    # [devices] 段：uid → 名称
    if grep -q '^\[devices\]' "$CC_CONF_DIR/config.toml"; then
        INFO "[devices] 段："
        awk '/^\[devices\]/{f=1;next} /^\[/{f=0} f&&/=/{print "    "$0}' \
            "$CC_CONF_DIR/config.toml"
    fi
    # [device-settings.<uid>] 段
    if grep -q '^\[device-settings' "$CC_CONF_DIR/config.toml"; then
        INFO "[device-settings] 段（uid + channel）："
        grep -n '^\[device-settings' "$CC_CONF_DIR/config.toml" | sed 's/^/    /'
    else
        INFO "[device-settings] 段为空（尚未在 UI 里配置过风扇）"
    fi
    # Graph profile 里已用的温度源
    INFO "配置中的 temp_source（如有）："
    grep -n 'temp_source' "$CC_CONF_DIR/config.toml" | sed 's/^/    /' || true
else
    NG "无 config.toml 可读"
fi

# 若 UI 配过，[device-settings] 的首个 uid 就是目标
if [ -n "$CC_CONF_DIR" ] && [ -f "$CC_CONF_DIR/config.toml" ]; then
    DEVICE_UID="$(grep -oP '^\[device-settings\.\K[0-9a-f]{64}' \
                  "$CC_CONF_DIR/config.toml" 2>/dev/null | head -1)"
    if [ -z "$DEVICE_UID" ]; then
        DEVICE_UID="$(grep -oP '^\[devices\]' -A200 "$CC_CONF_DIR/config.toml" 2>/dev/null \
                      | grep -oP '^\s*"\K[0-9a-f]{64}' | head -1)"
    fi
fi

# ── 4. daemon 日志里的设备枚举（备用 uid 来源）──────────────────────────────
SEC "4. 设备枚举（journalctl / docker logs，备用 uid 来源）"
if command -v journalctl >/dev/null 2>&1 \
   && systemctl list-unit-files 2>/dev/null | grep -q '^coolercontrold\.service'; then
    journalctl -u coolercontrold --no-pager -n 200 2>/dev/null \
        | grep -iE 'device|uid|hwmon|pwm|fan' | tail -25 | sed 's/^/  /'
elif command -v docker >/dev/null 2>&1; then
    for c in coolercontrol coolercontrold; do
        if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$c"; then
            INFO "docker logs $c（尾部）："
            docker logs --tail 40 "$c" 2>&1 | grep -iE 'device|uid|hwmon|pwm|fan' | sed 's/^/    /'
        fi
    done
fi

# ── 5. 内核侧 hwmon：真实存在哪些 pwm 通道 ──────────────────────────────────
SEC "5. 内核 hwmon 风扇通道（channel 名的来源）"
FOUND_ANY=0
for h in /sys/class/hwmon/hwmon*; do
    [ -d "$h" ] || continue
    NAME="$(cat "$h/name" 2>/dev/null)"
    PWMS="$(ls "$h" 2>/dev/null | grep -E '^pwm[0-9]+$' | tr '\n' ' ')"
    FANS="$(ls "$h" 2>/dev/null | grep -E '^fan[0-9]+_input$' | tr '\n' ' ')"
    if [ -n "$PWMS$FANS" ]; then
        FOUND_ANY=1
        printf '  %s  name=%-16s pwm=[%s] fan=[%s]\n' "$(basename "$h")" "$NAME" "$PWMS" "$FANS"
    fi
done
[ "$FOUND_ANY" = "0" ] && NG "未发现任何 pwm*/fan*_input（风扇可能由 EC 管理，不走 hwmon）"

# ★ 双风扇：把该设备下所有 pwmN 都列出来，前两路分别当 CPU / 机箱候选。
#   本机 it8613 实测有 pwm1(CPU) + pwm2(机箱)，故取前两路。
for h in /sys/class/hwmon/hwmon*; do
    [ -d "$h" ] || continue
    [ "$(cat "$h/name" 2>/dev/null)" = "it8613" ] || continue
    PWMS="$(ls "$h" 2>/dev/null | grep -E '^pwm[0-9]+$' | sort)"
    N="$(printf '%s\n' "$PWMS" | grep -c .)"
    if [ "$N" -ge 2 ]; then
        CHANNEL="$(printf '%s\n' "$PWMS" | sed -n 1p)"
        CASE_CHANNEL="$(printf '%s\n' "$PWMS" | sed -n 2p)"
        OK "检测到 $N 路 PWM → 双风扇：cpu_channel=$CHANNEL  case_channel=$CASE_CHANNEL"
        INFO "★ 前两路默认按「pwm1=CPU、pwm2=机箱」分配；若接线相反，"
        INFO "  用 --cpu-channel/--case-channel 交换即可（渲染前先确认哪个风扇对应哪路）。"
    elif [ "$N" -eq 1 ]; then
        CHANNEL="$(printf '%s\n' "$PWMS" | sed -n 1p)"
        INFO "只检测到 1 路 PWM（$CHANNEL）→ 单风扇，渲染时加 --single-channel"
    fi
done

# ── 6. CPU 温度传感器候选 ────────────────────────────────────────────────────
SEC "6. CPU 温度传感器候选（智能档曲线 temp_source）"
for h in /sys/class/hwmon/hwmon*; do
    [ -d "$h" ] || continue
    NAME="$(cat "$h/name" 2>/dev/null)"
    case "$NAME" in
        coretemp|k10temp|zenpower|cpu_thermal|acpitz)
            F="$(ls "$h" 2>/dev/null | grep -E '^temp[0-9]+_input$' | head -1)"
            if [ -n "$F" ]; then
                V="$(cat "$h/$F" 2>/dev/null)"
                printf '  %s  name=%-12s %s = %s m°C\n' \
                       "$(basename "$h")" "$NAME" "$F" "$V"
                [ -z "$CPU_TEMP_NAME" ] && CPU_TEMP_NAME="$NAME"
            fi
            ;;
    esac
done
[ -n "$CPU_TEMP_NAME" ] && OK "建议 CPU 温度源: name=\"$CPU_TEMP_NAME\"" \
                        || INFO "未自动识别 CPU 温度传感器，请在 Web UI 里查看"

# ── 7. 汇总：可直接复制的渲染命令 ────────────────────────────────────────────
SEC "7. 汇总"
echo "  device_uid        : ${DEVICE_UID:-<请在 Web UI → System 中查看，或读 config.toml>}"
echo "  cpu_channel       : ${CHANNEL:-<通常为 pwm1，见第 5 节>}"
echo "  case_channel      : ${CASE_CHANNEL:-<双风扇时通常为 pwm2；单风扇留空>}"
echo "  cpu_temp_name     : ${CPU_TEMP_NAME:-<见第 6 节>}"
echo "  cpu_temp_dev_uid  : ${CPU_TEMP_DEVICE_UID:-<Web UI 中该传感器所在设备 UID>}"
echo "  case_temp_name    : ${CASE_TEMP_NAME:-<机箱/GPU 温度源，可选；缺省则 pwm2 走兜底>}"
echo "  case_temp_dev_uid : ${CASE_TEMP_DEVICE_UID:-<同上>}"

if [ "$SHORT" = "1" ] && [ -n "$DEVICE_UID" ]; then
    echo
    if [ -n "$CHANNEL" ] && [ -n "$CASE_CHANNEL" ]; then
        # 双风扇
        echo "sudo ./render-config.py --device-uid $DEVICE_UID \\"
        echo "     --cpu-channel $CHANNEL --case-channel $CASE_CHANNEL \\"
        echo "     --cpu-temp-name \"${CPU_TEMP_NAME:-Package id 0}\" \\"
        echo "     --cpu-temp-device-uid <CPU_TEMP_DEVICE_UID> \\"
        echo "     --case-temp-name \"${CASE_TEMP_NAME:-temp1}\" \\"
        echo "     --case-temp-device-uid <CASE_TEMP_DEVICE_UID> \\"
        echo "     --out-dir $CC_CONF_DIR"
    else
        # 单风扇（或未探测到通道）
        echo "sudo ./render-config.py --device-uid $DEVICE_UID --cpu-channel ${CHANNEL:-pwm1} \\"
        echo "     --single-channel \\"
        echo "     --cpu-temp-name \"${CPU_TEMP_NAME:-Package id 0}\" \\"
        echo "     --cpu-temp-device-uid <CPU_TEMP_DEVICE_UID> --out-dir $CC_CONF_DIR"
    fi
fi

echo
echo "提示：最省事的方式是先在 CoolerControl Web UI（https://<NAS>:11987）里"
echo "      随便给风扇设一次转速，让 daemon 把 [device-settings] 段写出来，"
echo "      本脚本第 3 节即可直接读出真实 device_uid 与 channel。"
exit 0

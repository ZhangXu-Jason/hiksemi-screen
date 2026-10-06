#!/bin/bash
# ============================================================
# hiksemi-sub · 统一硬件探测入口 ★ 全项目唯一的 probe 脚本
# ------------------------------------------------------------
# 合并了原 probe-i2c-led.sh / enable-i2c.sh，并把排查期用过的一批
# 一次性 recon 脚本（probe-atmo-final / probe-led-service /
# probe-miniscreen / probe-rest-server / probe-serial-scan）压缩成
# 一个「设备能力快照」env 子命令 —— 结论已经写进 docs/，脚本不再留。
#
# 子命令：
#   （无） / env     设备硬件能力快照（只读）—— 换新机/刷固件后先跑这个
#   i2c-led          ★ I2C 控灯协议交互探测（需 root，会实际点灯）
#   enable-i2c       启用 I2C 灯控通道并持久化到重启
#   fan              委托 coolercontrol/probe-coolercontrol.sh 探测风扇落地条件
#
# 用法：
#   sudo bash probe.sh                # 只读快照，随时可跑
#   sudo bash probe.sh i2c-led        # 灯不亮/换硬件时用，会点灯
#   sudo bash probe.sh enable-i2c     # 全新部署后启用灯控
#   sudo bash probe.sh fan            # 看 CoolerControl 能不能落地
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
BAR="============================================================"
PYBIN="$(command -v python3 || command -v python || true)"

usage() {
    cat <<'EOF'
============================================================
 hiksemi-sub · 统一硬件探测入口 ★ 全项目唯一的 probe 脚本
------------------------------------------------------------
子命令：
  （无） / env     设备硬件能力快照（只读）—— 换新机/刷固件后先跑这个
  i2c-led          ★ I2C 控灯协议交互探测（需 root，会实际点灯）
  enable-i2c       启用 I2C 灯控通道并持久化到重启
  fan              委托 coolercontrol/probe-coolercontrol.sh 探测风扇落地条件

用法：
  sudo bash probe.sh                # 只读快照
  sudo bash probe.sh i2c-led        # 灯不亮/换硬件时用，会点灯
  sudo bash probe.sh enable-i2c     # 全新部署后启用灯控
  sudo bash probe.sh fan            # 看 CoolerControl 能不能落地
============================================================
EOF
}

CMD="${1:-env}"
case "$CMD" in
    -h|--help) usage; exit 0 ;;
    env|i2c-led|enable-i2c|fan) ;;
    *) echo "未知子命令：$CMD（--help 看用法）" >&2; exit 2 ;;
esac

need_root() {
    [ "$(id -u)" = "0" ] || { echo "❌ 请用 root 运行： sudo bash $0 $CMD"; exit 1; }
}

# ══════════════════════════════════════════════════════════════
# env · 设备硬件能力快照（只读）
# ══════════════════════════════════════════════════════════════
run_env() {
    echo "$BAR"
    echo " hiksemi-sub · 设备硬件能力快照（只读）"
    echo " 时间: $(date '+%F %T')   主机: $(hostname)"
    echo " 机型: $(cat /sys/class/dmi/id/product_name 2>/dev/null || echo '?')  内核: $(uname -r)"
    echo "$BAR"

    echo
    echo "############ ① LED 通道（I2C 0x26）############"
    echo "--- i2c_dev 模块 / 设备节点 ---"
    lsmod 2>/dev/null | grep -E "^i2c_dev" || echo "  ❌ i2c_dev 未加载（跑 probe.sh enable-i2c）"
    ls -l /dev/i2c-* 2>/dev/null || echo "  ❌ 无 /dev/i2c-*"
    echo "--- 持久化配置 /etc/modules-load.d/i2c-dev.conf ---"
    cat /etc/modules-load.d/i2c-dev.conf 2>/dev/null | sed 's/^/    /' || echo "    （未配置）"
    echo "--- 逐总线找 0x26 ---"
    local dev b hit=0
    for dev in /dev/i2c-*; do
        [ -c "$dev" ] || continue
        b=${dev##*/i2c-}
        if i2cget -f -y "$b" 0x26 0x90 >/dev/null 2>&1; then
            echo "    ✅ 总线 $b 上有 0x26（0x90=$(i2cget -f -y "$b" 0x26 0x90 2>/dev/null)）"
            hit=1
        fi
    done
    [ "$hit" = 0 ] && echo "    ⚠ 无总线响应 0x26（灯控不可用，或未装 i2c-tools / 非 root）"

    echo
    echo "############ ② 屏控通道（背光 / DPMS / fb）############"
    echo "--- 背光（★ 熄屏首选通道）---"
    local p mb
    for p in /sys/class/backlight/*/brightness; do
        [ -e "$p" ] || continue
        mb=$(cat "$(dirname "$p")/max_brightness" 2>/dev/null)
        echo "    ✅ $p = $(cat "$p" 2>/dev/null)  (max=${mb:-?})"
    done
    [ -e /sys/class/backlight ] || echo "    ⚠ 无背光节点"
    echo "--- DRM 连接器 / DPMS ---"
    local d
    for d in /sys/class/drm/card[0-9]*-*; do
        [ -e "$d" ] || continue
        local w="无dpms"; [ -e "$d/dpms" ] && { [ -w "$d/dpms" ] && w="可写" || w="不可写"; }
        echo "    $(basename "$d" | cut -d- -f2-)  status=$(cat "$d/status" 2>/dev/null)  dpms=${w}"
    done
    echo "--- framebuffer ---"
    for p in /sys/class/graphics/fb*/virtual_size; do
        [ -e "$p" ] || continue
        echo "    $(basename "$(dirname "$p")")  virtual_size=$(cat "$p" 2>/dev/null)  name=$(cat "$(dirname "$p")/name" 2>/dev/null)"
    done
    echo "--- X 是否在跑 ---"
    [ -e /tmp/.X11-unix/X0 ] && echo "    ✅ X0 socket 存在（X 下 blank 多半无效，须走背光）" \
                             || echo "    （无 X0 socket）"

    echo
    echo "############ ③ 输入设备（熄屏空闲判定依赖）############"
    local e _n=0 _ok=0
    for e in /dev/input/event*; do
        [ -e "$e" ] || continue
        _n=$((_n+1)); [ -r "$e" ] && _ok=$((_ok+1))
    done
    echo "    可读 event 设备: $_ok / $_n（身份 $(id -un) uid=$(id -u)）"
    grep -E "^N:" /proc/bus/input/devices 2>/dev/null | sed 's/^/      /'
    command -v xprintidle >/dev/null && echo "    ✅ xprintidle 可用" || echo "    ⚠ 未装 xprintidle（x11-utils）"

    echo
    echo "############ ④ 风扇通道（档位落地依赖）############"
    echo "--- hwmon / pwm ---"
    for d in /sys/class/hwmon/hwmon*; do
        [ -e "$d" ] || continue
        local nm; nm=$(cat "$d/name" 2>/dev/null)
        if ls "$d"/pwm[0-9]* >/dev/null 2>&1; then
            echo "    ✅ $d ($nm): $(ls "$d" | grep -E '^pwm[0-9]' | tr '\n' ' ')"
        fi
    done
    echo "--- thermal cooling_device ---"
    ls /sys/class/thermal/ 2>/dev/null | grep -E "cooling_device" | head -5 | sed 's/^/      /' \
        || echo "      （无）"
    echo "--- CoolerControl 是否安装 ---"
    if [ -f /etc/coolercontrol/config.toml ]; then
        echo "    ✅ /etc/coolercontrol/config.toml 存在（可走 CoolerControl 落地）"
    elif command -v coolercontrold >/dev/null 2>&1 || systemctl list-unit-files 2>/dev/null | grep -qi coolercontrol; then
        echo "    ✅ 检测到 CoolerControl 守护进程/unit（但尚无 config.toml）"
    else
        echo "    ⚠ 未检测到 CoolerControl（风扇档位将只记录+回显，或走 PWM 兜底）"
    fi

    echo
    echo "############ ⑤ 官方服务与配置源（参考）############"
    local u st
    for u in led-set.service pwm-fancontrol.service trim_miniscreen.service set_gpio-init.service; do
        st=$(systemctl is-enabled "$u" 2>/dev/null || echo "n/a")
        printf "    %-28s enabled=%s  active=%s\n" "$u" "$st" "$(systemctl is-active "$u" 2>/dev/null)"
    done
    echo "    /boot/board.json: $([ -f /boot/board.json ] && echo '存在（官方 LED/风扇/GPIO 配置源）' || echo '不存在（官方 setled/setgpio 服务会空跑）')"

    echo
    echo "############ ⑥ 串口（历史排查项，仅供参考）############"
    ls /dev/ttyS* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | tr '\n' ' ' | sed 's/^/    /'
    echo

    echo "$BAR"
    echo " 结论速查："
    echo "   · 灯控  → ① 有 0x26 = 可用；无则 probe.sh enable-i2c"
    echo "   · 熄屏  → ② 有背光节点 = 可用（X 下 blank 无效）"
    echo "   · 触屏  → ③ 全部可读 = 空闲判定正常"
    echo "   · 风扇  → ④ 有 CoolerControl = 四档可落地"
    echo " 详细排查： diag.sh --i2c / diag.sh --screen / probe.sh fan"
    echo "$BAR"
}

# ══════════════════════════════════════════════════════════════
# enable-i2c · 启用 I2C 灯控通道并持久化
# ══════════════════════════════════════════════════════════════
run_enable_i2c() {
    need_root
    echo "$BAR"
    echo " 启用 I2C 灯控通道"
    echo "$BAR"

    echo
    echo "【1】加载内核模块 i2c_dev"
    modprobe i2c_dev 2>&1 | sed 's/^/    /'
    if lsmod | grep -q "^i2c_dev"; then
        echo "    ✅ i2c_dev 已加载"
    else
        echo "    ❌ modprobe i2c_dev 失败，尝试 i2c-dev"
        modprobe i2c-dev 2>&1 | sed 's/^/    /'
        lsmod | grep -q "^i2c_dev" && echo "    ✅ 用 i2c-dev 加载成功" \
            || echo "    ❌ 仍失败（内核可能未编译 i2c-dev，检查 /lib/modules/$(uname -r)/kernel/drivers/i2c/i2c-dev.ko*）"
    fi

    echo
    echo "【2】检查设备节点"
    ls -l /dev/i2c-* 2>/dev/null || echo "    ❌ 仍无 /dev/i2c-*"

    echo
    echo "【3】扫描总线"
    command -v i2cdetect >/dev/null && i2cdetect -l || echo "    （未装 i2c-tools）"

    echo
    echo "【4】探测 0x26 雾灯控制器"
    local dev b
    for dev in /dev/i2c-*; do
        [ -c "$dev" ] || continue
        b=${dev##*/i2c-}
        if i2cget -f -y "$b" 0x26 0x90 >/dev/null 2>&1; then
            echo "    ✅ /dev/i2c-$b 上发现 0x26（0x90=$(i2cget -f -y "$b" 0x26 0x90 2>/dev/null)）"
        else
            echo "    ⚠ /dev/i2c-$b 上无 0x26（或只写，试写）"
            i2cset -f -y "$b" 0x26 0x90 0x0 >/dev/null 2>&1 && echo "       但【写】成功 → 确实是灯控"
        fi
    done

    echo
    echo "【5】持久化：写入 /etc/modules-load.d/i2c-dev.conf"
    echo "i2c_dev" > /etc/modules-load.d/i2c-dev.conf
    cat /etc/modules-load.d/i2c-dev.conf | sed 's/^/    /'
    echo "    ✅ 已配置开机自动加载"

    echo
    echo "$BAR"
    echo " 完成。下一步："
    echo "   1) 重启中间件（或重启整机）让灯控生效"
    echo "   2) 或先跑 probe.sh i2c-led 目视确认灯能亮"
    echo "$BAR"
}

# ══════════════════════════════════════════════════════════════
# i2c-led · I2C 控灯协议交互探测（会点灯）
# ══════════════════════════════════════════════════════════════
run_i2c_led() {
    need_root
    local BUS=1 ADDR=0x26 REG=0x90 LOG=/tmp/led-probe.log
    local C_R="\033[31m" C_G="\033[32m" C_Y="\033[33m" C_B="\033[36m" C_N="\033[0m"
    say()  { echo -e "$*" | tee -a "$LOG"; }
    step() { echo -e "\n${C_B}════════ $* ════════${C_N}" | tee -a "$LOG"; }
    w() { i2cset -f -y "$BUS" "$ADDR" "$REG" "$1" 2>&1 | tee -a "$LOG"; }
    r() { i2cget -f -y "$BUS" "$ADDR" "$REG" 2>&1 | tee -a "$LOG"; }

    echo "hiksemi-sub I2C LED 协议探测  $(date '+%F %T')  主机 $(hostname)" > "$LOG"
    say "${C_Y}$BAR${C_N}"
    say "${C_Y} I2C 控灯协议探测（会实际点灯，请盯着灯看）${C_N}"
    say "${C_Y} 日志：$LOG  —— 跑完请把此文件发回${C_N}"
    say "${C_Y}$BAR${C_N}"

    step "0. 环境检查"
    if ! command -v i2cset >/dev/null 2>&1; then
        say "${C_R}✗ 未安装 i2c-tools${C_N}"
        say "  请先执行： apt-get update && apt-get install -y i2c-tools"
        exit 1
    fi
    say "✓ i2c-tools 已安装"
    if [ ! -e "/dev/i2c-$BUS" ]; then
        say "${C_R}✗ /dev/i2c-$BUS 不存在，尝试加载 i2c_dev 模块${C_N}"
        modprobe i2c_dev 2>&1 | tee -a "$LOG"
        sleep 1
    fi
    say "✓ /dev/i2c-$BUS : $([ -e /dev/i2c-$BUS ] && echo 存在 || echo 仍不存在)"
    [ -e "/dev/i2c-$BUS" ] || { say "${C_R}✗ 总线 $BUS 不可用 —— 先跑 probe.sh enable-i2c${C_N}"; exit 1; }
    say ""; say "--- 全部 i2c 总线 ---"; ls -la /dev/i2c-* 2>&1 | tee -a "$LOG"
    say ""; say "--- i2cdetect -l ---"; i2cdetect -l 2>&1 | tee -a "$LOG"
    say ""; say "--- ${C_Y}i2cdetect -y $BUS（看 0x26 在不在）${C_N} ---"
    i2cdetect -y "$BUS" 2>&1 | tee -a "$LOG"

    step "1. 读取寄存器 0x90 初始值"
    local INIT; INIT=$(r)
    say "当前 0x$REG = ${C_Y}$INIT${C_N}"

    step "2. 逐值写入测试（★ 每步请看灯，然后按回车）"
    say "${C_Y}说明：每次写入后观察氛围灯变化，按回车继续。${C_N}"
    local V RC BACK ANS
    for V in 0x00 0x01 0x02 0x03 0x04 0x05 0x09 0xff; do
        say "──────── 准备写入 0x$REG = ${C_Y}$V${C_N} ────────"
        printf "  按回车执行写入 ... "; read -r _
        w "$V"; RC=$?; BACK=$(r)
        say "  写入 $V 完成(rc=$RC)，回读 0x$REG = ${C_Y}$BACK${C_N}"
        printf "  ${C_G}→ 灯变了吗？变成什么颜色/状态？（直接回车=无变化）: ${C_N}"
        read -r ANS; say "  观察记录: ${ANS:-（无变化）}"; echo "" >> "$LOG"
    done

    step "3. 试探相邻寄存器（可能 R/G/B 分立）"
    say "${C_Y}有些方案用 0x91/0x92/0x93 表示 R/G/B。逐个试。${C_N}"
    local REG2 val
    for REG2 in 0x91 0x92 0x93; do
        say "--- 试寄存器 $REG2 ---"
        printf "  按回车读 $REG2 ... "; read -r _
        val=$(i2cget -f -y "$BUS" "$ADDR" "$REG2" 2>&1); say "  $REG2 当前值 = $val"
        printf "  按回车写 $REG2=0xff ... "; read -r _
        i2cset -f -y "$BUS" "$ADDR" "$REG2" 0xff 2>&1 | tee -a "$LOG"
        printf "  ${C_G}→ 灯有变化吗？: ${C_N}"; read -r ANS; say "  观察: ${ANS:-（无变化）}"
        i2cset -f -y "$BUS" "$ADDR" "$REG2" 0x00 >/dev/null 2>&1
    done

    step "4. 恢复初始值"
    say "恢复 0x$REG = $INIT"; w "$INIT"

    step "完成"
    say "${C_G}探测结束。请把日志发回：$LOG${C_N}"
    say ""
    say "另请补充回答："
    say "  A. 哪个值让灯『亮』？哪个让灯『灭』？"
    say "  B. 不同值能显示不同【颜色】吗？"
    say "  C. 灯是【单色】还是【RGB 可调】？"
    say "  D. 原厂选『随主题/动态色』时，灯是什么表现？"
}

# ══════════════════════════════════════════════════════════════
# fan · 委托 coolercontrol 探测脚本
# ══════════════════════════════════════════════════════════════
run_fan() {
    local p="$SCRIPT_DIR/coolercontrol/probe-coolercontrol.sh"
    if [ -x "$p" ] || [ -f "$p" ]; then
        echo "→ 委托 $p"
        bash "$p" "${@:2}"
    else
        echo "❌ 未找到 $p"
        echo "   若未部署 coolercontrol 工具，请先跑 install-autostart.sh（auto 会自动带上）。"
        exit 1
    fi
}

case "$CMD" in
    env)        run_env ;;
    enable-i2c) run_enable_i2c ;;
    i2c-led)    run_i2c_led ;;
    fan)        run_fan "$@" ;;
esac

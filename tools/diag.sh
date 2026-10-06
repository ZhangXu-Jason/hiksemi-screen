#!/bin/bash
# ============================================================
# hiksemi-sub · 统一诊断入口 ★ 全项目唯一的诊断脚本
# ------------------------------------------------------------
# 合并了原 diagnose.sh（链路分段）+ diag-i2c.sh + diag-off-time.sh，
# 用子命令区分诊断对象，避免同一个项目散落多个 diag 脚本。
#
# 子命令：
#   （无） / --chain   数据链路分段诊断（默认）—— 推荐先跑这个
#   --i2c             I2C 灯控通道诊断（模块/总线/0x26/权限）
#   --screen          自动息屏通道诊断（blank / 背光 / DPMS 实测，★ 交互）
#   --power           关机/重启通道诊断（set_power 依赖）
#   --logs            只看解密后的程序日志（段 5）
#   --net             只验证数据链路（段 3 + 4）
#   --quiet           只输出自动判读结论
#
# ★★ 核心铁律：程序日志（/tmp/subLog/*.log）是 AES-128-ECB 加密的！
#    直接 tail / grep 明文【永远看不到内容】，必须先过 decode-hslog.py。
#
# 用法：
#   sudo bash diag.sh                 # 完整链路诊断
#   sudo bash diag.sh --i2c           # 灯不亮时先跑这个
#   sudo bash diag.sh --screen        # 息屏不生效时跑这个（需人眼确认闪烁）
#   sudo bash diag.sh --logs          # 只看程序日志
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEC="$SCRIPT_DIR/decode-hslog.py"
LOGDIR="/tmp/subLog"
MWLOG="/var/log/hiksemi-sub/middleware.log"
APP_LOG="/var/log/hiksemi-sub/app.log"
KIOSK_LOG="/var/log/hiksemi-sub/kiosk.log"
ADDR="127.0.0.1:20443"
BAR="============================================================"

usage() {
    cat <<'EOF'
============================================================
 hiksemi-sub · 统一诊断入口 ★ 全项目唯一的诊断脚本
------------------------------------------------------------
合并了原 diagnose.sh（链路分段）+ diag-i2c.sh + diag-off-time.sh，
用子命令区分诊断对象。

子命令：
  （无） / --chain   数据链路分段诊断（默认）—— 推荐先跑这个
  --i2c             I2C 灯控通道诊断（模块/总线/0x26/权限）
  --screen          自动息屏通道诊断（blank / 背光 / DPMS 实测，★ 交互）
  --power           关机/重启通道诊断（set_power 依赖）
  --logs            只看解密后的程序日志（段 5）
  --net             只验证数据链路（段 3 + 4）
  --quiet           只输出自动判读结论

★★ 核心铁律：程序日志（/tmp/subLog/*.log）是 AES-128-ECB 加密的！
   直接 tail / grep 明文【永远看不到内容】，必须先过 decode-hslog.py。

用法：
  sudo bash diag.sh                 # 完整链路诊断
  sudo bash diag.sh --i2c           # 灯不亮时先跑这个
  sudo bash diag.sh --screen        # 息屏不生效时跑这个（需人眼确认闪烁）
  sudo bash diag.sh --power         # 关机/重启不生效时跑这个
  sudo bash diag.sh --logs          # 只看程序日志
============================================================
EOF
}

MODE="chain"
for a in "$@"; do
    case "$a" in
        --chain|all)   MODE="chain" ;;
        --i2c)         MODE="i2c" ;;
        --screen)      MODE="screen" ;;
        --power)       MODE="power" ;;
        --logs)        MODE="logs" ;;
        --net)         MODE="net" ;;
        --quiet)       MODE="quiet" ;;
        -h|--help)     usage; exit 0 ;;
        *) echo "未知参数：$a（--help 看用法）" >&2; exit 2 ;;
    esac
done

PYBIN="$(command -v python3 || command -v python || true)"

sec()  { [ "$MODE" = "quiet" ] || { echo; echo "############ $* ############"; }; }
note() { [ "$MODE" = "quiet" ] || echo "$*"; }

# ══════════════════════════════════════════════════════════════
# 通用：解密程序日志（AES-128-ECB）
# ══════════════════════════════════════════════════════════════
dec_logs() {
    [ -z "$PYBIN" ] && { echo "（无 python，无法解密）"; return; }
    [ -f "$DEC" ]   || { echo "（缺解密器 $DEC）"; return; }
    if [ -f "$LOGDIR/HSLog_sub.log" ]; then
        "$PYBIN" "$DEC" "$LOGDIR/HSLog_sub.log" "$@"
    else
        "$PYBIN" "$DEC" "$LOGDIR"/*.log "$@"
    fi
}

# ★ 解密后按关键字 grep —— 这是看程序日志的唯一正确姿势
dec_grep() {
    local pat="$1" n="${2:-60}"
    [ -d "$LOGDIR" ] || { echo "（$LOGDIR 不存在 —— Vue 应用可能未挂载，见 --chain 段 2）"; return; }
    [ -z "$PYBIN" ] && { echo "（无 python，无法解密）"; return; }
    [ -f "$DEC" ]   || { echo "（缺解密器 $DEC）"; return; }
    if [ -f "$LOGDIR/HSLog_sub.log" ]; then
        "$PYBIN" "$DEC" "$LOGDIR/HSLog_sub.log" --grep "$pat" 2>&1 | tail -"$n"
    else
        "$PYBIN" "$DEC" "$LOGDIR"/*.log --grep "$pat" 2>&1 | tail -"$n"
    fi
}

# 打一枪本地接口，输出 HTTP 码 + 响应体
probe_action() {
    local action="$1" path="$2" ts token
    ts=$(date +%s)
    token="$(printf 'HISTORLOCALREQ%s' "$ts" | md5sum | awk '{print $1}')"
    local url="https://${ADDR}${path}?access_token=${token}:${ts}:100000&action=${action}&cmdfrom=devsub"
    local body
    body="$(curl -sk --max-time 8 "$url" 2>&1)"
    printf '  %-22s → %s\n' "$action" "$(echo "$body" | head -c 300)"
}

# ══════════════════════════════════════════════════════════════
# 模式一：--i2c —— I2C 灯控通道诊断
# ══════════════════════════════════════════════════════════════
run_i2c() {
    echo "$BAR"
    echo " I2C 灯控通道诊断  $(date '+%F %T')"
    echo "$BAR"

    echo
    echo "【1】i2c-dev 内核模块是否加载？"
    lsmod 2>/dev/null | grep -E "^i2c_dev" || echo "  ❌ i2c_dev 未加载"
    ls /dev/i2c-* 2>/dev/null && echo "  ↑ /dev/i2c-* 存在" || echo "  ❌ /dev/i2c-* 不存在"

    echo
    echo "【2】i2c 总线列表（i2cdetect -l）"
    command -v i2cdetect >/dev/null 2>&1 && i2cdetect -l \
        || echo "  ❌ 未安装 i2c-tools（apt install -y i2c-tools）"

    echo
    echo "【3】逐条总线探测 0x26（雾灯控制器）"
    for dev in /dev/i2c-*; do
        [ -c "$dev" ] || continue
        b=${dev##*/i2c-}
        echo "  --- 总线 $b ---"
        i2cdetect -y "$b" 2>&1 | sed 's/^/      /'
        echo -n "      i2cget 0x26 0x90 → "
        i2cget -f -y "$b" 0x26 0x90 2>&1 || echo "(读失败)"
    done

    echo
    echo "【4】当前中间件进程的权限与 /dev 可见性"
    local PID
    PID=$(pgrep -f "middleware.py" 2>/dev/null | head -1)
    if [ -n "$PID" ]; then
        echo "  中间件 PID=$PID  用户=$(ps -o user= -p "$PID" 2>/dev/null)"
        echo "  该进程能否看到 /dev/i2c-*："
        ls -la /proc/$PID/fd 2>/dev/null | grep -i i2c || echo "      （无 i2c 设备 fd）"
    else
        echo "  ⚠ 未发现运行中的 middleware.py 进程"
    fi

    echo
    echo "【5】Python 视角：中间件实际能访问的 0x26"
    [ -n "$PYBIN" ] && "$PYBIN" - <<'PY' || echo "  （无 python3，跳过）"
import glob, os, sys
try:
    import fcntl
except ImportError:
    print("  ❌ 无 fcntl 模块"); sys.exit()
print("  glob('/dev/i2c-*') =", glob.glob("/dev/i2c-*"))
print("  os.geteuid() =", os.geteuid(), "(0=root)")
I2C_SLAVE = 0x0703
for dev in sorted(glob.glob("/dev/i2c-*")):
    try:
        fd = os.open(dev, os.O_RDWR)
    except OSError as e:
        print(f"  {dev}: open 失败 {e}"); continue
    try:
        fcntl.ioctl(fd, I2C_SLAVE, 0x26)
        os.write(fd, bytes([0x90]))
        v = os.read(fd, 1)
        print(f"  {dev}: ✅ 0x26 可访问，0x90 = 0x{v[0]:02x}")
    except OSError as e:
        print(f"  {dev}: ❌ 0x26 访问失败 {e}")
    finally:
        os.close(fd)
PY

    echo
    echo "【6】用户实测命令原样复现（总线 1）"
    echo -n "  i2cget -f -y 1 0x26 0x90 → "
    i2cget -f -y 1 0x26 0x90 2>&1 | sed 's/^/      /'
    echo
    echo "  ★ 总线号可能不是 1（本机为 1）。若上面报错，看【3】里哪条总线有 0x26。"
    echo "  ★ 0x90 语义：0=关 / 1=solid / 2=rainbow / 3=breathing"
    echo "    另 0x92/93/94 = RGB 三通道，写序必须是【先 RGB 后模式】。"
    echo
    echo "$BAR"
}

# ══════════════════════════════════════════════════════════════
# 模式二：--screen —— 自动息屏通道诊断（★ 交互，需人眼确认）
# ══════════════════════════════════════════════════════════════
run_screen() {
    echo "$BAR"
    echo " 自动息屏通道诊断  $(date '+%F %T')"
    echo "$BAR"

    echo
    echo "【1】屏幕 blank 路径（★ 多个 fb 时哪个才是小屏）"
    echo "    注：/sys/.../blank 回读值变了 ≠ 屏幕真的黑了（本机踩过：写成功但屏不黑）"
    echo "    ★★★ 识别小屏【最可靠】的判据 = virtual_size（分辨率物理事实）"
    echo "        小屏 = 376,960（竖装）或 960,376"
    echo
    local p fbdir real vs nm conn mark hit root
    for p in /sys/class/graphics/fb*/blank; do
        [ -e "$p" ] || { echo "    ❌ 无 $p"; continue; }
        fbdir=$(dirname "$p"); real=$(readlink -f "$fbdir")
        vs=$(cat "$fbdir/virtual_size" 2>/dev/null); [ -z "$vs" ] && vs=$(cat "$real/virtual_size" 2>/dev/null)
        nm=$(cat "$fbdir/name" 2>/dev/null);         [ -z "$nm" ] && nm=$(cat "$real/name" 2>/dev/null)
        conn=""
        for root in "$(dirname "$real")" "$(dirname "$(dirname "$real")")"; do
            hit=$(ls "$root" 2>/dev/null | grep -iE "DSI|HDMI|eDP|LVDS" | head -1)
            [ -n "$hit" ] && conn="$hit" && break
        done
        mark=""
        case "$vs" in "376,960"|"960,376") mark="  ★★★ 就是小屏！" ;; esac
        echo "    $p"
        echo "        virtual_size=${vs:-?}  name=${nm:-?}  连接器=${conn:-未知}  blank=$(cat "$p" 2>/dev/null)$mark"
    done
    echo
    echo "    💡 若无任何 fb 的 virtual_size 是 376,960 / 960,376"
    echo "       → 内核没为小屏建 framebuffer，写 blank 必然无效，必须走背光/DPMS（见【2】【2b】）"

    echo
    echo "【2】背光节点（★ blank 无效时最直接的手段）"
    local _found_bl=0 mb
    for p in /sys/class/backlight/*/brightness /sys/class/backlight/*/bl_power; do
        [ -e "$p" ] || continue
        mb=$(cat "$(dirname "$p")/max_brightness" 2>/dev/null)
        echo "    ✅ $p = $(cat "$p" 2>/dev/null)   (max=${mb:-?})"
        _found_bl=1
    done
    [ "$_found_bl" = 0 ] && echo "    ⚠ 无 /sys/class/backlight/* 节点（走 PWM 私有通道则不可控）"
    echo "    💡 原厂亮度路径 = /sys/class/backlight/intel_backlight/brightness（值域 0–96000）"

    echo
    echo "【2b】DRM 连接器 / DPMS（★ 另一条关屏通道，对 DSI 小屏有效）"
    local d st dpms_val w
    for d in /sys/class/drm/card[0-9]*-*; do
        [ -e "$d" ] || continue
        st=$(cat "$d/status" 2>/dev/null)
        dpms_val=$(cat "$d/dpms" 2>/dev/null)
        if [ -e "$d/dpms" ]; then [ -w "$d/dpms" ] && w="可写" || w="不可写"; else w="无dpms"; fi
        echo "    $(basename "$d" | cut -d- -f2-)  status=${st:-?}  dpms=${w} 当前=${dpms_val:-?}"
    done

    echo
    echo "【3】输入设备（触摸/按键）"
    ls -l /dev/input/ 2>/dev/null | sed 's/^/    /'
    grep -E "^N:|^H:" /proc/bus/input/devices 2>/dev/null | sed 's/^/    /'
    echo "    --- 中间件能否读输入设备（关键！判据：存在 + 当前身份有 r 权限，不真读避免阻塞）---"
    local _ev_n=0 _ev_ok=0 e
    for e in /dev/input/event*; do
        [ -e "$e" ] || continue
        _ev_n=$((_ev_n+1))
        if [ -r "$e" ]; then
            echo "    ✅ 可读 $e  ($(stat -c '%A %U:%G' "$e" 2>/dev/null))"; _ev_ok=$((_ev_ok+1))
        else
            echo "    ❌ 不可读 $e  ($(stat -c '%A %U:%G' "$e" 2>/dev/null))"
        fi
    done
    echo "    → $_ev_ok / $_ev_n 可读（身份: $(id -un) uid=$(id -u)）"
    if [ "$_ev_ok" = 0 ]; then
        echo "    ⚠ 一个都读不到！中间件非 root 会走『只保存不熄屏』分支。"
        echo "      处置：确认中间件以 User=root 运行（systemd unit），或用 sudo 跑本脚本。"
    fi
    echo "    --- xprintidle 是否可用 ---"
    command -v xprintidle >/dev/null && echo "    ✅ 已安装（返回 $(xprintidle 2>/dev/null) ms）" \
        || echo "    ⚠ 未安装（apt install -y x11-utils）"

    echo
    echo "【3b】★ X server 是否在跑（决定 blank 通道能不能生效）"
    if [ -e /tmp/.X11-unix/X0 ]; then
        echo "    ✅ X0 socket 存在 → 程序跑在 X 上"
        echo "    ⚠⚠ 重要：X 通过 DRM 直接渲染，写 fb0/blank 通常【管不到 X 的画面】！"
        echo "        此时背光是唯一可靠手段（见【2】【8】）。"
    else
        echo "    ⚠ 无 X0 socket（程序可能不在 X 上跑，或用其它 DISPLAY）"
    fi
    if command -v xset >/dev/null 2>&1 && [ -n "$DISPLAY" ]; then
        echo "    DPMS 状态: $(xset q 2>/dev/null | grep -A1 'DPMS' | head -2 | tr '\n' ' ')"
    fi

    echo
    echo "【4】中间件状态文件"
    cat /var/lib/hiksemi-sub/off-time.json 2>/dev/null | sed 's/^/    /' || echo "    （尚无 off-time.json）"

    echo
    echo "【5】输入事件采样（★ 判断是否有设备在持续发噪声）"
    echo "    监测 3 秒，统计每个 event 设备产生的事件数与类型..."
    [ -n "$PYBIN" ] && "$PYBIN" - <<'PYEOF' 2>/dev/null || echo "    ⚠ 需 python3 才能采样"
import glob, os, select, time
EV_NAMES = {0:"SYN",1:"KEY",2:"REL",3:"ABS",4:"MSC",5:"SW"}
fds = {}
for p in sorted(glob.glob("/dev/input/event*")):
    try:
        name = ""
        try:
            with open(f"/sys/class/input/{os.path.basename(p)}/device/name") as f:
                name = f.read().strip()
        except Exception:
            pass
        fds[os.open(p, os.O_RDONLY | os.O_NONBLOCK)] = (p, name)
    except OSError:
        pass
if not fds:
    print("    ❌ 无可用输入设备")
else:
    stat = {fd: {} for fd in fds}
    real = {fd: 0 for fd in fds}
    t0 = time.time()
    while time.time() - t0 < 3:
        rl, _, _ = select.select(list(fds.keys()), [], [], 0.3)
        for fd in rl:
            try:
                data = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                continue
            for i in range(len(data) // 24):
                off = i * 24
                et = int.from_bytes(data[off+16:off+18], "little")
                cd = int.from_bytes(data[off+18:off+20], "little")
                vl = int.from_bytes(data[off+20:off+24], "little", signed=True)
                key = EV_NAMES.get(et, str(et))
                stat[fd][key] = stat[fd].get(key, 0) + 1
                if et == 1 or (et == 3 and cd in (0x35,0x36,0x3a,0x3b,0x39,0x30,0x31,0x32,0x33,0x34,0x37,0x38) and vl != 0) or (et == 2 and vl != 0):
                    real[fd] += 1
    for fd, (p, name) in fds.items():
        kinds = ", ".join(f"{k}×{v}" for k, v in sorted(stat[fd].items())) or "无"
        flag = " ⚠ 只有 SYN/MSC 噪声（中间件会忽略）" if real[fd] == 0 and stat[fd] else ""
        print(f"    {p}  {name!r}")
        print(f"        事件: {kinds}   真实操作数: {real[fd]}{flag}")
    for fd in fds:
        os.close(fd)
PYEOF

    echo
    echo "【6】关机/重启命令可用性（set_power 依赖）"
    local c pp
    for c in systemctl poweroff reboot shutdown; do
        pp=$(command -v "$c" 2>/dev/null)
        [ -n "$pp" ] && echo "    ✅ $c -> $pp" || echo "    ❌ $c 不存在"
    done
    command -v systemctl >/dev/null && echo "    systemd 运行中: $(systemctl is-system-running 2>/dev/null || echo '未知')"

    echo
    # ★ 所有"需要人眼看屏幕"的测试统一用【闪烁】协议：
    #     【7】blank→暗 1 次   【8】背光→暗 2 次   【8b】DPMS→暗 3 次
    local _STEP=0
    _blink() {
        local _times="$1" _off_cmd="$2" _on_cmd="$3" _round=0 i
        echo "          ★ 即将闪烁【$_times 次】→ 屏幕上应看到 $([ "$_times" = 1 ] && echo '暗一次' || echo "暗 $_times 次")"
        while [ "$_round" -lt "$_times" ]; do
            _round=$((_round+1))
            eval "$_off_cmd" 2>/dev/null
            for i in 3 2 1; do printf "\r            ↳ 第 %s/%s 次熄灭 %s 秒  " "$_round" "$_times" "$i"; sleep 1; done
            printf "\r            ↳ 第 %s/%s 次熄灭完成，恢复 2 秒\n" "$_round" "$_times"
            eval "$_on_cmd" 2>/dev/null
            sleep 2
        done
        echo "          ⏳ 闪烁结束（刚才应看到：暗 $_times 次）"
    }

    echo "【7】逐个 fb 测试熄屏 —— 预期【不黑】（X 下 blank 管不到画面）"
    echo "    ⚠ 本机踩坑：写 blank 返回成功、回读值也变了，但屏幕依然亮！"
    echo "    ★ 本节测试会【暗 1 次】—— 若屏幕连暗都不暗，说明 blank 无效。"
    echo
    local BLANK fbname
    for BLANK in /sys/class/graphics/fb*/blank; do
        [ -e "$BLANK" ] || continue
        fbname=$(basename "$(dirname "$BLANK")")
        echo "    ── 测试 $fbname 当前=$(cat "$BLANK" 2>/dev/null) ──"
        printf "       写入 1（黑屏）... 回读="
        echo 1 > "$BLANK" 2>/dev/null
        echo "$(cat "$BLANK" 2>/dev/null)"
        if [ "$(cat "$BLANK" 2>/dev/null)" = "1" ]; then
            _STEP=$((_STEP+1))
            echo "    ╔════════════════════════════════════════════════════════════╗"
            printf "    ║  步骤 %s  %-50s║\n" "$_STEP" "写 $fbname/blank（【暗 1 次】，预期不黑）"
            echo "    ╚════════════════════════════════════════════════════════════╝"
            _blink 1 "echo 1 > '$BLANK'" "echo 0 > '$BLANK'"
            echo "          ┌──────────────────────────────────────────────────────┐"
            echo "          │ 请回答：步骤 $_STEP 有没有【暗 1 次】？(有/一直亮)    │"
            echo "          └──────────────────────────────────────────────────────┘"
        else
            echo "       → ❌ 回读未变，写入失败（无权限？）"
        fi
        echo 0 > "$BLANK" 2>/dev/null
        sleep 1
        echo
    done

    echo
    echo "【8】★★★ 背光通道测试（★ 本机唯一被实测证明有效的熄屏手段）"
    echo "    原厂亮度路径 = /sys/class/backlight/intel_backlight/brightness（0–96000）"
    echo "    新中间件已把【背光】作为首选通道（X 下 blank 多半无效）"
    echo "    ★★ 本节测试会【暗 2 次】—— 看到连暗两次，就是背光通道生效！"
    echo
    local _bl_tested=0 _bl_work="" BL BP orig
    for BL in /sys/class/backlight/*/brightness; do
        [ -e "$BL" ] || continue
        _bl_tested=1
        orig=$(cat "$BL" 2>/dev/null)
        echo "    ── $BL 当前=$orig（写 0=熄背光）──"
        _STEP=$((_STEP+1))
        echo "    ╔════════════════════════════════════════════════════════════╗"
        printf "    ║  步骤 %s  %-50s║\n" "$_STEP" "写 brightness=0（【暗 2 次】，预期黑）"
        echo "    ╚════════════════════════════════════════════════════════════╝"
        _blink 2 "echo 0 > '$BL'" "echo $orig > '$BL'"
        echo "          ┌──────────────────────────────────────────────────────┐"
        echo "          │ 请回答：步骤 $_STEP 有没有【暗 2 次】？(有/一直亮)    │"
        echo "          └──────────────────────────────────────────────────────┘"
        _bl_work="$BL"
        printf "     ↳ 确认还原为 $orig ... "
        echo "$orig" > "$BL" 2>/dev/null && echo "回读=$(cat "$BL" 2>/dev/null)"
        echo
    done
    for BP in /sys/class/backlight/*/bl_power; do
        [ -e "$BP" ] || continue
        _bl_tested=1
        orig=$(cat "$BP" 2>/dev/null)
        echo "    ── $BP 当前=$orig（写 4=FB_BLANK_POWERDOWN）──"
        _STEP=$((_STEP+1))
        echo "    ╔════════════════════════════════════════════════════════════╗"
        printf "    ║  步骤 %s  %-50s║\n" "$_STEP" "写 bl_power=4（【暗 2 次】，预期黑）"
        echo "    ╚════════════════════════════════════════════════════════════╝"
        _blink 2 "echo 4 > '$BP'" "echo $orig > '$BP'"
        echo "          ┌──────────────────────────────────────────────────────┐"
        echo "          │ 请回答：步骤 $_STEP 有没有【暗 2 次】？(有/一直亮)    │"
        echo "          └──────────────────────────────────────────────────────┘"
        printf "     ↳ 确认还原为 $orig ... "
        echo "$orig" > "$BP" 2>/dev/null && echo "回读=$(cat "$BP" 2>/dev/null)"
        echo
    done
    if [ "$_bl_tested" = 0 ]; then
        echo "    ⚠ 无任何背光节点 → 熄屏将不可用（只能依赖 blank/DPMS）"
    elif [ -n "$_bl_work" ]; then
        echo "    ✅ 结论：背光通道【可用】（$_bl_work）—— 新中间件会优先使用它"
    else
        echo "    ⚠ 背光回读正常但需你肉眼确认是否真变暗"
    fi

    echo
    echo "【8b】★ DPMS 通道测试（DRM 层面关屏）—— 本节会【暗 3 次】"
    for d in /sys/class/drm/card[0-9]*-*; do
        [ -e "$d/dpms" ] || continue
        st=$(cat "$d/status" 2>/dev/null)
        [ "$st" = "connected" ] || continue
        orig=$(cat "$d/dpms" 2>/dev/null)
        echo "    ── $(basename "$d" | cut -d- -f2-) 当前 dpms=$orig（写 Off=关输出）──"
        _STEP=$((_STEP+1))
        echo "    ╔════════════════════════════════════════════════════════════╗"
        printf "    ║  步骤 %s  %-50s║\n" "$_STEP" "写 $(basename "$d" | cut -d- -f2-)/dpms=Off（【暗 3 次】）"
        echo "    ╚════════════════════════════════════════════════════════════╝"
        _blink 3 "echo Off > '$d/dpms'" "echo On > '$d/dpms'"
        echo "          ┌──────────────────────────────────────────────────────┐"
        echo "          │ 请回答：步骤 $_STEP 有没有【暗 3 次】？(有/一直亮)    │"
        echo "          └──────────────────────────────────────────────────────┘"
        printf "     ↳ 确认还原 On ... "
        echo On > "$d/dpms" 2>/dev/null && echo "回读=$(cat "$d/dpms" 2>/dev/null)"
        echo
    done

    echo
    echo "    ┌──────────────────────────────────────────────────────────┐"
    echo "    │ ★ 用「暗几次」判断是哪一节在生效：                        │"
    echo "    │     暗 1 次 → 【7】blank（X 下预期暗不了）                │"
    echo "    │     暗 2 次 → 【8】背光 brightness/bl_power（预期能暗）   │"
    echo "    │     暗 3 次 → 【8b】DPMS                                  │"
    echo "    │   只需回传：哪一节真的暗了、暗了几次即可。                │"
    echo "    └──────────────────────────────────────────────────────────┘"

    echo
    echo "【9】★ 中间件实际选中的屏控通道（看日志里的命中行）"
    tail -200 "$MWLOG" 2>/dev/null | \
        grep -E "命中可用 blank|选屏|framebuffer|背光|DPMS|空闲监视启动|未生效" | tail -15 | sed 's/^/      /' \
        || echo "      （无日志，或路径不是 $MWLOG）"
    echo
    echo "$BAR"
}

# ══════════════════════════════════════════════════════════════
# 模式：--power —— 关机/重启通道诊断
# ══════════════════════════════════════════════════════════════
run_power() {
    echo "$BAR"
    echo " 关机/重启通道诊断  $(date '+%F %T')   主机: $(hostname)"
    echo "$BAR"

    echo
    echo "【1】关机/重启命令可用性（set_power 依赖）"
    echo "    set_power 语义：power_mode=0 → 关机 ；power_mode=1 → 重启"
    local c p
    for c in systemctl poweroff reboot shutdown; do
        p=$(command -v "$c" 2>/dev/null)
        [ -n "$p" ] && echo "    ✅ $c -> $p" || echo "    ❌ $c 不存在"
    done

    echo
    echo "【2】systemd 运行状态（poweroff 走 systemd 才可靠）"
    if command -v systemctl >/dev/null 2>&1; then
        echo "    is-system-running : $(systemctl is-system-running 2>/dev/null || echo '未知')"
        echo "    default.target    : $(systemctl get-default 2>/dev/null || echo '?')"
    else
        echo "    ❌ 无 systemctl（init 非 systemd，set_power 可能失败）"
    fi

    echo
    echo "【3】容器/沙箱环境检测（会拦住 poweroff 的典型情形）"
    if [ -f /.dockerenv ] || grep -qa 'docker\|lxc' /proc/1/cgroup 2>/dev/null; then
        echo "    ⚠ 检测到容器环境 → systemctl poweroff 多半无效（需从宿主机操作）"
    else
        echo "    ✅ 非容器环境"
    fi
    echo "    PID 1 = $(cat /proc/1/comm 2>/dev/null || echo '?')（应为 systemd）"

    echo
    echo "【4】中间件是否 root 运行（非 root 无法真正关机）"
    local PID
    PID=$(pgrep -f "middleware.py" 2>/dev/null | head -1)
    if [ -n "$PID" ]; then
        echo "    ✅ 中间件 PID=$PID  用户=$(ps -o user= -p "$PID" 2>/dev/null)"
        [ "$(ps -o user= -p "$PID" 2>/dev/null)" != "root" ] && \
            echo "       ⚠ 非 root → set_power 只能记录，无法执行"
    else
        echo "    ⚠ 未发现运行中的 middleware.py 进程"
    fi

    echo
    echo "【5】真实发起过 set_power 吗（看中间件日志）"
    grep -E "set_power|power_mode" "$MWLOG" 2>/dev/null | tail -10 | sed 's/^/      /' \
        || echo "      （无记录，或日志路径不是 $MWLOG）"

    echo
    echo "【6】登录会话（poweroff 会杀掉本会话，属正常）"
    command -v who >/dev/null && who 2>/dev/null | sed 's/^/      /' || echo "      （who 不可用）"

    echo
    echo "$BAR"
    echo " 排查指引（详见 docs/power-control.md）："
    echo "   · 点关机无反应 → 看【1】命令是否缺失、【2】是否 systemd、【4】是否 root"
    echo "   · 点关机后界面卡住 → 属正常（先回包再异步执行，见文档）"
    echo "$BAR"
}

# ══════════════════════════════════════════════════════════════
# 模式三：--chain / --logs / --net / --quiet —— 数据链路分段诊断
# ══════════════════════════════════════════════════════════════
run_chain() {
if [ "$MODE" = "chain" ]; then
echo "$BAR"
echo " hiksemi-sub 小屏诊断（链路分段版）"
echo " 时间: $(date '+%F %T')   主机: $(hostname)"
echo "$BAR"
fi

if [ "$MODE" = "chain" ]; then
sec "段 0 · X 服务（没有 X 则程序无法显示）"
note "--- /tmp/.X11-unix/ ---";        ls -l /tmp/.X11-unix/ 2>&1
note "--- Xorg 进程 ---";              pgrep -a Xorg 2>&1 || echo "（无 Xorg 进程）"
note "--- systemd 服务状态 ---";       systemctl status hiksemi-sub --no-pager 2>&1 | head -8

sec "段 1 · 程序进程"
ps aux | grep -E "hiksemi-sub|middleware|kiosk|xinit" | grep -v grep || echo "（未发现相关进程）"

sec "段 2 · 端口探针（41598 = Vue 已挂载，20443 = 中间件）"
note "--- 监听情况 ---"
ss -lntp 2>/dev/null | grep -E "41598|20443" || echo "（41598 / 20443 均未监听）"
note "--- TMPDIR / 服务环境 ---"
note "TMPDIR=$TMPDIR"
systemctl show hiksemi-sub -p Environment 2>/dev/null
note "--- /screen 目录（主题初始化会阻塞 mount）---"
ls -ld /screen /screen/lang.config /screen/themes 2>&1
note "--- lang.config 内容 ---"
cat /screen/lang.config 2>&1

# ★ 附加段：飞牛（fnOS）自带监控服务 —— 「装了本服务后 Web UI 的 CPU/网络/存储全空」看这里
#   根因：resmon_service / sysinfo_service 开机要「挂载设备接口」（GPU/DRM），
#   与我们的 xinit 抢核显相撞 → 它们静默降级（服务 active 但指标全空），restart 才恢复。
#   修复三重：After=resmon/sysinfo + ExecStartPre --pre-delay + 开机后兜底补挂。
if command -v systemctl >/dev/null 2>&1; then
sec "附加段 · 飞牛自带监控服务（Web UI 监控全空时看这里）"
_HAS_FNOS=0
for _u in resmon_service sysinfo_service; do
    if systemctl cat "$_u" >/dev/null 2>&1; then
        _HAS_FNOS=1
        _st="$(systemctl is-active "$_u" 2>/dev/null)"
        note "--- $_u : $_st ---"
        case "$_st" in
            active) : ;;
            *) echo "  ✗ $_u 未运行 → Web UI 对应指标必空；先 sudo systemctl restart $_u" ;;
        esac
    fi
done
if [ "$_HAS_FNOS" = "1" ]; then
    note "--- hiksemi-sub 是否已排到监控服务之后（After=）---"
    systemctl show hiksemi-sub -p After 2>/dev/null | tr ' ' '\n' \
        | grep -E "resmon_service|sysinfo_service" || \
        echo "  ⚠ After= 未含 resmon/sysinfo → 开机未错开抢核显（重跑 install-autostart.sh 可修）"
    note "--- kiosk.log 的开机让路 / 补挂记录 ---"
    grep -nE "开机窗口|补挂" "$KIOSK_LOG" 2>/dev/null | tail -5 || echo "  （无记录：非开机启动，或装的还是旧版 kiosk.sh）"
    note "--- 这两个服务的最近日志（看有无「未挂上设备接口」/probe 异常）---"
    journalctl -b -u resmon_service -u sysinfo_service --no-pager 2>/dev/null | tail -8
fi
fi
fi

if [ "$MODE" = "chain" ] || [ "$MODE" = "net" ]; then
sec "段 3 · 中间件日志（未加密，看程序到底请求过什么）"
note "--- 最近 30 行 ---"
tail -30 "$MWLOG" 2>&1
note "--- openinfo 出现次数（>3 = 启动页在重试循环）---"
grep -c "openinfo" "$MWLOG" 2>/dev/null || echo 0

sec "段 4 · ★ 数据链路验证（逐 action 打真实 URL，核对响应）"
note "  中间件在 20443 上是否真的能返回正确数据 —— 绕过程序直接验证"
note ""
if ss -lnt 2>/dev/null | grep -q ':20443'; then
    # ★ 与客户端真实 URL 路径严格对齐（见 docs/local-rest-api.md）
    probe_action "get_sys_info"        "/rest/1.0/config"
    probe_action "get_hw_resource_info" "/rest/1.2/config"
    probe_action "space_manage"        "/rest/1.2/config"
    probe_action "get_space_list"      "/rest/1.2/quota"
    probe_action "get_eth_info_v2"     "/rest/1.1/config"
    probe_action "get_network_mode"    "/rest/1.1/config"
    note ""
    note "--- ★ 关键字段类型检查（is_active 必须是数字 1）---"
    local ts token
    ts=$(date +%s)
    token="$(printf 'HISTORLOCALREQ%s' "$ts" | md5sum | awk '{print $1}')"
    curl -sk --max-time 8 \
      "https://${ADDR}/rest/1.1/openinfo?access_token=${token}:${ts}:100000&action=get_sys_info&cmdfrom=devsub" \
      2>/dev/null | "$PYBIN" -c "
import sys, json
try:
    d = json.load(sys.stdin)
    ia = d.get('is_active')
    ok = isinstance(ia, int) and ia == 1
    print('  is_active      =', repr(ia), '(', type(ia).__name__, ') → 严格===1:', 'PASS' if ok else 'FAIL ← 会卡启动页')
    pm = d.get('product_model')
    print('  product_model  =', repr(pm), '→ 含 R1-A:', 'YES(会卡!)' if isinstance(pm, str) and 'R1-A' in pm else 'NO(正常)')
    print('  serial_number  =', repr(d.get('serial_number')))
    print('  mac            =', repr(d.get('mac')))
    print('  ★ 网卡(exe)    =', repr(d.get('exe_name') or d.get('eth_name') or '(未返回)'))
except Exception as e:
    print('  解析失败:', e, '（中间件可能未响应或返回非 JSON）')
" 2>&1
else
    note "  ✗ 20443 未监听 → 跳过。先看段 3 中间件日志，确认服务启动情况。"
fi
fi

if [ "$MODE" = "chain" ] || [ "$MODE" = "logs" ]; then
sec "段 5 · ★ 程序日志（AES-128-ECB 加密，已自动解密）"
note "--- 日志文件 ---"
ls -lt "$LOGDIR"/ 2>&1

note ""
note "--- ★ 启动判定全过程 getDeviceStatus（决定性）---"
dec_grep "getDeviceStatus|设备状态|绑定|openinfo|R1-A|is_active" 60

note ""
note "--- ★ 异常行（命中 = 直接根因）---"
dec_grep "异常|Error|错误|失败|Cannot|undefined|not a function|TypeError" 40

note ""
note "--- ★ 网络页相关（IP / eth）---"
dec_grep "eth|网卡|network|get_eth" 30

note ""
note "--- 解密后完整尾部（最近 50 行，全景）---"
dec_logs 2>&1 | tail -50

note ""
note "--- 全盘搜日志（防路径漂移）---"
find /tmp /var/tmp /root /home -maxdepth 3 \
     \( -name "HSLog*" -o -name "screenApp.log" -o -type d -name subLog \) 2>/dev/null

note ""
note "--- Electron 原始输出 app.log（未加密，崩溃/mount 失败的关键线索）---"
tail -30 "$APP_LOG" 2>&1
note "--- 关键字命中 ---"
grep -nE "FATAL|Error|Failed|Cannot|EPERM|EACCES|ENOENT|not found|GPU|Sandbox|libva|libGL|Segmentation|initTheme|getlogPath" \
     "$APP_LOG" 2>/dev/null | head -20 || echo "（无命中或文件不存在）"

note ""
note "--- kiosk.log ---"
tail -20 "$KIOSK_LOG" 2>&1

note ""
note "--- ★ kiosk 退出码 / 前置检查失败（xinit 只回报同一个码，真因在这里）---"
if grep -q "程序本体前置检查未通过" "$KIOSK_LOG" 2>/dev/null; then
    echo "  ✗ 最近一次是【程序本体前置检查失败】—— 程序目录/主程序不存在或不可执行："
    grep -A10 "程序本体前置检查未通过" "$KIOSK_LOG" 2>/dev/null | tail -14 | sed 's/^/    /'
else
    _last_rc="$(grep -oE 'hiksemi-sub 退出，rc=[0-9]+' "$KIOSK_LOG" 2>/dev/null | tail -1)"
    if [ -n "$_last_rc" ]; then
        echo "  最近一次：$_last_rc"
        grep -A8 "退出码解读" "$KIOSK_LOG" 2>/dev/null | tail -10 | sed 's/^/    /'
    else
        echo "  （暂无退出码记录 —— 可能程序仍在运行，或 kiosk.log 被清空）"
    fi
fi
fi

if [ "$MODE" != "logs" ] && [ "$MODE" != "net" ]; then
echo
echo "$BAR"
echo "===== 自动判读（按此结论决定下一步）====="
echo "$BAR"
HAS_APP=$(pgrep -f "hiksemi-sub" 2>/dev/null | grep -v "^$$\$" | head -1)
[ -z "$HAS_APP" ] && HAS_APP=$(ps aux 2>/dev/null | grep "[h]iksemi-sub" | head -1)
HAS_X=$(pgrep -x Xorg 2>/dev/null | head -1)
[ -z "$HAS_X" ] && HAS_X=$(ps aux 2>/dev/null | grep "[X]org" | head -1)
HAS_41598=$(ss -lnt 2>/dev/null | grep -c ':41598')
HAS_20443=$(ss -lnt 2>/dev/null | grep -c ':20443')
HAS_SUBLOG=$([ -d "$LOGDIR" ] && echo 1 || echo 0)
HAS_OPENINFO=$(grep -c "openinfo" "$MWLOG" 2>/dev/null || echo 0)

echo "程序进程(hiksemi-sub) : $([ -n "$HAS_APP" ] && echo 有 || echo 无)"
echo "X 服务(Xorg)          : $([ -n "$HAS_X" ] && echo 有 || echo 无)"
echo "41598 (Vue 已挂载)    : $([ "$HAS_41598" -gt 0 ] && echo 监听中 || echo 未监听)"
echo "20443 (中间件)        : $([ "$HAS_20443" -gt 0 ] && echo 监听中 || echo 未监听)"
echo "/tmp/subLog/ 目录     : $([ "$HAS_SUBLOG" = "1" ] && echo 存在 || echo 不存在)"
echo "中间件收到 openinfo   : $HAS_OPENINFO 次"
echo
echo "----- 链路分段结论 -----"
if [ -z "$HAS_APP" ]; then
    # ★ 区分「程序从没起来」与「起来后退出」—— 前者看前置检查，后者看退出码
    if grep -q "程序本体前置检查未通过" "$KIOSK_LOG" 2>/dev/null; then
        echo "✗ 段 1 断【前置检查失败】：主程序/目录不存在或不可执行（kiosk.sh 已拒绝启动）"
        echo "  → 段 5 的 kiosk.log 里有完整修复指引；核对 APP_DIR 是否指向真实程序目录"
        echo "  → 快速修：sudo bash \${TOOLS}/install-autostart.sh --dir <程序目录>"
    elif grep -q "退出码解读" "$KIOSK_LOG" 2>/dev/null; then
        echo "✗ 段 1 断【程序起来后退出】：kiosk.sh 已把退出码与 app.log 尾部写进 kiosk.log"
        echo "  → 段 5 的「退出码解读」即病因；rc=1 多为应用层报错（常见 /screen 不可写）"
    else
        echo "✗ 段 1 断：程序没在跑 → 看段 5 的 kiosk.log / app.log"
    fi
elif [ -z "$HAS_X" ]; then
    echo "✗ 段 0 断：X 没起来 → 程序无法显示 → 看段 0 的 Xorg 输出"
elif [ "$HAS_20443" -eq 0 ]; then
    echo "✗ 段 3 断：中间件未监听 20443 → 程序必然无数据 → 看段 3 的 middleware.log"
elif [ "$HAS_41598" -eq 0 ]; then
    echo "✗ 段 2 断【情形 A】：41598 未监听 = Vue 应用未挂载（App.vue onMounted 未执行）"
    echo "  → 卡在 mount 之前：X / chunk 加载 / initThemeConfigInNas"
    echo "  → 决定性线索在段 5 的 app.log 里 Electron 报错"
elif [ "$HAS_SUBLOG" = "0" ]; then
    echo "⚠ 段 5 断【情形 B】：41598 已监听但 /tmp/subLog/ 不存在"
    echo "  → 应用已挂载，但渲染进程日志未初始化 → 看 app.log 里 getlogPath 相关报错"
elif [ "$HAS_OPENINFO" = "0" ]; then
    echo "⚠ 段 4 断【情形 C】：应用已挂载 + 日志已生成，但中间件从未收到 openinfo"
    echo "  → 卡在 getDeviceStatus()：/screen/lang.config 读写失败 或 sessionStorage 异常"
    echo "  → 查段 2 的 /screen 权限，看段 5 解密后的子进程日志"
elif [ "$HAS_OPENINFO" -gt 3 ]; then
    echo "★ 段 4 异常：openinfo 出现 $HAS_OPENINFO 次（>3）= getDeviceStatus() 反复返回 false"
    echo "  → 看段 5 解密后的「设备状态-获取异常：xxx」即为根因"
    echo "  → 若无异常行，则是 model 判定问题（看段 4 的 product_model 是否含 R1-A）"
else
    echo "✓ 链路完整（有 openinfo 且未陷入重试）→ 问题在数据内容，非链路"
    echo "  → 重点看段 5 解密后日志里的具体页面字段；段 4 已逐个 action 验证响应形态"
fi
echo "$BAR"
echo
echo " 提示：日志是加密的，别直接 tail /tmp/subLog/*.log（只会看到乱码）。"
echo "       要手动看某关键字： python3 $DEC $LOGDIR/HSLog_sub.log --grep 关键字"
echo "       灯不亮 → sudo bash diag.sh --i2c"
echo "       息屏无效 → sudo bash diag.sh --screen"
echo "$BAR"
fi
}

case "$MODE" in
    i2c)    run_i2c ;;
    screen) run_screen ;;
    power)  run_power ;;
    *)      run_chain ;;
esac

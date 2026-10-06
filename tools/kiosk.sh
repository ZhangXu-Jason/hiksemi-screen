#!/bin/bash
# ============================================================
# hiksemi-sub Kiosk 启动脚本
# ------------------------------------------------------------
# 由 xinit 在 X(:0) 就绪后执行，负责：
#   0. 等 X(:0) 真正可连接（开机早期把「必失败一次」变成「一次成功」）
#   0.1 [--pre-delay 模式] 开机让路：等 fnOS 监控服务先「挂上设备接口」，再拉起 X
#       （由 systemd 的 ExecStartPre 调用 —— 这是唯一跑在 xinit 之前的位置）
#   1. 准备日志目录
#   2. 清屏 / 隐藏光标 / 关熄屏
#   3. 拉起窗口管理器（openbox）
#   4. 后台拉起服务中间件（随程序一起启停）
#   4.9 兜底：开机竞态下自动补挂 fnOS 监控服务（= 手工 restart resmon/sysinfo）
#   5. 前台运行 hiksemi-sub 本体
#
# 退出码 = hiksemi-sub 本体退出码（末尾 exit 透传），并写进 kiosk.log 解读。
#
# 用法（由 systemd 调用）：
#   /usr/bin/xinit /你的目录/hiksemi-sub/kiosk.sh -- :0 vt1 -nolisten tcp
#   /你的目录/hiksemi-sub/kiosk.sh --pre-delay     # 仅开机让路，不启 X（ExecStartPre）
# ============================================================

# ★ 自动探测程序目录：kiosk.sh 就在程序根目录下，故取其所在目录。
#   这样整个目录搬到任意位置（/opt、/usr/local、/srv...）都无需改脚本。
#   也支持用环境变量 HIKSEMI_DIR 显式覆盖。
if [ -n "$HIKSEMI_DIR" ]; then
    APP_DIR="$HIKSEMI_DIR"
else
    APP_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
fi
export DISPLAY=:0

# ★ 工具目录探测（tools/）：程序本体可能是 AppImage（解包/挂载目录易失），
#   故工具目录允许落在【持久位置】。按优先级取第一个存在的：
#     ① $HIKSEMI_TOOLS 显式指定
#     ② $APP_DIR/tools        （程序自带，向后兼容）
#     ③ /screen/tools         （★ 推荐：持久目录，程序更新不影响工具）
if [ -n "$HIKSEMI_TOOLS" ] && [ -d "$HIKSEMI_TOOLS" ]; then
    TOOLS_DIR="$HIKSEMI_TOOLS"
elif [ -d "$APP_DIR/tools" ]; then
    TOOLS_DIR="$APP_DIR/tools"
elif [ -d /screen/tools ]; then
    TOOLS_DIR="/screen/tools"
else
    TOOLS_DIR="$APP_DIR/tools"   # 兜底（后续会提示缺失）
fi

# ── 0. 日志目录 ──
# 统一放在 /var/log/hiksemi-sub/，一个服务一个目录。
# 回退策略：/var/log 不可写时退到程序目录下的 logs/（无 root 也能跑）
LOG_DIR=""
if mkdir -p /var/log/hiksemi-sub 2>/dev/null && [ -w /var/log/hiksemi-sub ]; then
    LOG_DIR="/var/log/hiksemi-sub"
else
    LOG_DIR="$APP_DIR/logs"
    mkdir -p "$LOG_DIR" 2>/dev/null
fi

KIOSK_LOG="$LOG_DIR/kiosk.log"
MID_LOG="$LOG_DIR/middleware.log"
XORG_LOG="$LOG_DIR/xorg.log"

# ── ★ 0.1 开机让路（--pre-delay）：给 fnOS 监控服务留出「挂设备接口」的时间窗 ──
# 由 systemd 的 ExecStartPre 调用：kiosk.sh --pre-delay
#   —— 这是整条链上**唯一跑在 xinit 之前**的位置，因此也是唯一能阻止
#      「我们的 X 抢核显」与「resmon/sysinfo 挂设备」相撞的地方。
#
# 背景（真机实测 + 飞牛官方社区确认）：
#   fnOS 的 resmon_service / sysinfo_service 开机时会「挂载设备接口」（GPU/DRM 等）。
#   若这一刻核显正被我们的 X 抢占（xinit 一上来就 open /dev/dri/card0 并抢 DRM
#   master），它们会【静默降级】：服务仍是 active，但 Web UI 的 CPU 占用 /
#   网络流量 / 存储读写【全部无数据】，必须手工
#       sudo systemctl restart resmon_service sysinfo_service
#   才能恢复。官方回复原话即「资源监控未挂上设备接口」。
#
# 做法：只在【开机窗口内】（uptime < HIKSEMI_BOOT_WINDOW_SEC，默认 300s）等待
#   HIKSEMI_BOOT_DELAY_SEC 秒（默认 8）再放行；
#   非开机窗口（例如手工 systemctl restart）不等待，避免拖慢日常调试。
if [ "${1:-}" = "--pre-delay" ]; then
    _up="$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 9999)"
    case "$_up" in ''|*[!0-9]*) _up=9999 ;; esac
    : "${HIKSEMI_BOOT_WINDOW_SEC:=300}"
    : "${HIKSEMI_BOOT_DELAY_SEC:=8}"
    if [ "$_up" -lt "$HIKSEMI_BOOT_WINDOW_SEC" ] && [ "$HIKSEMI_BOOT_DELAY_SEC" -gt 0 ]; then
        echo "[kiosk] ⏳ 开机窗口（uptime=${_up}s）：先等 ${HIKSEMI_BOOT_DELAY_SEC}s，让 fnOS 监控服务挂上设备接口，再拉起 X" >> "$KIOSK_LOG"
        sleep "$HIKSEMI_BOOT_DELAY_SEC"
    fi
    exit 0
fi

# ── 0.5 程序运行必需目录 /screen/ ──
# 客户端（渲染进程）会读写绝对路径 /screen/lang.config 与 /screen/themes/。
# 缺失时程序可能卡在「启动中/绑定页」或界面无数据，所以这里兜底创建。
if mkdir -p /screen/themes/default 2>/dev/null; then
    [ -f /screen/lang.config ] || echo '{"lang":"CN"}' > /screen/lang.config 2>/dev/null
    echo "[kiosk] /screen 目录就绪" >> "$KIOSK_LOG"
else
    echo "[kiosk] ⚠ 无法创建 /screen（程序可能无数据，需 root 执行）" >> "$KIOSK_LOG"
fi

# ── ★ 0.6 开机时序：等 X(:0) 真正可连接 ──────────────────────────────────────
# 背景：xinit 由它自己拉起 X，但【X 刚 fork 出来时 unix socket 尚未建立】。
#   此时立刻执行下面的 xsetroot / openbox，或后面 Electron 连 X，
#   都会因为「连不上 :0」而秒退（表现为服务 rc=1，重试后才成功）。
#   开机早期尤其明显：VT/DRM 未就绪、getty 仍在抢 tty1、/dev/dri 权限未定。
# 做法：socket 存在 + 能真正连上（xdpyinfo 最可靠）双确认，最多等 ~15s。
#   这能把「开机必失败一次（靠 Restart 兜）」变成「开机一次成功」。
#   HIKSEMI_X_WAIT_SEC 可调上限（0 = 不等，排障用）。
: "${HIKSEMI_X_WAIT_SEC:=15}"
_wait_x() {
    local i=0 max="$HIKSEMI_X_WAIT_SEC"
    [ "$max" = "0" ] && return 0
    while [ "$i" -lt "$max" ]; do
        if [ -S /tmp/.X11-unix/X0 ]; then
            if command -v xdpyinfo >/dev/null 2>&1; then
                xdpyinfo -display :0 >/dev/null 2>&1 && return 0
            else
                xsetroot -solid black -display :0 2>/dev/null && return 0
            fi
        fi
        i=$((i+1))
        sleep 1
    done
    return 1
}
if _wait_x; then
    echo "[kiosk] ✓ X(:0) 已就绪（≤${HIKSEMI_X_WAIT_SEC}s）" >> "$KIOSK_LOG"
else
    echo "[kiosk] ⚠ X(:0) 在 ${HIKSEMI_X_WAIT_SEC}s 内仍不可连接 —— 继续，失败请看 $XORG_LOG" >> "$KIOSK_LOG"
fi

# ── 1. 屏幕清理：黑底 + 隐藏鼠标指针 ──
xsetroot -solid black 2>/dev/null || true
# unclutter 装了才用，没装不影响
command -v unclutter >/dev/null 2>&1 && unclutter -idle 0 -root &

# ── 2. 关掉 DPMS / 屏保，避免小屏自动熄灭 ──
xset s off 2>/dev/null || true
xset -dpms 2>/dev/null || true
xset s noblank 2>/dev/null || true

# ── 3. 窗口管理器 ──
# openbox 负责窗口装饰与焦点；kiosk 场景其实可有可无，
# 但留着他能让窗口正常铺满、触摸焦点不出问题。
command -v openbox-session >/dev/null 2>&1 && openbox-session &
sleep 1

echo "[kiosk] === 启动 $(date '+%F %T') | 日志目录 $LOG_DIR ===" >> "$KIOSK_LOG"

# ── 4. 服务中间件（真实数据采集，替代早期 mock）──
# 监听 20443，按 data-mapping.json 的映射规则
# 从 /proc、/sys/class/hwmon 等采集真实数据供程序显示
# 位置优先级：$TOOLS_DIR/middleware/ → $APP_DIR/middleware/（标准/兼容）
MID_PID=""
MID_DIR=""
if [ -f "$TOOLS_DIR/middleware/middleware.py" ]; then
    MID_DIR="$TOOLS_DIR/middleware"
elif [ -f "$APP_DIR/middleware/middleware.py" ]; then
    MID_DIR="$APP_DIR/middleware"
fi

# 选可用的 python3（部分系统只有 python / python3.x）
PYBIN="$(command -v python3 || command -v python || true)"

if [ -z "$PYBIN" ]; then
    # 无 Python：这是「日志没生成」最常见原因
    echo "[kiosk] ✗ 找不到 python3，中间件无法启动！请 apt install -y python3" >> "$KIOSK_LOG"
    echo "[kiosk] ✗ 找不到 python3，中间件无法启动" >&2
elif [ -z "$MID_DIR" ]; then
    echo "[kiosk] ✗ 未找到 middleware.py（已查找 $TOOLS_DIR/middleware/ 与 $APP_DIR/middleware/）" >> "$KIOSK_LOG"
else
    # 启动前逐项校验，缺什么直接写进 kiosk.log（否则日志里什么线索都看不到）
    MID_PROBLEM=""
    [ -f "$MID_DIR/data-mapping.json" ] || MID_PROBLEM="$MID_PROBLEM 缺少 data-mapping.json;"
    [ -f "$MID_DIR/cert.pem" ]          || MID_PROBLEM="$MID_PROBLEM 缺少 cert.pem;"
    [ -f "$MID_DIR/key.pem" ]           || MID_PROBLEM="$MID_PROBLEM 缺少 key.pem;"
    if [ -n "$MID_PROBLEM" ]; then
        echo "[kiosk] ✗ 中间件前置文件不全：$MID_PROBLEM" >> "$KIOSK_LOG"
        echo "[kiosk]   目录：$MID_DIR（请运行 install-autostart.sh 生成证书）" >> "$KIOSK_LOG"
    fi

    # 日志文件先建好并 touch，保证「日志文件一定存在」（哪怕启动即失败）
    : >> "$MID_LOG" 2>/dev/null || true
    # ★ 中间件日志级别（quiet/normal/debug）：
    #   默认 normal —— 只读轮询已合并计数，日志量比原来降 90%+。
    #   排障时：export HIKSEMI_LOG=debug 后重启服务即可看到完整流水。
    export HIKSEMI_LOG="${HIKSEMI_LOG:-normal}"
    # ★ 打印中间件版本，便于排查"装的是哪一版"
    _mid_ver="$(grep -m1 '^MIDDLEWARE_VERSION' "$MID_DIR/middleware.py" 2>/dev/null \
                | sed 's/.*"\(.*\)".*/\1/' || true)"
    [ -n "$_mid_ver" ] && echo "[kiosk] 中间件版本：$_mid_ver" >> "$KIOSK_LOG"
    echo "[kiosk] 启动中间件：$PYBIN $MID_DIR/middleware.py（日志级别 $HIKSEMI_LOG）" >> "$KIOSK_LOG"
    echo "[kiosk] 中间件日志：$MID_LOG（排障可设 HIKSEMI_LOG=debug）" >> "$KIOSK_LOG"

    # 先跑一次自检（--check）：配置/证书/Python 环境有问题会直接写进日志
    "$PYBIN" "$MID_DIR/middleware.py" --check \
        --config "$MID_DIR/data-mapping.json" \
        --cert   "$MID_DIR/cert.pem" \
        --key    "$MID_DIR/key.pem" >> "$MID_LOG" 2>&1
    if [ $? -ne 0 ]; then
        echo "[kiosk] ✗ 中间件自检未通过（详见 $MID_LOG）" >> "$KIOSK_LOG"
    else
        echo "[kiosk] ✓ 中间件自检通过" >> "$KIOSK_LOG"
    fi

    # 用绝对路径启动（--config/--cert/--key 显式指定），
    # 避免因工作目录变化导致配置文件失联
    "$PYBIN" "$MID_DIR/middleware.py" \
        --config "$MID_DIR/data-mapping.json" \
        --cert   "$MID_DIR/cert.pem" \
        --key    "$MID_DIR/key.pem" \
        >> "$MID_LOG" 2>&1 &
    MID_PID=$!
    sleep 1
    # 存活检查：1 秒后进程还在吗？（不在说明启动即退出，日志里有 [FATAL] 原因）
    if kill -0 "$MID_PID" 2>/dev/null; then
        echo "[kiosk] ✓ middleware 运行中 (pid=$MID_PID)" >> "$KIOSK_LOG"
        # 端口就绪探测：等最多 5 秒，确认 20443 已监听
        PORT_OK=0
        for i in 1 2 3 4 5; do
            if (command -v ss >/dev/null 2>&1 && ss -lnt 2>/dev/null | grep -q ':20443') \
               || (command -v netstat >/dev/null 2>&1 && netstat -lnt 2>/dev/null | grep -q ':20443'); then
                PORT_OK=1; break
            fi
            sleep 1
        done
        if [ "$PORT_OK" = "1" ]; then
            echo "[kiosk] ✓ 20443 已监听，数据服务就绪" >> "$KIOSK_LOG"
        else
            echo "[kiosk] ⚠ 20443 未在 5 秒内监听（进程在但端口未起，看看 $MID_LOG）" >> "$KIOSK_LOG"
        fi
    else
        echo "[kiosk] ✗ middleware 启动后立即退出（pid=$MID_PID），请看 $MID_LOG" >> "$KIOSK_LOG"
        echo "[kiosk]   最后 8 行日志：" >> "$KIOSK_LOG"
        tail -n 8 "$MID_LOG" 2>/dev/null | sed 's/^/           /' >> "$KIOSK_LOG"
        MID_PID=""
    fi
fi

# ── 4.5 风扇落地通道状态（排障用）──
# 界面四档默认只保存+回显；三种落地通道任一配置才会真正驱动风扇。
# 这里把生效情况写进 kiosk.log，避免"改了档位风扇没动"时无从下手。
{
    echo "[kiosk] 风扇落地通道："
    if [ -n "$HIKSEMI_FAN_APPLY_CMD" ]; then
        echo "[kiosk]   ① 外部命令 HIKSEMI_FAN_APPLY_CMD=$HIKSEMI_FAN_APPLY_CMD"
    fi
    if [ -n "$HIKSEMI_FAN_COOLERCONTROL" ]; then
        echo "[kiosk]   ② CoolerControl HIKSEMI_FAN_COOLERCONTROL=$HIKSEMI_FAN_COOLERCONTROL"
        echo "[kiosk]      脚本=${HIKSEMI_FAN_COOLERCONTROL_SCRIPT:-<自动定位>}"
    fi
    if [ -n "$HIKSEMI_FAN_PWM_PATH" ]; then
        echo "[kiosk]   ③ sysfs PWM HIKSEMI_FAN_PWM_PATH=$HIKSEMI_FAN_PWM_PATH"
    fi
    if [ -z "$HIKSEMI_FAN_APPLY_CMD" ] && [ -z "$HIKSEMI_FAN_COOLERCONTROL" ] \
       && [ -z "$HIKSEMI_FAN_PWM_PATH" ]; then
        echo "[kiosk]   （均未配置 → 档位仅保存到 /var/lib/hiksemi-sub/fan.json 并回显）"
    fi
    if [ -x "$TOOLS_DIR/coolercontrol/apply-mode.sh" ]; then
        echo "[kiosk]   apply-mode.sh：$TOOLS_DIR/coolercontrol/apply-mode.sh（可执行）"
    else
        echo "[kiosk]   apply-mode.sh：未部署于 $TOOLS_DIR/coolercontrol/"
    fi
} >> "$KIOSK_LOG"

# 回退：完全没有中间件时用旧 mock 兜底
if [ -z "$MID_PID" ] && [ -n "$PYBIN" ] && [ -f "$TOOLS_DIR/mock_server.py" ]; then
    echo "[kiosk] 回退到 mock_server（$TOOLS_DIR/mock_server.py）" >> "$KIOSK_LOG"
    "$PYBIN" "$TOOLS_DIR/mock_server.py" >> "$LOG_DIR/mock.log" 2>&1 &
    MID_PID=$!
    sleep 1
fi

# 退出时收尾：关掉程序后连带中间件一起停
cleanup() {
    [ -n "$MID_PID" ] && kill "$MID_PID" 2>/dev/null
}
trap cleanup EXIT TERM INT

APP_LOG="$LOG_DIR/app.log"
echo "[kiosk] launching hiksemi-sub ..." >> "$KIOSK_LOG"
echo "[kiosk] 程序输出 → $APP_LOG" >> "$KIOSK_LOG"

# ── 4.8 ★ 程序本体前置校验（把 rc=127/126 的「裸 bash 报错」变成可读诊断）──
#   systemd 只回报 xinit 的退出码，看不出到底哪一步断了。这里在启动前逐项检查，
#   任何一项不过就写清原因并【提前退出】，避免日志里只有一行
#   「/xxx/hiksemi-sub: No such file or directory」而无从下手。
APP_BIN="$APP_DIR/hiksemi-sub"
APP_PROBLEM=""
[ -d "$APP_DIR" ]              || APP_PROBLEM="$APP_PROBLEM 程序目录不存在:$APP_DIR;"
[ -e "$APP_BIN" ]              || APP_PROBLEM="$APP_PROBLEM 主程序不存在:$APP_BIN;"
if [ -e "$APP_BIN" ] && [ ! -x "$APP_BIN" ]; then
    APP_PROBLEM="$APP_PROBLEM 主程序不可执行(缺 +x):$APP_BIN;"
fi
if [ -n "$APP_PROBLEM" ]; then
    if [ -n "$HIKSEMI_DIR" ]; then
        _app_src="HIKSEMI_DIR 环境变量（$HIKSEMI_DIR）"
    else
        _app_src="脚本所在目录自动推断（$APP_DIR）"
    fi
    {
        echo "[kiosk] ✗ 程序本体前置检查未通过："
        echo "[kiosk]   $APP_PROBLEM"
        echo "[kiosk]   APP_DIR   =$APP_DIR  （来源：$_app_src）"
        echo "[kiosk]   TOOLS_DIR =$TOOLS_DIR"
        echo "[kiosk]   ▶ 修复指引："
        echo "[kiosk]     ① 确认程序解包/安装目录，例如： ls -l /screen/hiksemi-sub"
        echo "[kiosk]     ② 重装自启并显式指定程序目录："
        echo "[kiosk]        sudo bash $TOOLS_DIR/install-autostart.sh --dir <程序目录>"
        echo "[kiosk]     ③ 若是 AppImage 首次运行尚未解包：先手动跑一次该 AppImage"
        echo "[kiosk]        （解包目录通常在 /tmp/.mount_* 或 ~/.config 下，重启后会变）"
        echo "[kiosk]     ④ 目录移动过：重启服务即可（systemctl restart hiksemi-sub）"
    } >> "$KIOSK_LOG"
    echo "[kiosk] ✗ 程序本体前置检查未通过（详见 $KIOSK_LOG）" >&2
    exit 127
fi
echo "[kiosk] ✓ 程序本体检查通过：$APP_BIN（$(ls -l "$APP_BIN" 2>/dev/null | awk '{print $1, $3":"$4}')）" >> "$KIOSK_LOG"

# ── ★ 4.9 兜底：把 fnOS 监控服务「补挂」回来（= 手工 restart resmon/sysinfo）──
# 背景：§0.1 的 --pre-delay + service 的 After=resmon_service/sysinfo_service 只是
#   从「时序」上避让，受机型/内核/存储/并发影响无法保证 100%。一旦相撞，
#   resmon/sysinfo 会【静默降级】：单元仍 active，但 Web UI 的 CPU / 网络 / 存储
#   全部无数据，用户必须手工
#       sudo systemctl restart resmon_service sysinfo_service
#   才恢复 —— 本段把这件手工活自动化。
#
# ★★ 2026-10-06 关键修正（解释上一版兜底「加了也无效」）：
#   旧版只在【后台】sleep 60s 后补挂。但 kiosk.sh 一旦退出（开机早期程序崩溃重拉、
#   X 未就绪重试都很常见），systemd 会连同 cgroup 杀掉所有后台子进程
#   （默认 KillMode=control-group）⇒ 兜底【永远到不了 60s】，监控依旧无数据。
#   现改为【在启动程序之前同步执行一次】，与主进程存活解耦（这正是它 reliably 生效
#   的原因）；只有主进程存活时才额外做一次延迟补挂。
#   另把 try-restart 换成 restart：降级态下单元仍 active，restart 才是与用户实测
#   有效命令严格对齐的做法（try-restart 对异常态可能不动作）。
#   此刻 X 已由 xinit 拉起并稳定持有 DRM master，故此处 restart 必定能重新挂上
#   设备接口（效果 = 用户手工命令）。
#   仅在开机窗口内执行（uptime < HIKSEMI_BOOT_WINDOW_SEC，默认 300s）；
#   手工 restart 本服务时不触发。关闭：Environment=HIKSEMI_MONITOR_RESYNC=0

# 内部：重启所有「确实存在」的飞牛监控单元，回显处理过的单元名（空格分隔）
_fn_monitors() {
    local u out=""
    for u in resmon_service.service sysinfo_service.service; do
        systemctl cat "$u" >/dev/null 2>&1 || continue   # 单元不存在（非飞牛）→ 跳过
        timeout 20 systemctl restart "$u" >/dev/null 2>&1 && out="$out ${u%.service}"
    done
    printf '%s' "$out"
}

# 内部：是否处于开机窗口（uptime < HIKSEMI_BOOT_WINDOW_SEC）
_in_boot_window() {
    local up
    up="$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 9999)"
    case "$up" in ''|*[!0-9]*) up=9999 ;; esac
    [ "$up" -lt "${HIKSEMI_BOOT_WINDOW_SEC:-300}" ]
}

if [ "${HIKSEMI_MONITOR_RESYNC:-auto}" != "0" ] && _in_boot_window; then
    sleep "${HIKSEMI_MONITOR_RESYNC_GRACE:-3}"   # 让 X 的 DRM master 抢占先稳定
    _MR_SYNC="$(_fn_monitors)"
    if [ -n "$_MR_SYNC" ]; then
        echo "[kiosk] ♻ 已补挂 fnOS 监控服务（同步兜底）：$_MR_SYNC" >> "$KIOSK_LOG"
        echo "[kiosk] ♻ 监控服务补挂成功（同步）:$_MR_SYNC" >&2
    fi
    # 进程若存活，再延迟补一次（覆盖极慢机型）；若主进程被重拉，子进程随 cgroup
    # 一起消失 —— 无副作用（这正是它只作「附加保险」的原因）。
    (
        sleep "${HIKSEMI_MONITOR_RESYNC_LATE:-150}"
        _h="$(_fn_monitors)"
        [ -n "$_h" ] && {
            echo "[kiosk] ♻ 已补挂 fnOS 监控服务（延迟兜底）：$_h" >> "$KIOSK_LOG"
            echo "[kiosk] ♻ 监控服务补挂成功（延迟）:$_h" >&2
        }
    ) &
fi

# ── 5. 程序本体 ──
# ★ 关键：Electron 的 stdout/stderr 同时写 app.log + journal（tee 双写）。
#   若程序在 Vue mount 之前崩溃（X 未就绪 / chunk 加载失败 / initThemeConfigInNas 抛错），
#   /tmp/subLog/ 不会有任何日志，唯一线索就是这里的 app.log。
#   不用 exec：exec 与管道同用时，退出码/信号语义会失真，改为前台等待。
{
    echo "===== hiksemi-sub 启动 $(date '+%F %T') | DISPLAY=$DISPLAY ====="
    echo "APP_DIR=$APP_DIR  TMPDIR=$TMPDIR"
    echo "X11-unix: $(ls /tmp/.X11-unix/ 2>&1)"
    echo "----- Electron 输出 -----"
} >> "$APP_LOG"

"$APP_BIN" --no-sandbox 2>&1 | tee -a "$APP_LOG"
APP_RC=${PIPESTATUS[0]}
echo "[kiosk] hiksemi-sub 退出，rc=$APP_RC（$(date '+%F %T')）" >> "$KIOSK_LOG"
echo "----- hiksemi-sub 退出 rc=$APP_RC -----" >> "$APP_LOG"

# ★ 退出码 → 病因速查（systemd 只会回报 xinit 的同一个码，看不出语义，这里补全）
case "$APP_RC" in
    0)   _rc_hint="程序正常退出（有人手动关闭？kiosk 场景不应出现）" ;;
    1)   _rc_hint="程序启动后主动退出（应用层报错，非崩溃）—— \
最常见：① /screen 不可写导致 initThemeConfigInNas 抛错；\
② 程序目录不完整（升级/移动后残留）；③ 主进程 devStatusInit 前异常。\
▶ 决定性线索在 app.log 尾部与 /tmp/subLog 解密日志" ;;
    126) _rc_hint="主程序存在但不可执行（缺 +x，见上方前置检查）" ;;
    127) _rc_hint="命令/文件不存在（主程序或解释器缺失，见上方前置检查）" ;;
    133) _rc_hint="SIGTRAP —— 典型 chrome-sandbox 权限问题：\
确认 $APP_DIR/chrome-sandbox 为 -rwsr-xr-x root root 且所在分区无 nosuid" ;;
    134) _rc_hint="SIGABRT —— 通常是 GPU/GL 初始化失败，试 --disable-gpu 或设 LIBGL_ALWAYS_SOFTWARE=1" ;;
    139) _rc_hint="SIGSEGV —— 渲染进程崩溃，多为显示栈/驱动不匹配（X 是否在跑？见 diag.sh 段 0）" ;;
    *)   _rc_hint="未知退出码，请结合 app.log 尾部判断" ;;
esac
{
    echo "[kiosk] ── 退出码解读（rc=$APP_RC）──"
    echo "[kiosk] $_rc_hint"
    echo "[kiosk] app.log 尾部："
    tail -n 12 "$APP_LOG" 2>/dev/null | sed 's/^/[kiosk]   /'
} >> "$KIOSK_LOG"
echo "[kiosk] rc=$APP_RC：$_rc_hint" >&2
exit "$APP_RC"

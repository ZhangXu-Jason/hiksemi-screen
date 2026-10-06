#!/bin/bash
# ============================================================
# hiksemi-sub 开机自启 一键安装脚本
# ------------------------------------------------------------
# 作用：
#   1. 部署 kiosk.sh / middleware /（可选）coolercontrol 到工具目录
#   2. 安装 systemd service 并注入路径环境变量
#   3. 设置权限、修 chrome-sandbox SUID、启用 I2C 灯控通道
#   4. 注册并启用 systemd 服务（开机自动拉起小彩屏）
#
# 用法（在飞牛OS 上，需 root）：
#   sudo bash install-autostart.sh                      # 全自动（自动探测）
#   sudo bash install-autostart.sh --dir /usr/local/hiksemi-sub
#   sudo bash install-autostart.sh --tools-dir /screen/tools
#   sudo bash install-autostart.sh --fan-coolercontrol on    # 强制接 CoolerControl
#   sudo bash install-autostart.sh --dry-run                 # 只打印将要做什么
#
# 卸载：
#   sudo bash install-autostart.sh uninstall
#
# ── 环境变量（高级/测试用）────────────────────────────────
#   HIKSEMI_TOOLS             工具目录（等价 --tools-dir）
#   HIKSEMI_LOG_DIR           日志目录（默认 /var/log/hiksemi-sub）
#   HIKSEMI_SERVICE_DST       service 文件目标路径（默认 /etc/systemd/system/...）
#   HIKSEMI_INSTALL_SKIP_SYSTEM=1
#                             只做文件部署，跳过所有系统级动作
#                             （/screen、modprobe、systemctl、chown SUID）；
#                             供自动化测试使用，勿在真机长期开启
# ============================================================
set -e

SERVICE_NAME="hiksemi-sub.service"
SERVICE_DST="${HIKSEMI_SERVICE_DST:-/etc/systemd/system/$SERVICE_NAME}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 解析参数 ──
#   --dir <路径>               程序目录（AppImage 解包/安装目录）
#   --tools-dir <路径>         工具目录（默认 /screen/tools）
#   --fan-coolercontrol <模式> 风扇落地：auto（默认）/ on / off
#   --dry-run                  只打印将要执行的动作，不落盘、不改系统
#   uninstall                  卸载服务
ACTION="install"
APP_DIR_OVERRIDE=""
TOOLS_DIR_OVERRIDE=""
FAN_CC_MODE="auto"
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        uninstall)          ACTION="uninstall" ;;
        --dir)              shift; APP_DIR_OVERRIDE="$1" ;;
        --dir=*)            APP_DIR_OVERRIDE="${1#--dir=}" ;;
        --tools-dir)        shift; TOOLS_DIR_OVERRIDE="$1" ;;
        --tools-dir=*)      TOOLS_DIR_OVERRIDE="${1#--tools-dir=}" ;;
        --fan-coolercontrol|--fan-cc)
                            shift; FAN_CC_MODE="${1:-auto}" ;;
        --fan-coolercontrol=*) FAN_CC_MODE="${1#--fan-coolercontrol=}" ;;
        --fan-cc=*)         FAN_CC_MODE="${1#--fan-cc=}" ;;
        --dry-run|-n)       DRY_RUN=1 ;;
        -h|--help)          ACTION="help" ;;
        *)                  ;;
    esac
    shift
done

# 测试/CI 钩子：只做文件部署，跳过系统级动作
SKIP_SYSTEM="${HIKSEMI_INSTALL_SKIP_SYSTEM:-0}"

# 颜色
R='\033[31m'; G='\033[32m'; Y='\033[33m'; B='\033[36m'; N='\033[0m'
ok()   { echo -e "${G}[OK]${N} $1"; }
warn() { echo -e "${Y}[!!]${N} $1"; }
err()  { echo -e "${R}[XX]${N} $1"; }
info() { echo -e "${B}[--]${N} $1"; }

# 是否允许「系统级动作」（dry-run 与 SKIP_SYSTEM 均禁止）
SYS_OK=1
[ "$DRY_RUN" = "1" ] && SYS_OK=0
[ "$SKIP_SYSTEM" = "1" ] && SYS_OK=0

# 需要落盘的系统动作（文件部署）是否允许（dry-run 禁止，SKIP_SYSTEM 允许）
FILE_OK=1
[ "$DRY_RUN" = "1" ] && FILE_OK=0

# ── 帮助 ──
if [ "$ACTION" = "help" ]; then
    sed -n '3,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

# ★ 程序目录自动搜索（程序与工具分离的布局下用）
#   只在「默认位置找不到主程序」时调用；命中即输出该目录并返回 0。
#   搜索面刻意收窄到常见安装位置 + 飞牛OS 存储池（/vol*），不做全盘扫描。
#   ★ 不搜 AppImage 挂载点（/tmp/.mount_*）——那是易失目录，装 service 无意义。
_find_app_dir() {
    local d
    for d in \
        /usr/local/hiksemi-sub \
        /opt/hiksemi-sub \
        /srv/hiksemi-sub \
        /screen/hiksemi-sub \
        /root/hiksemi-sub \
        /usr/local/* /opt/* /srv/* \
        /vol*/hiksemi-sub /vol*/*/hiksemi-sub /vol*/*/*/hiksemi-sub \
        /home/*/hiksemi-sub
    do
        [ -d "$d" ] || continue
        [ -f "$d/hiksemi-sub" ] || continue
        printf '%s\n' "${d%/}"
        return 0
    done
    return 1
}

# ★ 程序目录解析优先级：
#   1) --dir 显式指定
#   2) 脚本所在 tools/ 的上一级（旧布局：tools 在程序目录内）
#   3) ★ 常见位置自动搜索（新布局：工具在 /screen/tools，程序在别处）
APP_AUTODETECTED=""
if [ -n "$APP_DIR_OVERRIDE" ]; then
    APP_DIR="$(cd "$APP_DIR_OVERRIDE" 2>/dev/null && pwd || echo "$APP_DIR_OVERRIDE")"
else
    APP_DIR="$(cd "$SRC_DIR/.." && pwd)"
    # 默认位置没有主程序 ⇒ 大概率是「工具在 /screen/tools、程序在别处」的推荐布局，
    # 自动搜一次，避免用户被一句「主程序不存在」卡住。
    if [ ! -f "$APP_DIR/hiksemi-sub" ]; then
        if _alt="$(_find_app_dir)"; then
            APP_DIR="$_alt"
            APP_AUTODETECTED="$_alt"
        fi
    fi
fi

# ★ 工具目录（tools/）落地位置解析优先级：
#   1) --tools-dir 显式指定
#   2) 环境变量 HIKSEMI_TOOLS
#   3) 若 <APP_DIR>/tools 存在（程序自带）→ 用它（向后兼容）
#   4) ★ 默认 /screen/tools（持久目录，推荐）
#   理由：程序本体常为 AppImage（解包/挂载目录易失），而工具需持久存在。
if [ -n "$TOOLS_DIR_OVERRIDE" ]; then
    TOOLS_DIR="$TOOLS_DIR_OVERRIDE"
elif [ -n "$HIKSEMI_TOOLS" ]; then
    TOOLS_DIR="$HIKSEMI_TOOLS"
elif [ -d "$APP_DIR/tools" ]; then
    TOOLS_DIR="$APP_DIR/tools"
else
    TOOLS_DIR="/screen/tools"
fi
# 归一化为绝对路径（兼容相对路径 / 末尾斜杠）
TOOLS_DIR="$(cd "$TOOLS_DIR" 2>/dev/null && pwd || echo "$TOOLS_DIR")"

# ── 必须是 root（dry-run / SKIP_SYSTEM 不要求）──
if [ "$SYS_OK" = "1" ] && [ "$(id -u)" -ne 0 ]; then
    err "请用 root 运行： sudo bash $0"
    exit 1
fi

# ============================================================
# 卸载分支
# ============================================================
if [ "$ACTION" = "uninstall" ]; then
    echo ">> 卸载 $SERVICE_NAME"
    if [ "$SYS_OK" = "1" ]; then
        systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
        rm -f "$SERVICE_DST"
        systemctl daemon-reload
        # 恢复安装时 mask 的 getty@tty1（返回 tty1 登录终端）
        systemctl unmask getty@tty1.service >/dev/null 2>&1 && \
            ok "已 unmask getty@tty1（tty1 恢复登录终端）"
    else
        info "[跳过] systemctl disable --now $SERVICE_NAME ; rm -f $SERVICE_DST"
    fi
    ok "已卸载（kiosk.sh / 中间件 / coolercontrol / 程序目录均保留，未删除）"
    echo "    如需彻底清理，手工删除："
    echo "      $TOOLS_DIR/            （工具目录）"
    echo "      /var/lib/hiksemi-sub/  （风扇/灯/熄屏状态文件）"
    exit 0
fi

# ============================================================
# 安装分支
# ============================================================
echo "=========================================="
echo " hiksemi-sub 开机自启安装"
echo "=========================================="
if [ "$DRY_RUN" = "1" ]; then
    warn "dry-run 模式：只打印将要执行的动作，不落盘、不改系统"
fi
if [ "$SKIP_SYSTEM" = "1" ] && [ "$DRY_RUN" != "1" ]; then
    warn "HIKSEMI_INSTALL_SKIP_SYSTEM=1：仅做文件部署，跳过所有系统级动作"
fi
echo ""

# ── 1. 前置检查 ──
echo ">> [1/7] 前置检查"

# ★ 程序目录定位失败时的统一指引
#   （真机踩过：工具在 /screen/tools、程序在别处的新布局下，默认探测必然不命中；
#     只甩一句「主程序不存在」会让用户完全没有方向）
_app_dir_hint() {
    echo ""
    echo "  安装器把「tools/ 的上一级」当作程序目录（当前 = $APP_DIR），但那里不是程序目录。"
    echo "  常见原因：程序（AppImage 解包目录）与工具目录是分开的（本项目推荐布局）。"
    echo ""
    echo "  ① 程序已解包在别处 → 用 --dir 显式指定："
    echo "       sudo bash $0 --dir /usr/local/hiksemi-sub"
    echo "  ② 还是 .AppImage 没解包 → 先解包到持久目录（勿用 /tmp 挂载点）："
    echo "       chmod +x HistorSub-Linux-*.AppImage"
    echo "       ./HistorSub-Linux-*.AppImage --appimage-extract"
    echo "       sudo mv squashfs-root /usr/local/hiksemi-sub"
    echo "  ③ 先自动定位（找含可执行 hiksemi-sub 的目录）："
    echo "       sudo find / -xdev -name hiksemi-sub -type f 2>/dev/null"
    echo "  ④ 只预演、不落盘："
    echo "       sudo bash $0 --dry-run"
}

if [ ! -d "$APP_DIR" ]; then
    err "程序目录不存在：$APP_DIR"
    _app_dir_hint
    exit 1
fi
if [ ! -f "$APP_DIR/hiksemi-sub" ]; then
    err "主程序不存在：$APP_DIR/hiksemi-sub"
    _app_dir_hint
    exit 1
fi
if [ -n "$APP_AUTODETECTED" ]; then
    warn "自动探测到程序目录：$APP_AUTODETECTED（--dir 可显式指定）"
fi
ok "程序目录: $APP_DIR"
ok "工具目录: $TOOLS_DIR"

# ★ 检查挂载点是否带 nosuid（会导致 chrome-sandbox SUID 失效）
MOUNT_OPTS="$(findmnt -no OPTIONS --target "$APP_DIR" 2>/dev/null || true)"
MOUNT_SRC="$(findmnt -no TARGET --target "$APP_DIR" 2>/dev/null || echo '未知')"
if echo "$MOUNT_OPTS" | grep -qw nosuid; then
    warn "目录位于 nosuid 挂载点（$MOUNT_SRC），chrome-sandbox SUID 将失效！"
    warn "  程序仍可用（service 走 root + PAMName 路径），但沙箱不生效。"
    warn "  如需完整沙箱，请改放到根分区目录（如 /opt 或 /usr/local）。"
else
    ok "挂载点 $MOUNT_SRC 无 nosuid，SUID 可生效"
fi

# ── 统一日志目录（kiosk / 中间件 / X 都写这里）──
#   优先级：HIKSEMI_LOG_DIR 显式指定 > /var/log/hiksemi-sub（默认）
LOG_DIR="/var/log/hiksemi-sub"
if [ -n "${HIKSEMI_LOG_DIR:-}" ]; then
    LOG_DIR="$HIKSEMI_LOG_DIR"
    mkdir -p "$LOG_DIR" 2>/dev/null && chmod 755 "$LOG_DIR" 2>/dev/null || true
    ok "日志目录 $LOG_DIR（来自 HIKSEMI_LOG_DIR）"
elif [ "$SYS_OK" = "1" ] && mkdir -p "$LOG_DIR" 2>/dev/null; then
    chmod 755 "$LOG_DIR"
    ok "日志目录 $LOG_DIR 就绪"
else
    warn "无法创建 $LOG_DIR，kiosk.sh 将回退到 $APP_DIR/logs"
    LOG_DIR="$APP_DIR/logs"
    [ "$FILE_OK" = "1" ] && mkdir -p "$LOG_DIR" 2>/dev/null
    true
fi

# ── ★ 程序运行必需目录 /screen/ ──
# 客户端 getDeviceStatus() 会读写绝对路径 /screen/lang.config，
# 主题系统还会用 /screen/themes/ 存放主题配置与默认主题包。
# 这些目录不存在时：
#   - 语言配置读写失败（getDeviceStatus 里的 fs 调用抛错）
#   - 主题初始化失败
# 结果可能是「小屏一直停在启动中/绑定页」或「界面空白无数据」。
if [ "$SYS_OK" != "1" ]; then
    info "[跳过] 创建 /screen/ 与 /screen/lang.config（测试模式）"
elif mkdir -p /screen/themes/default 2>/dev/null; then
    chmod 755 /screen
    ok "/screen 目录就绪（程序运行必需）"
    # 预置语言配置：避免程序首次启动时因文件缺失走异常分支
    if [ ! -f /screen/lang.config ]; then
        echo '{"lang":"CN"}' > /screen/lang.config 2>/dev/null && \
            ok "已写入 /screen/lang.config（lang=CN）"
    else
        ok "/screen/lang.config 已存在"
    fi
    # 预置主题静态资源（从程序 dist 复制，避免在线下载）
    if [ -f "$APP_DIR/resources/app/dist/theme-static/config.json" ]; then
        [ -f /screen/themes/config.json ] || \
            cp -f "$APP_DIR/resources/app/dist/theme-static/config.json" \
                  /screen/themes/config.json 2>/dev/null
        [ -f /screen/themes/default/default-theme.zip ] || \
            cp -f "$APP_DIR/resources/app/dist/theme-static/default-theme.zip" \
                  /screen/themes/default/default-theme.zip 2>/dev/null
        ok "已预置主题资源到 /screen/themes/"
    fi
else
    warn "无法创建 /screen（权限不足？）—— 程序可能卡在启动页/无数据"
fi

# ── ★ 启用 I2C 灯控通道（氛围灯）──
# 硬件：I2C 从设备 0x26（RGB 灯带）。需要内核加载 i2c_dev 模块，
# 否则 /dev/i2c-* 不存在 → 中间件探测不到 → 氛围灯设置不生效。
# 这里加载并持久化（/etc/modules-load.d/），保证重启后仍在。
echo ""
echo ">> 启用 I2C 灯控通道（氛围灯）"
if [ "$SYS_OK" != "1" ]; then
    info "[跳过] modprobe i2c_dev 与 /etc/modules-load.d/i2c-dev.conf（测试模式）"
else
    modprobe i2c_dev 2>/dev/null || modprobe i2c-dev 2>/dev/null || true
    if lsmod 2>/dev/null | grep -q "^i2c_dev"; then
        ok "i2c_dev 模块已加载"
    else
        warn "i2c_dev 模块加载失败（氛围灯可能不可控；检查内核是否含 i2c-dev）"
    fi
    if mkdir -p /etc/modules-load.d 2>/dev/null; then
        echo "i2c_dev" > /etc/modules-load.d/i2c-dev.conf 2>/dev/null && \
            ok "已配置开机自动加载（/etc/modules-load.d/i2c-dev.conf）"
    fi
    if ls /dev/i2c-* >/dev/null 2>&1; then
        ok "I2C 设备节点就绪：$(ls /dev/i2c-* | tr '\n' ' ')"
    else
        warn "仍无 /dev/i2c-*（氛围灯将不可控，其余功能不受影响）"
    fi
fi

for cmd in xinit systemctl; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        if [ "$SYS_OK" = "1" ]; then
            if [ "$cmd" = "xinit" ]; then
                err "缺少命令：$cmd（先装 xinit： apt install -y xinit）"
            else
                err "缺少命令：$cmd"
            fi
            exit 1
        fi
        warn "缺少命令：$cmd（测试模式下仅告警）"
    fi
done
# xinit 真实路径（不同发行版可能是 /usr/bin 或 /usr/local/bin）
XINIT_BIN="$(command -v xinit || echo xinit)"
ok "xinit 路径: $XINIT_BIN"

# ── 2. 修 chrome-sandbox 权限 ──
echo ""
echo ">> [2/7] 修 chrome-sandbox SUID 权限"
if [ -f "$APP_DIR/chrome-sandbox" ]; then
    if [ "$SYS_OK" = "1" ]; then
        chown root:root "$APP_DIR/chrome-sandbox"
        chmod 4755 "$APP_DIR/chrome-sandbox"
    else
        info "[跳过] chown root:root + chmod 4755 $APP_DIR/chrome-sandbox"
    fi
    ls -l "$APP_DIR/chrome-sandbox" | sed 's/^/    /'
    ok "SUID 已设置"
else
    warn "未找到 chrome-sandbox，跳过"
fi

# ── 3. 检查服务中间件（位置 / 版本 / 证书 / 配置）──
echo ""
echo ">> [3/7] 检查服务中间件"
echo "    工具目录（TOOLS_DIR）: $TOOLS_DIR"

# ★ 确定 [SOURCES]：本机仓库里的 middleware 源在哪里（用于首次安装拷贝）
SRC_MID=""
for _c in "$TOOLS_DIR/middleware" "$SRC_DIR/middleware" "$APP_DIR/middleware" \
          "$APP_DIR/tools/middleware"; do
    [ -f "$_c/middleware.py" ] && { SRC_MID="$_c"; break; }
done

# ★ 目标位置：跟随 TOOLS_DIR（即 /screen/tools/middleware 或 $APP/tools/middleware）
MID_DIR="$TOOLS_DIR/middleware"

# 首次安装：若目标无中间件，但仓库里有源 → 拷过去
# ★ 就地安装（SRC_MID == MID_DIR）时源目标同一个目录，cp 会报 "same file" 且返回非 0，
#   set -e 下会中断安装；用 -ef 判定同一性后直接跳过。
if [ "$SRC_MID" -ef "$MID_DIR" ]; then
    info "中间件已在工具目录内（就地安装，无需复制）"
elif [ ! -f "$MID_DIR/middleware.py" ] && [ -n "$SRC_MID" ]; then
    if [ "$FILE_OK" = "1" ]; then
        echo "    首次安装：复制中间件 $SRC_MID → $MID_DIR"
        mkdir -p "$MID_DIR"
        cp -rn "$SRC_MID/." "$MID_DIR/" 2>/dev/null || true
    else
        info "[跳过] 复制中间件 $SRC_MID → $MID_DIR"
    fi
fi

# 兼容兜底：目标仍无，但 $APP_DIR/middleware/ 有 → 用它（老部署不改动）
if [ ! -f "$MID_DIR/middleware.py" ] && [ -f "$APP_DIR/middleware/middleware.py" ]; then
    MID_DIR="$APP_DIR/middleware"
    warn "未在 $TOOLS_DIR/middleware 找到中间件，改用 $APP_DIR/middleware（兼容模式）"
fi

if [ -n "$MID_DIR" ] && [ -f "$MID_DIR/middleware.py" ]; then
    ok "middleware.py 存在（$MID_DIR）"

    # ★ 版本校验与备份：先把版本打出来，便于确认装的是哪一版
    _mid_ver="$(grep -m1 '^MIDDLEWARE_VERSION' "$MID_DIR/middleware.py" 2>/dev/null \
                | sed 's/.*"\(.*\)".*/\1/' || true)"
    if [ -n "$_mid_ver" ]; then
        ok "中间件版本：$_mid_ver"
    else
        warn "未能从 middleware.py 解析到 MIDDLEWARE_VERSION（可能被改动过）"
    fi
    # 升级时备份旧中间件（保留最近一份，便于回滚）
    if [ ! -f "$MID_DIR/middleware.py.orig" ] && [ "$FILE_OK" = "1" ]; then
        cp -f "$MID_DIR/middleware.py" "$MID_DIR/middleware.py.orig" 2>/dev/null && \
            ok "已留存原始副本 middleware.py.orig（便于回滚）"
    fi

    if [ -f "$MID_DIR/data-mapping.json" ]; then
        ok "data-mapping.json（数据映射配置）存在"
    else
        err "缺少 data-mapping.json（中间件无法启动）"
        exit 1
    fi
    if [ -f "$MID_DIR/cert.pem" ] && [ -f "$MID_DIR/key.pem" ]; then
        ok "HTTPS 证书齐全"
    elif [ "$FILE_OK" = "1" ]; then
        warn "缺少 cert.pem / key.pem，正在生成..."
        if command -v openssl >/dev/null 2>&1; then
            openssl req -x509 -newkey rsa:2048 -nodes \
                -keyout "$MID_DIR/key.pem" \
                -out "$MID_DIR/cert.pem" \
                -days 3650 -subj "/CN=127.0.0.1" 2>/dev/null
            ok "自签证书已生成"
        else
            warn "未装 openssl，请手动生成证书（见 docs/deploy-guide.md）"
        fi
    else
        warn "缺少 cert.pem / key.pem（dry-run 下不生成）"
    fi
elif [ -f "$TOOLS_DIR/mock_server.py" ]; then
    warn "未找到中间件，回退检查旧 mock 服务"
    if [ -f "$TOOLS_DIR/cert.pem" ] && [ -f "$TOOLS_DIR/key.pem" ]; then
        ok "mock_server.py + 证书齐全"
    elif [ "$FILE_OK" = "1" ]; then
        warn "缺少 cert.pem / key.pem，正在生成..."
        if command -v openssl >/dev/null 2>&1; then
            openssl req -x509 -newkey rsa:2048 -nodes \
                -keyout "$TOOLS_DIR/key.pem" \
                -out "$TOOLS_DIR/cert.pem" \
                -days 3650 -subj "/CN=127.0.0.1" 2>/dev/null
            ok "自签证书已生成"
        else
            warn "未装 openssl，请手动生成证书（见 docs/deploy-guide.md）"
        fi
    fi
else
    warn "未找到中间件和 mock，程序将无数据源（界面空）"
fi

# ── 4. 部署风扇落地适配器（CoolerControl，可选）──
# 目的：把小彩屏界面的「智能/低速/标准/强劲」四档真正落到风扇转速。
# 中间件侧靠环境变量 HIKSEMI_FAN_COOLERCONTROL=1 打开通道，
# 由 <工具目录>/coolercontrol/apply-mode.sh 把档位写进 CoolerControl 的
# /etc/coolercontrol/{modes.json,config.toml} 并重启其 daemon 生效。
echo ""
echo ">> [4/7] 部署风扇落地适配器（CoolerControl）"

SRC_CC="$SRC_DIR/coolercontrol"
CC_DST="$TOOLS_DIR/coolercontrol"

# 判据：机器上是否已装 CoolerControl（任一成立即可）
_have_coolercontrol() {
    [ -f /etc/coolercontrol/config.toml ] && return 0
    command -v coolercontrold >/dev/null 2>&1 && return 0
    command -v ccd >/dev/null 2>&1 && return 0
    systemctl list-unit-files 2>/dev/null | grep -q '^coolercontrold' && return 0
    if command -v docker >/dev/null 2>&1; then
        docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qi coolercontrol && return 0
    fi
    return 1
}

case "$FAN_CC_MODE" in
    on|ON|1|yes|true)   FAN_CC_ENABLED=1 ;;
    off|OFF|0|no|false) FAN_CC_ENABLED=0 ;;
    auto|'')
        if _have_coolercontrol; then
            FAN_CC_ENABLED=1
            info "自动探测：已装 CoolerControl → 启用风扇适配器"
        else
            FAN_CC_ENABLED=0
            info "自动探测：未发现 CoolerControl → 不启用（可用 --fan-coolercontrol on 强制）"
        fi
        ;;
    *)
        warn "未知的 --fan-coolercontrol 取值：$FAN_CC_MODE（按 auto 处理）"
        if _have_coolercontrol; then FAN_CC_ENABLED=1; else FAN_CC_ENABLED=0; fi
        ;;
esac

# 源不存在时无法部署（本地开发目录才会有这个子目录）
if [ ! -d "$SRC_CC" ] && [ "$FAN_CC_ENABLED" = "1" ]; then
    if [ -d "$CC_DST" ]; then
        warn "源目录 $SRC_CC 不存在，沿用已部署的 $CC_DST"
    else
        err "源目录 $SRC_CC 不存在，且 $CC_DST 也没有 → 无法部署风扇适配器"
        warn "  （请从完整工具包运行本脚本，或先手工拷贝 tools/coolercontrol/）"
        FAN_CC_ENABLED=0
    fi
fi

if [ "$FAN_CC_ENABLED" = "1" ]; then
    # ★ 用 -n（no-clobber）：不覆盖设备上可能已渲染过的文件，重装安全
    # ★ 就地安装时 SRC_CC == CC_DST（项目直接放在工具目录下）→ 跳过，避免
    #   "same file" 噪声与非 0 退出（-ef 比字符串相等更准，能识别不同写法同一文件）
    if [ -d "$SRC_CC" ] && [ "$FILE_OK" = "1" ] && [ "$SRC_CC" -ef "$CC_DST" ]; then
        info "coolercontrol 已在工具目录内（就地安装，无需复制）"
    elif [ -d "$SRC_CC" ] && [ "$FILE_OK" = "1" ]; then
        mkdir -p "$CC_DST"
        cp -rn "$SRC_CC/." "$CC_DST/" 2>/dev/null || true
    elif [ "$FILE_OK" != "1" ]; then
        info "[跳过] 复制 $SRC_CC/ → $CC_DST/"
    fi
    if [ "$FILE_OK" = "1" ]; then
        chmod +x "$CC_DST"/*.sh "$CC_DST"/*.py 2>/dev/null || true
    fi
    _cc_n="$(ls -1 "$CC_DST" 2>/dev/null | wc -l | tr -d ' ')"
    ok "风扇适配器就绪：$CC_DST（$_cc_n 个文件）"
    if [ -f "$CC_DST/apply-mode.sh" ]; then
        ok "落地脚本：$CC_DST/apply-mode.sh"
    else
        warn "缺少 apply-mode.sh（中间件会报「找不到」）"
    fi
    # 提示：模板里的占位符必须先用真实 device_uid 渲染过才能生效
    if grep -q '@DEVICE_UID@' "$CC_DST/config.toml" 2>/dev/null; then
        warn "config.toml 仍是模板（含 @DEVICE_UID@ 占位符）→ 尚未生效！"
        echo "    请先探测真机并渲染："
        echo "      sudo $CC_DST/probe-coolercontrol.sh"
        echo "      sudo $CC_DST/render-config.py ...   # 见 docs/coolercontrol.md §部署"
    else
        ok "config.toml 已渲染（无占位符）"
    fi
    # 报告 daemon 形态
    if [ "$SYS_OK" = "1" ]; then
        if command -v docker >/dev/null 2>&1 && \
           docker ps --format '{{.Names}}' 2>/dev/null | grep -qi coolercontrol; then
            ok "CoolerControl 运行形态：Docker 容器"
        elif systemctl is-active --quiet coolercontrold 2>/dev/null; then
            ok "CoolerControl 运行形态：systemd（coolercontrold 运行中）"
        elif _have_coolercontrol; then
            warn "CoolerControl 已安装但未运行（切换档位时会尝试启动/重启它）"
        else
            warn "未检测到 CoolerControl 本体：档位仍会被写入配置，但不会实际调速"
            warn "  安装方式见 docs/coolercontrol.md §1"
        fi
    fi
else
    info "跳过风扇适配器部署（档位仍会保存到 /var/lib/hiksemi-sub/fan.json 并回显到界面）"
    [ "$FILE_OK" = "1" ] || info "[dry-run] 若启用，将部署到 $CC_DST"
fi

# ── 5. 安装 kiosk.sh ──
echo ""
echo ">> [5/7] 安装 kiosk.sh"
# ★ kiosk.sh 跟随 tools 目录（保持 tools/ 为一体，避免出现两份副本）
KIOSK_DST="$TOOLS_DIR/kiosk.sh"
if [ "$FILE_OK" = "1" ]; then
    mkdir -p "$TOOLS_DIR"
    if [ -f "$SRC_DIR/kiosk.sh" ]; then
        # ★ 源与目标可能是同一个文件：就地安装时 SRC_DIR == TOOLS_DIR（用户把项目放在
        #   /screen/tools 后直接在这里跑脚本）。此时 cp 会报
        #   "cp: 'X' and 'X' are the same file" 并以非 0 退出；set -e 下整套安装中断。
        #   判据用同一性比较（而非字符串相等），因为路径写法可能不同但指向同一 inode。
        if [ "$SRC_DIR/kiosk.sh" -ef "$KIOSK_DST" ]; then
            info "kiosk.sh 已在工具目录内（就地安装，无需复制）"
        else
            cp -f "$SRC_DIR/kiosk.sh" "$KIOSK_DST"
            ok "已安装到 $KIOSK_DST"
        fi
    elif [ -f "$KIOSK_DST" ]; then
        ok "已存在 $KIOSK_DST"
    fi
    if [ -f "$KIOSK_DST" ]; then
        chmod +x "$KIOSK_DST"
        ok "kiosk.sh 就绪且可执行"
    else
        err "kiosk.sh 不存在，无法继续（源：$SRC_DIR/kiosk.sh）"
        exit 1
    fi
else
    info "[跳过] 安装 $SRC_DIR/kiosk.sh → $KIOSK_DST"
fi

# ── 6. 安装 service 文件 ──
echo ""
echo ">> [6/7] 安装 systemd 服务"
if [ ! -f "$SRC_DIR/$SERVICE_NAME" ]; then
    err "找不到 $SERVICE_NAME（应与本脚本同目录）"
    exit 1
fi

# ★ 先渲染到临时文件 → 校验 → 再落盘。
#   好处：① dry-run 也能看到最终生效配置；② 校验失败不会写出半成品。
# ★ 临时目录归一化：git-bash / MSYS 下 TMPDIR 可能是 Windows 路径（含 `\` 或盘符），
#   `rm` 的安全策略会拒绝这类路径，导致脚本以非 0 退出，故回退到 /tmp。
TMP_BASE="${TMPDIR:-/tmp}"
case "$TMP_BASE" in
    *\\*|[A-Za-z]:/*) TMP_BASE="/tmp" ;;
esac
[ -d "$TMP_BASE" ] || TMP_BASE="/tmp"
SVCTMP="$(mktemp "$TMP_BASE/hiksemi-sub.svc.XXXXXX")"
trap 'rm -f "$SVCTMP" 2>/dev/null || true' EXIT
cp -f "$SRC_DIR/$SERVICE_NAME" "$SVCTMP"

# 替换占位符：
#   @APP_DIR@   → 程序目录（kiosk.sh 用 HIKSEMI_DIR 接收）
#   @TOOLS_DIR@ → 工具目录（= /screen/tools 或 --tools-dir 指定）
#   @KIOSK@     → kiosk.sh 实际路径（= $TOOLS_DIR/kiosk.sh）
# 并把 xinit 路径换成实际路径（兼容 xinit 不在 /usr/bin 的系统）
# 注意：保留 -logfile 参数（X 日志写入统一日志目录）
sed -i "s|@APP_DIR@|$APP_DIR|g" "$SVCTMP"
sed -i "s|@TOOLS_DIR@|$TOOLS_DIR|g" "$SVCTMP"
sed -i "s|@KIOSK@|$KIOSK_DST|g" "$SVCTMP"
# kiosk.sh 由 xinit 执行；用环境变量把程序目录传进去（kiosk.sh 优先读它）
sed -i "s|^ExecStart=.*|ExecStart=$XINIT_BIN $KIOSK_DST -- :0 vt1 -nolisten tcp -logfile $LOG_DIR/xorg.log|" "$SVCTMP"
# 兜底：确保 Environment= 行存在且指向正确路径。
# ★ 注意：模板里可能有【多行】Environment=（如 HIKSEMI_LOG），
#   所以只替换含 HIKSEMI_DIR 的那一行，不能笼统替换 ^Environment=.*
if grep -q '^Environment=.*HIKSEMI_DIR=' "$SVCTMP"; then
    sed -i "s|^Environment=HIKSEMI_DIR=.*|Environment=HIKSEMI_DIR=$APP_DIR HIKSEMI_TOOLS=$TOOLS_DIR|" "$SVCTMP"
elif grep -q '^Environment=' "$SVCTMP"; then
    # 有 Environment 行但无 HIKSEMI_DIR → 在首个 Environment 行前插入
    sed -i "0,/^Environment=/s||Environment=HIKSEMI_DIR=$APP_DIR HIKSEMI_TOOLS=$TOOLS_DIR\n&|" "$SVCTMP"
else
    sed -i "/^ExecStart=/i Environment=HIKSEMI_DIR=$APP_DIR HIKSEMI_TOOLS=$TOOLS_DIR" "$SVCTMP"
fi
# 若模板未带日志级别行，则补一行（默认 normal）
grep -q '^Environment=HIKSEMI_LOG=' "$SVCTMP" || \
    sed -i "/^Environment=HIKSEMI_DIR=/a Environment=HIKSEMI_LOG=normal" "$SVCTMP"
# ── 风扇通道开关：只在启用时注入，未启用时确保清除 ──
if [ "$FAN_CC_ENABLED" = "1" ]; then
    sed -i "/^Environment=HIKSEMI_FAN_COOLERCONTROL=/d" "$SVCTMP"
    sed -i "/^Environment=HIKSEMI_DIR=/a Environment=HIKSEMI_FAN_COOLERCONTROL=1 HIKSEMI_FAN_COOLERCONTROL_SCRIPT=$CC_DST/apply-mode.sh" "$SVCTMP"
else
    if [ "$FILE_OK" = "1" ]; then
        sed -i "/^Environment=HIKSEMI_FAN_COOLERCONTROL=/d" "$SVCTMP"
    fi
    info "未注入 HIKSEMI_FAN_COOLERCONTROL（风扇档位仅保存+回显）"
fi

echo "    实际生效配置："
grep -E '^(ExecStart|Environment|Documentation|PAMName|ExecStartPre)=' "$SVCTMP" | sed 's/^/    /'

# 校验：不留占位符
if grep -q '@APP_DIR@\|@TOOLS_DIR@\|@KIOSK@' "$SVCTMP"; then
    err "路径占位符未替换干净，请检查"
    exit 1
fi
# 校验：HIKSEMI_DIR / HIKSEMI_TOOLS 必须存在且指向本次解析结果
if ! grep -q "^Environment=HIKSEMI_DIR=$APP_DIR HIKSEMI_TOOLS=$TOOLS_DIR\$" "$SVCTMP"; then
    err "service 的 HIKSEMI_DIR/HIKSEMI_TOOLS 注入异常，请检查"
    exit 1
fi
# 校验：启用风扇通道时，开关与脚本路径必须成对出现且脚本存在
if [ "$FAN_CC_ENABLED" = "1" ]; then
    if ! grep -q '^Environment=.*HIKSEMI_FAN_COOLERCONTROL=1' "$SVCTMP"; then
        err "已启用风扇适配器，但 service 未注入 HIKSEMI_FAN_COOLERCONTROL=1"
        exit 1
    fi
    if ! grep -q "HIKSEMI_FAN_COOLERCONTROL_SCRIPT=$CC_DST/apply-mode.sh" "$SVCTMP"; then
        err "service 未注入 HIKSEMI_FAN_COOLERCONTROL_SCRIPT"
        exit 1
    fi
    if [ "$FILE_OK" = "1" ] && [ ! -x "$CC_DST/apply-mode.sh" ]; then
        err "apply-mode.sh 不可执行：$CC_DST/apply-mode.sh"
        exit 1
    fi
    ok "风扇通道已写入 service（HIKSEMI_FAN_COOLERCONTROL=1）"
fi

# 校验：启动限流参数必须在 [Unit] 段（放 [Service] 会被 systemd 忽略并告警）
# 这是「开机 X 未就绪 → 连续失败 5 次被拉黑」的修复关键，写错位置等于没修。
_UNIT_SEC="$(awk '/^\[Unit\]/{f=1;next} /^\[/{f=0} f' "$SVCTMP")"
_SVC_SEC="$(awk '/^\[Service\]/{f=1;next} /^\[/{f=0} f' "$SVCTMP")"
if ! printf '%s\n' "$_UNIT_SEC" | grep -q '^StartLimitBurst='; then
    warn "service 的 [Unit] 段缺 StartLimitBurst —— 开机重试可能仍被拉黑"
fi
if printf '%s\n' "$_SVC_SEC" | grep -q '^StartLimitBurst=\|^StartLimitIntervalSec='; then
    warn "启动限流参数出现在 [Service] 段（systemd 会忽略）—— 请移到 [Unit] 段"
fi

# 校验：必须排到飞牛监控服务之后启动，且启动前走一次 --pre-delay（教训 87）。
# 否则开机时我们的 X 会抢核显，导致 resmon/sysinfo「挂不上设备接口」
#   → 飞牛 Web UI 的 CPU / 网络流量 / 存储读写【全部无数据】（须手工 restart 才恢复）。
if printf '%s\n' "$_UNIT_SEC" | grep -q '^After=.*resmon_service\.service.*sysinfo_service\.service'; then
    ok "已排在飞牛监控服务（resmon/sysinfo）之后启动"
else
    warn "service 的 [Unit] 段缺 After=…resmon_service.service sysinfo_service.service"
    warn "  → 开机时 X 可能与监控服务抢核显，导致 Web UI 监控无数据（见 docs/deploy-guide.md §7）"
fi
if printf '%s\n' "$_SVC_SEC" | grep -q -- '--pre-delay'; then
    ok "启动前有开机让路窗口（kiosk.sh --pre-delay）"
else
    warn "service 缺 ExecStartPre=… --pre-delay —— 开机抢核显时监控服务可能无数据"
fi

if [ "$FILE_OK" = "1" ]; then
    cp -f "$SVCTMP" "$SERVICE_DST"
    chmod 644 "$SERVICE_DST" 2>/dev/null || true
    ok "已安装到 $SERVICE_DST"
else
    info "[dry-run] 将写入 $SERVICE_DST"
fi

# ── 7. 启用服务 ──
echo ""
echo ">> [7/7] 启用并启动服务"
if [ "$SYS_OK" = "1" ]; then
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
    ok "已设置开机自启"

    # ★ mask getty@tty1：本服务独占 tty1，而 logind 会在「VT1 激活且无属主」时
    #   动态拉起 autovt@tty1（解析为 getty@tty1）—— 即使写了 Conflicts+After，
    #   getty 是运行时被拉起的（不在开机事务里），After 无从等待，它的事务仍会
    #   因 Conflicts 反向停掉本服务（表现为开机几秒内 failed、手动 restart 正常）。
    #   mask 是唯一可靠解：getty@tty1 永远起不来，竞态源头消失。
    #   （仅屏蔽 tty1 这一路；tty2-6 的 getty 不受影响，SSH/控制台管理无碍。）
    if systemctl mask getty@tty1.service >/dev/null 2>&1; then
        ok "已 mask getty@tty1（防止 logind 动态拉起抢 tty1；卸载时自动恢复）"
    else
        warn "mask getty@tty1 失败 —— 若开机仍秒失败，请手动执行："
        echo "    sudo systemctl mask getty@tty1"
    fi

    echo ""
    echo "------------------------------------------"
    read -r -p "现在立即启动服务吗？[y/N] " ans
    if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
        systemctl restart "$SERVICE_NAME"
        sleep 3
        echo ""
        systemctl status "$SERVICE_NAME" --no-pager -l | head -20
    else
        echo "稍后可手动启动： sudo systemctl start $SERVICE_NAME"
    fi
else
    info "[跳过] systemctl daemon-reload / enable / 交互式启动（测试模式）"
    [ "$DRY_RUN" = "1" ] && echo "    正式安装时执行： sudo bash $0 --dir $APP_DIR --tools-dir $TOOLS_DIR --fan-coolercontrol $FAN_CC_MODE"
fi

cat <<EOF

==========================================
 安装完成
==========================================

本次生效布局：
  程序目录 APP_DIR : $APP_DIR
  工具目录 TOOLS_DIR: $TOOLS_DIR
  中间件            : $MID_DIR
  风扇适配器        : $([ "$FAN_CC_ENABLED" = "1" ] && echo "$CC_DST（已启用）" || echo "未启用")
  日志目录          : $LOG_DIR

常用命令：
  状态：   sudo systemctl status  $SERVICE_NAME
  启动：   sudo systemctl start   $SERVICE_NAME
  停止：   sudo systemctl stop    $SERVICE_NAME
  重启：   sudo systemctl restart $SERVICE_NAME
  日志：   sudo journalctl -u $SERVICE_NAME -f
  卸载：   sudo systemctl disable --now $SERVICE_NAME

排查文件（统一日志目录）：
  $LOG_DIR/kiosk.log          kiosk 脚本日志
  $LOG_DIR/middleware.log     中间件日志
  $LOG_DIR/xorg.log           X server 日志
  sudo journalctl -u $SERVICE_NAME -f   systemd 侧日志

注意：服务占用 tty1，会与 getty@tty1 冲突（已通过 Conflicts 处理）。
     若你想在 tty1 上登录，先 sudo systemctl stop $SERVICE_NAME
EOF

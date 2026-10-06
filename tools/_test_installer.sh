#!/usr/bin/env bash
# =============================================================================
# _test_installer.sh —— install-autostart.sh / kiosk.sh / service 模板 回归测试
# =============================================================================
# 目标：在【不碰系统】的前提下验证安装器的真实行为。
#
# 手法：安装器内置两个测试钩子
#   HIKSEMI_INSTALL_SKIP_SYSTEM=1  只做文件部署，跳过 /screen、modprobe、
#                                  systemctl、chown SUID 等系统级动作
#   HIKSEMI_SERVICE_DST=<路径>     service 文件落点（避免写 /etc/systemd/system）
#   HIKSEMI_LOG_DIR=<路径>         日志目录
# 于是可以在 Windows git-bash / Linux 上把整套安装跑进临时目录并逐项断言。
#
# 运行： bash tools/_test_installer.sh
# 退出码：0 = 全通过
# =============================================================================
set -uo pipefail

TOOLS_DIR_SRC="$(cd "$(dirname "$0")" && pwd)"
INSTALLER="$TOOLS_DIR_SRC/install-autostart.sh"

PASS=0
FAIL=0
declare -a FAILED

_t() {   # _t <描述> <期望> <实际>
    if [ "$2" = "$3" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILED+=("$1  ⟵ 期望[$2] 实际[$3]")
        echo "  ✗ $1"
        echo "      期望: $2"
        echo "      实际: $3"
    fi
}

_contains() {   # _contains <描述> <文件> <模式>
    if grep -qF -- "$3" "$2" 2>/dev/null; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILED+=("$1  ⟵ 在 $2 中找不到 [$3]")
        echo "  ✗ $1"
        echo "      文件: $2"
        echo "      缺少: $3"
    fi
}

_not_contains() {   # _not_contains <描述> <文件> <模式>
    if grep -qF -- "$3" "$2" 2>/dev/null; then
        FAIL=$((FAIL + 1))
        FAILED+=("$1  ⟵ $2 中不应出现 [$3]")
        echo "  ✗ $1（不应出现）"
        echo "      文件: $2"
        echo "      出现: $3"
    else
        PASS=$((PASS + 1))
    fi
}

# ── 判定「某环境变量是否真的生效」─────────────────────────────
# ★ 只认非注释行（^Environment=...），因为 service 模板里带有被注释的示例行。
#   真机排障同理： grep '^Environment=HIKSEMI_FAN_COOLERCONTROL' /etc/systemd/system/hiksemi-sub.service
_active() {   # _active <描述> <文件> <变量名>
    if grep -qE "^Environment=([^ ]+ )*$3=" "$2" 2>/dev/null; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1))
        FAILED+=("$1  ⟵ $2 中无生效行 Environment=$3=")
        echo "  ✗ $1（未生效）"
        echo "      文件: $2"
        echo "      期望: 存在非注释行 Environment=...$3=..."
    fi
}

_not_active() {   # _not_active <描述> <文件> <变量名>
    if grep -qE "^Environment=([^ ]+ )*$3=" "$2" 2>/dev/null; then
        FAIL=$((FAIL + 1))
        FAILED+=("$1  ⟵ $2 中不应有生效行 Environment=$3=")
        echo "  ✗ $1（不应生效）"
        echo "      文件: $2"
        echo "      出现: $(grep -E "^Environment=.*$3=" "$2" | head -1)"
    else
        PASS=$((PASS + 1))
    fi
}

_exists()   { _t "$1" "yes" "$([ -e "$2" ] && echo yes || echo no)"; }
_absent()   { _t "$1" "no"  "$([ -e "$2" ] && echo yes || echo no)"; }
_execable() { _t "$1" "yes" "$([ -x "$2" ] && echo yes || echo no)"; }

echo "============================================================"
echo " 安装器回归测试（install-autostart.sh）"
echo "============================================================"

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [0] 静态检查 ──"
# ─────────────────────────────────────────────────────────────
for f in install-autostart.sh kiosk.sh coolercontrol/apply-mode.sh \
         coolercontrol/probe-coolercontrol.sh hiksemi-sub.service; do
    if [ -f "$TOOLS_DIR_SRC/$f" ]; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1)); FAILED+=("文件缺失：$f"); echo "  ✗ 文件缺失：$f"
    fi
done
for sh in install-autostart.sh kiosk.sh coolercontrol/apply-mode.sh \
          coolercontrol/probe-coolercontrol.sh; do
    if bash -n "$TOOLS_DIR_SRC/$sh" 2>/dev/null; then
        PASS=$((PASS + 1))
    else
        FAIL=$((FAIL + 1)); FAILED+=("bash -n 失败：$sh"); echo "  ✗ bash -n 失败：$sh"
    fi
done
if "C:/Users/zhangxu/.workbuddy/binaries/python/versions/3.13.12/python.exe" -c \
      "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" \
      "$TOOLS_DIR_SRC/coolercontrol/render-config.py" 2>/dev/null \
   || python3 -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read())" \
      "$TOOLS_DIR_SRC/coolercontrol/render-config.py" 2>/dev/null; then
    PASS=$((PASS + 1))
else
    FAIL=$((FAIL + 1)); FAILED+=("render-config.py 语法错误"); echo "  ✗ render-config.py 语法错误"
fi

# --help 必须覆盖新增开关
HELP_OUT="$(bash "$INSTALLER" --help 2>&1)"
_t "--help 退出码 0" "0" "$?"
for kw in "--fan-coolercontrol" "--dry-run" "--tools-dir" "uninstall"; do
    case "$HELP_OUT" in
        *"$kw"*) PASS=$((PASS + 1)) ;;
        *) FAIL=$((FAIL + 1)); FAILED+=("--help 未提及 $kw"); echo "  ✗ --help 未提及 $kw" ;;
    esac
done
# --help 不应把脚本正文打出来
case "$HELP_OUT" in
    *"set -e"*) FAIL=$((FAIL + 1)); FAILED+=("--help 泄漏脚本正文"); echo "  ✗ --help 泄漏脚本正文" ;;
    *) PASS=$((PASS + 1)) ;;
esac

# service 模板必须保留三个占位符
for ph in "@APP_DIR@" "@TOOLS_DIR@" "@KIOSK@"; do
    _contains "service 模板含占位符 $ph" "$TOOLS_DIR_SRC/hiksemi-sub.service" "$ph"
done
# ★ 模板本身不得预先打开风扇通道（示例行必须是注释）
_not_active "service 模板默认不启用风扇通道" "$TOOLS_DIR_SRC/hiksemi-sub.service" \
            "HIKSEMI_FAN_COOLERCONTROL"
_contains "service 模板附风扇通道示例（注释）" "$TOOLS_DIR_SRC/hiksemi-sub.service" \
          '#Environment=HIKSEMI_FAN_COOLERCONTROL=1'
# kiosk.sh 必须上报风扇落地通道（排障关键）
_contains "kiosk.sh 记录 HIKSEMI_FAN_COOLERCONTROL" "$TOOLS_DIR_SRC/kiosk.sh" \
          'HIKSEMI_FAN_COOLERCONTROL=$HIKSEMI_FAN_COOLERCONTROL'
# ★ kiosk.sh 必须在启动程序【之前】做前置检查，否则 systemd 只回报 xinit 的裸退出码
#   （真机踩过：主程序缺失 → 只有一行 `xxx: No such file or directory`，无从下手）
_contains "kiosk.sh 有程序本体前置检查" "$TOOLS_DIR_SRC/kiosk.sh" \
          '程序本体前置检查未通过'
_contains "kiosk.sh 前置检查含修复指引" "$TOOLS_DIR_SRC/kiosk.sh" \
          'install-autostart.sh --dir'
# ★ kiosk.sh 必须给退出码做语义解读（rc=1/126/127/133 各有病因）
_contains "kiosk.sh 有退出码解读表" "$TOOLS_DIR_SRC/kiosk.sh" \
          '退出码解读（rc='
_contains "kiosk.sh 解读覆盖 133(SIGTRAP)" "$TOOLS_DIR_SRC/kiosk.sh" \
          'SIGTRAP'
_contains "kiosk.sh 解读覆盖 rc=1 应用层报错" "$TOOLS_DIR_SRC/kiosk.sh" \
          '程序启动后主动退出'

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [1] 非 root / 无参数时应拒绝（真实模式）──"
# ─────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    bash "$INSTALLER" --dir /nonexistent-app --tools-dir /tmp/xx-notused \
        >/dev/null 2>&1
    _t "非 root 真实安装应退出 1" "1" "$?"
else
    echo "  （当前是 root，跳过该用例）"
fi
# 程序目录不存在必须报错退出
bash "$INSTALLER" --dir /nonexistent-app-xyz --tools-dir /tmp/xx-notused \
    --dry-run >/dev/null 2>&1
_t "程序目录不存在应退出 1（dry-run 也校验）" "1" "$?"

# ★ 定位失败必须给出可操作指引 —— 不能只甩一句「主程序不存在：xxx」把用户卡死。
#   （真机踩过：工具在 /screen/tools、程序在别处的新布局下，默认探测必然不命中）
ERRF="$(mktemp /tmp/hsinst-err.XXXXXX)"
trap 'rm -f "$ERRF" 2>/dev/null || true' EXIT

# 场景 A：--dir 指向的目录不存在
bash "$INSTALLER" --dir /nonexistent-app-xyz --tools-dir /tmp/xx-notused \
    --dry-run > "$ERRF" 2>&1
_t "程序目录不存在应退出 1" "1" "$?"
_contains "报错(目录不存在)给出 --dir 用法" "$ERRF" "--dir /usr/local/hiksemi-sub"
_contains "报错(目录不存在)给出 AppImage 解包指引" "$ERRF" "--appimage-extract"
_contains "报错(目录不存在)给出自动定位命令" "$ERRF" "find / -xdev -name hiksemi-sub"
_contains "报错(目录不存在)说明默认探测来源" "$ERRF" "tools/ 的上一级"

# 场景 B：目录存在、但没有 hiksemi-sub（★ 真机实际遇到的形态）
EMPTYDIR="$(mktemp -d /tmp/hsempty.XXXXXX)"
bash "$INSTALLER" --dir "$EMPTYDIR" --tools-dir /tmp/xx-notused \
    --dry-run > "$ERRF" 2>&1
_t "目录存在但无主程序应退出 1" "1" "$?"
_contains "报错(无主程序)给出 --dir 用法" "$ERRF" "--dir /usr/local/hiksemi-sub"
_contains "报错(无主程序)给出 AppImage 解包指引" "$ERRF" "--appimage-extract"
_contains "显式 --dir 时不触发自动搜索（路径原样报出）" "$ERRF" "$EMPTYDIR"
rm -rf "$EMPTYDIR" "$ERRF" 2>/dev/null || true

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [2] 真实文件部署（SKIP_SYSTEM，启用 CoolerControl）──"
# ─────────────────────────────────────────────────────────────
TMPROOT="$(mktemp -d /tmp/hsinst.XXXXXX)"
trap 'rm -rf "$TMPROOT" 2>/dev/null || true' EXIT

APP="$TMPROOT/app"
TOOLS="$TMPROOT/screen-tools"
SVC="$TMPROOT/systemd/hiksemi-sub.service"
LOGD="$TMPROOT/log"
mkdir -p "$APP/resources/app/dist/theme-static" "$(dirname "$SVC")"
: > "$APP/hiksemi-sub"
: > "$APP/chrome-sandbox"
echo '{"themes":[]}' > "$APP/resources/app/dist/theme-static/config.json"
: > "$APP/resources/app/dist/theme-static/default-theme.zip"

# 注意：不要预置 config.toml —— 先验证"模板未渲染"提示，再在 [3] 验证 cp -rn 不覆盖

OUT1="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" HIKSEMI_SERVICE_DST="$SVC" \
        bash "$INSTALLER" --dir "$APP" --tools-dir "$TOOLS" \
        --fan-coolercontrol on 2>&1)"
RC1=$?
_t "安装退出码 0（on）" "0" "$RC1"

# 中间件 / kiosk 部署
_exists   "中间件已部署"        "$TOOLS/middleware/middleware.py"
_exists   "data-mapping 已部署" "$TOOLS/middleware/data-mapping.json"
_exists   "kiosk.sh 已部署"     "$TOOLS/kiosk.sh"
_execable "kiosk.sh 可执行"     "$TOOLS/kiosk.sh"

# CoolerControl 部署（6 个文件 + 可执行位）
for f in config.toml modes.json render-config.py apply-mode.sh \
         probe-coolercontrol.sh README.md; do
    _exists   "coolercontrol/$f 已部署" "$TOOLS/coolercontrol/$f"
done
for f in apply-mode.sh probe-coolercontrol.sh render-config.py; do
    _execable "coolercontrol/$f 可执行" "$TOOLS/coolercontrol/$f"
done
# 首次部署应是模板原样（含占位符）
_contains "首次部署的 config.toml 仍是模板" "$TOOLS/coolercontrol/config.toml" "@DEVICE_UID@"

# service 渲染
_exists "service 已写入指定位置" "$SVC"
_t "service 无残留占位符" "0" "$(grep -c '@APP_DIR@\|@TOOLS_DIR@\|@KIOSK@' "$SVC")"
_contains "HIKSEMI_DIR/HIKSEMI_TOOLS 注入正确" "$SVC" \
          "Environment=HIKSEMI_DIR=$APP HIKSEMI_TOOLS=$TOOLS"
_contains "HIKSEMI_LOG 默认 normal 已补" "$SVC" "Environment=HIKSEMI_LOG=normal"
_active "风扇开关已生效" "$SVC" "HIKSEMI_FAN_COOLERCONTROL"
_active "风扇脚本路径已生效" "$SVC" "HIKSEMI_FAN_COOLERCONTROL_SCRIPT"
_contains "ExecStart 指向 kiosk.sh" "$SVC" "$TOOLS/kiosk.sh -- :0 vt1"
_contains "ExecStart 带 -logfile" "$SVC" "-logfile $LOGD/xorg.log"
_t "风扇开关与脚本在同一 Environment 行" "1" \
   "$(grep -c '^Environment=HIKSEMI_FAN_COOLERCONTROL=1 HIKSEMI_FAN_COOLERCONTROL_SCRIPT=' "$SVC")"

# ★ 开机 X 未就绪修复：X 等待上限 + 启动限流（限流必须在 [Unit] 段）
_contains "HIKSEMI_X_WAIT_SEC 已注入" "$SVC" "Environment=HIKSEMI_X_WAIT_SEC=15"
Unit="$(awk '/^\[Unit\]/{f=1;next} /^\[/{f=0} f' "$SVC")"
Service="$(awk '/^\[Service\]/{f=1;next} /^\[/{f=0} f' "$SVC")"
case "$Unit" in
    *"StartLimitIntervalSec=120"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("service [Unit] 段缺 StartLimitIntervalSec"); echo "  ✗ [Unit] 缺 StartLimitIntervalSec" ;;
esac
case "$Unit" in
    *"StartLimitBurst=40"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("service [Unit] 段缺 StartLimitBurst"); echo "  ✗ [Unit] 缺 StartLimitBurst" ;;
esac
case "$Service" in
    *"StartLimitIntervalSec="*|*"StartLimitBurst="*)
        # 只把「真正的赋值行」算违规（注释里提到参数名不算）
        if printf '%s\n' "$Service" | grep -qE '^[[:space:]]*StartLimit(IntervalSec|Burst)='; then
            FAIL=$((FAIL + 1)); FAILED+=("启动限流误放 [Service] 段（systemd 会忽略）"); echo "  ✗ 限流误放 [Service] 段"
        else
            PASS=$((PASS + 1))
        fi ;;
    *) PASS=$((PASS + 1)) ;;
esac
# ★ Conflicts 必须配 After（配 Before = 开机被 getty 的事务停掉，见教训 84）
case "$Unit" in
    *"Before=getty"*)
        FAIL=$((FAIL + 1)); FAILED+=("service 含 Before=getty（应为 After，否则开机被 getty 停掉）"); echo "  ✗ Before=getty 反配" ;;
    *) PASS=$((PASS + 1)) ;;
esac
case "$Unit" in
    *"After="*"getty@tty1"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("service [Unit] 段缺 After=…getty@tty1"); echo "  ✗ [Unit] 缺 After=getty@tty1" ;;
esac

# ★ 与飞牛监控服务的开机竞争（教训 87）：
#   必须排到 resmon_service / sysinfo_service 之后 + 启动前走 --pre-delay，
#   否则开机时我们的 X 抢核显会让它们「挂不上设备接口」→ Web UI 监控全空。
case "$Unit" in
    *"After="*"resmon_service.service"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("service [Unit] 缺 After=…resmon_service.service"); echo "  ✗ [Unit] 缺 After=resmon_service" ;;
esac
case "$Unit" in
    *"After="*"sysinfo_service.service"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("service [Unit] 缺 After=…sysinfo_service.service"); echo "  ✗ [Unit] 缺 After=sysinfo_service" ;;
esac
_contains "启动前让路：ExecStartPre … --pre-delay" "$SVC" "--pre-delay"
_contains "开机让路时长可配（HIKSEMI_BOOT_DELAY_SEC）" "$SVC" "HIKSEMI_BOOT_DELAY_SEC"
_contains "监控兜底开关可配（HIKSEMI_MONITOR_RESYNC）" "$SVC" "HIKSEMI_MONITOR_RESYNC"
_contains "监控兜底延迟可配（HIKSEMI_MONITOR_RESYNC_LATE）" "$SVC" "HIKSEMI_MONITOR_RESYNC_LATE"

# ★★ 2026-10-06：监控补挂必须「同步执行 + restart」——
#   旧版后台 sleep 60s 会随 cgroup 一起被杀（KillMode=control-group），
#   开机早期 kiosk 常被重拉 ⇒ 兜底永远跑不到（真机复现：加了仍无数据）。
_contains "kiosk 监控补挂用 restart（对齐手工命令，非 try-restart）" \
          "$TOOLS_DIR_SRC/kiosk.sh" 'timeout 20 systemctl restart "$u"'
_contains "kiosk 监控补挂在启动程序前【同步】执行" \
          "$TOOLS_DIR_SRC/kiosk.sh" '_MR_SYNC="$(_fn_monitors)"'
case "$(cat "$TOOLS_DIR_SRC/kiosk.sh" 2>/dev/null)" in
    *"_monitor_resync &"*)
        FAIL=$((FAIL + 1)); FAILED+=("kiosk 仍保留旧的【后台 sleep 60s】补挂实现（会被 cgroup 杀掉）")
        echo "  ✗ 旧版后台补挂实现仍在" ;;
    *) PASS=$((PASS + 1)) ;;
esac

# 输出可读性
case "$OUT1" in
    *"风扇适配器就绪"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("安装输出缺少风扇适配器结论"); echo "  ✗ 缺少『风扇适配器就绪』" ;;
esac
case "$OUT1" in
    *"仍是模板（含 @DEVICE_UID@ 占位符）"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("未提示模板未渲染"); echo "  ✗ 未提示模板未渲染" ;;
esac
case "$OUT1" in
    *"[7/7]"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("步骤编号未更新为 /7"); echo "  ✗ 步骤编号未更新为 /7" ;;
esac

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [3] 重装幂等性 ──"
# ─────────────────────────────────────────────────────────────
# 模拟"设备上已经渲染过 config.toml"，验证 cp -rn 不覆盖用户/设备数据
printf 'SENTINEL-DO-NOT-CLOBBER\n' > "$TOOLS/coolercontrol/config.toml"
OUT1B="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" HIKSEMI_SERVICE_DST="$SVC" \
         bash "$INSTALLER" --dir "$APP" --tools-dir "$TOOLS" \
         --fan-coolercontrol on 2>&1)"
_t "重装退出码 0" "0" "$?"
_t "重装后开关行仍只有一行" "1" \
   "$(grep -c '^Environment=HIKSEMI_FAN_COOLERCONTROL=' "$SVC")"
_t "重装后已渲染的 config.toml 未被覆盖（cp -rn）" \
   "SENTINEL-DO-NOT-CLOBBER" "$(head -n1 "$TOOLS/coolercontrol/config.toml")"
_t "重装后 middleware.py.orig 备份已存在" "yes" \
   "$([ -f "$TOOLS/middleware/middleware.py.orig" ] && echo yes || echo no)"

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [4] 关闭风扇适配器（--fan-coolercontrol off）──"
# ─────────────────────────────────────────────────────────────
TOOLS2="$TMPROOT/screen-tools2"
SVC2="$TMPROOT/systemd/hiksemi-sub-off.service"
OUT2="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" HIKSEMI_SERVICE_DST="$SVC2" \
        bash "$INSTALLER" --dir "$APP" --tools-dir "$TOOLS2" \
        --fan-coolercontrol off 2>&1)"
_t "安装退出码 0（off）" "0" "$?"
_absent "off 时未部署 coolercontrol 目录" "$TOOLS2/coolercontrol"
_not_active "off 时 service 无生效的风扇开关" "$SVC2" "HIKSEMI_FAN_COOLERCONTROL"
_contains "off 时仍部署中间件" "$TOOLS2/middleware/middleware.py" "MIDDLEWARE_VERSION"
_contains "off 时仍部署 kiosk.sh" "$SVC2" "$TOOLS2/kiosk.sh -- :0 vt1"
case "$OUT2" in
    *"跳过风扇适配器部署"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("off 未提示跳过"); echo "  ✗ off 未提示跳过" ;;
esac

# 已启用的 service 再次以 off 安装 → 开关行必须被清掉（幂等降级）
OUT2B="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" HIKSEMI_SERVICE_DST="$SVC" \
         bash "$INSTALLER" --dir "$APP" --tools-dir "$TOOLS" \
         --fan-coolercontrol off 2>&1)"
_t "从 on 降级到 off 退出码 0" "0" "$?"
_not_active "降级后开关行已清除" "$SVC" "HIKSEMI_FAN_COOLERCONTROL"

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [4b] 就地安装（SRC_DIR == TOOLS_DIR，不得报 same file）──"
# ─────────────────────────────────────────────────────────────
# 真机踩过：用户把项目放在 /screen/tools 后**就在该目录里**跑 install-autostart.sh，
# 此时「存放脚本的目录」== 「工具目录」⇒ cp 源与目标同一个文件，
# 报 `cp: 'X' and 'X' are the same file` 并以非 0 退出；set -e 下整套安装中断。
INPLACE="$TMPROOT/inplace"
mkdir -p "$INPLACE/tools/middleware" "$INPLACE/tools/coolercontrol" "$INPLACE/svc"
cp "$TOOLS_DIR_SRC/install-autostart.sh" "$INPLACE/tools/"
cp "$TOOLS_DIR_SRC/kiosk.sh"             "$INPLACE/tools/"
cp "$TOOLS_DIR_SRC/hiksemi-sub.service"  "$INPLACE/tools/"
cp "$TOOLS_DIR_SRC/middleware/middleware.py"      "$INPLACE/tools/middleware/"
cp "$TOOLS_DIR_SRC/middleware/data-mapping.json"  "$INPLACE/tools/middleware/"
cp "$TOOLS_DIR_SRC/coolercontrol/apply-mode.sh"   "$INPLACE/tools/coolercontrol/"
: > "$INPLACE/hiksemi-sub"

# ★ 关键：在 tools 目录【内】执行（脚本所在目录 == 工具目录）
OUT_IP="$(cd "$INPLACE/tools" && \
          HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" \
          HIKSEMI_SERVICE_DST="$INPLACE/svc/x.service" \
          bash ./install-autostart.sh --dir "$INPLACE" --tools-dir "$INPLACE/tools" 2>&1)"
RC_IP=$?
_t "就地安装退出码 0（不被 same file 打断）" "0" "$RC_IP"
_t "就地安装无 'same file' 报错" "0" \
   "$(printf '%s\n' "$OUT_IP" | grep -c 'same file')"
_t "就地安装无 [XX] 报错行" "0" \
   "$(printf '%s\n' "$OUT_IP" | grep -c '\[XX\]')"
case "$OUT_IP" in
    *"就地安装，无需复制"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("就地安装未提示跳过复制"); echo "  ✗ 就地安装未提示跳过复制" ;;
esac
# 就地安装也要产出可用 service（渲染/校验/落盘链路不被跳过）
_exists "就地安装仍产出 service" "$INPLACE/svc/x.service"
_contains "就地安装 service ExecStart 指向工具内 kiosk.sh" \
          "$INPLACE/svc/x.service" "ExecStart="
_t "就地安装 service 无残留占位符" "0" \
   "$(grep -c '@APP_DIR@\|@TOOLS_DIR@\|@KIOSK@' "$INPLACE/svc/x.service")"

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [5] 自动探测（--fan-coolercontrol auto，默认）──"
# ─────────────────────────────────────────────────────────────
_have_cc=0
if [ -f /etc/coolercontrol/config.toml ] \
   || command -v coolercontrold >/dev/null 2>&1 \
   || systemctl list-unit-files 2>/dev/null | grep -q '^coolercontrold' \
   || { command -v docker >/dev/null 2>&1 && \
        docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qi coolercontrol; }; then
    _have_cc=1
fi
TOOLS3="$TMPROOT/screen-tools3"
SVC3="$TMPROOT/systemd/hiksemi-sub-auto.service"
OUT3="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" HIKSEMI_SERVICE_DST="$SVC3" \
        bash "$INSTALLER" --dir "$APP" --tools-dir "$TOOLS3" 2>&1)"
_t "auto 安装退出码 0" "0" "$?"
case "$OUT3" in
    *"自动探测："*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("auto 未输出探测结论"); echo "  ✗ auto 未输出探测结论" ;;
esac
if [ "$_have_cc" = "1" ]; then
    _active   "auto：本机有 CoolerControl → 已注入开关" "$SVC3" "HIKSEMI_FAN_COOLERCONTROL"
    _exists   "auto：coolercontrol 已部署" "$TOOLS3/coolercontrol/apply-mode.sh"
else
    _not_active "auto：本机无 CoolerControl → 未注入开关" "$SVC3" "HIKSEMI_FAN_COOLERCONTROL"
    _absent     "auto：coolercontrol 未部署" "$TOOLS3/coolercontrol"
fi
# 未知取值应告警并按 auto 处理（不崩）
TOOLS4="$TMPROOT/screen-tools4"
SVC4="$TMPROOT/systemd/hiksemi-sub-weird.service"
OUT4="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 HIKSEMI_LOG_DIR="$LOGD" HIKSEMI_SERVICE_DST="$SVC4" \
        bash "$INSTALLER" --dir "$APP" --tools-dir "$TOOLS4" \
        --fan-coolercontrol maybe 2>&1)"
_t "未知取值不崩溃" "0" "$?"
case "$OUT4" in
    *"未知的 --fan-coolercontrol 取值"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("未知取值未告警"); echo "  ✗ 未知取值未告警" ;;
esac

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [6] --dry-run：不落盘、不改系统，但展示最终配置 ──"
# ─────────────────────────────────────────────────────────────
TOOLS5="$TMPROOT/screen-tools5"
SVC5="$TMPROOT/systemd/hiksemi-sub-dry.service"
OUT5="$(HIKSEMI_SERVICE_DST="$SVC5" bash "$INSTALLER" --dir "$APP" \
        --tools-dir "$TOOLS5" --fan-coolercontrol on --dry-run 2>&1)"
_t "dry-run 退出码 0（无需 root）" "0" "$?"
_absent "dry-run 未创建 service 文件" "$SVC5"
_absent "dry-run 未创建工具目录" "$TOOLS5"
case "$OUT5" in
    *"dry-run 模式"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("dry-run 未声明模式"); echo "  ✗ dry-run 未声明模式" ;;
esac
case "$OUT5" in
    *"HIKSEMI_FAN_COOLERCONTROL=1"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("dry-run 未展示 CC 配置行"); echo "  ✗ dry-run 未展示 CC 配置行" ;;
esac
case "$OUT5" in
    *"HIKSEMI_DIR=$APP HIKSEMI_TOOLS=$TOOLS5"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("dry-run 未展示路径注入"); echo "  ✗ dry-run 未展示路径注入" ;;
esac
# dry-run 不得产生任何落盘副作用（含回退日志目录）
_absent "dry-run 未创建 APP_DIR/logs 回退目录" "$APP/logs"

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [7] uninstall（保留文件）──"
# ─────────────────────────────────────────────────────────────
OUT7="$(HIKSEMI_INSTALL_SKIP_SYSTEM=1 bash "$INSTALLER" uninstall \
        --tools-dir "$TOOLS" 2>&1)"
_t "uninstall 退出码 0" "0" "$?"
_exists "uninstall 后中间件保留" "$TOOLS/middleware/middleware.py"
_exists "uninstall 后 coolercontrol 保留" "$TOOLS/coolercontrol/apply-mode.sh"
case "$OUT7" in
    *"均保留"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("uninstall 未说明保留范围"); echo "  ✗ uninstall 未说明保留范围" ;;
esac

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [8] 与中间件的环境变量契约一致性 ──"
# ─────────────────────────────────────────────────────────────
MW="$TOOLS_DIR_SRC/middleware/middleware.py"
_contains "中间件认识 HIKSEMI_FAN_COOLERCONTROL" "$MW" \
          'FAN_COOLERCONTROL_ENV = "HIKSEMI_FAN_COOLERCONTROL"'
_contains "中间件认识 HIKSEMI_FAN_COOLERCONTROL_SCRIPT" "$MW" \
          'FAN_COOLERCONTROL_SCRIPT_ENV = "HIKSEMI_FAN_COOLERCONTROL_SCRIPT"'
_contains "中间件默认候选含 /screen/tools 路径" "$MW" \
          '"/screen/tools/coolercontrol/apply-mode.sh"'
# 安装器注入的脚本路径必须在中间件的候选集合里（默认布局下）
_contains "安装器注入路径与中间件候选一致" "$INSTALLER" \
          'HIKSEMI_FAN_COOLERCONTROL_SCRIPT=$CC_DST/apply-mode.sh'

# ★ getty@tty1 双层防线（2026-10-05）：mask 是主修（logind 动态拉起竞态），
#   卸载必须恢复
_contains "安装器会 mask getty@tty1（logind 动态拉起竞态的主修）" "$INSTALLER" \
          'systemctl mask getty@tty1.service'
_contains "卸载器会 unmask getty@tty1（恢复 tty1 登录终端）" "$INSTALLER" \
          'systemctl unmask getty@tty1.service'

# ─────────────────────────────────────────────────────────────
echo ""
echo "── [9] kiosk.sh 前置检查 / 退出码解读（功能性）──"
# ─────────────────────────────────────────────────────────────
# 这些用例真正【跑一次 kiosk.sh】，证明：
#   ① 主程序缺失时不会只甩裸 bash 报错，而是写清原因 + 修复指引，并 rc=127；
#   ② 程序主动 exit 1 时，kiosk.log 里有「退出码解读」把 rc=1 翻译成病因。
# 用独立的 HIKSEMI_LOG 环境（无 root 时自动回退到 APP_DIR/logs），避免污染真机日志。
KT="$(mktemp -d /tmp/hskt.XXXXXX)"
mkdir -p "$KT/app" "$KT/tools"
cp "$TOOLS_DIR_SRC/kiosk.sh" "$KT/tools/kiosk.sh"

# 场景 A：程序目录为空（主程序不存在）
mkdir -p "$KT/empty"
HIKSEMI_DIR="$KT/empty" HIKSEMI_TOOLS="$KT/tools" bash "$KT/tools/kiosk.sh" >/dev/null 2>&1
_RC_A=$?
_t "kiosk.sh 主程序缺失 → rc=127" "127" "$_RC_A"
# 日志目录：/var/log/hiksemi-sub 可写时用它，否则回退 APP_DIR/logs
_KLOG="/var/log/hiksemi-sub/kiosk.log"
[ -w /var/log/hiksemi-sub ] 2>/dev/null || _KLOG="$KT/empty/logs/kiosk.log"
case "$(cat "$_KLOG" 2>/dev/null)" in
    *"程序本体前置检查未通过"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("kiosk 主程序缺失未写前置检查"); echo "  ✗ 未写前置检查" ;;
esac
case "$(cat "$_KLOG" 2>/dev/null)" in
    *"修复指引"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("kiosk 主程序缺失未给修复指引"); echo "  ✗ 未给修复指引" ;;
esac

# 场景 B：主程序存在但 exit 1
printf '#!/bin/bash\necho "FATAL: initThemeConfigInNas EACCES /screen/lang.config"\nexit 1\n' \
    > "$KT/app/hiksemi-sub"
chmod +x "$KT/app/hiksemi-sub"
HIKSEMI_DIR="$KT/app" HIKSEMI_TOOLS="$KT/tools" bash "$KT/tools/kiosk.sh" >/dev/null 2>&1
_RC_B=$?
_t "kiosk.sh 程序 exit 1 → rc=1（原样透传）" "1" "$_RC_B"
case "$(cat "$_KLOG" 2>/dev/null)" in
    *"退出码解读（rc=1）"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("kiosk rc=1 未写退出码解读"); echo "  ✗ 未写退出码解读" ;;
esac
case "$(cat "$_KLOG" 2>/dev/null)" in
    *"initThemeConfigInNas"*) PASS=$((PASS + 1)) ;;
    *) FAIL=$((FAIL + 1)); FAILED+=("kiosk 未把 app.log 尾部带进 kiosk.log"); echo "  ✗ 未带 app.log 尾部" ;;
esac

# 场景 C：主程序存在但不可执行
# ★ 平台差异：Windows / Git-Bash（MSYS/NTFS）不保留 Unix 的 x 位语义，
#   chmod -x 之后 [ -x ] 仍会返回真 —— 这是测试环境限制，不是脚本 bug。
#   先探测本环境是否真的支持「去掉 x 位」，不支持就跳过该用例（不计入失败）。
mkdir -p "$KT/app2"
printf '#!/bin/bash\nexit 0\n' > "$KT/app2/hiksemi-sub"
chmod +x "$KT/app2/hiksemi-sub"
chmod -x "$KT/app2/hiksemi-sub"
if [ -x "$KT/app2/hiksemi-sub" ]; then
    echo "  ⊘ 跳过「主程序不可执行」用例（本文件系统不支持 x 位语义，仅真机 Linux 可测）"
else
    HIKSEMI_DIR="$KT/app2" HIKSEMI_TOOLS="$KT/tools" bash "$KT/tools/kiosk.sh" >/dev/null 2>&1
    _t "kiosk.sh 主程序不可执行 → rc=127" "127" "$?"
    case "$(cat "$_KLOG" 2>/dev/null)" in
        *"不可执行"*) PASS=$((PASS + 1)) ;;
        *) FAIL=$((FAIL + 1)); FAILED+=("kiosk 未识别不可执行"); echo "  ✗ 未识别不可执行" ;;
    esac
fi
# 场景 D：--pre-delay 开机让路模式（由 ExecStartPre 调用）
#   断言：① rc=0（让路模式正常返回）；② 【没有跑 §5】——不该生成 app.log，
#         否则说明它把程序本体也拉起来了（那就不是「让路」而是重复启动）。
HIKSEMI_BOOT_DELAY_SEC=0 bash "$KT/tools/kiosk.sh" --pre-delay >/dev/null 2>&1
_t "kiosk.sh --pre-delay → rc=0（让路模式正常返回）" "0" "$?"
if [ -f "$KT/tools/logs/app.log" ]; then
    FAIL=$((FAIL + 1)); FAILED+=("--pre-delay 不该启动程序（却生成了 app.log）"); echo "  ✗ --pre-delay 启动了程序"
else
    PASS=$((PASS + 1))
fi

rm -rf "$KT" 2>/dev/null || true

echo ""
echo "============================================================"
if [ "$FAIL" -eq 0 ]; then
    echo -e " 结果：\033[32m$PASS 通过 / 0 失败\033[0m"
else
    echo -e " 结果：\033[31m$PASS 通过 / $FAIL 失败\033[0m"
    printf '   - %s\n' "${FAILED[@]}"
fi
echo "============================================================"
[ "$FAIL" -eq 0 ] || exit 1

#!/bin/bash
# ============================================================
# hiksemi-sub · 统一测试入口 ★ 全项目唯一的测试脚本
# ------------------------------------------------------------
# 把散落的测试收成一处调用：
#   · middleware/_test_*.py  —— 中间件单元/集成测试（8 个文件）
#   · _test_coolercontrol.py —— CoolerControl 配置渲染器测试（双风扇/降级/校验）
#   · _test_installer.sh     —— 安装器回归测试
#
# 改动 middleware.py / data-mapping.json / install-autostart.sh 后，
# 跑这一条就够：  bash test.sh
#
# 子命令：
#   （无） / all   跑全部（中间件 + 渲染器 + 安装器）
#   middleware     只跑中间件 py 测试
#   coolercontrol  只跑 CoolerControl 渲染器测试
#   installer      只跑安装器测试
#   -v / --verbose 显示完整输出（默认只显示汇总）
#
# Python 解析顺序：$HIKSEMI_PYTHON → python3 → python → ~/.workbuddy 托管解释器
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
MW_DIR="$SCRIPT_DIR/middleware"
CC_TEST="$SCRIPT_DIR/_test_coolercontrol.py"
INSTALLER_TEST="$SCRIPT_DIR/_test_installer.sh"
BAR="============================================================"

usage() {
    cat <<'EOF'
============================================================
 hiksemi-sub · 统一测试入口 ★ 全项目唯一的测试脚本
------------------------------------------------------------
子命令：
  （无） / all   跑全部（中间件 + 渲染器 + 安装器）
  middleware     只跑中间件 py 测试
  coolercontrol  只跑 CoolerControl 渲染器测试
  installer      只跑安装器测试
  -v / --verbose 显示完整输出（默认只显示汇总）
============================================================
EOF
}

MODE="all"
VERBOSE=0
for a in "$@"; do
    case "$a" in
        all)          MODE="all" ;;
        middleware)   MODE="middleware" ;;
        coolercontrol) MODE="coolercontrol" ;;
        installer)    MODE="installer" ;;
        -v|--verbose) VERBOSE=1 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "未知参数：$a（--help 看用法）" >&2; exit 2 ;;
    esac
done

# ── 找 Python ───────────────────────────────────────────────
find_python() {
    [ -n "$HIKSEMI_PYTHON" ] && [ -x "$HIKSEMI_PYTHON" ] && { echo "$HIKSEMI_PYTHON"; return; }
    command -v python3 2>/dev/null && return
    command -v python  2>/dev/null && return
    local c
    for c in "$HOME"/.workbuddy/binaries/python/versions/*/python.exe \
             "$HOME"/.workbuddy/binaries/python/envs/default/bin/python; do
        [ -x "$c" ] && { echo "$c"; return; }
    done
    echo ""
}
PY="$(find_python)"

strip_ansi() { sed 's/\x1b\[[0-9;]*m//g'; }

TOTAL_PASS=0
TOTAL_FAIL=0
declare -a ROWS=()
declare -a FAILED_TESTS=()

# ── 单个 py 测试 ────────────────────────────────────────────
run_py() {
    local f="$1" out rc line p fl
    out="$("$PY" "$f" 2>&1)"; rc=$?
    line="$(printf '%s\n' "$out" | strip_ansi | grep -E '结果：' | tail -1)"
    p="$(printf '%s' "$line" | grep -oE '[0-9]+ 通过'   | grep -oE '[0-9]+')"
    fl="$(printf '%s' "$line" | grep -oE '[0-9]+ 失败' | grep -oE '[0-9]+')"
    p="${p:-0}"; fl="${fl:-0}"
    # 无「结果：」行 = 脚本异常退出，全算失败
    if [ -z "$line" ]; then p=0; fl=1; fi
    TOTAL_PASS=$((TOTAL_PASS+p)); TOTAL_FAIL=$((TOTAL_FAIL+fl))
    local mark="✓"; [ "$fl" -ne 0 ] && { mark="✗"; FAILED_TESTS+=("$f"); }
    ROWS+=("$(printf '  %s %-28s %4s 通过 / %s 失败' "$mark" "$(basename "$f")" "$p" "$fl")")
    if [ "$VERBOSE" = 1 ]; then
        echo "──────── $f ────────"
        printf '%s\n' "$out"
    fi
    [ "$fl" -eq 0 ] && [ "$rc" -eq 0 ]
}

# ── 安装器测试 ──────────────────────────────────────────────
run_installer() {
    local out rc line p fl
    out="$(bash "$INSTALLER_TEST" 2>&1)"; rc=$?
    line="$(printf '%s\n' "$out" | strip_ansi | grep -E '结果：' | tail -1)"
    p="$(printf '%s' "$line" | grep -oE '[0-9]+ 通过'   | grep -oE '[0-9]+')"
    fl="$(printf '%s' "$line" | grep -oE '[0-9]+ 失败' | grep -oE '[0-9]+')"
    p="${p:-0}"; fl="${fl:-0}"
    if [ -z "$line" ]; then p=0; fl=1; fi
    TOTAL_PASS=$((TOTAL_PASS+p)); TOTAL_FAIL=$((TOTAL_FAIL+fl))
    local mark="✓"; [ "$fl" -ne 0 ] && { mark="✗"; FAILED_TESTS+=("_test_installer.sh"); }
    ROWS+=("$(printf '  %s %-28s %4s 通过 / %s 失败' "$mark" "_test_installer.sh" "$p" "$fl")")
    if [ "$VERBOSE" = 1 ]; then
        echo "──────── _test_installer.sh ────────"
        printf '%s\n' "$out"
    fi
    [ "$fl" -eq 0 ] && [ "$rc" -eq 0 ]
}

echo "$BAR"
echo " hiksemi-sub 测试  $(date '+%F %T')"
[ "$MODE" != "installer" ] && echo " Python: ${PY:-未找到！}"
echo "$BAR"

if [ "$MODE" = "all" ] || [ "$MODE" = "middleware" ]; then
    if [ -z "$PY" ]; then
        echo "  ✗ 未找到 python，跳过中间件测试（设 HIKSEMI_PYTHON 指定解释器）"
        TOTAL_FAIL=$((TOTAL_FAIL+1))
        FAILED_TESTS+=("middleware（无 python）")
    else
        shopt -s nullglob
        for f in "$MW_DIR"/_test_*.py; do run_py "$f"; done
        shopt -u nullglob
    fi
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "coolercontrol" ]; then
    if [ -z "$PY" ]; then
        echo "  ✗ 未找到 python，跳过 CoolerControl 渲染器测试"
        TOTAL_FAIL=$((TOTAL_FAIL+1))
        FAILED_TESTS+=("coolercontrol（无 python）")
    elif [ -f "$CC_TEST" ]; then
        run_py "$CC_TEST"
    else
        echo "  ✗ 缺 $CC_TEST"; TOTAL_FAIL=$((TOTAL_FAIL+1)); FAILED_TESTS+=("coolercontrol（缺脚本）")
    fi
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "installer" ]; then
    if [ -f "$INSTALLER_TEST" ]; then run_installer
    else echo "  ✗ 缺 $INSTALLER_TEST"; TOTAL_FAIL=$((TOTAL_FAIL+1)); FAILED_TESTS+=("installer（缺脚本）"); fi
fi

echo
echo "$BAR"
printf '%s\n' "${ROWS[@]}"
echo "$BAR"
if [ "$TOTAL_FAIL" -eq 0 ]; then
    echo " 合计：$TOTAL_PASS 通过 / 0 失败   ✅ 全部通过"
else
    echo " 合计：$TOTAL_PASS 通过 / $TOTAL_FAIL 失败   ❌"
    printf '   - %s\n' "${FAILED_TESTS[@]}"
fi
echo "$BAR"
[ "$TOTAL_FAIL" -eq 0 ] || exit 1

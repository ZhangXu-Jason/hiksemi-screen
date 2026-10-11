#!/usr/bin/env bash
# =============================================================================
# probe-coolercontrol.sh —— 真机探测：CoolerControl 安装情况 + device-uid/channel
# =============================================================================
# 只读脚本，不修改任何配置。目的：为 render-config.py 取得必需的参数。
#
# 用法：
#     sudo ./probe-coolercontrol.sh            # 完整探测
#     ./probe-coolercontrol.sh --short         # 只打印可直接复制的一行命令
#     sudo ./probe-coolercontrol.sh modes      # ★ 诊断「Mode 在 UI 里内容为空」
#         （比对 daemon 真值设备表 / modes.json 引用的 UID / 通道 / profile）
#     sudo ./probe-coolercontrol.sh sync       # ★★ 诊断「屏幕 ↔ CoolerControl 不同步」
#         （工具版本 / 档位绑定 / 反向档位 / daemon 停启通道 / 中间件是否已重启 / 自我回滚防护 / config.toml 一致性）
#     sudo ./probe-coolercontrol.sh watch      # ★★ 实时盯 modes.json 变化（抓"切了又弹回"）
#     CC_DIR=/path ./probe-coolercontrol.sh modes   # 配置目录不在 /etc/coolercontrol 时
# =============================================================================
set +e

SHORT=0
[ "${1:-}" = "--short" ] && SHORT=1

SEC() { printf '\n\033[1;36m【%s】\033[0m\n' "$*"; }
OK()  { printf '  \033[32m✓\033[0m %s\n' "$*"; }
NG()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
INFO(){ printf '  · %s\n' "$*"; }

# ── 0.0b watch 分支：★ 实时盯 modes.json 的 current_active_mode，抓自我回滚 ──
#   用法：sudo ./probe-coolercontrol.sh watch [秒数，默认 30]
#   在屏幕上切一次档，观察这里打印的序列：
#     正常 →  3 → 2（切成低速后就停在 2）
#     自我回滚 → 3 → 2 → 3（切过去又弹回来了，说明反向同步把它覆盖了）
if [ "${1:-}" = "watch" ] || [ "${1:-}" = "--watch" ]; then
    CC_DIR="${CC_DIR:-/etc/coolercontrol}"
    MODES="$CC_DIR/modes.json"
    MAP="$CC_DIR/hs-fan-mode-map.json"
    SECS="${2:-30}"
    [ -f "$MODES" ] || { echo "找不到 $MODES"; exit 1; }
    PYBIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"
    rev_name() {  # uid → 屏幕档位（用映射反查）
        [ -n "$PYBIN" ] && [ -f "$MAP" ] && \
            CC_MODE_CONFIG="$MODES" CC_FAN_MODE_MAP="$MAP" \
            "$PYBIN" -c "
import json,os,sys
d=json.load(open(os.environ['CC_MODE_CONFIG'],encoding='utf-8'))
cur=d.get('current_active_mode')
name=next((m.get('name') for m in d.get('modes',[]) if m.get('uid')==cur),'?')
print(f\"{cur}  ({name})\")" 2>/dev/null
    }
    echo "★ 实时监视 $MODES 的 current_active_mode（${SECS}s）"
    echo "  现在去小彩屏上切一次风扇档位，观察下面的序列。"
    echo "  正常     : 3 → 2（停在 2）"
    echo "  自我回滚 : 3 → 2 → 3（弹回去了 = 本 bug）"
    echo "──────────────────────────────────────────────────────"
    LAST=""
    i=0
    while [ "$i" -lt "$SECS" ]; do
        NOW="$(rev_name)"
        if [ "$NOW" != "$LAST" ]; then
            printf '  %s  →  %s\n' "$(date '+%H:%M:%S')" "$NOW"
            LAST="$NOW"
        fi
        sleep 1
        i=$((i + 1))
    done
    echo "──────────────────────────────────────────────────────"
    echo "若出现「3 → 2 → 3」："
    echo "  ① 确认中间件是新版： bash $0 sync  （看 ⑥ 环）"
    echo "  ② 若 ⑥ 环 NG → 重跑安装器并重启 hiksemi-sub"
    exit 0
fi

# ── 0.0 sync 分支：★ 「屏幕 ↔ CoolerControl 不同步」一次性取证 ─────────────
#   症状：屏幕切档 CC 没反应 / 在 CC UI 里切模式屏幕不跟 / 新建模式后失联。
#   链条有七环，任一环断都会表现成"不同步"，逐环查：
#     ① 工具是不是新版（旧脚本 = 修了也没用）
#     ② 档位能不能解析出 Mode（绑定）+ 名字与内容是否相符
#     ③ 反向：当前 Mode 能不能反查出屏幕档位
#     ④ daemon 停/启通道（v4 切档 = stop → 写 config.toml → start）
#     ⑤ 中间件有没有重启（反向同步是新代码，跑着旧进程就不会生效）
#     ⑥ 自我回滚防护（★ 屏幕切档后自己弹回去的根因）
#     ⑦ config.toml 与当前 Mode 是否一致（★「模式改了配置没改」的直接判据）
if [ "${1:-}" = "sync" ] || [ "${1:-}" = "--sync" ]; then
    CC_DIR="${CC_DIR:-/etc/coolercontrol}"
    HERE="$(cd "$(dirname "$0")" && pwd)"
    PYBIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"
    export CC_MODE_CONFIG="$CC_DIR/modes.json"
    export CC_FAN_MODE_MAP="$CC_DIR/hs-fan-mode-map.json"

    SEC "① 工具版本（旧脚本 = 一切白搭）"
    MISS=0
    for f in fan-mode-map.py render-config.py apply-mode.sh; do
        if [ -f "$HERE/$f" ]; then OK "$f"; else NG "缺少 $HERE/$f"; MISS=1; fi
    done
    if grep -q -- "--scan" "$HERE/apply-mode.sh" 2>/dev/null; then
        OK "apply-mode.sh 是新版（支持 --scan）"
    else
        NG "apply-mode.sh 是【旧版】（无 --scan）⇒ 档位绑定修复根本没到位"
        MISS=1
    fi
    if grep -q "fan-mode-map.py" "$HERE/apply-mode.sh" 2>/dev/null; then
        OK "apply-mode.sh 走映射解析（不再写死 uid）"
    else
        NG "apply-mode.sh 仍在写死 uid"
        MISS=1
    fi
    if [ "$MISS" = "1" ]; then
        INFO "修复：重跑安装器（已改为内容比对同步，覆盖的旧文件会备份成 *.bak-install-*）"
        INFO "      sudo bash $(dirname "$HERE")/install-autostart.sh --fan-coolercontrol on"
        INFO "      或手工：sudo cp -f <本地>/tools/coolercontrol/* $HERE/"
    fi

    SEC "② 档位 → Mode 绑定"
    [ -f "$CC_DIR/modes.json" ] && OK "modes.json: $CC_DIR/modes.json" \
        || NG "找不到 $CC_DIR/modes.json"
    [ -f "$CC_FAN_MODE_MAP" ] && OK "映射文件: $CC_FAN_MODE_MAP" \
        || INFO "无映射文件（走自动认领）—— 渲染时会自动生成"
    if [ -n "$PYBIN" ] && [ -f "$HERE/fan-mode-map.py" ]; then
        "$PYBIN" "$HERE/fan-mode-map.py" --scan 2>&1 | sed 's/^/  /'
    fi

    SEC "③ 当前激活 & 反向档位"
    if [ -n "$PYBIN" ] && [ -f "$HERE/fan-mode-map.py" ]; then
        CUR="$("$PYBIN" "$HERE/fan-mode-map.py" --reverse 2>/dev/null)"
        if [ -n "$CUR" ]; then
            OK "当前激活的 Mode 对应屏幕档位 = $CUR"
        else
            NG "当前激活的 Mode 识别不出档位 ⇒ 屏幕回显不会跟"
            INFO "修：sudo $HERE/apply-mode.sh --bind 5=<uid 或 Mode 名字>"
        fi
    fi

    SEC "④ daemon 停/启通道（★ v4 切档 = stop → 写 config.toml → start，必须能停能启）"
    # ★ 与 apply-mode.sh 的 cc_detect_ctl 保持【同一套判定】：
    #   ① HIKSEMI_FAN_CC_RESTART（自定义整条命令）优先；
    #   ② 再 systemd（list-unit-files 模糊匹配）；
    #   ③ 再 docker（名字 → 镜像名兜底）。
    #   ★ 注意：自定义命令【拆不开】stop/start ⇒ 只能走 --restart 整条兜底
    #     （apply-mode.sh 会因此走 restart 路径，probe 这里要如实说明）。
    FOUND=0
    if [ -n "${HIKSEMI_FAN_CC_RESTART:-}" ]; then
        OK "HIKSEMI_FAN_CC_RESTART 已设（自定义命令）"
        INFO "命令: ${HIKSEMI_FAN_CC_RESTART}"
        INFO "  ★ 自定义命令无法拆 stop/start ⇒ 切档将走【整条 restart】兜底"
        FOUND=1
    fi
    if command -v systemctl >/dev/null 2>&1; then
        U="$(systemctl list-unit-files --type=service --plain --no-legend 2>/dev/null \
             | awk '{print $1}' | grep -i -m1 'coolercontrol' || true)"
        if [ -n "$U" ]; then
            OK "systemd 服务: $U ($(systemctl is-active "${U%.service}" 2>/dev/null))"
            INFO "  ★ apply-mode.sh 实际会：stop/start ${U%.service}（v4 顺序）"
            FOUND=1
        fi
    fi
    if command -v docker >/dev/null 2>&1; then
        C="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep -i -m1 'coolercontrol' || true)"
        if [ -z "$C" ]; then
            C="$(docker ps -a --format '{{.Names}}\t{{.Image}}' 2>/dev/null \
                 | grep -i 'coolercontrol' | head -1 | cut -f1 || true)"
        fi
        if [ -n "$C" ]; then
            OK "docker 容器: $C（按名字或镜像名模糊匹配）"
            INFO "  ★ apply-mode.sh 实际会：docker stop/start $C（v4 顺序）"
            FOUND=1
        fi
    fi
    if [ "$FOUND" = "0" ]; then
        NG "既没找到 systemd 服务 / docker 容器，也没设 HIKSEMI_FAN_CC_RESTART
      ⇒ ★ v4 切档要 stop→写→start，找不到通道 ⇒ 只能【整条 restart 兜底】。
      ⇒ 修法（任选其一）：
         export HIKSEMI_FAN_CC_RESTART='docker restart <容器名>'
         （写进 hiksemi-sub.service 的 Environment=，
           或直接在中间件用 HIKSEMI_FAN_APPLY_CMD 指定整条命令）"
    fi

    SEC "⑤ 中间件是否已重启（★ 反向同步是新代码，跑旧进程就不生效）"
    SVC=/etc/systemd/system/hiksemi-sub.service
    if [ ! -f "$SVC" ]; then
        INFO "没找到 $SVC（中间件不是 systemd 起的？跳过）"
    else
        if grep -q '^Environment=HIKSEMI_FAN_COOLERCONTROL=1' "$SVC"; then
            OK "已注入 HIKSEMI_FAN_COOLERCONTROL=1"
        else
            NG "service 里没有生效的 HIKSEMI_FAN_COOLERCONTROL=1 ⇒ 屏幕与 CC 不会联动"
            INFO "修：sudo bash $(dirname "$HERE")/install-autostart.sh --fan-coolercontrol on"
        fi
        command -v systemctl >/dev/null 2>&1 &&
            INFO "hiksemi-sub: $(systemctl is-active hiksemi-sub 2>/dev/null)"
        ST="$(systemctl show -p ExecMainStartTimestamp hiksemi-sub 2>/dev/null | cut -d= -f2-)"
        MW="$(dirname "$HERE")/middleware/middleware.py"
        if [ -n "$ST" ] && [ -f "$MW" ]; then
            E1="$(date -d "$ST" +%s 2>/dev/null)"
            E2="$(stat -c %Y "$MW" 2>/dev/null)"
            INFO "中间件启动于: $ST"
            INFO "middleware.py 修改于: $(date -r "$MW" '+%F %T' 2>/dev/null)"
            if [ -n "$E1" ] && [ -n "$E2" ] && [ "$E1" -lt "$E2" ]; then
                NG "★ 中间件进程比 middleware.py 旧 ⇒ 改动【没生效】"
                INFO "修：sudo systemctl restart hiksemi-sub"
            else
                OK "中间件比 middleware.py 新（改动已在跑）"
            fi
        fi
    fi

    SEC "⑥ 反向同步与自我回滚防护（★ 屏幕切档后自己弹回去的根因）"
    MW="$(dirname "$HERE")/middleware/middleware.py"
    TIER="$HERE/fan-tier.py"
    if [ ! -f "$MW" ]; then
        INFO "找不到 middleware.py，跳过"
    else
        # ★ 现在反向同步的规则来自 fan-tier.py；中间件只负责"读回值 != 本地值才同步"
        if grep -q "_cc_current_fan_mode" "$MW" 2>/dev/null && \
           grep -q "ext != cur" "$MW" 2>/dev/null; then
            OK "反向同步已就位（读回值 ≠ 本地值才同步 ⇒ 天然不会自我回滚）"
        else
            NG "中间件【缺】反向同步/回滚防护 ⇒ 屏幕切档可能被弹回原档"
            INFO "修：同步新版 middleware.py 并重启："
            INFO "    sudo bash $(dirname "$HERE")/install-autostart.sh --fan-coolercontrol on"
            INFO "    sudo systemctl restart hiksemi-sub"
        fi
        if [ -f "$TIER" ]; then
            OK "档位规则模块 fan-tier.py 已就位（唯一权威来源）"
        else
            NG "缺少 fan-tier.py ⇒ 中间件反向同步会关闭（屏幕回显只跟本地状态）"
            INFO "修：重跑安装器同步 tools/coolercontrol/"
        fi
        # ★ 反向同步是否真的在读 modes.json（中间件启用态）
        SVC=/etc/systemd/system/hiksemi-sub.service
        if [ -f "$SVC" ] && grep -q 'HIKSEMI_FAN_COOLERCONTROL=1' "$SVC"; then
            OK "反向同步已启用（HIKSEMI_FAN_COOLERCONTROL=1）"
        fi
    fi

    SEC "⑦ config.toml 与 modes.json 是否一致（★ 模式改了但实际配置没变）"
    #   症状：切档后 modes.json 的 current_active_mode 变了，但 config.toml 里的
    #   [device-settings] 还是旧档的内容 ⇒ 屏幕显示 A、风扇实际跑 B。
    #   这里直接对比「当前激活 Mode 的 all_device_settings」与 config.toml 的真实设置。
    if [ -n "$PYBIN" ] && [ -f "$HERE/fan-mode-map.py" ]; then
        "$PYBIN" - "$CC_DIR" <<'PY'
import json, os, re, sys
cc = sys.argv[1]
cfg_p = os.path.join(cc, "config.toml")
mod_p = os.path.join(cc, "modes.json")

def info(s): print(f"  · {s}")
def ok(s):   print(f"  \033[32m✓\033[0m {s}")
def ng(s):   print(f"  \033[31m✗\033[0m {s}")

if not os.path.isfile(mod_p):
    ng(f"找不到 {mod_p}"); sys.exit(0)
try:
    mj = json.load(open(mod_p, encoding="utf-8"))
except Exception as e:
    ng(f"读 modes.json 失败: {e}"); sys.exit(0)

cur = mj.get("current_active_mode")
modes = {m.get("uid"): m for m in mj.get("modes", [])}
cm = modes.get(cur)
if not cm:
    ng(f"current_active_mode={cur!r} 不在 modes 列表里")
    sys.exit(0)
info(f"当前激活 Mode: {cm.get('name')}  (uid={cur})")

# config.toml 的 [device-settings.<uid>] 段（daemon 真实生效的设置）
text = ""
if os.path.isfile(cfg_p):
    text = open(cfg_p, encoding="utf-8", errors="replace").read()
cur_dev = {}
# 取【最后一个】 [device-settings.<uid>] 段（daemon 会重写）
# ★ 段头两种写法都要认（带引号 / 不带）⇒ 去引号再比较；通道键 likewise 允许引号，
#   否则 `"fan3" = {...}` 匹配不上 ⇒ 误报「不一致」（本环最常见的假阴性）。
for m in re.finditer(r"^\[device-settings\.([^\]]+)\][ \t]*$(.*?)(?=^\[|\Z)",
                     text, flags=re.M | re.S):
    uid = m.group(1).strip().strip('"').strip("'")
    body = m.group(2)
    d = {}
    for line in body.splitlines():
        mm = re.match(r'^\s*["\']?(fan\w+)["\']?\s*=\s*\{(.*)\}\s*$', line)
        if not mm:
            continue
        ch, kv = mm.group(1), mm.group(2)
        pu = re.search(r'profile_uid\s*=\s*"([^"]*)"', kv)
        sf = re.search(r'speed_fixed\s*=\s*(\d+)', kv)
        d[ch] = pu.group(1) if pu else (f"Fixed {sf.group(1)}%" if sf else "?")
    cur_dev[uid] = d

# 对比 Mode 期望的设置 vs config.toml 真实设置
def norm(d):
    return {k: v for k, v in (d or {}).items() if v}

want_all = {}
for dev, chans in (cm.get("all_device_settings") or {}).items():
    want_all[dev] = norm({c: (st.get("profile_uid") or
                              (f"Fixed {st.get('speed_fixed')}%" if "speed_fixed" in st else "?"))
                          for c, st in (chans or {}).items()})

if not want_all:
    ng("当前 Mode 的 all_device_settings 为空 ⇒ 激活它不会改任何设置（会重置为默认）")
    info("  ⇒ 这就是「模式变了但配置没变」的原因：Mode 里根本没有设置内容")
    sys.exit(0)

mismatch = 0
for dev, chans in want_all.items():
    got = cur_dev.get(dev, {})
    for ch, want in chans.items():
        have = got.get(ch)
        if have != want:
            mismatch += 1
            ng(f"设备 {dev[:12]}… 通道 {ch}: config.toml={have!r} ≠ Mode={want!r}")
if mismatch == 0:
    ok("config.toml 的 [device-settings] 与当前 Mode 内容一致（已真正生效）")
else:
    ng(f"有 {mismatch} 处不一致 ⇒ 档位【没真正落到 config.toml】")
    info("  ①②③ 均先确认工具是新版（v4 切档 = stop daemon → 写 config.toml → start）")
    info("  ① 若 apply-mode.sh 不是新版 ⇒ 同步工具目录")
    info("  ② apply-mode.sh 返回 5 = 写完回读仍不一致（⑨ 环会 NG），按告警原因查")
    info("  ③ 若配置里有 @DEVICE_UID@ 等占位符残留 ⇒ render-config.py --auto 重渲")
    info("  ④ 兜底：apply-mode.sh --restart <mode>（整条重启 daemon，慎用）")
PY
    else
        INFO "缺少 python3 或 fan-mode-map.py，跳过"
    fi

    echo
    INFO "把上面整段输出贴给排障方即可定位。常见结论："
    INFO "  · ① 有 NG        → 脚本没同步到真机（重跑安装器，已改为内容比对同步）"
    INFO "  · ② 有『未匹配』 → 名字不含档位关键词，需 --bind"
    INFO "  · ② 有『内容与档位不符』→ 该 Mode 挂错了 profile"
    INFO "        （例如「hs 低速 25%」里居然是 hs-standard=60%）"
    INFO "        ⇒ 修：render-config.py --auto --merge-modes <modes.json> --rename-modes"
    INFO "  · ③ 有 NG        → 反向档位识别不出，需 --bind"
    INFO "  · ④ 有 NG        → 找不到 daemon 停/启通道（v4 切档需 stop→写→start；缺则只能整条 restart 兜底）"
    INFO "  · ⑤ 有 NG        → 改了中间件没重启"
    INFO "  · ⑥ 有 NG        → 反向同步/规则模块缺失 ⇒ 屏幕切档会自己弹回"
    INFO "  · ⑦ 有 NG        → 模式改了但 config.toml 没跟上（档位没真正生效）"
    exit 0
fi

# ── 0. modes 诊断分支（放在最前，跑完即退出）───────────────────────────────
# 症状：CoolerControl Web UI 打开某个 Mode，「设备 / 通道 / 设置」一片空白。
# 99% 不是模板结构问题（我们的 modes.json 完全符合 daemon 契约），而是
# modes.json 里的 device UID 与 daemon 当前认定的设备对不上（UID 漂移）。
if [ "${1:-}" = "modes" ] || [ "${1:-}" = "--modes" ]; then
    CC_DIR="${CC_DIR:-/etc/coolercontrol}"
    SEC "0. CoolerControl 运行形态"
    if command -v systemctl >/dev/null 2>&1; then
        for s in coolercontrold coolercontrol; do
            systemctl list-unit-files 2>/dev/null | grep -q "^${s}\.service" &&
                INFO "$s: $(systemctl is-active "$s" 2>/dev/null)"
        done
    fi
    if command -v docker >/dev/null 2>&1; then
        docker ps -a --format '{{.Names}}\t{{.Image}}' 2>/dev/null |
            grep -i coolercontrol | sed 's/^/  容器: /'
    fi
    [ -d "$CC_DIR" ] && OK "配置目录: $CC_DIR" || NG "配置目录不存在: $CC_DIR"
    for f in config.toml modes.json; do
        if [ -f "$CC_DIR/$f" ]; then
            INFO "$f  mtime=$(date -r "$CC_DIR/$f" '+%F %T' 2>/dev/null)  size=$(wc -c < "$CC_DIR/$f")"
        else
            NG "缺少 $CC_DIR/$f"
        fi
    done

    PYBIN="$(command -v python3 2>/dev/null || command -v python 2>/dev/null)"
    [ -n "$PYBIN" ] || { NG "找不到 python3/python，无法诊断"; exit 1; }
    "$PYBIN" - "$CC_DIR" <<'PY'
import json, os, re, sys

cc = sys.argv[1]
cfg_p = os.path.join(cc, "config.toml")
mod_p = os.path.join(cc, "modes.json")

def sec(t): print(f"\n\033[1;36m【{t}】\033[0m")
def ok(s):  print(f"  \033[32m✓\033[0m {s}")
def ng(s):  print(f"  \033[31m✗\033[0m {s}")
def info(s):print(f"  · {s}")

# ---- 读 config.toml：[devices] / [[profiles]] / [device-settings] ----
text = ""
if os.path.isfile(cfg_p):
    with open(cfg_p, encoding="utf-8", errors="replace") as f:
        text = f.read()
else:
    ng(f"无 {cfg_p}")

devices, profiles, ds = {}, [], {}
if text:
    m = re.search(r"^\[devices\][ \t]*$(.*?)(?=^[ \t]*\[)", text, flags=re.M | re.S)
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r'^"?([0-9a-fA-F]{64})"?[ \t]*=[ \t]*"(.*)"[ \t]*$', line.strip())
            if mm: devices[mm.group(1)] = mm.group(2)
    # [[profiles]] 块内的 uid
    blocks = re.split(r"^\[\[profiles\]\][ \t]*$", text, flags=re.M)[1:]
    for b in blocks:
        u = re.search(r'^uid[ \t]*=[ \t]*"([^"]+)"', b, flags=re.M)
        if u: profiles.append(u.group(1))
    # [device-settings."uid"] 段内的通道
    for sm in re.finditer(r'^\[device-settings\.[ \t]*"?([0-9a-fA-F]{64})"?\][ \t]*$(.*?)(?=^[ \t]*\[)',
                          text, flags=re.M | re.S):
        chans = re.findall(r'^"?([A-Za-z0-9_]+)"?[ \t]*=[ \t]*\{', sm.group(2), flags=re.M)
        ds[sm.group(1)] = chans

sec("1. daemon 真值设备表（config.toml [devices]，daemon 每次启动重写 = 权威）")
if not devices:
    ng("读不到 [devices] 段 —— daemon 可能从未成功启动过")
else:
    for u, n in devices.items():
        mark = ""
        if u in ds: mark = f"   ← 已配置通道 {ds[u]}"
        print(f"  {u[:12]}…  {n}{mark}")

sec("2. config.toml 里的 profiles")
info("profiles: " + (", ".join(profiles) if profiles else "（无）"))
for u, chans in ds.items():
    info(f"device-settings {u[:12]}… → {chans or '（空）'}"
         + ("  ✓" if u in devices else "  ✗ 该 UID 不在 [devices] 中（已漂移！）"))

# ★ 真机【已知可用通道】= daemon 写进 [device-settings] 的通道（唯一权威）
known_chans = set()
for u, chans in ds.items():
    if u in devices:
        known_chans.update(chans)
bad_chan = set()

sec("3. modes.json 引用的设备 / 通道 / profile")
if not os.path.isfile(mod_p):
    ng(f"无 {mod_p}")
    sys.exit(1)
with open(mod_p, encoding="utf-8") as f:
    mj = json.load(f)
bad_uid, bad_prof, empty = set(), set(), []
for m in mj.get("modes", []):
    ads = m.get("all_device_settings", {})
    if not ads:
        empty.append(m.get("uid"))
        continue
    for u, chans in ads.items():
        name = devices.get(u)
        if name is None:
            bad_uid.add(u)
            extra = ""
            if not re.fullmatch(r"[0-9a-f]{64}", str(u)):
                extra = "  ← 不像 64 位 hex：modes.json 未渲染（还留着占位符）？"
            print(f"  {m['uid']:<12} 设备 {str(u)[:12]}…  \033[31m✗ 不在 [devices] 中\033[0m{extra}")
        else:
            parts = []
            for c, v in chans.items():
                mark = ""
                if known_chans and c not in known_chans:
                    bad_chan.add(c)
                    mark = " \033[31m✗ 不是真机已知通道\033[0m"
                parts.append(f"{c}→{v.get('profile_uid') or v.get('speed_fixed')}{mark}")
            print(f"  {m['uid']:<12} {name[:24]:<24} " + ", ".join(parts))
        for c, v in chans.items():
            p = v.get("profile_uid")
            if p and p not in profiles:
                bad_prof.add(f"{m['uid']}:{p}")
info(f"current_active_mode = {mj.get('current_active_mode')}")
info(f"真机已知通道（[device-settings]）: "
     + (", ".join(sorted(known_chans)) if known_chans else "（无）"))

sec("4. 结论")
if empty:
    ng(f"这些 Mode 的 all_device_settings 是空的（UI 必空白）: {', '.join(empty)}")
    print("    → modes.json 未渲染（还带着 @DEVICE_UID@ 占位符）或被 daemon 覆盖；")
    print(f"      重新渲染：sudo ./render-config.py --host hs-afs-r1 --auto --config-dir {cc}")
if bad_uid:
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(u)) for u in bad_uid):
        ng("modes.json 里的设备 UID 不是合法 64 位 hex ⇒ 这份文件【没有渲染过】")
        print("    → 模板直接拷过去用了。渲染：")
        print(f"      sudo ./render-config.py --host hs-afs-r1 --auto --config-dir {cc}")
    else:
        ng(f"{len(bad_uid)} 个设备 UID 与 daemon 真值对不上 ⇒ UI 里 Mode 内容必然空白")
        print("    ★ 这就是「看不到设备/通道/设置」的根因之一：UID 漂移")
        print("    修复（按设备名重新对齐 UID）：")
        print(f"      sudo ./render-config.py --host hs-afs-r1 --auto --config-dir {cc}")
elif not empty:
    ok("modes.json 引用的设备 UID 全部命中 daemon 真值")
if bad_chan:
    ng(f"引用了真机上不存在的通道: {', '.join(sorted(bad_chan))}")
    print("    ★★ 这是「看不到设备/通道/设置」最常见的真因：")
    print("       CoolerControl 的通道名是 fanN（hwmon 设备），★ 不是 sysfs 里的 pwmN。")
    print(f"       真机已知通道: {', '.join(sorted(known_chans)) or '（无）'}")
    print("    修复（自动采用真机通道名）：")
    print(f"      sudo ./render-config.py --host hs-afs-r1 --auto --config-dir {cc}")
elif known_chans and not empty:
    ok("引用的通道名都在真机已知通道内")
if bad_prof:
    ng(f"引用了不存在的 profile: {', '.join(sorted(bad_prof))}")
elif not empty:
    ok("所有 profile_uid 都在 config.toml 的 profiles 中")
if not empty and not bad_uid and not bad_prof and not bad_chan:
    print("  → 内容层面无问题。若 UI 仍空白，检查：")
    print("    ① daemon 读的是不是这个目录（docker 挂载路径 / XDG 路径）")
    print("    ② 改完要重启 daemon：sudo systemctl restart coolercontrold")
    print("    ③ UI 硬刷新（Ctrl+F5），前端会缓存设备表")
    print()
    print("  ★ 若问题是『屏幕切档 / CC 切模式不同步』（而不是 UI 空白），")
    print("    那是【档位 ↔ Mode 绑定】问题，跑这个：")
    print("      sudo ./apply-mode.sh --scan        # 看每档绑定到谁")
    print("      sudo ./apply-mode.sh --bind 5=<uid 或 Mode 名字>")
PY
    echo
    exit 0
fi

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

# ── 5. 内核侧 hwmon：真实存在哪些风扇通道 ────────────────────────────────────
# ★★ 关键：CoolerControl 的【通道名】是 fanN，不是 sysfs 的 pwmN。
#    写错通道名与 UID 漂移症状一样 —— UI 里 Mode 的「设备/通道/设置」全空白。
#    唯一权威来源是 config.toml 的 [device-settings] 段（daemon 只写它认识的通道），
#    本节的 sysfs 列表只用来交叉验证。
SEC "5. 内核 hwmon 风扇通道（sysfs 视角：pwmN / fanN_input）"
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

# ★ 双风扇候选：★ CoolerControl 通道名 = fanN，与【sysfs 的 pwmN 编号不一定一致】。
#   可靠做法：从【该设备下真实存在的 pwmN】推出 pwm 编号集合，再交叉 tach(fanN_input)。
#   ★★ 绝不能用「路数」去猜名字（如 4 路 ⇒ fan1…fan4）——本机 pwm 是 2,3,4,5，
#      却只有 fan2/fan3/fan4 有 tach，凑出来的 fan5 根本不存在（真机踩过）。
#   最终以 §5.1（config.toml 的 [device-settings]）为权威。
for h in /sys/class/hwmon/hwmon*; do
    [ -d "$h" ] || continue
    [ "$(cat "$h/name" 2>/dev/null)" = "it8613" ] || continue
    PWMS="$(ls "$h" 2>/dev/null | grep -E '^pwm[0-9]+$' | sed 's/^pwm//' | sort -n)"
    FANS="$(ls "$h" 2>/dev/null | grep -E '^fan[0-9]+_input$' | sed 's/^fan//;s/_input$//' | sort -n)"
    N_PWM="$(printf '%s\n' "$PWMS" | grep -c .)"
    N_FAN="$(printf '%s\n' "$FANS" | grep -c .)"
    if [ "$N_PWM" -ge 1 ]; then
        # 只按【真实编号】列出，不做「路数→fan1..fanN」的臆测
        INFO "it8613 真实 pwm 编号: [$(printf '%s' "$PWMS" | tr '\n' ' ')]（共 $N_PWM 路）"
        [ "$N_FAN" -gt 0 ] && \
            INFO "it8613 真实 tach(fanN_input) 编号: [$(printf '%s' "$FANS" | tr '\n' ' ')]（共 $N_FAN 路）"
        # 有 tach 的通道才是「接了风扇」的强候选（pwm 有但无 tach 多为空路）
        if [ "$N_FAN" -ge 2 ]; then
            INFO "★ 有测速反馈的候选通道: $(printf '%s' "$FANS" | sed 's/^/fan/;s/$/ /g' | tr -d '\n')"
            INFO "  ⚠ 哪个是 CPU、哪个是机箱【无法从 sysfs 判断】（接线决定）"
        elif [ "$N_PWM" = "1" ]; then
            INFO "只 1 路 PWM → 单风扇，渲染时加 --single-channel"
        fi
        INFO "  ★ 通道名的【唯一权威】是 §5.1（config.toml [device-settings]），"
        INFO "    用 render-config.py --auto 自动取真值，不要按本节的编号手写。"
    fi
done

# ── 5.1 ★ daemon 实际写入的通道名（权威）───────────────────────────────────
SEC "5.1 daemon 实际写入的通道名（[device-settings]，★ 权威来源）"
if [ -n "$CC_CONF_DIR" ] && [ -f "$CC_CONF_DIR/config.toml" ]; then
    awk '/^\[device-settings/{f=1;print "    "$0;next} /^\[/{f=0} f&&NF{print "      "$0}' \
        "$CC_CONF_DIR/config.toml"
    awk '/^\[settings\.[0-9a-f]+\.channel_settings\]/{f=1;print "    "$0;next} /^\[/{f=0} f&&NF{print "      "$0}' \
        "$CC_CONF_DIR/config.toml"
    INFO "⇒ 渲染时用 --auto-channels（或 --auto）即可自动采用这里的通道名，"
    INFO "  并按通道当前挂的 profile 名推断角色（…cpu…→CPU 风扇、…case…→机箱风扇）。"
else
    NG "无 config.toml，无法给出权威通道名（先让 daemon 成功启动一次）"
fi

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
echo "  cpu_channel       : <见 §5.1（[device-settings] 里 daemon 实际写的 fanN）>"
echo "  case_channel      : <见 §5.1（同上；单风扇留空/加 --single-channel）>"
echo "    ★ 通道名不要按 §5 的 pwmN/fanN 编号手写（编号≠通道名，且无法从 sysfs 判角色）"
echo "  cpu_temp_name     : temp1   ← ★ CoolerControl 的 temp_name 是【传感器名】，"
echo "                               对应 hwmon 的 temp1_input，不是设备显示名"
echo "  cpu_temp_dev_uid  : ${CPU_TEMP_DEVICE_UID:-<Web UI 中该传感器所在设备 UID>}"
echo "  case_temp_name    : ${CASE_TEMP_NAME:-temp1（机箱/GPU 温度源；缺省则走兜底）}"
echo "  case_temp_dev_uid : ${CASE_TEMP_DEVICE_UID:-<同上>}"
echo
echo "  ★ 最省事：sudo ./render-config.py --host hs-afs-r1 --auto --config-dir $CC_CONF_DIR"
echo "    （--auto = --auto-uid + --auto-channels，UID 与通道名全部按真机真值对齐）"
echo "  ★ 另：config.toml 里若残留 [device-settings.\"@DEVICE_UID@\"] 占位符段，"
echo "    说明渲染不干净 —— 用 --auto 重渲（apply-mode.sh 落地时也会自动清掉它）。"

if [ "$SHORT" = "1" ]; then
    echo
    # ★ 不再手工拼 --cpu-channel：sysfs 的 pwmN ≠ CoolerControl 的通道名 fanN，
    #   手抄极易写错（写错 = UI 里 Mode 内容空白）。统一交给 --auto 去真机取。
    echo "sudo ./render-config.py --host hs-afs-r1 --auto \\"
    echo "     --config-dir ${CC_CONF_DIR:-/etc/coolercontrol}"
    echo "#  --auto = --auto-uid + --auto-channels：UID 与通道名全部按真机真值对齐"
fi

echo
echo "提示：最省事的方式是先在 CoolerControl Web UI（https://<NAS>:11987）里"
echo "      随便给风扇设一次转速，让 daemon 把 [device-settings] 段写出来，"
echo "      本脚本 §3 / §5.1 即可直接读出真实 device_uid 与【通道名】(fanN)。"
exit 0

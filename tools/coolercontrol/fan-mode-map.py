#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fan-mode-map.py —— 【档位 ↔ CoolerControl Mode】映射解析器
=============================================================================
为什么需要这个东西
-----------------------------------------------------------------------------
小彩屏上的四档（智能/低速/标准/强劲）要落到 CoolerControl，就必须指名道姓地
激活某一个 Mode。最初的实现把 Mode 的 uid 写死成 `hs-smart` / `hs-low` /
`hs-standard` / `hs-high`，问题来了：

  ★ 用户在 CoolerControl Web UI 里【自己新建】的 Mode，uid 是随机 UUID
    （1a700658-89d7-4d03-…），不是 hs-smart。写死的 uid 立刻失效 ⇒
    屏幕切档 → apply-mode.sh 找不到目标 Mode → 切换失败 →
    「屏幕和 CoolerControl 不同步」。

本模块把「档位 → 哪个 Mode」这件事【外置成可配置的映射】，并且即使不配置，
也能按 Mode 名字自动认领，做到「用户随便建、随便改名，屏幕照样能切」。

映射来源（优先级从高到低）—— ★ 规则实现在 fan-tier.py（唯一权威），本文件只做包装
-----------------------------------------------------------------------------
  1) 映射文件 / 环境变量里的【显式绑定】（值可以是 uid，也可以是 Mode 名字）
  2) 默认 uid：hs-smart / hs-low / hs-standard / hs-high（渲染器产出的那套）
  3) 名字前缀轮：  "hs 智能…" / "hs 低速…" / "hs 标准…" / "hs 强劲…"
  4) 名字关键词轮：智能|smart / 低速|low / 标准|standard / 强劲|high|性能

反向（CoolerControl → 屏幕）同理，用于让屏幕回显跟上 UI 里的手动切换。

用法
-----------------------------------------------------------------------------
    ./fan-mode-map.py --scan                    # 列出所有 Mode + 每档当前绑定到谁
    ./fan-mode-map.py --bind 5=<uid|名字> 2=…   # 显式绑定（写进映射文件）
    ./fan-mode-map.py --resolve 5               # 输出该档位应激活的 Mode uid
    ./fan-mode-map.py --reverse                 # 输出当前激活 Mode 对应的档位号
    ./fan-mode-map.py --explain                 # ★ 四档完整解析说明（JSON，含依据）
    ./fan-mode-map.py --path                    # 打印映射文件实际路径

★ 绑定值可以是 uid，也可以是 Mode 名字；两种写法由 fan-tier._binding_lookup
  统一收口，正反向共用（不存在"正向认 A、反向认 B"的漂移）。
★ --bind 只接受 5/2/3/4（无 1 档）；绑定指向不存在的 Mode 时会明确告警。

路径可用环境变量覆盖：
    CC_MODE_CONFIG    modes.json 路径（默认 /etc/coolercontrol/modes.json）
    CC_FAN_MODE_MAP   映射文件路径（默认与 modes.json 同目录）
"""

import json
import os
import sys

# ★★ 规则唯一来源：同目录的 fan-tier.py（文件名含连字符，须用 importlib 加载）。
#   本文件只负责「读文件 + 命令行包装 + 渲染诊断」，不再自己实现一套解析规则
#   （历史上这里与中间件各写一份、且匹配顺序不一致，会导致同一台机器上
#    「屏幕显示 A、CC 实际 B」）。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import importlib.util as _ilu   # noqa: E402

_spec = _ilu.spec_from_file_location("fan_tier", os.path.join(_HERE, "fan-tier.py"))
FT = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(FT)

FAN_MODES = FT.FAN_MODES
FAN_MODE_ORDER = FT.FAN_MODE_ORDER
DEFAULT_UIDS = FT.DEFAULT_UIDS
NAME_PREFIX = FT.NAME_PREFIX
NAME_KEYS = FT.NAME_KEYS
MODES_DEFAULT = FT.MODES_DEFAULT
MAP_FILENAME = FT.MAP_FILENAME
TIER_EXPECT = FT.TIER_EXPECT
content_mismatch = FT.content_mismatch



def modes_path():
    return os.environ.get("CC_MODE_CONFIG", "").strip() or MODES_DEFAULT


def map_path():
    """映射文件路径：环境变量 → 与 modes.json 同目录 → 状态目录兜底"""
    p = os.environ.get("CC_FAN_MODE_MAP", "").strip()
    if p:
        return p
    return os.path.join(os.path.dirname(modes_path()) or "/etc/coolercontrol",
                        MAP_FILENAME)


def load_modes(path=None):
    """读 modes.json，返回 dict；失败返回 {}"""
    path = path or modes_path()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def load_map(path=None):
    """读映射文件。键统一成 str(fan_mode)，值原样保留（uid 或名字）。"""
    path = path or map_path()
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        try:
            out[str(int(k))] = str(v).strip()
        except (TypeError, ValueError):
            continue
    return out


def save_map(mapping, path=None):
    path = path or map_path()
    d = os.path.dirname(path)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in mapping.items()},
                  f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path


def _index_by_uid(modes):
    return FT.index_by_uid(modes)


def _norm(s):
    return FT.norm_name(s)


def resolve(mode, modes=None, mapping=None):
    """档位 → Mode uid。★ 规则全部委托 fan-tier（唯一权威）。找不到返回 None。"""
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return None
    modes = modes if modes is not None else load_modes()
    # mapping 的键形态由 fan-tier.normalize_binding 统一收口（str/int 均可）
    return FT.resolve(mode, modes, binding=mapping)


def resolve_reason(mode, modes=None, mapping=None):
    """档位 → (uid, reason)。reason ∈ bind/default/name/none，供诊断。"""
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return None, "none"
    modes = modes if modes is not None else load_modes()
    return FT.resolve_tier(mode, modes, binding=mapping)


def reverse(uid, modes=None, mapping=None):
    """Mode uid → 档位。★ 规则全部委托 fan-tier。识别不出返回 None。"""
    if not uid:
        return None
    modes = modes if modes is not None else load_modes()
    return FT.reverse(uid, modes, binding=mapping)


def bad_uids(modes):
    """返回 modes.json 里所有不像 64 位 hex 的设备键（未渲染的占位符残留）"""
    bad = []
    for m in modes.get("modes", []):
        for u in (m.get("all_device_settings") or {}):
            if not FT.UID_RE.fullmatch(str(u)):
                bad.append((m.get("uid"), str(u)))
    return bad


# ── CLI ─────────────────────────────────────────────────────────────────────
_REASON_LABEL = {"bind": "显式绑定", "default": "默认 uid",
                 "name": "名字匹配", "none": "未匹配"}


def cmd_scan():
    modes = load_modes()
    mp = load_map()
    p = modes_path()
    print(f"modes.json : {p}")
    print(f"映射文件   : {map_path()}"
          + ("（已配置）" if mp else "（未配置 → 走自动匹配）"))
    if not modes:
        print("  ⚠ 读不到 modes.json，无法扫描")
        return 1
    bad = bad_uids(modes)
    print("\n可用 Mode：")
    cur = modes.get("current_active_mode")
    for m in modes.get("modes", []):
        mark = " ← 当前激活" if m.get("uid") == cur else ""
        chans = ", ".join(
            f"{c}→{(v or {}).get('profile_uid') or (v or {}).get('speed_fixed')}"
            for u, ch in (m.get("all_device_settings") or {}).items()
            for c, v in ch.items())
        print(f"  {str(m.get('uid'))[:36]:<38} {m.get('name','')[:28]:<30} "
              f"{chans[:40]}{mark}")

    # ★ 解析说明：直接调 fan-tier.explain()（与实际落地同一条代码路径）
    trace = {e["tier"]: e for e in FT.explain(modes, binding=mp)}
    by_uid2 = _index_by_uid(modes)
    print("\n档位绑定解析：")
    rc = 0
    mismatched = []
    broken_binds = []
    for m_ in FAN_MODE_ORDER:
        e = trace[m_]
        u = e["uid"]
        src = _REASON_LABEL.get(e["reason"], e["reason"])
        if e["bind_value"] and not e["bind_ok"]:
            # 绑定了但解析不到 → 明确告警（此前只是静默回退）
            broken_binds.append((m_, e["bind_value"]))
            src += " \033[33m（绑定失效）\033[0m"
        if u:
            mm = by_uid2.get(u) or {}
            nm = mm.get("name", "?")
            desc, badc = content_mismatch(mm, m_)
            flag = ""
            if badc:
                flag = ("  \033[31m⚠ 内容与档位不符（应为 "
                        + "/".join(sorted(TIER_EXPECT.get(m_, ()))) + "）\033[0m")
                mismatched.append((m_, nm, desc))
                rc = 1
            print(f"  {m_} {FAN_MODES[m_]:<4} → {str(u)[:36]:<38} {str(nm)[:24]:<26} "
                  f"[{src}]  内容={desc}{flag}")
        else:
            print(f"  {m_} {FAN_MODES[m_]:<4} → \033[31m未匹配\033[0m"
                  f"  ← 用 --bind 绑定，或把 Mode 名字改成含「{FAN_MODES[m_]}」")
            rc = 1
    if broken_binds:
        rc = 1
        print("\n\033[33m⚠ 有绑定指向了当前不存在的 Mode（已回退到自动匹配）\033[0m：")
        for m_, bv in broken_binds:
            print(f"    档位 {m_}（{FAN_MODES[m_]}）绑的 {bv!r} 在 modes.json 里找不到"
                  "（Mode 被删/改名了？）")
        print("    修：重新绑定，或解除绑定回到自动认领——")
        for m_, _ in broken_binds:
            print(f"      sudo ./apply-mode.sh --bind {m_}=<新 uid 或 Mode 名字>")
            print(f"      sudo ./apply-mode.sh --bind {m_}=          # 解除")
    if mismatched:
        print("\n\033[31m✗ 档位名字与内容不符\033[0m —— 屏幕切到这一档会跑成别的转速"
              "（例如「低速」实际 60%）：")
        for m_, nm, desc in mismatched:
            print(f"    档位 {m_}（{FAN_MODES[m_]}）绑定的 Mode「{nm}」里面是 {desc}")
        print("    修（一条命令，保留你在 UI 里的 Mode，只纠正内容）：")
        print("      sudo ./render-config.py --host <机型> --auto \\")
        print(f"           --merge-modes {p} --rename-modes")
    if bad:
        rc = 1
        print(f"\n\033[31m✗ 发现 {len(bad)} 处非法设备键（占位符残留？）\033[0m")
        for u, k in bad[:6]:
            print(f"    Mode {str(u)[:12]}… 键 {k!r}")
        print("    这些键会让 daemon 无法识别该 Mode ⇒ 用 render-config.py "
              "--merge-modes 重新渲染")
    return rc


def cmd_bind(pairs):
    mp = load_map()
    modes = load_modes()
    rc = 0
    for pair in pairs:
        if "=" not in pair:
            print(f"忽略非法参数 {pair!r}（应为 档位=uid 或 档位=名字）", file=sys.stderr)
            rc = 2
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        # ★ 档位必须是四档之一（此前 int() 能过就收 ⇒ --bind 1=x 会存进去）
        try:
            kt = int(k)
        except ValueError:
            print(f"忽略非法档位 {k!r}（只接受 5/2/3/4）", file=sys.stderr)
            rc = 2
            continue
        if kt not in FAN_MODE_ORDER:
            print(f"忽略非法档位 {k!r}（只接受 "
                  f"{'/'.join(str(t) for t in FAN_MODE_ORDER)}；★ 无 1 档）",
                  file=sys.stderr)
            rc = 2
            continue
        k = str(kt)
        if not v:
            mp.pop(k, None)
            print(f"已解除档位 {k} 的绑定")
            continue
        # ★ 绑定值统一走 fan-tier 的 _binding_lookup（uid / 名字同一套规则）：
        #   能立刻解析成 uid 就存 uid（最稳）；解析不到就把用户原样输入存起来
        #   （Mode 可能稍后才建），并明确告警 —— 不静默存一个"看着像成功"的值。
        uid, how = FT._binding_lookup(v, modes)
        if uid:
            mp[k] = uid
            note = "" if how == "uid" else "（按名字解析）"
            print(f"档位 {k}（{FAN_MODES.get(kt, '?')}）→ {uid} {note}")
        else:
            mp[k] = v
            print(f"档位 {k}（{FAN_MODES.get(kt, '?')}）→ {v}"
                  "  \033[33m⚠ 当前 modes.json 里找不到它，已原样保存；"
                  "Mode 建好后需复查 --scan\033[0m")
            rc = 1
    p = save_map(mp)
    print(f"已写入 {p}")
    return rc


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    a = argv[0]
    if a == "--path":
        print(map_path())
        return 0
    if a == "--scan":
        return cmd_scan()
    if a == "--bind":
        return cmd_bind(argv[1:])
    if a == "--resolve":
        if len(argv) < 2:
            print("用法：--resolve <5|2|3|4>", file=sys.stderr)
            return 2
        mp = load_map()
        u, reason = resolve_reason(argv[1], mapping=mp)
        if not u:
            bv = mp.get(str(argv[1])) or mp.get(argv[1]) or ""
            print(f"档位 {argv[1]} 匹配不到任何 Mode"
                  + (f"（绑定值 {bv!r} 解析不到 Mode，且名字也认领不了）" if bv else
                     "（名字里没有档位关键词，也没做 --bind）"), file=sys.stderr)
            return 1
        print(u)
        return 0
    if a == "--explain":
        modes = load_modes()
        print(json.dumps(FT.explain(modes, binding=load_map()),
                         ensure_ascii=False, indent=2))
        return 0
    if a == "--reverse":
        modes = load_modes()
        cur = modes.get("current_active_mode")
        m_ = reverse(cur, modes)
        if m_ is None:
            print(f"当前激活的 Mode {str(cur)[:12]}… 识别不出对应档位"
                  "（名字里没有 智能/低速/标准/强劲，也没做 --bind）", file=sys.stderr)
            return 1
        print(m_)
        return 0
    print(f"未知子命令 {a!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

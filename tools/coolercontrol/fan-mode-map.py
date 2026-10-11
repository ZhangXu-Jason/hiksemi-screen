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

映射来源（优先级从高到低）
-----------------------------------------------------------------------------
  1) 映射文件 / 环境变量里的【显式绑定】（值可以是 uid，也可以是 Mode 名字）
  2) 默认 uid：hs-smart / hs-low / hs-standard / hs-high（渲染器产出的那套）
  3) 名字前缀：  "hs 智能…" / "hs 低速…" / "hs 标准…" / "hs 强劲…"
  4) 名字包含关键词：智能|smart / 低速|low / 标准|standard / 强劲|high|性能

反向（CoolerControl → 屏幕）同理，用于让屏幕回显跟上 UI 里的手动切换。

用法
-----------------------------------------------------------------------------
    ./fan-mode-map.py --scan                    # 列出所有 Mode + 每档当前绑定到谁
    ./fan-mode-map.py --bind 5=<uid|名字> 2=…   # 显式绑定（写进映射文件）
    ./fan-mode-map.py --resolve 5               # 输出该档位应激活的 Mode uid
    ./fan-mode-map.py --reverse                 # 输出当前激活 Mode 对应的档位号
    ./fan-mode-map.py --path                    # 打印映射文件实际路径

路径可用环境变量覆盖：
    CC_MODE_CONFIG    modes.json 路径（默认 /etc/coolercontrol/modes.json）
    CC_FAN_MODE_MAP   映射文件路径（默认与 modes.json 同目录）
"""

import json
import os
import re
import sys

# ── 四档常量（与中间件 FAN_MODES 保持一致）──────────────────────────────────
FAN_MODES = {5: "智能", 2: "低速", 3: "标准", 4: "强劲"}
FAN_MODE_ORDER = (5, 2, 3, 4)

# ① 渲染器产出的默认 uid
DEFAULT_UIDS = {5: "hs-smart", 2: "hs-low", 3: "hs-standard", 4: "hs-high"}
# ② 旧版名字前缀（用户在 UI 里照抄我们模板名建的 Mode）
NAME_PREFIX = {5: "hs 智能", 2: "hs 低速", 3: "hs 标准", 4: "hs 强劲"}
# ③ 名字关键词兜底（用户起了自己的名字，比如「静音」「性能」）
NAME_KEYS = {
    5: ("智能", "smart", "auto", "自动"),
    2: ("低速", "low", "静音", "quiet", "silent"),
    3: ("标准", "standard", "normal", "medium", "常规"),
    4: ("强劲", "high", "full", "性能", "max", "turbo"),
}

MODES_DEFAULT = "/etc/coolercontrol/modes.json"
MAP_FILENAME = "hs-fan-mode-map.json"

UID_RE = re.compile(r"[0-9a-f]{64}")


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
    return {m.get("uid"): m for m in modes.get("modes", [])
            if isinstance(m, dict)}


def _norm(s):
    return re.sub(r"\s+", "", str(s or "")).lower()


def _match_by_name(modes, mode, exact=None):
    """按 Mode 名字认领档位。exact 非空时先做精确匹配。"""
    lst = modes.get("modes", [])
    if exact:
        e = _norm(exact)
        for m in lst:
            if _norm(m.get("name")) == e:
                return m.get("uid")
        for m in lst:                      # 退一步：包含
            if e and e in _norm(m.get("name")):
                return m.get("uid")
    # 前缀（"hs 智能…"）
    pref = _norm(NAME_PREFIX.get(mode, ""))
    if pref:
        for m in lst:
            if _norm(m.get("name")).startswith(pref):
                return m.get("uid")
    # 关键词
    for key in NAME_KEYS.get(mode, ()):
        for m in lst:
            n = _norm(m.get("name"))
            if _norm(key) and _norm(key) in n:
                return m.get("uid")
    return None


def resolve(mode, modes=None, mapping=None):
    """档位 → Mode uid。找不到返回 None。"""
    try:
        mode = int(mode)
    except (TypeError, ValueError):
        return None
    modes = modes if modes is not None else load_modes()
    mapping = mapping if mapping is not None else load_map()
    by_uid = _index_by_uid(modes)

    # ① 显式绑定（值可能是 uid，也可能是名字）
    want = mapping.get(str(mode), "").strip()
    if want:
        if want in by_uid:
            return want
        hit = _match_by_name(modes, mode, exact=want)
        if hit:
            return hit
        print(f"[fan-map] ⚠ 档位 {mode} 绑定的 {want!r} 在当前 modes.json 里找不到，"
              "回退自动匹配", file=sys.stderr)

    # ② 默认 uid
    d = DEFAULT_UIDS.get(mode)
    if d and d in by_uid:
        return d

    # ③④ 名字认领
    return _match_by_name(modes, mode)


def reverse(uid, modes=None, mapping=None):
    """Mode uid → 档位。识别不出返回 None。"""
    if not uid:
        return None
    modes = modes if modes is not None else load_modes()
    mapping = mapping if mapping is not None else load_map()
    by_uid = _index_by_uid(modes)

    # ① 显式绑定里直接指向这个 uid 的
    for k, v in mapping.items():
        try:
            k = int(k)
        except (TypeError, ValueError):
            continue
        if v == uid:
            return k
        if v and v not in by_uid and _match_by_name(modes, k, exact=v) == uid:
            return k

    # ② 默认 uid
    for m_, u in DEFAULT_UIDS.items():
        if u == uid:
            return m_

    # ③ 名字
    m = by_uid.get(uid)
    name = _norm((m or {}).get("name"))
    if not name:
        return None
    for m_ in FAN_MODE_ORDER:
        if _norm(NAME_PREFIX.get(m_, "")) and name.startswith(_norm(NAME_PREFIX[m_])):
            return m_
    for m_ in FAN_MODE_ORDER:
        for key in NAME_KEYS.get(m_, ()):
            if _norm(key) and _norm(key) in name:
                return m_
    return None


def bad_uids(modes):
    """返回 modes.json 里所有不像 64 位 hex 的设备键（未渲染的占位符残留）"""
    bad = []
    for m in modes.get("modes", []):
        for u in (m.get("all_device_settings") or {}):
            if not UID_RE.fullmatch(str(u)):
                bad.append((m.get("uid"), str(u)))
    return bad


# ── CLI ─────────────────────────────────────────────────────────────────────
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
    print("\n档位绑定解析：")
    rc = 0
    for m_ in FAN_MODE_ORDER:
        u = resolve(m_, modes, mp)
        src = "显式绑定" if mp.get(str(m_)) else ("默认 uid" if u == DEFAULT_UIDS.get(m_) else "名字匹配")
        if u:
            nm = (_index_by_uid(modes).get(u) or {}).get("name", "?")
            print(f"  {m_} {FAN_MODES[m_]:<4} → {u[:36]:<38} {nm[:24]:<26} [{src}]")
        else:
            print(f"  {m_} {FAN_MODES[m_]:<4} → \033[31m未匹配\033[0m"
                  f"  ← 用 --bind 绑定，或把 Mode 名字改成含「{FAN_MODES[m_]}」")
            rc = 1
    if bad:
        rc = 1
        print(f"\n\033[31m✗ 发现 {len(bad)} 处非法设备键（占位符残留？）\033[0m")
        for u, k in bad[:6]:
            print(f"    Mode {str(u)[:12]}… 键 {k!r}")
        print("    这些键会让 daemon 无法识别该 Mode ⇒ 重新渲染或手工删掉这些键")
    return rc


def cmd_bind(pairs):
    mp = load_map()
    modes = load_modes()
    by_uid = _index_by_uid(modes)
    for pair in pairs:
        if "=" not in pair:
            print(f"忽略非法参数 {pair!r}（应为 档位=uid 或 档位=名字）", file=sys.stderr)
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        try:
            k = str(int(k))
        except ValueError:
            print(f"忽略非法档位 {k!r}（只接受 5/2/3/4）", file=sys.stderr)
            continue
        if not v:
            mp.pop(k, None)
            print(f"已解除档位 {k} 的绑定")
            continue
        # 允许用名字绑定：能解析成 uid 就存 uid（更稳）
        hit = v if v in by_uid else _match_by_name(modes, int(k), exact=v)
        mp[k] = hit or v
        print(f"档位 {k}（{FAN_MODES.get(int(k), '?')}）→ {mp[k]}"
              + ("" if hit else "  ⚠ 当前 modes.json 里没有它，保存但暂时不可用"))
    p = save_map(mp)
    print(f"已写入 {p}")
    return 0


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
        u = resolve(argv[1])
        if not u:
            print(f"档位 {argv[1]} 匹配不到任何 Mode", file=sys.stderr)
            return 1
        print(u)
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

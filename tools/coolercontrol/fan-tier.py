#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fan-tier.py —— 风扇档位 ↔ CoolerControl Mode 的【唯一】权威规则源。

★ 为什么单独一个文件：
    以前这套规则被复制了 4 份（中间件 / fan-mode-map / apply-mode.sh 内嵌
    heredoc / render-config），且**规则并不一致**（前缀与关键词的匹配顺序不同）
    ⇒ 同一台机器上可能得出不同档位归属，表现为"屏幕显示 A、CC 实际跑 B"。
    现在所有调用方都 import 本模块，规则只此一份。

本模块**纯标准库、无副作用、可被 import 也可当命令行用**：
    python3 fan-tier.py --uids                 # 打印默认 uid 表（JSON）
    python3 fan-tier.py --keys                 # 打印名字关键词表（JSON）
    python3 fan-tier.py --tier 2               # 档位 → 默认 uid
    python3 fan-tier.py --self-test            # 自检（供测试调用）

规则（★ 唯一权威，改动只改这里）：
    解析优先级（正向：档位 → Mode uid）
      ① 显式绑定（映射文件里的值，可以是 uid，也可以是 Mode 名字）
      ② 默认 uid（本模块 DEFAULT_UIDS，即渲染器产出的 hs-*）
      ③ 名字认领：先【整表前缀】轮，再【整表关键词】轮
    反向解析（Mode uid → 档位）同规则，只是方向相反。
"""

import json
import os
import re
import sys

# ── 四档定义（与中间件 FAN_MODES 一致）──────────────────────────────────────
FAN_MODES = {5: "智能", 2: "低速", 3: "标准", 4: "强劲"}
FAN_MODE_ORDER = (5, 2, 3, 4)          # 解析优先级顺序（★ 5 在前）

# ── ① 渲染器产出的默认 uid ─────────────────────────────────────────────────
DEFAULT_UIDS = {5: "hs-smart", 2: "hs-low", 3: "hs-standard", 4: "hs-high"}

# ── ② Mode 名字前缀（用户在 UI 里照抄我们模板名建的 Mode）─────────────────
NAME_PREFIX = {5: "hs 智能", 2: "hs 低速", 3: "hs 标准", 4: "hs 强劲"}

# ── ③ Mode 名字关键词兜底（用户起了自己的名字，比如「静音」「性能」）──────
#   ★ 匹配顺序 = 先前缀轮（NAME_PREFIX），再关键词轮（NAME_KEYS），
#     两轮都是「按 FAN_MODE_ORDER 遍历档位」，保证结果确定且可预期。
NAME_KEYS = {
    5: ("智能", "smart", "auto", "自动"),
    2: ("低速", "low", "静音", "quiet", "silent"),
    3: ("标准", "standard", "normal", "medium", "常规"),
    4: ("强劲", "high", "full", "性能", "max", "turbo"),
}

MODES_DEFAULT = "/etc/coolercontrol/modes.json"
MAP_FILENAME = "hs-fan-mode-map.json"

# ★ 每一档「正确的内容」长什么样（用于发现"名字与内容不符"）
TIER_EXPECT = {
    5: {"hs-smart", "hs-cpu-graph", "hs-case-graph"},
    2: {"hs-low"},
    3: {"hs-standard"},
    4: {"hs-high"},
}

UID_RE = re.compile(r"[0-9a-f]{64}")


# ── 工具 ────────────────────────────────────────────────────────────────────
def norm_name(s):
    """名字归一化：去所有空白 + 转小写（比中文大小写无关，英文关键词需小写）。"""
    return re.sub(r"\s+", "", str(s or "")).lower()


def mode_map_path(modes_path):
    """映射文件路径：环境变量 CC_FAN_MODE_MAP → 与 modes.json 同目录。"""
    p = os.environ.get("CC_FAN_MODE_MAP", "").strip()
    if p:
        return p
    return os.path.join(os.path.dirname(modes_path) or "/etc/coolercontrol",
                        MAP_FILENAME)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def index_by_uid(modes_obj):
    """{uid: mode} —— 传 modes.json 的整个对象或 modes 数组都行。"""
    arr = modes_obj
    if isinstance(modes_obj, dict):
        arr = modes_obj.get("modes") or []
    out = {}
    for m in arr or []:
        if isinstance(m, dict) and m.get("uid"):
            out[m["uid"]] = m
    return out


def load_binding(modes_path):
    """读映射文件 → {档位(int): 绑定值(str)}；文件不存在返回 {}。"""
    raw = load_json(mode_map_path(modes_path))
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                out[int(k)] = str(v).strip()
            except (TypeError, ValueError):
                continue
    return out


# ── 名字认领（唯一实现）────────────────────────────────────────────────────
def tier_by_name(name, claimed=()):
    """按 Mode 名字认领档位。返回档位或 None。

    claimed = 已被更高优先级占用的档位集合（避免两档抢同一个 Mode）。
    ★ 两轮：先整表前缀，再整表关键词；每轮内按 FAN_MODE_ORDER。
    """
    n = norm_name(name)
    if not n:
        return None
    for t in FAN_MODE_ORDER:                     # ① 前缀轮
        if t in claimed:
            continue
        if norm_name(NAME_PREFIX[t]) in n:
            return t
    for t in FAN_MODE_ORDER:                     # ② 关键词轮
        if t in claimed:
            continue
        for key in NAME_KEYS.get(t, ()):
            k = norm_name(key)
            if k and k in n:
                return t
    return None


# ── 正向：档位 → Mode uid ───────────────────────────────────────────────────
def resolve(tier, modes_obj, binding=None):
    """档位 → Mode uid。返回 uid 或 None（不乱猜）。

    优先级：① 显式绑定（值可为 uid 或名字）② 默认 uid ③ 名字认领。
    """
    by_uid = index_by_uid(modes_obj)
    if binding is None:
        mp = modes_path_env()
        binding = load_binding(mp) if mp else {}

    want = (binding or {}).get(tier, "")
    if want:
        if want in by_uid:
            return want
        # 绑定值可能是「Mode 名字」——按名字精确/前缀/关键词认领
        hit = _match_by_name(modes_obj, want)
        if hit:
            return hit

    d = DEFAULT_UIDS.get(tier)
    if d and d in by_uid:
        return d

    # 名字认领：本档未被更高优先级档位占用
    claimed = set()
    for t in FAN_MODE_ORDER:
        if t == tier:
            break
        u = _explicit_or_default(t, by_uid, binding)
        if u:
            claimed.add(t)
    named = {m["uid"]: m for m in _modes_list(modes_obj)}
    for uid, m in named.items():
        if uid in _taken_elsewhere(by_uid, binding, tier):
            continue
        got = tier_by_name(m.get("name"), claimed)
        if got == tier:
            return uid
    return None


def _explicit_or_default(tier, by_uid, binding):
    want = (binding or {}).get(tier, "")
    if want and want in by_uid:
        return want
    d = DEFAULT_UIDS.get(tier)
    return d if d in by_uid else None


def _taken_elsewhere(by_uid, binding, tier):
    """已被其它档位明确占用（显式绑定/默认 uid）的 uid 集合。"""
    out = set()
    for t in FAN_MODE_ORDER:
        if t == tier:
            continue
        want = (binding or {}).get(t, "")
        if want and want in by_uid:
            out.add(want)
        d = DEFAULT_UIDS.get(t)
        if d and d in by_uid:
            out.add(d)
    return out


def _modes_list(modes_obj):
    if isinstance(modes_obj, dict):
        return [m for m in (modes_obj.get("modes") or []) if isinstance(m, dict)]
    return [m for m in (modes_obj or []) if isinstance(m, dict)]


def _match_by_name(modes_obj, want):
    """按名字匹配（精确 → 前缀 → 关键词）。返回 uid 或 None。"""
    w = norm_name(want)
    if not w:
        return None
    named = _modes_list(modes_obj)
    for m in named:                              # 精确
        if norm_name(m.get("name")) == w:
            return m.get("uid")
    for m in named:                              # 前缀
        n = norm_name(m.get("name"))
        if n and (n.startswith(w) or w.startswith(n)):
            return m.get("uid")
    for m in named:                              # 关键词（反向：名字含于 want）
        n = norm_name(m.get("name"))
        if n and n in w:
            return m.get("uid")
    return None


# ── 反向：Mode uid → 档位 ───────────────────────────────────────────────────
def reverse(uid, modes_obj, binding=None):
    """当前激活的 Mode uid → 屏幕档位。返回档位(int) 或 None。

    优先级：① 显式绑定 ② 默认 uid ③ 名字认领（带"已被占用"排除）。
    """
    if not uid:
        return None
    by_uid = index_by_uid(modes_obj)
    if binding is None:
        mp = modes_path_env()
        binding = load_binding(mp) if mp else {}

    for t in FAN_MODE_ORDER:                     # ① 显式绑定
        if (binding or {}).get(t) == uid:
            return t
    for t in FAN_MODE_ORDER:                     # ② 默认 uid
        if DEFAULT_UIDS.get(t) == uid:
            return t
    name = (by_uid.get(uid) or {}).get("name")   # ③ 名字认领
    if name is None:
        return None
    # 已被 ① ② 占用的档位不能再被名字认领（防两档抢一个 Mode）
    claimed = set()
    for t in FAN_MODE_ORDER:
        if _explicit_or_default(t, by_uid, binding):
            claimed.add(t)
    return tier_by_name(name, claimed)


# ── 内容一致性（"名字与内容不符"诊断）──────────────────────────────────────
def mode_profiles(mode):
    """收集一个 Mode 所有 channel 引用的 profile_uid / speed_fixed 描述。"""
    profs, fixed = set(), []
    for dev in (mode.get("all_device_settings") or {}).values():
        if not isinstance(dev, dict):
            continue
        for st in dev.values():
            if not isinstance(st, dict):
                continue
            if st.get("profile_uid"):
                profs.add(str(st["profile_uid"]))
            elif "speed_fixed" in st:
                fixed.append(f"{st['speed_fixed']}%")
    return profs, sorted(set(fixed))


def content_mismatch(mode, tier):
    """判断 Mode 内容是否与档位相符。返回 (描述, 是否不符)。"""
    profs, fixed = mode_profiles(mode)
    shown = []
    if profs:
        shown.extend(sorted(profs))
    if fixed:
        shown.append("Fixed " + "/".join(fixed))
    desc = ", ".join(shown) or "（空）"
    bad = [p for p in profs if p not in TIER_EXPECT.get(tier, set())]
    if not profs and not fixed:
        return desc, True
    if fixed and tier in (2, 3, 4):
        return desc, False          # 直接写死转速也算有效内容
    return desc, bool(bad)


# ── 环境（供中间件 import 时复用）──────────────────────────────────────────
def modes_path_env():
    return os.environ.get("CC_MODE_CONFIG", "").strip() or MODES_DEFAULT


# ── CLI（给 shell 脚本用，避免内嵌 python heredoc 再写一份规则）───────────
def _cli(argv):
    if "--uids" in argv:
        print(json.dumps({str(k): v for k, v in DEFAULT_UIDS.items()},
                         ensure_ascii=False))
        return 0
    if "--keys" in argv:
        print(json.dumps({str(k): list(v) for k, v in NAME_KEYS.items()},
                         ensure_ascii=False))
        return 0
    if "--tier" in argv:
        i = argv.index("--tier")
        try:
            t = int(argv[i + 1])
        except (IndexError, ValueError):
            print("用法: fan-tier.py --tier <5|2|3|4>", file=sys.stderr)
            return 2
        u = DEFAULT_UIDS.get(t)
        if not u:
            print(f"非法档位: {t}", file=sys.stderr)
            return 2
        print(u)
        return 0
    if "--self-test" in argv:
        return _self_test()
    print(__doc__)
    return 0


def _self_test():
    ok = 0
    fail = 0

    def chk(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}")

    # 名字认领：前缀优先于关键词
    chk("前缀「hs 智能」→5", tier_by_name("hs 智能（CPU 曲线 + 机箱曲线）") == 5)
    chk("关键词「静音模式」→2", tier_by_name("静音模式") == 2)
    chk("关键词「Silent」→2", tier_by_name("Silent") == 2)
    chk("关键词「性能」→4", tier_by_name("性能模式") == 4)
    chk("无关键词 → None", tier_by_name("我的自定义模式") is None)
    # 前缀轮先于关键词轮：名字同时含「标准」与「hs 低速」→ 认前缀（2）
    chk("前缀轮优先于关键词轮",
        tier_by_name("hs 低速（其实是标准）") == 2)
    # claimed 排除
    chk("claimed 排除生效",
        tier_by_name("hs 智能", claimed={5}) != 5)
    # 反向
    modes = {"modes": [
        {"uid": "U5", "name": "hs 智能"},
        {"uid": "U2", "name": "hs 低速 25%"},
        {"uid": "UX", "name": "My Silent Cooler"},
    ]}
    chk("reverse 名字「My Silent Cooler」→2",
        reverse("UX", modes) == 2)
    chk("reverse 默认 uid",
        reverse("hs-smart", modes) == 5)
    chk("reverse 绑定优先",
        reverse("UX", modes, binding={4: "UX"}) == 4)
    chk("reverse 认不出 → None",
        reverse("UZ", {"modes": [{"uid": "UZ", "name": "我的模式"}]}) is None)
    # 正向
    chk("正向：显式绑定命中 uid",
        resolve(4, modes, binding={4: "U2"}) == "U2")
    chk("正向：绑定值是名字",
        resolve(4, modes, binding={4: "My Silent Cooler"}) == "UX")
    chk("正向：默认 uid 命中",
        resolve(5, modes, binding={}) == "U5")
    chk("正向：认不出不猜",
        resolve(3, modes, binding={}) is None)
    print(f"  fan-tier 自检：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))

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

★★ 统一解析内核（本次重构）：
    正反向**共用同一个 `resolve_tier()`**（返回 `(uid, reason)`）——反向就是
    "对四档各跑一次正向，谁解析到该 uid 就是谁"。这样彻底消除了早期
    "正向一套 claimed 计算、反向又一套"的重复（那种写法容易正反漂移）。
    绑定值的两种写法（uid / 名字）统一由 `_binding_lookup()` 收口，
    调用方不必再关心映射文件里存的到底是哪种。
    `explain()` 是 resolve_tier 的"带解释"版本，供 --scan / probe 诊断，
    与实际落地走同一条代码路径 ⇒ 诊断结论必然与真实行为一致。
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
    return normalize_binding(load_json(mode_map_path(modes_path)))


def normalize_binding(raw):
    """把「任意形态的绑定表」归一成 {档位(int): 绑定值(str)}。

    ★ 唯一收口：调用方可能传 dict（JSON 里键是字符串 "2"）或已是 int 键的
      表。全部在这里转成 int 键，后续逻辑只认 int —— 彻底消除"字符串键
      查不到 int 键"这类静默失效（曾导致 --bind 写进去却读不出来）。
    非法键/空值丢弃。
    """
    out = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            kt = int(k)
        except (TypeError, ValueError):
            continue
        if kt not in FAN_MODE_ORDER:
            continue
        sv = str(v).strip()
        if sv:
            out[kt] = sv
    return out


# ── 显式绑定：解析一个「绑定值」（uid 或 Mode 名字）到真实 uid ───────────────
def _binding_lookup(want, modes_obj):
    """把绑定值（uid 或 Mode 名字）解析成当前 modes.json 里的真实 uid。

    绑定值**允许两种写法**，本函数统一收口（调用方不必再关心是哪种）：
      ① 直接写 uid（最稳，推荐）—— 在 modes.json 里存在则原样返回；
      ② 写 Mode 名字 —— 精确 → 前缀 → 关键词 三档匹配。
    返回 (uid, how)：uid=None 表示"这个绑定值当前解析不到 Mode"，
    how ∈ {"uid","name","none"} 说明是按哪种方式命中的（供诊断/报错）。
    """
    w = str(want or "").strip()
    if not w:
        return None, "none"
    by_uid = index_by_uid(modes_obj)
    if w in by_uid:
        return w, "uid"
    hit = _match_by_name(modes_obj, w)
    if hit:
        return hit, "name"
    return None, "none"


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


# ── 统一解析：档位 → (uid, 依据) ────────────────────────────────────────────
#   ★★ 全部解析逻辑收敛到这一个函数（resolve 与 reverse 都基于它），
#     彻底消除"两个方向各算一遍 claimed"的重复与漂移。
#   解析依据（reason），供 --scan / 报错给出人话：
#     "bind"     显式绑定命中（uid 或名字）
#     "default"  默认 uid（hs-smart / hs-low / hs-standard / hs-high）
#     "name"     名字认领（前缀/关键词）
#     "none"     认不出（uid=None）
def _claim_map(modes_obj, binding):
    """预计算「档位 → 已由更高优先级占用的 uid」。

    用**各档自己**的显式绑定/默认 uid 算出占用表；与档位无关，
    只依赖 binding + modes.json。正反向共用同一张表 ⇒ 结果必然一致。
    """
    by_uid = index_by_uid(modes_obj)
    claims = {t: None for t in FAN_MODE_ORDER}     # tier -> uid
    for t in FAN_MODE_ORDER:
        b = (binding or {}).get(t, "")
        if b:
            uid, how = _binding_lookup(b, modes_obj)
            if uid:
                claims[t] = uid
                continue
        d = DEFAULT_UIDS.get(t)
        if d and d in by_uid:
            claims[t] = d
    return claims


def resolve_tier(tier, modes_obj, binding=None, claims=None):
    """档位 → (uid, reason)。uid=None 表示认不出（不乱猜）。

    优先级（正反向完全一致）：
      ① 显式绑定（值可为 uid 或名字）② 默认 uid ③ 名字认领。
    ★ claims 可预先算好（批量解析时省重复计算）；也可以传进来复用。
    """
    if tier not in FAN_MODE_ORDER:
        return None, "none"
    by_uid = index_by_uid(modes_obj)
    if binding is None:
        mp = modes_path_env()
        binding = load_binding(mp) if mp else {}
    else:
        binding = normalize_binding(binding)
    if claims is None:
        claims = _claim_map(modes_obj, binding)

    # ① 显式绑定
    b = (binding or {}).get(tier, "")
    if b:
        uid, how = _binding_lookup(b, modes_obj)
        if uid:
            return uid, "bind"

    # ② 默认 uid
    d = DEFAULT_UIDS.get(tier)
    if d and d in by_uid:
        return d, "default"

    # ③ 名字认领：排除「已被其它档位占用」的 uid 与档位
    taken = {u for t, u in claims.items() if t != tier and u}
    claimed_tiers = {t for t in FAN_MODE_ORDER if t != tier and claims.get(t)}
    for uid, m in index_by_uid(modes_obj).items():
        if uid in taken:
            continue
        if tier_by_name(m.get("name"), claimed_tiers) == tier:
            return uid, "name"
    return None, "none"


# ── 正向：档位 → Mode uid ───────────────────────────────────────────────────
def resolve(tier, modes_obj, binding=None):
    """档位 → Mode uid。返回 uid 或 None（不乱猜）。"""
    return resolve_tier(tier, modes_obj, binding=binding)[0]


def _modes_list(modes_obj):
    if isinstance(modes_obj, dict):
        return [m for m in (modes_obj.get("modes") or []) if isinstance(m, dict)]
    return [m for m in (modes_obj or []) if isinstance(m, dict)]


def _match_by_name(modes_obj, want):
    """按名字匹配（精确 → 前缀 → 关键词）。返回 uid 或 None。

    ★ 用于解析「绑定值写的是 Mode 名字」这种情况。命名模糊时不猜：
      前缀匹配要求**较短一方至少 2 个字符**（避免 1 个字的名字命中一切）。
    """
    w = norm_name(want)
    if not w:
        return None
    named = _modes_list(modes_obj)
    for m in named:                              # ① 精确
        if norm_name(m.get("name")) == w:
            return m.get("uid")
    for m in named:                              # ② 前缀（双向，短边 ≥2 字符）
        n = norm_name(m.get("name"))
        if not n:
            continue
        if n.startswith(w) and len(w) >= 2:
            return m.get("uid")
        if w.startswith(n) and len(n) >= 2:
            return m.get("uid")
    for m in named:                              # ③ 关键词（Mode 名字含于绑定值）
        n = norm_name(m.get("name"))
        if n and len(n) >= 2 and n in w:
            return m.get("uid")
    return None


# ── 反向：Mode uid → 档位 ───────────────────────────────────────────────────
def reverse(uid, modes_obj, binding=None, claims=None):
    """当前激活的 Mode uid → 屏幕档位。返回档位(int) 或 None。

    ★ 与正向 resolve_tier 共享同一套规则，但方向是"这个具体 Mode 属于哪一档"：
      ① 显式绑定：哪个档位绑到了这个 uid（值可为 uid 或名字，统一 _binding_lookup）
      ② 默认 uid：该 uid 是否就是某档的 hs-*
      ③ 名字认领：**只看这个 Mode 自己的名字**，用 tier_by_name 判定，
         并排除已被 ① ② 占用的档位。⇒ 多个 Mode 同名争抢同一档时，
         每个 Mode 都能独立反查出该档（不像"谁赢"那样只有一个能反查）。
    共享 _claim_map/_binding_lookup ⇒ 正反向不会漂移（这就是"共享内核"）。
    """
    if not uid:
        return None
    if binding is None:
        mp = modes_path_env()
        binding = load_binding(mp) if mp else {}
    else:
        binding = normalize_binding(binding)
    by_uid = index_by_uid(modes_obj)
    if claims is None:
        claims = _claim_map(modes_obj, binding)

    # ① 显式绑定命中这个 uid（绑定值可能是 uid，也可能是这个名字）
    for t in FAN_MODE_ORDER:
        b = (binding or {}).get(t, "")
        if b and _binding_lookup(b, modes_obj)[0] == uid:
            return t
    # ② 默认 uid（★ 不看 uid 是否在 modes.json 里——默认 uid 是契约的一部分，
    #    即使当前文件缺了它，也应能反查出它代表哪一档）
    for t in FAN_MODE_ORDER:
        if DEFAULT_UIDS.get(t) == uid:
            return t
    if uid not in by_uid:
        return None
    # ③ 名字认领（排除已被 ① ② 占用的档位）
    claimed_tiers = {t for t in FAN_MODE_ORDER if claims.get(t)}
    return tier_by_name((by_uid.get(uid) or {}).get("name"), claimed_tiers)


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


def explain(modes_obj, binding=None):
    """返回四档的完整解析说明（供 --scan / probe 诊断）。

    结构：[{tier, name, uid, reason, mode_name, bind_value, bind_ok}, ...]
      reason   : "bind" | "default" | "name" | "none"
      bind_value: 该档在映射文件里写的是什么（没有则 ""）
      bind_ok  : 有绑定且能解析到 Mode 时为 True；有绑定但解析不到为 False
                 （⇒ 会回退到默认/名字，值得告警）
    ★ 这是唯一解析入口 resolve_tier 的"带解释"版本，诊断与实际落地必然一致。
    """
    if binding is None:
        mp = modes_path_env()
        binding = load_binding(mp) if mp else {}
    else:
        binding = normalize_binding(binding)
    by_uid = index_by_uid(modes_obj)
    claims = _claim_map(modes_obj, binding)
    out = []
    for t in FAN_MODE_ORDER:
        uid, reason = resolve_tier(t, modes_obj, binding=binding, claims=claims)
        bv = str((binding or {}).get(t, "") or "")
        bok = None
        if bv:
            bok = _binding_lookup(bv, modes_obj)[0] is not None
        out.append({
            "tier": t,
            "name": FAN_MODES.get(t, "?"),
            "uid": uid,
            "reason": reason,
            "mode_name": (by_uid.get(uid) or {}).get("name") if uid else None,
            "bind_value": bv,
            "bind_ok": bok,
        })
    return out


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
    if "--explain" in argv:
        mp = modes_path_env()
        modes = load_json(mp) or {}
        print(json.dumps(explain(modes), ensure_ascii=False, indent=2))
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

    # ── 统一内核：resolve_tier 的 (uid, reason) ──────────────────────────
    chk("resolve_tier 绑定 → reason=bind",
        resolve_tier(4, modes, binding={4: "U2"}) == ("U2", "bind"))
    # 该 fixture 里没有字面名 hs-smart 的 Mode ⇒ 5 档只能靠名字认领到 U5
    chk("resolve_tier 名字认领（5档）→ reason=name",
        resolve_tier(5, modes, binding={}) == ("U5", "name"))
    # 两个 Mode 都自称档位 2（U2 前缀、UX 关键词）时，正向只认一个（先到者 U2）；
    # 但反向按"这个 Mode 自己的名字"判定 ⇒ U2、UX 都能各自反查出档位 2。
    chk("resolve_tier 名字认领（2档）→ reason=name",
        resolve_tier(2, modes, binding={}) == ("U2", "name"))
    chk("多 Mode 争同一档：各自都能反查",
        reverse("U2", modes, binding={}) == 2 and
        reverse("UX", modes, binding={}) == 2)
    # 真·默认 uid 路径：modes 里存在字面 hs-low 时
    _dm = {"modes": [{"uid": "hs-low", "name": "随便什么名"}]}
    chk("resolve_tier 默认 uid → reason=default",
        resolve_tier(2, _dm, binding={}) == ("hs-low", "default"))
    chk("resolve_tier 认不出 → (None,'none')",
        resolve_tier(3, modes, binding={}) == (None, "none"))
    chk("resolve_tier 非法档位 → (None,'none')",
        resolve_tier(1, modes, binding={}) == (None, "none"))

    # ── 绑定值两种写法统一收口（uid / 名字）────────────────────────────
    chk("_binding_lookup 按 uid", _binding_lookup("U2", modes) == ("U2", "uid"))
    chk("_binding_lookup 按名字",
        _binding_lookup("My Silent Cooler", modes) == ("UX", "name"))
    chk("_binding_lookup 名字前缀",
        _binding_lookup("hs 低速", modes) == ("U2", "name"))
    chk("_binding_lookup 不存在 → (None,'none')",
        _binding_lookup("不存在的模式", modes) == (None, "none"))
    # ★ 绑定值是名字时，正向/反向都必须走同一条 _binding_lookup
    chk("正向：绑定值=名字 → 命中",
        resolve(2, modes, binding={2: "My Silent Cooler"}) == "UX")
    chk("反向：绑定值=名字 → 命中",
        reverse("UX", modes, binding={2: "My Silent Cooler"}) == 2)

    # ── 正反向对称：正向认谁，反向就认谁 ──────────────────────────────
    _sym = {"modes": [
        {"uid": "A", "name": "我的智能模式"},
        {"uid": "B", "name": "静音"},
    ]}
    _fwd = {t: resolve_tier(t, _sym, binding={}) for t in FAN_MODE_ORDER}
    chk("对称性：正向 5→A",
        next(u for u, r in [_fwd[5]] if r == "name") == "A")
    chk("对称性：正向 2→B", _fwd[2] == ("B", "name"))
    chk("对称性：反向 A→5", reverse("A", _sym, binding={}) == 5)
    chk("对称性：反向 B→2", reverse("B", _sym, binding={}) == 2)

    # ── explain()：与实际落地同一条代码路径 ──────────────────────────
    _ex = {e["tier"]: e for e in explain(modes, binding={4: "U2"})}
    chk("explain 覆盖四档", set(_ex.keys()) == set(FAN_MODE_ORDER))
    chk("explain 4 档 reason=bind", _ex[4]["reason"] == "bind")
    chk("explain bind_ok=True（能解析到）", _ex[4]["bind_ok"] is True)
    chk("explain mode_name 带上真名", _ex[4]["mode_name"] == "hs 低速 25%")
    # 绑定写了但解析不到 → bind_ok=False（诊断要能标红）
    _ex2 = {e["tier"]: e for e in
            explain(modes, binding={4: "幽灵模式不存在的名字xyz"})}
    chk("explain 绑定失效 → bind_ok=False", _ex2[4]["bind_ok"] is False)
    chk("explain 绑定失效 → 回退（reason≠bind）", _ex2[4]["reason"] != "bind")
    # 空白绑定值不误判
    chk("explain 无绑定 → bind_value 为空", _ex[5]["bind_value"] == "")

    print(f"  fan-tier 自检：{ok} 通过 / {fail} 失败")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))

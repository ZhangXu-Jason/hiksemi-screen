#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render-config.py —— 把 CoolerControl 模板渲染成真机上可直接使用的配置文件

用法
----
  # 1) 先探测真机，拿到 device-uid / channel / 温度源
  ./probe-coolercontrol.sh

  # 2) 渲染（写入 CoolerControl 配置目录）—— 推荐用机型预设
  sudo ./render-config.py --host hs-afs-r1 --out-dir /etc/coolercontrol

  # 2b) ★ 更推荐：按真机真值自动对齐（device UID + 通道名）后再渲染
  sudo ./render-config.py --host hs-afs-r1 --auto --config-dir /etc/coolercontrol

  # 3) 手工指定（换机器时）
  sudo ./render-config.py \
        --device-uid 6a1f...c9 \
        --cpu-channel pwm1  --case-channel pwm2 \
        --cpu-temp-name  "Intel(R) Core(TM) i3-N305" \
        --cpu-temp-device-uid 336d... \
        --case-temp-name "temp1" \
        --case-temp-device-uid 58ce... \
        --out-dir /etc/coolercontrol

  # 4) 只校验不落盘
  ./render-config.py --host hs-afs-r1 --check

  # 5) 单风扇机器（只有一路 PWM）
  ./render-config.py --host hs-afs-r1 --single-channel

  # 6) ★ UID 漂移自愈：Web UI 打开 Mode 时「设备/通道/设置」全空白
  #    （多半是 daemon 重算/升级后 device UID 变了，modes.json 里的键对不上任何设备）
  sudo ./render-config.py --host hs-afs-r1 --auto-uid

占位符
------
  @DEVICE_UID@              必填，风扇所在设备的 hwmon UID
  @CPU_CHANNEL@             必填，CPU 风扇通道名（如 pwm1）
  @CASE_CHANNEL@            选填，机箱风扇通道名（如 pwm2）；缺省时复制 CPU 通道名
  @CPU_TEMP_NAME@           CPU 温度传感器名（CPU 曲线用）
  @CPU_TEMP_DEVICE_UID@     CPU 温度传感器所在设备 UID
  @CASE_TEMP_NAME@          机箱/GPU 温度传感器名（机箱曲线用）
  @CASE_TEMP_DEVICE_UID@    机箱/GPU 温度传感器所在设备 UID

★ 为什么必须真机传参
   device_uid 是 CoolerControl 依据设备属性算出的 64 位 sha256，
   channel 名来自 hwmon 的 pwmN/fanN —— 二者都无法凭空生成。
   传错 uid 会导致配置被 daemon 忽略（不报错、也不生效）。

★ 双风扇说明
   本机 it8613 暴露两路可控风扇，各自跟随独立温度源：
     通道 fan3 → CPU 核心温度 → 走 hs-cpu-graph
     通道 fan2 → 核显 i915 温度 → 走 hs-case-graph
   ★★ 通道名是 CoolerControl 的通道名：hwmon 设备一律 fanN，
      ★ 不是 sysfs 里的 pwmN —— 写错通道名与 UID 漂移症状完全一样
      （UI 里 Mode 的「设备/通道/设置」一片空白），务必用
      --auto-channels 或 probe-coolercontrol.sh 取真值。
   ★ temp_name 填【传感器名】（hwmon 的 temp1_input ⇒ "temp1"），不是设备显示名。
   both 通道都会写进 [device-settings] 与每个 Mode，避免 activate_mode 时被 Reset。

★ device UID 会漂移（重要）
   UID 是 daemon 依据设备属性算出的 sha256，【daemon 升级 / 内核 hwmon 变化】
   都可能让它改变。一旦漂移：
     · modes.json 的 all_device_settings 键不再是任何已知设备
       ⇒ Web UI 打开 Mode 时「设备 / 通道 / 设置」一片空白（JSON 语法完全正确，
         只是没有任何一条能对上设备，最容易误判成「模板错了」）
     · config.toml 的 [device-settings] 同样失效 ⇒ 风扇不受控
   daemon 每次启动都会用真值重写 config.toml 的 [devices] 段 ⇒ 那是唯一权威来源。
   ⇒ 用 --auto-uid 按【设备名】重新对齐 UID，可一键自愈：
       ./render-config.py --host hs-afs-r1 --auto-uid
   排查：probe-coolercontrol.sh modes
"""

import argparse
import json
import os
import re
import shutil
import sys
import tomllib  # Python 3.11+；本机 3.13 可用

HERE = os.path.dirname(os.path.abspath(__file__))
TPL_CONFIG = os.path.join(HERE, "config.toml")
TPL_MODES = os.path.join(HERE, "modes.json")

UID_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# ★ 已实测机型的预设（--host <名> 一键填参，避免手抄 64 位 UID）
#   数据来源：真机 config.toml 的 [devices] 段（由 CoolerControl daemon 写出）。
#   新增机型：先 probe-coolercontrol.sh 取真值，再按此结构登记。
HOST_PRESETS = {
    "hs-afs-r1": {
        "device_uid": "71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742",
        # ★★ 通道名是【CoolerControl 的通道名】，hwmon 设备一律是 fanN，
        #    ★ 不是 sysfs 里的 pwmN！真机 it8613 暴露 fan1..fan5，其中：
        #      fan3 = CPU 风扇、fan2 = 机箱风扇（fan4/fan5 已在 UI 里 disabled）。
        #    教训：曾按 sysfs 的 pwm1/pwm2 写死 ⇒ UI 里 Mode「通道/设置」全空白
        #    （通道名 join 不上，与 UID 漂移症状一模一样，极易误判）。
        "cpu_channel": "fan3",
        "case_channel": "fan2",
        # ★ temp_name 是【传感器名】（hwmon 的 temp1_input → "temp1"），
        #   ★ 不是设备显示名！曾误写成 "Intel(R) Core(TM) i3-N305" ⇒ 温度源对不上。
        "cpu_temp_name": "temp1",
        "cpu_temp_device_uid":
            "336d28e0c7580fdf9135eb62a4626ff7c3cbfdfef20a61d19fc92198f26bc7bd",
        "case_temp_name": "temp1",
        "case_temp_device_uid":
            "58cea05ead42a096567b4a1d62ed932c28b0a81abdf9ded743e0645afc0a4818",
        # ★ --auto-uid 用：按【设备名】在 config.toml 的 [devices] 段里查真值 UID。
        #   注意 case_temp_name 是【传感器名】(temp1)，而这里是【设备显示名】(i915)。
        "fan_device_name": "it8613",
        "cpu_temp_device_name": "Intel(R) Core(TM) i3-N305",
        "case_temp_device_name": "i915",
        "note": "海康 HS-AFS-R1（Intel i3-N305）：双风扇 = it8613 fan3(CPU)/fan2(机箱)；"
                "温度源 = CPU 核心 + 核显 i915（传感器名都是 temp1）",
    },
}

# 保留 uid（daemon 会自动补写，这里只用于校验提示）
BASE_PROFILES = ("0", "hs-low", "hs-standard", "hs-high")

# ── 档位 ↔ Mode 绑定 ────────────────────────────────────────────────────────
#   5=智能 / 2=低速 / 3=标准 / 4=强劲（与小彩屏 UI 的 fan_mode 一致）
#   ★ 常量与名字认领规则统一来自同目录 fan-tier.py（唯一权威），本文件不另存副本。
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import importlib.util as _ilu   # noqa: E402

_ft_spec = _ilu.spec_from_file_location("fan_tier", os.path.join(_HERE, "fan-tier.py"))
FT = _ilu.module_from_spec(_ft_spec)
_ft_spec.loader.exec_module(FT)

FAN_MODE_ORDER = FT.FAN_MODE_ORDER
DEFAULT_UIDS = FT.DEFAULT_UIDS
# 映射文件随渲染产物一起写出；屏幕切档与「CC→屏幕」反向同步都读它
MAP_FILENAME = FT.MAP_FILENAME


def die(msg):
    print(f"[render] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def info(msg):
    print(f"[render] {msg}")


def read_tpl(path):
    if not os.path.isfile(path):
        die(f"模板不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()


def substitute(text, mapping):
    """替换 @KEY@ 占位符；若残留未替换的占位符则报错。"""
    for k, v in mapping.items():
        text = text.replace(f"@{k}@", v)
    leftover = sorted(set(re.findall(r"@[A-Z_]+@", text)))
    if leftover:
        die(f"仍有未替换的占位符: {', '.join(leftover)}"
            "（请用 --no-graph 或补全对应参数）")
    return text


# ── ★ device UID 自动对齐（--auto-uid）───────────────────────────────────────
#   CoolerControl 的 device UID 是 daemon 依据设备属性算出的 sha256，
#   daemon 升级 / 内核 hwmon 变化都会让它【漂移】。漂移后：
#     · modes.json 的 all_device_settings 键不再对应任何已知设备
#       ⇒ Web UI 打开 Mode 时「设备 / 通道 / 设置」一片空白（JSON 语法完全正确！
#         所以别急着改模板结构，先怀疑 UID）
#     · config.toml 的 [device-settings] 同样失效 ⇒ 风扇不受控
#   daemon 每次启动都会用真值重写 config.toml 的 [devices] 段 ⇒ 唯一权威来源。
#   ⇒ 按【设备名】反查 UID 即可一键自愈。

def parse_devices_section(cfg_path):
    """从现有 config.toml 的 [devices] 段解析 {uid: device_name}。

    ★ 故意不用 tomllib：真机上这份文件由 daemon 重写，可能含我们模板里没有的
      项；而这里只需要 [devices] 段的 `uid = "name"` 行，正则足够稳且不挑版本。
    返回 (dict, err_msg)；失败时 dict 为 None。
    """
    try:
        with open(cfg_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return None, f"读取失败: {e}"
    m = re.search(r"^\[devices\][ \t]*$(.*?)(?=^[ \t]*\[)", text,
                  flags=re.M | re.S)
    if not m:
        return None, "未找到 [devices] 段"
    dev = {}
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        mm = re.match(r'^"?([0-9a-fA-F]{64})"?[ \t]*=[ \t]*"(.*)"[ \t]*$', line)
        if mm:
            dev[mm.group(1)] = mm.group(2)
    if not dev:
        return None, '[devices] 段里没有可解析的 uid = "name" 行'
    return dev, ""


def find_device_uid(devmap, want_name, label):
    """按设备名在 [devices] 里反查 UID。返回 (uid|None, 提示msg)。"""
    if not want_name:
        return None, f"{label}: 未指定设备名，跳过对齐"
    exact = sorted(u for u, n in devmap.items() if n == want_name)
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return exact[0], (f"{label}: [devices] 中有 {len(exact)} 个同名 "
                          f"{want_name!r}，取第一个")
    fuzzy = sorted(u for u, n in devmap.items() if want_name.lower() in n.lower())
    if fuzzy:
        msg = (f"{label}: 未精确匹配 {want_name!r}，按子串命中 "
               f"{devmap[fuzzy[0]]!r}")
        if len(fuzzy) > 1:
            msg += f"（共 {len(fuzzy)} 个候选，取第一个）"
        return fuzzy[0], msg
    return None, f"{label}: [devices] 中找不到设备名 {want_name!r}"


def parse_device_settings(text):
    """从 config.toml 解析 [device-settings.*] 段 → {uid: {channel: profile_uid|None}}。

    ★ 这是【通道名的唯一权威来源】：daemon 只会把它认识的通道写进来。
      （sysfs 里叫 pwmN，CoolerControl 的通道名却是 fanN —— 不能靠 ls 猜）
    """
    out = {}
    for sm in re.finditer(
            r'^\[device-settings\.[ \t]*"?([^"\]]+)"?\][ \t]*$(.*?)(?=^[ \t]*\[)',
            text, flags=re.M | re.S):
        uid = sm.group(1)
        if "@" in uid:          # 未渲染的占位符残留，跳过
            continue
        chans = {}
        for cm in re.finditer(r'^"?([^"\s=]+)"?[ \t]*=[ \t]*\{([^}]*)\}',
                              sm.group(2), flags=re.M):
            pm = re.search(r'profile_uid[ \t]*=[ \t]*"([^"]*)"', cm.group(2))
            chans[cm.group(1)] = pm.group(1) if pm else None
        out[uid] = chans
    return out


def parse_disabled_channels(text, dev_uid):
    """解析 [settings.<uid>.channel_settings] 里被 disabled 的通道。"""
    off = set()
    pat = (r'^\[\s*settings\.[ \t]*"?%s"?\.channel_settings\s*\][ \t]*$(.*?)(?=^[ \t]*\[)'
           % re.escape(dev_uid))
    m = re.search(pat, text, flags=re.M | re.S)
    if not m:
        return off
    for cm in re.finditer(r'^"?([^"\s=]+)"?[ \t]*=[ \t]*\{([^}]*)\}', m.group(1), flags=re.M):
        if re.search(r'disabled[ \t]*=[ \t]*true', cm.group(2)):
            off.add(cm.group(1))
    return off


def set_profile_fixed(text, uid, val):
    """把指定 profile 的 speed_fixed 改成 val。

    ★ 用途：用户在 Web UI 里手调过转速（如低速 25→30、标准 55→60），
      重新渲染时应能带上这些偏好，否则一渲染就被打回模板默认值。
    """
    blocks = text.split("[[profiles]]")
    for i, b in enumerate(blocks[1:], 1):
        if re.search(r'^uid[ \t]*=[ \t]*"%s"' % re.escape(uid), b, flags=re.M):
            nb, n = re.subn(r'(speed_fixed[ \t]*=[ \t]*)(\d+)',
                            lambda m: m.group(1) + str(val), b, count=1)
            if n:
                blocks[i] = nb
                return "[[profiles]]".join(blocks), True
    return text, False


def sync_devices_section(text, name_to_uid):
    """让 [devices] 段里指定设备名的行改用【本次实际使用的 UID】。

    该段只是给人看的名字表（daemon 每次启动都会重写），但如果不跟着改，
    渲染产物内部就会自相矛盾：[devices] 是旧 UID、[device-settings] 是新 UID
    ⇒ 排查脚本（probe-coolercontrol.sh modes）和肉眼都会误判成「UID 漂移」。
    """
    m = re.search(r"^\[devices\][ \t]*$(.*?)(?=^[ \t]*\[)", text, flags=re.M | re.S)
    if not m:
        return text
    body = m.group(1)
    new_body = body
    for line in body.splitlines():
        mm = re.match(r'^"?([0-9a-fA-F]{64})"?[ \t]*=[ \t]*"(.*)"[ \t]*$', line.strip())
        if not mm:
            continue
        old, name = mm.group(1), mm.group(2)
        new = name_to_uid.get(name)
        if new and new != old:
            new_body = new_body.replace(old, new)
    if new_body == body:
        return text
    return text[:m.start(1)] + new_body + text[m.end(1):]


def strip_json_comments(text):
    """去掉模板里的 _comment 字段（CoolerControl 不认未知顶层键不会报错，
    但保持输出干净）。返回 (清洗后的 JSON 文本, python 对象)。"""
    obj = json.loads(text)
    obj.pop("_comment", None)
    return json.dumps(obj, ensure_ascii=False, indent=2), obj


def validate_toml(text, expect_channels=("pwm1", "pwm2"), fixed_expect=None):
    try:
        doc = tomllib.loads(text)
    except Exception as e:
        die(f"渲染后的 TOML 语法错误: {e}")

    # ★★ 必需顶层段校验（daemon 启动 check_readable() 硬索引，缺段 = panic）：
    #    "legacy690" 尤其致命 —— legacy690_ids() 是第一个调用，toml_edit 对
    #    不存在的键直接 panic "index not found"（config.rs，实测真机启动崩溃）。
    #    官方默认 config 的完整段集：devices/legacy690/device-settings/
    #    profiles/functions/settings（profiles/functions 可为空数组）。
    for section in ("devices", "legacy690", "device-settings", "settings"):
        if section not in doc:
            die(f"渲染后的 config.toml 缺必需顶层段 [{section}]"
                f" —— daemon 启动会 panic 'index not found'（legacy690 尤其致命）")

    # profiles 必含 hs-low / hs-standard / hs-high，且 speed_fixed 合法
    profiles = {p.get("uid"): p for p in doc.get("profiles", [])}
    for uid, want in (("hs-low", 25), ("hs-standard", 55), ("hs-high", 100)):
        if fixed_expect and uid in fixed_expect:
            want = fixed_expect[uid]
        p = profiles.get(uid)
        if not p:
            die(f"缺少 profile: {uid}")
        if p.get("p_type") != "Fixed":
            die(f"profile {uid} 的 p_type 应为 Fixed，实际 {p.get('p_type')!r}")
        sf = p.get("speed_fixed")
        if not isinstance(sf, int) or not (0 <= sf <= 100):
            die(f"profile {uid} 的 speed_fixed 非法: {sf!r}")
        if sf != want:
            info(f"⚠ profile {uid} 的 speed_fixed={sf}（预期 {want}），请确认")

    if "0" not in profiles:
        info("⚠ 缺少保留 profile uid=\"0\"（daemon 会自动补写，建议显式保留）")

    # ★ Graph profile 深度校验：温度单调递增、首点 == temp_min、占空比范围合法
    for uid, p in profiles.items():
        if p.get("p_type") != "Graph":
            continue
        pts = p.get("speed_profile")
        if not isinstance(pts, list) or len(pts) < 2:
            die(f"profile {uid} 的 speed_profile 不是 >=2 点的列表")
        ts, ds = [], []
        for i, pt in enumerate(pts):
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                die(f"profile {uid} 的第 {i} 个曲线点不是 [温度, 占空比]: {pt!r}")
            ts.append(float(pt[0]))
            ds.append(pt[1])
        if ts != sorted(ts):
            die(f"profile {uid} 的曲线点温度未单调递增: {ts}")
        for d in ds:
            if not isinstance(d, int) or not (0 <= d <= 100):
                die(f"profile {uid} 的占空比非法: {d!r}（必须 0–100 整数）")
        tmin, tmax = p.get("temp_min"), p.get("temp_max")
        if abs(ts[0] - tmin) > 1e-9:
            die(f"profile {uid} 首点温度 {ts[0]} ≠ temp_min {tmin}（daemon 以 temp_min 为曲线左端）")
        if ts[-1] > tmax + 1e-9:
            die(f"profile {uid} 末点温度 {ts[-1]} > temp_max {tmax}")
        ts_src = p.get("temp_source")
        if not isinstance(ts_src, dict):
            die(f"profile {uid} 缺少 temp_source 内联表 {{ temp_name, device_uid }}")
        if not ts_src.get("temp_name") or not ts_src.get("device_uid"):
            die(f"profile {uid} 的 temp_source 缺 temp_name / device_uid")
        if not UID_RE.match(str(ts_src["device_uid"])):
            die(f"profile {uid} 的 temp_source.device_uid 不像 64 位 hex: "
                f"{ts_src['device_uid']!r}")
        if not isinstance(p.get("function_uid"), str):
            die(f"profile {uid} 缺 function_uid（Graph 必须挂整形函数，如 hs-fan-fn）")
        if p["function_uid"] not in {f.get("uid") for f in doc.get("functions", [])}:
            die(f"profile {uid} 的 function_uid={p['function_uid']!r} 在 [[functions]] 中不存在")

    # functions 键名必须蛇形；检测常见的连字符误写
    for fn in doc.get("functions", []):
        for bad in ("duty-minimum", "duty-maximum", "step-size-min-decreasing",
                    "step-size-max-decreasing", "step-size-min-increasing",
                    "step-size-max-increasing", "response-delay"):
            if bad in fn:
                die(f"function {fn.get('uid')} 使用了连字符键 {bad!r}，"
                    "CoolerControl 要求蛇形（duty_minimum 等）")

    # device-settings 必须内联表（tomllib 解析后就是 dict，这里校验通道值形状）
    ds = doc.get("device-settings", {})
    if not ds:
        die("device-settings 为空（风扇将不受控）")
    for dev_uid, chans in ds.items():
        if not isinstance(chans, dict):
            die(f"device-settings.{dev_uid} 不是表")
        if not chans:
            die(f"device-settings.{dev_uid} 没有任何通道")
        for ch, st in chans.items():
            if not isinstance(st, dict):
                die(f"device-settings.{dev_uid}.{ch} 不是内联表")
            if "profile_uid" in st and "speed_fixed" in st:
                die(f"device-settings.{dev_uid}.{ch} 同时给出 profile_uid 与 "
                    "speed_fixed（互斥）")
            if "profile_uid" in st and st["profile_uid"] not in profiles:
                die(f"device-settings.{dev_uid}.{ch} 引用了不存在的 profile "
                    f"{st['profile_uid']!r}")
    info(f"TOML 校验通过：{len(profiles)} profiles / "
         f"{len(doc.get('functions', []))} functions / "
         f"{len(ds)} device-settings / "
         f"{sum(len(c) for c in ds.values())} 通道")
    return doc


def validate_modes(obj, expect_uids=("hs-smart", "hs-low", "hs-standard", "hs-high"),
                   expect_channels=()):
    modes = obj.get("modes")
    if not isinstance(modes, list) or not modes:
        die("modes.json 缺少非空 modes 数组")
    got = [m.get("uid") for m in modes]
    for u in expect_uids:
        if u not in got:
            die(f"modes.json 缺少 mode: {u}")
    for m in modes:
        ads = m.get("all_device_settings")
        if not isinstance(ads, dict) or not ads:
            die(f"mode {m.get('uid')} 的 all_device_settings 为空")
        for dev_uid, chans in ads.items():
            # ★ 每个 Mode 必须覆盖全部预期通道，否则 activate_mode 会把
            #   未覆盖的通道 Reset（转速失控）。
            if expect_channels:
                missing = [c for c in expect_channels if c not in chans]
                if missing:
                    die(f"mode {m.get('uid')} 未覆盖通道 {missing}"
                        f"（会导致 activate_mode 时被 Reset）；实际 {sorted(chans)}")
            for ch, st in chans.items():
                if not isinstance(st, dict):
                    die(f"mode {m.get('uid')} 的 {dev_uid}.{ch} 不是对象")
                if st.get("channel_name") != ch:
                    die(f"mode {m.get('uid')} 的 {dev_uid}.{ch} 的 channel_name"
                        f"={st.get('channel_name')!r} 与键名不一致")
                kinds = [k for k in ("profile_uid", "speed_fixed",
                                     "lighting", "lcd") if k in st]
                if len(kinds) != 1:
                    die(f"mode {m.get('uid')} 的 {dev_uid}.{ch} 必须且只能有一个"
                        f"设置项，实际 {kinds}")
    if obj.get("current_active_mode") not in got:
        die(f"current_active_mode={obj.get('current_active_mode')!r} 不在 modes 中")
    info(f"modes.json 校验通过：{len(modes)} modes，"
         f"当前激活 = {obj.get('current_active_mode')}")
    return obj


# ── ★ 与「已存在的 modes.json」合并（--merge-modes）────────────────────────
#   痛点：用户在 CoolerControl Web UI 里【自己新建】的 Mode，uid 是随机 UUID。
#   直接覆盖 modes.json ⇒ 用户 UI 里那 4 个模式全没了（虽然名字一模一样）。
#   更糟的是：用户手工往模式里填内容时极易填错 —— 真机就出现过
#   「名字叫 hs 低速 25%，内容却挂着 hs-standard（60%）」这种自相矛盾，
#   于是「屏幕切低速 → 实际跑 60%」，看起来像 bug，其实是数据错了。
#   --merge-modes <现有文件> 让渲染产物【继承】现有 Mode 的 uid / name / order，
#   只把内容（通道 / profile / 转速）换成渲染结果 ⇒ 内容正确 + UI 模式不丢。
TIER_PROFILE = {5: "hs-smart", 2: "hs-low", 3: "hs-standard", 4: "hs-high"}
TIER_LABEL = {5: "智能", 2: "低速", 3: "标准", 4: "强劲"}
HEX64 = re.compile(r"[0-9a-f]{64}")


def _mode_profiles(mode):
    """收集一个 Mode 里所有 channel 引用的 profile uid"""
    out = set()
    for dev in (mode.get("all_device_settings") or {}).values():
        if not isinstance(dev, dict):
            continue
        for st in dev.values():
            if isinstance(st, dict) and st.get("profile_uid"):
                out.add(str(st["profile_uid"]))
    return out


def _tier_by_name(mode, claimed):
    """按 Mode 名字认领档位（前缀优先，其次关键词）。认不出返回 None。

    ★ 规则委托 fan-tier.py（唯一权威），与 middleware / fan-mode-map.py 完全同源。
    """
    return FT.tier_by_name(mode.get("name"), claimed)


def _tier_by_content(mode, claimed):
    """按 Mode 现在挂的 profile 认领档位（名字看不出时才用）。"""
    profs = _mode_profiles(mode)
    for tier in FAN_MODE_ORDER:
        if tier not in claimed and TIER_PROFILE[tier] in profs:
            return tier
    return None


def merge_modes(modes_obj, existing, fixed_pct=None, rename=False):
    """把渲染产物的 4 个档位 Mode 与 existing 对齐（继承 uid / name / order）。

    返回 (档位→最终 uid 的 dict, 保留的额外 Mode 数, 丢弃的额外 Mode 数)
    """
    emodes = [m for m in (existing.get("modes") or []) if isinstance(m, dict)]
    by_uid = {m.get("uid"): m for m in emodes}
    assigned, claimed = {}, set()

    # ① 渲染 uid 原样还在（用户没改过）→ 直接认领
    for m in modes_obj.get("modes", []):
        u = m.get("uid")
        t = next((k for k, v in DEFAULT_UIDS.items() if v == u), None)
        if t is not None and t not in claimed and u in by_uid:
            assigned[t] = by_uid[u]
            claimed.add(t)

    # ② 名字认领 → 再退到内容认领（★ 名字优先：真机出现过「名字叫 hs 低速 25%、
    #    内容却挂着 hs-standard(60%)」的自相矛盾，此时必须信名字，才能把内容纠正过来）
    for fn in (_tier_by_name, _tier_by_content):
        for m in emodes:
            if any(m is a for a in assigned.values()):
                continue
            t = fn(m, claimed)
            if t is not None:
                assigned[t] = m
                claimed.add(t)

    uid_map = {}
    merged = 0
    for m in modes_obj.get("modes", []):
        old_u = m.get("uid")
        t = next((k for k, v in DEFAULT_UIDS.items() if v == old_u), None)
        em = assigned.get(t)
        if em is None:
            continue
        if em.get("uid"):
            m["uid"] = em["uid"]
        if em.get("name") and not rename:
            m["name"] = em["name"]
        if rename and t in (2, 3, 4) and fixed_pct:
            pct = fixed_pct.get(DEFAULT_UIDS[t])
            if pct is not None:
                m["name"] = f"hs {TIER_LABEL[t]} {pct}%"
        uid_map[old_u] = m["uid"]
        merged += 1
        if m["uid"] != old_u:
            info(f"合并档位 {t}（{TIER_LABEL[t]}）：沿用 UI 里的 "
                 f"{str(m['uid'])[:12]}…（原来会是 {old_u}）")

    # ③ 额外 Mode：属于用户自己的东西 —— 干净就留，脏（占位符/缺通道）就丢并告警
    kept, dropped = [], 0
    bad_names = []
    for m in emodes:
        if any(m is a for a in assigned.values()):
            continue
        devs = m.get("all_device_settings") or {}
        dirty = any(not HEX64.fullmatch(str(k)) for k in devs)
        for dev in devs.values():
            if isinstance(dev, dict):
                dirty = dirty or any(not HEX64.fullmatch(str(k)) for k in dev)
        if dirty:
            dropped += 1
            bad_names.append(str(m.get("name") or m.get("uid"))[:24])
        else:
            kept.append(m)
    modes_obj["modes"].extend(kept)
    if dropped:
        info(f"⚠ 丢弃 {dropped} 个未渲染的额外 Mode（含占位符键，daemon 认不出）："
             f"{', '.join(bad_names)}")
    if kept:
        info(f"保留 {len(kept)} 个额外的自定义 Mode（内容不动）")

    # ④ order / current_active_mode / previous_active_mode 跟着映射
    order, seen = [], set()
    for u in (existing.get("order") or []):
        nu = uid_map.get(u, u)
        if nu not in seen:
            order.append(nu)
            seen.add(nu)
    for m in modes_obj["modes"]:
        if m["uid"] not in seen:
            order.append(m["uid"])
            seen.add(m["uid"])
    valid = {m["uid"] for m in modes_obj["modes"]}
    modes_obj["order"] = [u for u in order if u in valid]
    cur = existing.get("current_active_mode")
    if cur in valid:
        modes_obj["current_active_mode"] = cur
    elif cur and uid_map.get(cur) in valid:
        modes_obj["current_active_mode"] = uid_map[cur]
    prev = existing.get("previous_active_mode")
    modes_obj["previous_active_mode"] = uid_map.get(prev, prev) if prev else prev
    if modes_obj["previous_active_mode"] not in valid:
        modes_obj["previous_active_mode"] = None
    # ★ 兜底：原有激活项被丢弃（或本来就悬空）时回落到智能档，
    #   否则 current_active_mode 会指向不存在的 Mode 而让校验失败。
    if modes_obj.get("current_active_mode") not in valid:
        modes_obj["current_active_mode"] = (uid_map.get(DEFAULT_UIDS[5])
                                            or (modes_obj["order"] or [None])[0])
    return ({t: m["uid"] for t, m in assigned.items()}, len(kept), dropped)


def main():
    ap = argparse.ArgumentParser(
        description="渲染 CoolerControl 模板（config.toml / modes.json）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="",
                    help="已实测机型预设（免手抄 UID）：" + " / ".join(HOST_PRESETS))
    ap.add_argument("--device-uid", default="",
                    help="风扇设备 UID（64 位 hex）；用 --host 时可省略")
    ap.add_argument("--cpu-channel", "--channel", dest="cpu_channel", default="",
                    help="CPU 风扇通道名，如 pwm1；用 --host 时可省略")
    ap.add_argument("--case-channel", default="",
                    help="机箱风扇通道名，如 pwm2；缺省时复制 CPU 通道名")
    ap.add_argument("--cpu-temp-name", default="",
                    help="CPU 温度传感器名（CPU 曲线）")
    ap.add_argument("--cpu-temp-device-uid", default="",
                    help="CPU 温度传感器设备 UID")
    ap.add_argument("--case-temp-name", default=None,
                    help="机箱/GPU 温度传感器名（机箱曲线）；传空串可显式清空")
    ap.add_argument("--case-temp-device-uid", default=None,
                    help="机箱/GPU 温度传感器设备 UID；传空串可显式清空")
    ap.add_argument("--no-case-graph", action="store_true",
                    help="禁用机箱曲线，pwm2 智能档走 Fixed 40% 兜底")
    ap.add_argument("--single-channel", action="store_true",
                    help="单风扇机器：只渲染一路（用 --cpu-channel），智能档走 CPU 曲线")
    ap.add_argument("--no-graph", action="store_true",
                    help="不生成 Graph 曲线，智能档退化为 Fixed 兜底")
    ap.add_argument("--auto-channels", action="store_true",
                    help="★ 从 --config-dir 的 config.toml [device-settings] 段读取"
                         "【真机真实通道名】（hwmon 是 fanN，不是 sysfs 的 pwmN）")
    ap.add_argument("--auto", action="store_true",
                    help="= --auto-uid + --auto-channels（推荐：全部按真机真值对齐）")
    ap.add_argument("--auto-uid", action="store_true",
                    help="★ 从 --config-dir 的 config.toml [devices] 段按【设备名】"
                         "自动对齐 device UID（修 Mode 在 UI 显示空白 / 风扇不受控）")
    ap.add_argument("--config-dir", default="",
                    help="--auto-uid 读取的 CoolerControl 配置目录（默认 /etc/coolercontrol）")
    ap.add_argument("--fan-device-name", default="",
                    help="风扇设备名（[devices] 段的显示名，默认 it8613）")
    ap.add_argument("--cpu-temp-device-name", default="",
                    help="CPU 温度所在设备的显示名（[devices] 段）")
    ap.add_argument("--case-temp-device-name", default="",
                    help="机箱温度所在设备的显示名（[devices] 段）")
    ap.add_argument("--merge-modes", default="", metavar="PATH",
                    help="★ 与【已存在的 modes.json】合并：继承其 Mode 的 uid / name / "
                         "order / current_active_mode，只把内容换成渲染结果 ⇒ 内容正确 + "
                         "你在 Web UI 里自建的模式不丢。推荐 "
                         "--merge-modes /etc/coolercontrol/modes.json")
    ap.add_argument("--rename-modes", action="store_true",
                    help="--merge-modes 时把 Mode 名字重写成与实际转速一致"
                         "（hs 低速 25% / hs 标准 55% / hs 强劲 100%）")
    ap.add_argument("--out-dir", default="",
                    help="输出目录（默认 /etc/coolercontrol）")
    ap.add_argument("--fixed-pct", default="",
                    help="三档固定转速百分比，如 30,60,100（默认 25,55,100）——"
                         "手调过转速时用它把偏好带进渲染结果")
    ap.add_argument("--fallback-pct", default="",
                    help="智能档兜底百分比，如 30,35（默认 35,40）")
    ap.add_argument("--check", action="store_true",
                    help="只做渲染与校验，不写文件")
    ap.add_argument("--backup", action="store_true", default=True,
                    help="覆盖前备份已存在文件（默认开启）")
    ap.add_argument("--list-hosts", action="store_true",
                    help="列出内置机型预设后退出")
    args = ap.parse_args()

    if args.list_hosts:
        for name, p in HOST_PRESETS.items():
            print(f"{name}\n    风扇设备 UID : {p['device_uid']}\n"
                  f"    CPU 通道     : {p.get('cpu_channel', '?')}\n"
                  f"    机箱通道     : {p.get('case_channel', '（无）')}\n"
                  f"    CPU 温度源   : {p['cpu_temp_name']}\n"
                  f"    CPU 温度 UID : {p['cpu_temp_device_uid']}\n"
                  f"    机箱温度源   : {p.get('case_temp_name', '（无）')}\n"
                  f"    机箱温度 UID : {p.get('case_temp_device_uid', '（无）')}\n"
                  f"    --auto-uid 设备名: 风扇={p.get('fan_device_name', '?')} / "
                  f"CPU温度={p.get('cpu_temp_device_name', '?')} / "
                  f"机箱温度={p.get('case_temp_device_name', '?')}\n"
                  f"    备注         : {p.get('note', '')}")
        return 0

    # ★ 用 None 区分「未传参」与「显式传空串」：
    #   None  → 允许 --host 预设填充
    #   ""    → 用户显式清空，预设不得覆盖
    case_temp_name = args.case_temp_name
    case_temp_uid = args.case_temp_device_uid

    # ★ 预设填充：--host 给出的值只在用户【未显式提供】该参数时生效，
    #   命令行显式参数优先级更高（便于临时覆盖单个字段）。
    if args.host:
        p = HOST_PRESETS.get(args.host.strip())
        if not p:
            die(f"未知机型预设 {args.host!r}（可选: {', '.join(HOST_PRESETS)}）")
        args.device_uid = args.device_uid or p["device_uid"]
        args.cpu_channel = args.cpu_channel or p.get("cpu_channel", "")
        args.case_channel = args.case_channel or p.get("case_channel", "")
        args.cpu_temp_name = args.cpu_temp_name or p.get("cpu_temp_name", "")
        args.cpu_temp_device_uid = args.cpu_temp_device_uid or p.get("cpu_temp_device_uid", "")
        if case_temp_name is None:
            case_temp_name = p.get("case_temp_name", "")
        if case_temp_uid is None:
            case_temp_uid = p.get("case_temp_device_uid", "")
        args.fan_device_name = args.fan_device_name or p.get("fan_device_name", "")
        args.cpu_temp_device_name = args.cpu_temp_device_name or \
            p.get("cpu_temp_device_name", "")
        args.case_temp_device_name = args.case_temp_device_name or \
            p.get("case_temp_device_name", "")
        info(f"使用机型预设 {args.host}：{p.get('note', '')}")
    case_temp_name = (case_temp_name or "").strip()
    case_temp_uid = (case_temp_uid or "").strip()
    args.fan_device_name = (args.fan_device_name or "it8613").strip()

    out_dir = args.out_dir or "/etc/coolercontrol"

    # ── ★ 按真机真值自动对齐（--auto / --auto-uid / --auto-channels）─────────
    #   现象：Web UI 打开 Mode 时设备/通道/设置一片空白，或风扇根本不受控。
    #   两大成因：① device UID 漂移；② 【通道名写错】——sysfs 里叫 pwmN，
    #   而 CoolerControl 的通道名是 fanN，按 pwm1/pwm2 写死必然 join 不上。
    auto_uid = args.auto_uid or args.auto
    auto_ch = args.auto_channels or args.auto
    cc_dir = args.config_dir or out_dir
    cfg_path = os.path.join(cc_dir, "config.toml")
    cc_text = ""
    if auto_uid or auto_ch:
        try:
            with open(cfg_path, encoding="utf-8", errors="replace") as f:
                cc_text = f.read()
        except OSError as e:
            die(f"--auto* 需要读取真机 {cfg_path} 作为真值，但读取失败: {e}"
                "\n  提示：CoolerControl daemon 至少成功启动过一次才会生成它；"
                "也可用 --config-dir 指定其它目录")

    if auto_uid:
        devmap, err = parse_devices_section(cfg_path)
        if not devmap:
            die(f"--auto-uid 需要 {cfg_path} 的 [devices] 段作为真值，但{err}"
                "\n  提示：确保 CoolerControl daemon 至少成功启动过一次"
                "（它会重写该段），或改用 --device-uid 手工指定")
        info(f"从 {cfg_path} 读到 {len(devmap)} 个设备（daemon 真值）")
        slots = (
            ("device_uid", args.fan_device_name, args.device_uid),
            ("cpu_temp_device_uid", args.cpu_temp_device_name, args.cpu_temp_device_uid),
            ("case_temp_device_uid", args.case_temp_device_name, case_temp_uid),
        )
        for slot, want, cur in slots:
            new, msg = find_device_uid(devmap, want, slot)
            if msg:
                info("⚠ " + msg)
            if not new:
                if slot == "device_uid":
                    die(f"device_uid 无法对齐（{msg}）——风扇设备找不到，"
                        "请用 --fan-device-name 指定 [devices] 段里的设备名，"
                        "或直接用 --device-uid")
                continue
            if new != cur:
                info(f"★ {slot}: {(cur or '（空）')[:8]}… → {new[:8]}…"
                     f"（设备名 {want!r}）")
            if slot == "device_uid":
                args.device_uid = new
            elif slot == "cpu_temp_device_uid":
                args.cpu_temp_device_uid = new
            else:
                case_temp_uid = new

    if auto_ch:
        dsmap = parse_device_settings(cc_text)
        chans = dsmap.get(args.device_uid.strip(), {})
        if not chans:
            info(f"⚠ [device-settings] 里没有设备 {args.device_uid[:8]}… 的通道，"
                 "无法自动对齐通道名（保留预设值）")
        else:
            off = parse_disabled_channels(cc_text, args.device_uid.strip())
            usable = [c for c in chans if c not in off]
            info(f"真机已配置通道: {', '.join(sorted(chans))}"
                 + (f"（已禁用: {', '.join(sorted(off))}）" if off else ""))
            # ★ 按通道当前挂的 profile 名推断角色（…cpu…→CPU 风扇，…case…→机箱风扇）
            guess_cpu = [c for c in usable
                         if (chans.get(c) or "").lower().find("cpu") >= 0]
            guess_case = [c for c in usable
                          if (chans.get(c) or "").lower().find("case") >= 0]
            if len(guess_cpu) == 1 and len(guess_case) == 1 \
                    and guess_cpu[0] != guess_case[0]:
                if args.cpu_channel != guess_cpu[0]:
                    info(f"★ cpu_channel : {args.cpu_channel or '（空）'} → "
                         f"{guess_cpu[0]}（该通道当前挂 {chans.get(guess_cpu[0])}）")
                if args.case_channel != guess_case[0]:
                    info(f"★ case_channel: {args.case_channel or '（空）'} → "
                         f"{guess_case[0]}（该通道当前挂 {chans.get(guess_case[0])}）")
                args.cpu_channel, args.case_channel = guess_cpu[0], guess_case[0]
            else:
                info(f"⚠ 无法从 profile 名推断通道角色（cpu={guess_cpu} case={guess_case}），"
                     f"保留预设；可用 --cpu-channel/--case-channel 在 "
                     f"{sorted(usable)} 中手工指定")

    if not args.device_uid or not args.cpu_channel:
        die("必须提供 --device-uid 与 --cpu-channel（或用 --host <机型> 一键填充）")

    uid = args.device_uid.strip()
    if not UID_RE.match(uid):
        die(f"device-uid 不像 64 位 hex: {uid!r}（请用 probe-coolercontrol.sh 取得）")
    cpu_ch = args.cpu_channel.strip()
    if not cpu_ch:
        die("cpu-channel 不能为空")

    # ── 决定单风扇 / 双风扇 ────────────────────────────────────────────────
    case_ch = "" if args.single_channel else args.case_channel.strip()
    dual = bool(case_ch)
    if dual and case_ch == cpu_ch:
        die(f"cpu-channel 与 case-channel 相同（{cpu_ch}）—— 双风扇必须两路不同；"
            "若机器只有一路 PWM，请加 --single-channel")

    # ── 决定智能档走曲线还是兜底 ───────────────────────────────────────────
    if args.no_case_graph:
        case_temp_name = case_temp_uid = ""
    use_cpu_graph = not args.no_graph
    if use_cpu_graph and (not args.cpu_temp_name or not args.cpu_temp_device_uid):
        info("未提供 CPU 温度源 → CPU 曲线禁用")
        use_cpu_graph = False
    # 机箱曲线：仅在双风扇 + 提供了机箱温度源时启用
    use_case_graph = dual and not args.no_graph and \
        bool(case_temp_name and case_temp_uid)
    if dual and not args.no_graph and not use_case_graph:
        info("未提供机箱温度源 → pwm2 智能档降级为 Fixed 40%")

    cpu_smart = "hs-cpu-graph" if use_cpu_graph else "hs-smart-fallback"
    case_smart = "hs-case-graph" if use_case_graph else "hs-case-fallback"
    if not dual:
        case_smart = cpu_smart  # 单风扇时 pwm2 不存在，仅为占位一致

    mapping = {
        "DEVICE_UID": uid,
        "CPU_CHANNEL": cpu_ch,
        "CASE_CHANNEL": case_ch or cpu_ch,
        "CPU_SMART_PROFILE": cpu_smart,
        "CASE_SMART_PROFILE": case_smart,
        "CPU_TEMP_NAME": args.cpu_temp_name.strip() or "N/A",
        "CPU_TEMP_DEVICE_UID": args.cpu_temp_device_uid.strip() or "0" * 64,
        "CASE_TEMP_NAME": case_temp_name.strip() or "N/A",
        "CASE_TEMP_DEVICE_UID": case_temp_uid.strip() or "0" * 64,
    }

    # ── 渲染 config.toml ────────────────────────────────────────────────
    cfg_text = read_tpl(TPL_CONFIG)
    # ★ 锚点必须是「下一个块的标题注释行」而不是 [[profiles]]：
    #   曲线块内自带多行注释，用 (?=#) 会提前命中导致删不干净。
    CPU_HDR = r"# -+ ★ 智能档 · CPU 风扇通道温度曲线"
    CASE_HDR = r"# -+ ★ 智能档 · 机箱风扇通道温度曲线"
    CASE_STOP = r"(?=# -+ 智能档兜底（探测不到温度源时使用）)"
    if not use_cpu_graph and not use_case_graph:
        # 两条曲线都删：CPU 头 → 兜底头
        cfg_text = re.sub(CPU_HDR + r".*?\n" + CASE_STOP, "", cfg_text,
                          count=1, flags=re.S)
    elif not use_cpu_graph:
        cfg_text = re.sub(CPU_HDR + r".*?\n(?=" + CASE_HDR + r")", "", cfg_text,
                          count=1, flags=re.S)
    elif not use_case_graph:
        cfg_text = re.sub(CASE_HDR + r".*?\n" + CASE_STOP, "", cfg_text,
                          count=1, flags=re.S)
    if not dual:
        # 单风扇：删掉整条 case 通道行 + 机箱兜底 profile
        cfg_text = re.sub(r'^"@CASE_CHANNEL@"\s*= \{[^\n]*\n', "", cfg_text, flags=re.M)
        cfg_text = re.sub(
            r"# -+ 智能档兜底（仅缺机箱温度源.*?\n(?=\n# =====)", "", cfg_text, flags=re.S)
    cfg_text = substitute(cfg_text, mapping)
    # ★ [devices] 段跟着用同样的 UID，避免产物内部自相矛盾（见 sync_devices_section）
    cfg_text = sync_devices_section(cfg_text, {
        args.fan_device_name: uid,
        args.cpu_temp_device_name: args.cpu_temp_device_uid,
        args.case_temp_device_name: case_temp_uid,
    })
    # ★ 保留用户在 UI 里手调过的转速（否则一渲染就被打回模板默认值）
    fixed_expect = {}
    fixed_pct_map = {}
    if args.fixed_pct:
        vals = [v.strip() for v in args.fixed_pct.split(",")]
        if len(vals) != 3:
            die(f"--fixed-pct 需要 3 个值（低速,标准,强劲），实际 {args.fixed_pct!r}")
        for uid, v in zip(("hs-low", "hs-standard", "hs-high"), vals):
            if not v.isdigit() or not (0 <= int(v) <= 100):
                die(f"--fixed-pct 的 {v!r} 不是 0–100 的整数")
            cfg_text, hit = set_profile_fixed(cfg_text, uid, int(v))
            if hit:
                info(f"转速覆盖 {uid} → {v}%")
            else:
                info(f"⚠ 未找到 profile {uid}，跳过转速覆盖")
            fixed_expect[uid] = int(v)
            fixed_pct_map[uid] = int(v)
    if args.fallback_pct:
        vals = [v.strip() for v in args.fallback_pct.split(",")]
        if len(vals) != 2:
            die(f"--fallback-pct 需要 2 个值（CPU兜底,机箱兜底），实际 "
                f"{args.fallback_pct!r}")
        for uid, v in zip(("hs-smart-fallback", "hs-case-fallback"), vals):
            if not v.isdigit() or not (0 <= int(v) <= 100):
                die(f"--fallback-pct 的 {v!r} 不是 0–100 的整数")
            cfg_text, hit = set_profile_fixed(cfg_text, uid, int(v))
            if hit:
                info(f"兜底覆盖 {uid} → {v}%")
    expect_channels = (cpu_ch, case_ch) if dual else (cpu_ch,)
    cfg_doc = validate_toml(cfg_text, expect_channels=expect_channels,
                            fixed_expect=fixed_expect or None)

    # ── 渲染 modes.json ────────────────────────────────────────────────
    modes_raw = read_tpl(TPL_MODES)
    modes_raw = substitute(modes_raw, mapping)
    modes_text, modes_obj = strip_json_comments(modes_raw)
    if not dual:
        # 单风扇：从每个 Mode 移除机箱通道
        for m in modes_obj["modes"]:
            for dev in m["all_device_settings"].values():
                dev.pop(case_ch, None)
        modes_text = json.dumps(modes_obj, ensure_ascii=False, indent=2)

    # ── ★ 与现有 modes.json 合并（--merge-modes）───────────────────────
    merged_uids = {}
    final_uids = dict(DEFAULT_UIDS)
    if args.merge_modes:
        ex_path = args.merge_modes
        if not os.path.isfile(ex_path):
            die(f"--merge-modes 指定的文件不存在：{ex_path}")
        try:
            with open(ex_path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception as e:
            die(f"--merge-modes 读取/解析失败（{ex_path}）：{e}")
        info(f"合并现有 modes.json：{ex_path}"
             f"（{len(existing.get('modes') or [])} 个 Mode）")
        merged_uids, n_keep, n_drop = merge_modes(
            modes_obj, existing,
            fixed_pct={DEFAULT_UIDS[t]: fixed_pct_map[DEFAULT_UIDS[t]]
                       for t in FAN_MODE_ORDER if DEFAULT_UIDS[t] in fixed_pct_map},
            rename=args.rename_modes)
        modes_text = json.dumps(modes_obj, ensure_ascii=False, indent=2)
        info(f"合并完成：{len(merged_uids)} 档沿用现有 Mode"
             + (f"，额外保留 {n_keep} 个" if n_keep else "")
             + (f"，丢弃 {n_drop} 个占位符 Mode" if n_drop else ""))
        final_uids.update(merged_uids)

    validate_modes(modes_obj,
                   expect_uids=tuple(final_uids[m_] for m_ in FAN_MODE_ORDER),
                   expect_channels=expect_channels)

    if args.check:
        info("--check：校验全部通过，未写入任何文件")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    # ── ★ 档位 ↔ Mode 映射文件 ──────────────────────────────────────────
    #   屏幕切档时靠它定位要激活哪个 Mode（apply-mode.sh / 中间件反向同步共用）。
    #   默认指向渲染器产出的 hs-* uid；用户若改用自己在 Web UI 建的模式，
    #   跑 `apply-mode.sh --bind 5=<uid 或 Mode 名字>` 改这里即可，不必重渲染。
    fan_map_text = json.dumps(
        {str(m_): final_uids[m_] for m_ in FAN_MODE_ORDER},
        ensure_ascii=False, indent=2) + "\n"
    targets = {
        os.path.join(out_dir, "config.toml"): cfg_text,
        os.path.join(out_dir, "modes.json"): modes_text,
        os.path.join(out_dir, MAP_FILENAME): fan_map_text,
    }
    for path, text in targets.items():
        if os.path.exists(path):
            bak = path + ".bak-render"
            try:
                shutil.copy2(path, bak)
                info(f"已备份原文件 → {bak}")
            except Exception as e:
                die(f"备份失败（不改动原文件）: {e}")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        info(f"已写入 {path}（{len(text)} 字节）")

    fan_desc = (f"双风扇 {cpu_ch}(CPU)+{case_ch}(机箱)" if dual
                else f"单风扇 {cpu_ch}")
    info(f"完成。{fan_desc}")
    info(f"  智能档 {cpu_ch} = {'CPU 曲线 hs-cpu-graph' if use_cpu_graph else '兜底 Fixed 35%'}")
    if dual:
        info(f"  智能档 {case_ch} = "
             f"{'机箱曲线 hs-case-graph' if use_case_graph else '兜底 Fixed 40%'}")
    info("下一步：sudo systemctl restart coolercontrold  "
         "（或 docker restart coolercontrol）后打开 http://<NAS>:11987 核对")
    return 0


if __name__ == "__main__":
    sys.exit(main())

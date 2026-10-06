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
   本机 it8613 暴露 pwm1 / pwm2 两路，各自跟随独立温度源：
     pwm1 → CPU 核心温度 → 走 hs-cpu-graph
     pwm2 → 核显 i915 温度 → 走 hs-case-graph
   both 通道都会写进 [device-settings] 与每个 Mode，避免 activate_mode 时被 Reset。
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
        "cpu_channel": "pwm1",
        "case_channel": "pwm2",
        "cpu_temp_name": "Intel(R) Core(TM) i3-N305",
        "cpu_temp_device_uid":
            "336d28e0c7580fdf9135eb62a4626ff7c3cbfdfef20a61d19fc92198f26bc7bd",
        "case_temp_name": "temp1",
        "case_temp_device_uid":
            "58cea05ead42a096567b4a1d62ed932c28b0a81abdf9ded743e0645afc0a4818",
        "note": "海康 HS-AFS-R1（Intel i3-N305）：双风扇 = it8613 pwm1(CPU)/pwm2(机箱)；"
                "温度源 = CPU 核心 + 核显 i915",
    },
}

# 保留 uid（daemon 会自动补写，这里只用于校验提示）
BASE_PROFILES = ("0", "hs-low", "hs-standard", "hs-high")


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


def strip_json_comments(text):
    """去掉模板里的 _comment 字段（CoolerControl 不认未知顶层键不会报错，
    但保持输出干净）。返回 (清洗后的 JSON 文本, python 对象)。"""
    obj = json.loads(text)
    obj.pop("_comment", None)
    return json.dumps(obj, ensure_ascii=False, indent=2), obj


def validate_toml(text, expect_channels=("pwm1", "pwm2")):
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
    ap.add_argument("--out-dir", default="",
                    help="输出目录（默认 /etc/coolercontrol）")
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
                  f"    备注         : {p.get('note', '')}")
        return 0

    # ★ 用 None 区分「未传参」与「显式传空串」：
    #   None  → 允许 --host 预设填充
    #   ""    → 用户显式清空，预设不得覆盖
    case_temp_name = "" if args.case_temp_name == "" else (
        args.case_temp_name or "")
    case_temp_uid = "" if args.case_temp_device_uid == "" else (
        args.case_temp_device_uid or "")

    # ★ 预设填充：--host 给出的值只在用户【未显式提供】该参数时生效，
    #   命令行显式参数优先级更高（便于临时覆盖单个字段）。
    # ★ 用 None 区分「未传参」与「显式传空串」：
    #   None → 允许 --host 预设填充； "" → 用户显式清空，预设不得覆盖
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
        info(f"使用机型预设 {args.host}：{p.get('note', '')}")
    case_temp_name = (case_temp_name or "").strip()
    case_temp_uid = (case_temp_uid or "").strip()

    out_dir = args.out_dir or "/etc/coolercontrol"

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
    CPU_HDR = r"# -+ ★ 智能档 pwm1 · CPU 温度曲线"
    CASE_HDR = r"# -+ ★ 智能档 pwm2 · 机箱温度曲线"
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
    expect_channels = (cpu_ch, case_ch) if dual else (cpu_ch,)
    cfg_doc = validate_toml(cfg_text, expect_channels=expect_channels)

    # ── 渲染 modes.json ────────────────────────────────────────────────
    modes_raw = read_tpl(TPL_MODES)
    modes_raw = substitute(modes_raw, mapping)
    modes_text, modes_obj = strip_json_comments(modes_raw)
    if not dual:
        # 单风扇：从每个 Mode 移除 pwm2 通道
        for m in modes_obj["modes"]:
            for dev in m["all_device_settings"].values():
                dev.pop(case_ch, None)
        modes_text = json.dumps(modes_obj, ensure_ascii=False, indent=2)
    validate_modes(modes_obj, expect_channels=expect_channels)

    if args.check:
        info("--check：校验全部通过，未写入任何文件")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    targets = {
        os.path.join(out_dir, "config.toml"): cfg_text,
        os.path.join(out_dir, "modes.json"): modes_text,
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
    info(f"  智能档 pwm1 = {'CPU 曲线 hs-cpu-graph' if use_cpu_graph else '兜底 Fixed 35%'}")
    if dual:
        info(f"  智能档 pwm2 = {'机箱曲线 hs-case-graph' if use_case_graph else '兜底 Fixed 40%'}")
    info("下一步：sudo systemctl restart coolercontrold  "
         "（或 docker restart coolercontrol）后打开 http://<NAS>:11987 核对")
    return 0


if __name__ == "__main__":
    sys.exit(main())

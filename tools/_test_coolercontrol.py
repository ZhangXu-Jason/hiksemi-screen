#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_coolercontrol.py —— CoolerControl 配置渲染器回归测试

覆盖 render-config.py 的核心行为（这些都是「错了就静默不生效/风扇失控」的关键路径）：
  1. 双风扇默认渲染：fan3→CPU 曲线、fan2→机箱曲线，两通道都进 device-settings 与每个 Mode
  2. 单风扇 --single-channel：只剩一路，不再出现 fan2
  3. --no-graph：两条 Graph 都被裁掉，智能档指向 Fixed 兜底
  4. --no-case-graph：只保留 CPU 曲线，fan2 走 Fixed 兜底
  5. 显式清空机箱温度源（--case-temp-name ""）能覆盖 --host 预设
  6. 校验器能抓出坏配置（温度非单调 / 首点≠temp_min / function_uid 不存在 / Mode 漏通道）
  7. 渲染产物无残留占位符、TOML/JSON 均可解析

不需要真机：全部用 --host 预设 + 临时目录。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
CC_DIR = os.path.join(HERE, "coolercontrol")
RENDER = os.path.join(CC_DIR, "render-config.py")
TPL_CONFIG = os.path.join(CC_DIR, "config.toml")
TPL_MODES = os.path.join(CC_DIR, "modes.json")

PASS = 0
FAIL = 0
FAILED = []


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  \033[31m✗\033[0m {name}" + (f"  —— {detail}" if detail else ""))


def sec(t):
    print(f"\n\033[1;36m── {t} ──\033[0m")


def run_render(args, out_dir):
    """跑 render-config.py，返回 (rc, stdout+stderr)。"""
    cmd = [sys.executable, RENDER, *args, "--out-dir", out_dir]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr)


def load(out_dir):
    """读渲染产物，返回 (toml_doc, modes_obj, cfg_text, modes_text)。"""
    with open(os.path.join(out_dir, "config.toml"), encoding="utf-8") as f:
        cfg = f.read()
    with open(os.path.join(out_dir, "modes.json"), encoding="utf-8") as f:
        mtext = f.read()
    return tomllib.loads(cfg), json.loads(mtext), cfg, mtext


def smart_chans(modes_obj):
    """取 hs-smart mode 里 {channel: profile_uid}。"""
    for m in modes_obj["modes"]:
        if m["uid"] == "hs-smart":
            dev = next(iter(m["all_device_settings"].values()))
            return {k: v["profile_uid"] for k, v in dev.items()}
    return {}


def all_mode_channels(modes_obj):
    """返回 {mode_uid: set(channels)}。"""
    out = {}
    for m in modes_obj["modes"]:
        dev = next(iter(m["all_device_settings"].values()))
        out[m["uid"]] = set(dev.keys())
    return out


def graph_uids(doc):
    return {p["uid"] for p in doc.get("profiles", []) if p.get("p_type") == "Graph"}


# ══════════════════════════════════════════════════════════════════════════
print("=" * 60)
print(" CoolerControl 渲染器测试")
print("=" * 60)

TMPROOT = tempfile.mkdtemp(prefix="cc-test-")
try:
    # ── 1. 双风扇默认渲染 ────────────────────────────────────────────────
    sec("[1] 双风扇默认渲染（--host hs-afs-r1）")
    d = os.path.join(TMPROOT, "dual")
    rc, out = run_render(["--host", "hs-afs-r1"], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    ok("两条 Graph 曲线都在", graph_uids(doc) == {"hs-cpu-graph", "hs-case-graph"},
       str(graph_uids(doc)))
    ds = doc.get("device-settings", {})
    chans = list(ds.values())[0]
    ok("device-settings 覆盖 fan3+fan2", set(chans.keys()) == {"fan3", "fan2"},
       str(set(chans.keys())))
    ok("fan3 智能档 → hs-cpu-graph", chans["fan3"].get("profile_uid") == "hs-cpu-graph")
    ok("fan2 智能档 → hs-case-graph", chans["fan2"].get("profile_uid") == "hs-case-graph")
    ok("hs-smart 两路分别指向两条曲线",
       smart_chans(modes) == {"fan3": "hs-cpu-graph", "fan2": "hs-case-graph"},
       str(smart_chans(modes)))
    mc = all_mode_channels(modes)
    ok("每个 Mode 都覆盖 fan3+fan2",
       all(v == {"fan3", "fan2"} for v in mc.values()), str(mc))
    ok("无残留占位符", not re.findall(r"@[A-Z_]+@", cfg + mtext),
       str(set(re.findall(r"@[A-Z_]+@", cfg + mtext))))
    # ★ temp_name 必须是【传感器名】(temp1)，不是设备显示名（真机实测修正）
    ok("CPU 曲线温度源 = i3-N305 设备的 temp1",
       next(p for p in doc["profiles"] if p["uid"] == "hs-cpu-graph")["temp_source"]
       == {"temp_name": "temp1",
           "device_uid": "336d28e0c7580fdf9135eb62a4626ff7c3cbfdfef20a61d19fc92198f26bc7bd"})
    ok("CPU 曲线 temp_name 不是设备显示名（防止退回旧写法）",
       "i3-N305" not in str(next(p for p in doc["profiles"]
                                 if p["uid"] == "hs-cpu-graph")["temp_source"]["temp_name"]))
    ok("机箱曲线温度源 = i915 的 temp1",
       next(p for p in doc["profiles"] if p["uid"] == "hs-case-graph")["temp_source"]
       == {"temp_name": "temp1",
           "device_uid": "58cea05ead42a096567b4a1d62ed932c28b0a81abdf9ded743e0645afc0a4818"})
    # ★ daemon 必需顶层段（缺 [legacy690] → 启动 panic "index not found"，真机实测）
    need = {"devices", "legacy690", "device-settings", "settings"}
    _missing = sorted(need - set(doc.keys()))
    ok("daemon 必需段齐全（含 legacy690）", not _missing,
       "缺失: " + ", ".join(_missing))
    ok("legacy690 是空表（普通设备无 Asetek 690LC）", doc.get("legacy690") == {},
       str(doc.get("legacy690")))

    # ── 2. 单风扇 ────────────────────────────────────────────────────────
    sec("[2] 单风扇（--single-channel）")
    d = os.path.join(TMPROOT, "single")
    rc, out = run_render(["--host", "hs-afs-r1", "--single-channel"], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    chans = list(doc.get("device-settings", {}).values())[0]
    ok("device-settings 只有 fan3", set(chans.keys()) == {"fan3"}, str(set(chans.keys())))
    ok("单风扇智能档 → hs-cpu-graph",
       smart_chans(modes) == {"fan3": "hs-cpu-graph"}, str(smart_chans(modes)))
    ok("机箱曲线已移除", "hs-case-graph" not in graph_uids(doc))
    ok("机箱兜底 profile 已移除",
       not any(p["uid"] == "hs-case-fallback" for p in doc["profiles"]))
    ok("每个 Mode 只覆盖 fan3",
       all(v == {"fan3"} for v in all_mode_channels(modes).values()))

    # ── 3. --no-graph ───────────────────────────────────────────────────
    sec("[3] --no-graph（两路都走 Fixed 兜底）")
    d = os.path.join(TMPROOT, "nog")
    rc, out = run_render(["--host", "hs-afs-r1", "--no-graph"], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    ok("没有任何 Graph profile", graph_uids(doc) == set(), str(graph_uids(doc)))
    ok("智能档两路都指向兜底",
       smart_chans(modes) == {"fan3": "hs-smart-fallback", "fan2": "hs-case-fallback"},
       str(smart_chans(modes)))
    ok("仍覆盖双通道", all(v == {"fan3", "fan2"}
                      for v in all_mode_channels(modes).values()))

    # ── 4. --no-case-graph ──────────────────────────────────────────────
    sec("[4] --no-case-graph（只保留 CPU 曲线）")
    d = os.path.join(TMPROOT, "nogc")
    rc, out = run_render(["--host", "hs-afs-r1", "--no-case-graph"], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    ok("只剩 CPU 曲线", graph_uids(doc) == {"hs-cpu-graph"}, str(graph_uids(doc)))
    ok("fan3 走曲线、fan2 走兜底",
       smart_chans(modes) == {"fan3": "hs-cpu-graph", "fan2": "hs-case-fallback"},
       str(smart_chans(modes)))

    # ── 5. 显式清空机箱温度源（覆盖 --host 预设）────────────────────────
    sec("[5] 显式清空机箱温度源能覆盖 --host 预设")
    d = os.path.join(TMPROOT, "clearcase")
    rc, out = run_render(["--host", "hs-afs-r1",
                          "--case-temp-name", "", "--case-temp-device-uid", ""], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    ok("机箱曲线被裁掉（因无温度源）", graph_uids(doc) == {"hs-cpu-graph"},
       str(graph_uids(doc)))
    ok("fan2 智能档 → hs-case-fallback",
       smart_chans(modes).get("fan2") == "hs-case-fallback",
       str(smart_chans(modes)))

    # ── 6. 校验器能抓出坏配置 ────────────────────────────────────────────
    sec("[6] 校验器负例（坏配置必须被拒）")
    # 6a. 首点温度 ≠ temp_min
    bad = open(TPL_CONFIG, encoding="utf-8").read()
    bad = (bad.replace("@DEVICE_UID@", "a" * 64)
              .replace("@CPU_CHANNEL@", "fan3").replace("@CASE_CHANNEL@", "fan2")
              .replace("@CPU_TEMP_NAME@", "Core").replace("@CPU_TEMP_DEVICE_UID@", "b" * 64)
              .replace("@CASE_TEMP_NAME@", "temp1").replace("@CASE_TEMP_DEVICE_UID@", "c" * 64)
              .replace("@CPU_SMART_PROFILE@", "hs-cpu-graph")
              .replace("@CASE_SMART_PROFILE@", "hs-case-graph"))
    # 让 CPU 曲线首点温度变成 31（≠ temp_min 30）
    bad_broken = bad.replace("[30.0,  22],", "[31.0,  22],", 1)
    sys.path.insert(0, CC_DIR)
    import importlib.util
    spec = importlib.util.spec_from_file_location("rc_mod", RENDER)
    rc_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc_mod)
    # 用 die() 会 sys.exit(1)，需捕获
    def expect_die(fn, label):
        try:
            fn()
            ok(label, False, "未按预期拒绝")
        except SystemExit:
            ok(label, True)
        except Exception as e:
            ok(label, False, f"异常类型不符: {type(e).__name__}: {e}")
    expect_die(lambda: rc_mod.validate_toml(bad_broken), "拒绝「首点温度≠temp_min」")
    # 6b. 温度非单调
    bad_mono = bad.replace("[45.0,  22],", "[20.0,  22],", 1)
    expect_die(lambda: rc_mod.validate_toml(bad_mono), "拒绝「温度非单调递增」")
    # 6c. function_uid 指向不存在的函数
    bad_fn = bad.replace('function_uid = "hs-fan-fn"', 'function_uid = "nope"')
    expect_die(lambda: rc_mod.validate_toml(bad_fn), "拒绝「function_uid 不存在」")
    # 6d. Mode 漏通道
    modes_obj = json.loads(open(TPL_MODES, encoding="utf-8").read().replace(
        "@DEVICE_UID@", "a" * 64).replace("@CPU_CHANNEL@", "fan3").replace(
        "@CASE_CHANNEL@", "fan2").replace("@CPU_SMART_PROFILE@", "hs-cpu-graph").replace(
        "@CASE_SMART_PROFILE@", "hs-case-graph"))
    modes_obj.pop("_comment", None)
    # 删掉 hs-smart 里的 fan2
    for m in modes_obj["modes"]:
        if m["uid"] == "hs-smart":
            for dev in m["all_device_settings"].values():
                dev.pop("fan2", None)
    expect_die(lambda: rc_mod.validate_modes(modes_obj, expect_channels=("fan3", "fan2")),
               "拒绝「Mode 漏掉 fan2 通道」")

    # ── 7. --list-hosts / 参数校验 ──────────────────────────────────────
    sec("[7] CLI 行为")
    p = subprocess.run([sys.executable, RENDER, "--list-hosts"],
                       capture_output=True, text=True)
    ok("--list-hosts rc=0", p.returncode == 0)
    ok("--list-hosts 含 hs-afs-r1", "hs-afs-r1" in p.stdout)
    ok("--list-hosts 显示机箱通道 fan2", "fan2" in p.stdout)
    p = subprocess.run([sys.executable, RENDER, "--host", "no-such-host"],
                       capture_output=True, text=True)
    ok("未知机型 rc≠0", p.returncode != 0)
    ok("未知机型给出可选列表", "hs-afs-r1" in (p.stdout + p.stderr))
    p = subprocess.run([sys.executable, RENDER,
                        "--device-uid", "short", "--cpu-channel", "fan3"],
                       capture_output=True, text=True)
    ok("非法 device-uid rc≠0", p.returncode != 0)
    p = subprocess.run([sys.executable, RENDER, "--host", "hs-afs-r1",
                        "--cpu-channel", "fan3", "--case-channel", "fan3"],
                       capture_output=True, text=True)
    ok("双通道同名 rc≠0", p.returncode != 0)
    # ── 8. --auto-uid：device UID 漂移自愈 ───────────────────────────────
    #  真机症状：CoolerControl Web UI 打开 Mode 时「设备/通道/设置」一片空白。
    #  ★ 99% 不是模板结构问题，而是 daemon 重算/升级后 UID 漂移，
    #    modes.json 的键对不上任何设备。--auto-uid 按【设备名】反查真值自愈。
    sec("[8] --auto-uid（device UID 漂移自愈）")
    import hashlib
    base = os.path.join(TMPROOT, "base")
    rc, out = run_render(["--host", "hs-afs-r1"], base)
    ok("基准渲染 rc=0", rc == 0, out[-300:])
    with open(os.path.join(base, "config.toml"), encoding="utf-8") as f:
        cfg_text = f.read()
    uids = sorted(set(re.findall(r"[0-9a-f]{64}", cfg_text)))
    drift_text = cfg_text
    for u in uids:                      # 模拟 daemon 升级后全部 UID 漂移
        drift_text = drift_text.replace(
            u, hashlib.sha256(("drift" + u).encode()).hexdigest())
    drift_dir = os.path.join(TMPROOT, "drift")
    os.makedirs(drift_dir, exist_ok=True)
    with open(os.path.join(drift_dir, "config.toml"), "w", encoding="utf-8") as f:
        f.write(drift_text)

    devmap, err = rc_mod.parse_devices_section(
        os.path.join(drift_dir, "config.toml"))
    ok("能解析 [devices] 段", bool(devmap), err)
    truth = {n: u for u, n in (devmap or {}).items()}

    d2 = os.path.join(TMPROOT, "auto")
    rc, out = run_render(["--host", "hs-afs-r1", "--auto-uid",
                          "--config-dir", drift_dir], d2)
    ok("--auto-uid rc=0", rc == 0, out[-500:])
    doc, modes, cfg, mtext = load(d2)
    used = next(iter(modes["modes"][0]["all_device_settings"]))
    ok("modes.json 用漂移后的 it8613 UID", used == truth.get("it8613"),
       f"{used[:12]} vs {str(truth.get('it8613'))[:12]}")
    ok("device-settings 用同一个 UID", set(doc["device-settings"].keys()) == {used},
       str(set(doc["device-settings"].keys())))
    # ★ [devices] 段若仍写旧 UID，产物内部会自相矛盾，排查脚本会误报「UID 漂移」
    ok("[devices] 段同步为新 UID（产物内部自洽）",
       re.search(r'^\s*"?%s"?\s*=\s*"it8613"' % used, cfg, flags=re.M) is not None)
    cpu_g = next(p for p in doc["profiles"] if p["uid"] == "hs-cpu-graph")
    case_g = next(p for p in doc["profiles"] if p["uid"] == "hs-case-graph")
    ok("CPU 曲线温度源 UID 已对齐",
       cpu_g["temp_source"]["device_uid"] == truth.get("Intel(R) Core(TM) i3-N305"))
    ok("机箱曲线温度源 UID 已对齐",
       case_g["temp_source"]["device_uid"] == truth.get("i915"))
    # 负例：没有 config.toml / 风扇设备名找不到 —— 都必须报错而不是静默用旧值
    empty_cc = os.path.join(TMPROOT, "emptycc")
    os.makedirs(empty_cc, exist_ok=True)
    rc, out = run_render(["--host", "hs-afs-r1", "--auto-uid",
                          "--config-dir", empty_cc], os.path.join(TMPROOT, "neg1"))
    ok("无 config.toml 时 rc≠0", rc != 0)
    rc, out = run_render(["--host", "hs-afs-r1", "--auto-uid",
                          "--config-dir", drift_dir,
                          "--fan-device-name", "no-such-device"],
                         os.path.join(TMPROOT, "neg2"))
    ok("风扇设备名找不到时 rc≠0", rc != 0)

    # ── 9. 探测脚本含 modes 诊断子命令 ──────────────────────────────────
    sec("[9] probe-coolercontrol.sh modes 诊断")
    probe = os.path.join(CC_DIR, "probe-coolercontrol.sh")
    ptxt = open(probe, encoding="utf-8").read()
    ok("支持 modes 子命令", re.search(r'^\s*if \[ "\$\{1:-\}" = "modes" \]',
                                     ptxt, flags=re.M) is not None)
    ok("诊断会比对 daemon 真值 [devices] 段", "[devices]" in ptxt)
    ok("诊断会提示 --auto-uid 修复", "--auto-uid" in ptxt)
    ok("诊断能识别未渲染的占位符", "未渲染" in ptxt)

    # ── 10. --auto-channels：真机通道名是 fanN，不是 sysfs 的 pwmN ────────
    #  ★ 真机实测（用户 NAS 的 /etc/coolercontrol/config.toml）：
    #    [device-settings.71b063a3…] fan2 = { profile_uid = "hs-case-graph" }
    #                                fan3 = { profile_uid = "hs-cpu-graph" }
    #    ⇒ CoolerControl 的通道名 = fanN；按 pwm1/pwm2 写死 ⇒ UI 里 Mode 内容空白。
    sec("[10] --auto-channels（真机通道名 fanN）")
    real_dir = os.path.join(TMPROOT, "real")
    os.makedirs(real_dir, exist_ok=True)
    real_cfg = """[devices]
71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742 = "it8613"
336d28e0c7580fdf9135eb62a4626ff7c3cbfdfef20a61d19fc92198f26bc7bd = "Intel(R) Core(TM) i3-N305"
58cea05ead42a096567b4a1d62ed932c28b0a81abdf9ded743e0645afc0a4818 = "i915"

[device-settings.71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742]
fan2 = { profile_uid = "hs-case-graph" }
fan3 = { profile_uid = "hs-cpu-graph" }

[settings.71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742.channel_settings]
fan5 = { disabled = true }
fan4 = { disabled = true }
"""
    with open(os.path.join(real_dir, "config.toml"), "w", encoding="utf-8") as f:
        f.write(real_cfg)
    d3 = os.path.join(TMPROOT, "autoch")
    rc, out = run_render(["--host", "hs-afs-r1", "--auto-channels",
                          "--config-dir", real_dir], d3)
    ok("--auto-channels rc=0", rc == 0, out[-500:])
    doc, modes, cfg, mtext = load(d3)
    ok("CPU 通道学到 fan3", set(doc["device-settings"].values().__iter__().__next__())
       >= {"fan3"}, str(list(doc["device-settings"].values())[0].keys()))
    ok("机箱通道学到 fan2", "fan2" in list(doc["device-settings"].values())[0])
    ok("hs-smart: fan3→CPU 曲线、fan2→机箱曲线",
       smart_chans(modes) == {"fan3": "hs-cpu-graph", "fan2": "hs-case-graph"},
       str(smart_chans(modes)))
    ok("每个 Mode 都覆盖 fan3+fan2",
       all(v == {"fan3", "fan2"} for v in all_mode_channels(modes).values()))
    # --auto = 两者都做（这份真机 config 没有漂移，通道应保持不变）
    d4 = os.path.join(TMPROOT, "autoboth")
    rc, out = run_render(["--host", "hs-afs-r1", "--auto",
                          "--config-dir", real_dir], d4)
    ok("--auto rc=0", rc == 0, out[-500:])
    doc4, modes4, _, _ = load(d4)
    ok("--auto 也学到 fan3/fan2",
       set(list(doc4["device-settings"].values())[0]) == {"fan2", "fan3"})
    ok("--auto 保留真机 device_uid",
       "71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742"
       in doc4["device-settings"])
    # 禁用通道不应被选中
    ds_text = open(os.path.join(d4, "config.toml"), encoding="utf-8").read()
    ok("不会把已禁用的 fan4/fan5 当通道",
       not re.search(r'"fan[45]"\s*=\s*\{', ds_text.split("[device-settings]")[1]))

    # ── 11. 探测脚本能识别「通道名对不上」 ──────────────────────────────
    sec("[11] probe-coolercontrol.sh 的通道检查")
    ok("诊断会列出真机已知通道", "真机已知通道" in ptxt)
    ok("诊断能报「不是真机已知通道」", "不是真机已知通道" in ptxt)
    ok("诊断提示 --auto（而非只提示 --auto-uid）", "--auto --config-dir" in ptxt)
    ok("探测脚本说明通道名是 fanN 而非 pwmN", "不是 sysfs 里的 pwmN" in ptxt)

    # ── 12. 档位 ↔ Mode 绑定解耦（用户在 UI 新建模式后仍要能同步）────────
    #   背景：用户在 CoolerControl Web UI 里自己建的 Mode，uid 是随机 UUID，
    #   不是 hs-smart —— 写死 uid 会让屏幕切档直接失败（「屏幕与 CC 不同步」）。
    sec("[12] fan-mode-map.py：档位绑定不再写死 uid")
    MAP = os.path.join(CC_DIR, "fan-mode-map.py")
    APPLY = os.path.join(CC_DIR, "apply-mode.sh")
    ok("fan-mode-map.py 存在", os.path.isfile(MAP))

    ui_dir = os.path.join(TMPROOT, "uibuilt")
    os.makedirs(ui_dir, exist_ok=True)
    U_SMART = "1a700658-89d7-4d03-8443-c9260d1604d9"
    U_LOW = "57070995-90a9-41cc-b0dd-d88c7f4aac34"
    U_STD = "c89036e4-e6db-4c96-98a8-f8702288959b"
    U_HIGH = "52c9453f-9659-4d5b-b9f8-d768f1e028c7"
    U_MINE = "11111111-2222-3333-4444-555555555555"
    DEV = "71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742"
    modes_ui = {
        "order": [U_SMART, U_LOW, U_STD, U_HIGH, U_MINE],
        "current_active_mode": U_SMART,
        "modes": [
            {"uid": U_SMART, "name": "hs 智能（CPU 曲线 + 机箱曲线）",
             "all_device_settings": {DEV: {"fan3": {"channel_name": "fan3",
                                                    "profile_uid": "hs-cpu-graph"}}}},
            {"uid": U_LOW, "name": "hs 低速 25%",
             "all_device_settings": {DEV: {"fan3": {"channel_name": "fan3",
                                                    "profile_uid": "hs-low"}}}},
            {"uid": U_STD, "name": "hs 标准 55%",
             "all_device_settings": {DEV: {"fan3": {"channel_name": "fan3",
                                                    "profile_uid": "hs-standard"}}}},
            {"uid": U_HIGH, "name": "hs 强劲 100%",
             "all_device_settings": {DEV: {"fan3": {"channel_name": "fan3",
                                                    "profile_uid": "hs-high"}}}},
            # 名字与档位完全无关的自定义模式：自动认领不了，只能 --bind
            {"uid": U_MINE, "name": "我的自定义模式",
             "all_device_settings": {DEV: {"fan3": {"channel_name": "fan3",
                                                    "profile_uid": "hs-low"}}}},
        ],
    }
    ui_modes_p = os.path.join(ui_dir, "modes.json")
    ui_map_p = os.path.join(ui_dir, "hs-fan-mode-map.json")

    def write_modes(cur=None):
        if cur:
            modes_ui["current_active_mode"] = cur
        with open(ui_modes_p, "w", encoding="utf-8") as f:
            json.dump(modes_ui, f, ensure_ascii=False)
        os.utime(ui_modes_p, (time.time() + 1, time.time() + 1))

    def run_map(args):
        env = dict(os.environ, CC_MODE_CONFIG=ui_modes_p, CC_FAN_MODE_MAP=ui_map_p)
        p = subprocess.run([sys.executable, MAP, *args],
                           capture_output=True, text=True, env=env)
        return p.returncode, (p.stdout + p.stderr).strip()

    write_modes()
    rc, out = run_map(["--resolve", "5"])
    ok("UUID Mode（名字 hs 智能…）解析出档位 5",
       rc == 0 and out.endswith(U_SMART), out)
    for m_, want in ((2, U_LOW), (3, U_STD), (4, U_HIGH)):
        rc, out = run_map(["--resolve", str(m_)])
        ok(f"档位 {m_} 解析到用户在 UI 建的 UUID Mode", rc == 0 and out.endswith(want),
           out)

    rc, out = run_map(["--reverse"])
    ok("反向：当前激活 UUID → 档位 5", rc == 0 and out.endswith("5"), out)

    write_modes(U_LOW)
    rc, out = run_map(["--reverse"])
    ok("反向：切到「hs 低速」→ 档位 2", rc == 0 and out.endswith("2"), out)

    # 名字完全无关 → 识别不出（不瞎猜），--bind 之后即可用
    write_modes(U_MINE)
    rc, out = run_map(["--reverse"])
    ok("自定义模式识别不出（不乱猜，返回非 0）", rc != 0, out)
    rc, out = run_map(["--bind", f"2={U_MINE}"])
    ok("--bind 写入映射", rc == 0 and os.path.isfile(ui_map_p), out)
    ok("映射文件内容正确",
       json.load(open(ui_map_p, encoding="utf-8")).get("2") == U_MINE)
    rc, out = run_map(["--resolve", "2"])
    ok("★ 绑定优先：档位 2 现在指向自定义模式", rc == 0 and out.endswith(U_MINE), out)
    rc, out = run_map(["--reverse"])
    ok("★ 绑定后反向也能识别 → 2", rc == 0 and out.endswith("2"), out)
    # 绑完再解绑，验证 --bind 空值可解除
    rc, out = run_map(["--bind", "2="])
    rc, out = run_map(["--resolve", "2"])
    ok("解绑后回落到名字匹配（hs 低速）", rc == 0 and out.endswith(U_LOW), out)

    rc, out = run_map(["--scan"])
    ok("--scan 能跑通且列出全部档位绑定", rc == 0 and "智能" in out, out[-400:])

    # 占位符残留（@DEVICE_UID@）必须被 --scan 抓出来
    raw_dir = os.path.join(TMPROOT, "rawmode")
    os.makedirs(raw_dir, exist_ok=True)
    raw = {"modes": [{"uid": U_SMART, "name": "hs 智能",
                      "all_device_settings": {"@DEVICE_UID@": {}}}],
           "current_active_mode": U_SMART}
    with open(os.path.join(raw_dir, "modes.json"), "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)
    env_raw = dict(os.environ,
                   CC_MODE_CONFIG=os.path.join(raw_dir, "modes.json"),
                   CC_FAN_MODE_MAP=os.path.join(raw_dir, "hs-fan-mode-map.json"))
    p = subprocess.run([sys.executable, MAP, "--scan"],
                       capture_output=True, text=True, env=env_raw)
    ok("--scan 能抓出未渲染的占位符键（@DEVICE_UID@）",
       p.returncode != 0 and "@DEVICE_UID@" in (p.stdout + p.stderr),
       (p.stdout + p.stderr)[-300:])

    # apply-mode.sh 接线
    atxt = open(APPLY, encoding="utf-8", errors="replace").read()
    ok("apply-mode.sh 支持 --scan", "--scan" in atxt)
    ok("apply-mode.sh 支持 --bind", "--bind" in atxt)
    ok("apply-mode.sh 支持 --current（反向）", "--current" in atxt)
    ok("apply-mode.sh 解析交给 fan-mode-map.py", "fan-mode-map.py" in atxt)
    ok("apply-mode.sh 导出 CC_MODE_CONFIG 给子脚本",
       "export CC_MODE_CONFIG" in atxt)
    ok("apply-mode.sh 不再只认 hs-* uid（有名字回退提示）",
       "--bind {fan_mode}" in atxt)
    ok("切换失败时提示用 --scan/--bind 绑定", "--scan" in atxt and "--bind" in atxt)

    # 渲染产物必须自带映射文件（否则屏幕切档只能靠名字蒙）
    d5 = os.path.join(TMPROOT, "withmap")
    rc, out = run_render(["--host", "hs-afs-r1"], d5)
    map_out = os.path.join(d5, "hs-fan-mode-map.json")
    ok("渲染同时写出档位映射文件", rc == 0 and os.path.isfile(map_out), out[-300:])
    ok("映射默认指向 hs-* 四个 Mode",
       os.path.isfile(map_out) and
       json.load(open(map_out, encoding="utf-8")) ==
       {"5": "hs-smart", "2": "hs-low", "3": "hs-standard", "4": "hs-high"})

    # 真跑一次 --scan（有 bash 时）
    bash = shutil.which("bash")
    if bash:
        env2 = dict(os.environ, CC_MODE_CONFIG=ui_modes_p,
                    CC_FAN_MODE_MAP=ui_map_p)
        p = subprocess.run([bash, APPLY, "--scan"], capture_output=True,
                           text=True, env=env2)
        ok("bash apply-mode.sh --scan 可执行且输出档位绑定",
           p.returncode == 0 and "智能" in (p.stdout + p.stderr),
           (p.stdout + p.stderr)[-300:])

    # 「屏幕 ↔ CC 不同步」六环诊断（sync 子命令）
    ok("probe 支持 sync 子命令", '"sync"' in ptxt or "'sync'" in ptxt)
    ok("sync ① 先查工具是不是新版", "工具版本" in ptxt)
    ok("sync ② 查档位绑定", "档位 → Mode 绑定" in ptxt)
    ok("sync ③ 查反向档位", "反向档位" in ptxt)
    ok("sync ④ 查重启通道（改完要有人重启 daemon）", "重启通道" in ptxt)
    ok("sync ⑤ 查中间件是否已重启（新代码跑了没）", "中间件是否已重启" in ptxt)
    ok("sync ⑥ 查自我回滚防护（屏幕切档弹回的根因）",
       "自我回滚防护" in ptxt and "_cc_current_fan_mode" in ptxt)
    ok("sync 提示重跑安装器即可同步（不再是 cp -rn 永不覆盖）",
       "内容比对同步" in ptxt or "cp -f" in ptxt)

    # watch 子命令：实时抓「切了又弹回」
    ok("probe 支持 watch 子命令（实时盯 modes.json）",
       '"watch"' in ptxt or "'watch'" in ptxt)
    ok("watch 说明「3 → 2 → 3 = 自我回滚」", "自我回滚" in ptxt and "3 → 2 → 3" in ptxt)

    # ── 13. --merge-modes（保留 UI 自建 Mode + 纠正「名字与内容不符」）──────
    sec("[13] --merge-modes（保留 UI 自建 Mode，纠正挂错的 profile）")
    UID_S = "1a700658-89d7-4d03-8443-c9260d1604d9"
    UID_L = "57070995-90a9-41cc-b0dd-d88c7f4aac34"
    UID_M = "c89036e4-e6db-4c96-98a8-f8702288959b"
    UID_H = "52c9453f-9659-4d5b-b9f8-d768f1e028c7"
    DEVR = "71b063a3608e1f4f066afff5ba8ad37f92375b73d6005164d9e16752347f8742"

    def _ui_mode(uid, name, p3, p2):
        # junk=True：模拟用户把模板原文直接粘进 UI 建的 Mode（真机上就是这种）
        ads = {DEVR: {"fan3": {"channel_name": "fan3", "profile_uid": p3},
                      "fan2": {"channel_name": "fan2", "profile_uid": p2}}}
        ads["@DEVICE_UID@"] = {
            "@CPU_CHANNEL@": {"channel_name": "@CPU_CHANNEL@",
                              "profile_uid": "@CPU_SMART_PROFILE@"}}
        return {"uid": uid, "name": name, "all_device_settings": ads}

    ui = {"modes": [
        _ui_mode(UID_S, "hs 智能（CPU 曲线 + 机箱曲线）",
                 "hs-cpu-graph", "hs-case-graph"),
        # ★ 真机原样：名字说「低速 25%」，内容却挂着 hs-standard(60%)
        _ui_mode(UID_L, "hs 低速 25%", "hs-standard", "hs-standard"),
        _ui_mode(UID_M, "hs 标准 55%", "hs-standard", "hs-standard"),
        _ui_mode(UID_H, "hs 强劲 100%", "hs-high", "hs-high"),
    ], "order": [UID_S, UID_L, UID_M, UID_H],
        "current_active_mode": UID_S, "previous_active_mode": UID_H}
    ui_p = os.path.join(TMPROOT, "ui-modes.json")
    with open(ui_p, "w", encoding="utf-8") as f:
        json.dump(ui, f, ensure_ascii=False)

    d13 = os.path.join(TMPROOT, "merged")
    rc, out = run_render(["--host", "hs-afs-r1", "--merge-modes", ui_p], d13)
    ok("--merge-modes 渲染成功", rc == 0, out[-400:])
    mj = json.load(open(os.path.join(d13, "modes.json"), encoding="utf-8"))

    def profs_of(obj, name):
        m = next((x for x in obj["modes"] if x["name"] == name), None)
        if not m:
            return None
        return {st["profile_uid"]
                for dev in m["all_device_settings"].values()
                for st in dev.values() if isinstance(st, dict)}

    ok("沿用用户在 UI 里建的 Mode uid（模式不丢）",
       {m["uid"] for m in mj["modes"]} == {UID_S, UID_L, UID_M, UID_H},
       str([m["uid"][:8] for m in mj["modes"]]))
    ok("★「名字=低速、内容=60%」的 Mode 被纠正为 hs-low",
       profs_of(mj, "hs 低速 25%") == {"hs-low"}, str(profs_of(mj, "hs 低速 25%")))
    ok("「标准」Mode 内容 = hs-standard", profs_of(mj, "hs 标准 55%") == {"hs-standard"})
    ok("「强劲」Mode 内容 = hs-high", profs_of(mj, "hs 强劲 100%") == {"hs-high"})
    ok("占位符设备键已被清掉（daemon 认不出的键不能留）",
       all(k == DEVR for m in mj["modes"] for k in m["all_device_settings"]))
    ok("保留 order / current_active_mode",
       mj["order"] == [UID_S, UID_L, UID_M, UID_H]
       and mj["current_active_mode"] == UID_S)
    mpj = json.load(open(os.path.join(d13, "hs-fan-mode-map.json"), encoding="utf-8"))
    ok("映射文件指向用户自建的 UUID（屏幕切档直接命中）",
       mpj == {"5": UID_S, "2": UID_L, "3": UID_M, "4": UID_H}, str(mpj))

    # --rename-modes + --fixed-pct：名字与实际转速对齐
    d14 = os.path.join(TMPROOT, "merged-renamed")
    rc, out = run_render(["--host", "hs-afs-r1", "--merge-modes", ui_p,
                          "--rename-modes", "--fixed-pct", "30,60,100"], d14)
    mj2 = json.load(open(os.path.join(d14, "modes.json"), encoding="utf-8"))
    nn = sorted(m["name"] for m in mj2["modes"])
    ok("--rename-modes 让名字与实际转速一致（不再「名字 25%、实际 60%」）",
       any("30%" in n for n in nn) and any("60%" in n for n in nn)
       and any("100%" in n for n in nn), str(nn))
    cfg2 = open(os.path.join(d14, "config.toml"), encoding="utf-8").read()
    ok("--fixed-pct 30,60,100 已写进 profile",
       "speed_fixed = 30" in cfg2 and "speed_fixed = 60" in cfg2)
    ok("合并后 uid 无重复", len({m["uid"] for m in mj2["modes"]}) == len(mj2["modes"]))

    # 边界：current_active_mode 指向被丢弃的占位符 Mode → 回落智能档，而不是校验失败
    junk_p = os.path.join(TMPROOT, "junk-active.json")
    jd = {"modes": [dict(m) for m in ui["modes"]] + [
        {"uid": "u-junk", "name": "随便建的", "all_device_settings": {
            "@DEVICE_UID@": {"@CPU_CHANNEL@": {"channel_name": "@CPU_CHANNEL@",
                                              "profile_uid": "@P@"}}}}],
        "order": [UID_S, UID_L, UID_M, UID_H, "u-junk"],
        "current_active_mode": "u-junk"}
    with open(junk_p, "w", encoding="utf-8") as f:
        json.dump(jd, f, ensure_ascii=False)
    d15 = os.path.join(TMPROOT, "merged-junkactive")
    rc, out = run_render(["--host", "hs-afs-r1", "--merge-modes", junk_p], d15)
    cur15 = (json.load(open(os.path.join(d15, "modes.json"), encoding="utf-8"))
             ["current_active_mode"]) if rc == 0 else None
    ok("激活项指向被丢弃的占位符 Mode 时回落到智能档（不报错）",
       rc == 0 and cur15 == UID_S, out[-300:])

    # ── 14. 档位内容一致性诊断 + 重启通道健壮性 ─────────────────────────────
    sec("[14] 内容一致性诊断（--scan）与重启通道健壮性")
    env_c = dict(os.environ, CC_MODE_CONFIG=ui_p)
    env_c.pop("CC_FAN_MODE_MAP", None)
    p = subprocess.run([sys.executable, MAP, "--scan"],
                       capture_output=True, text=True, env=env_c)
    txt = p.stdout + p.stderr
    ok("--scan 能报出「档位名字与内容不符」（这正是 60% 的根因）",
       p.returncode != 0 and "内容与档位不符" in txt, txt[-300:])
    ok("--scan 给出 --merge-modes 修复命令", "--merge-modes" in txt)
    env_ok = dict(os.environ, CC_MODE_CONFIG=os.path.join(d13, "modes.json"),
                  CC_FAN_MODE_MAP=os.path.join(d13, "hs-fan-mode-map.json"))
    p2 = subprocess.run([sys.executable, MAP, "--scan"],
                        capture_output=True, text=True, env=env_ok)
    ok("合并后的 modes.json 通过内容一致性检查",
       p2.returncode == 0, (p2.stdout + p2.stderr)[-300:])

    a2 = open(APPLY, encoding="utf-8", errors="replace").read()
    ok("重启通道改为模糊匹配（systemd/容器名被改也能命中）",
       "grep -i -m1 'coolercontrol'" in a2)
    ok("支持 HIKSEMI_FAN_CC_RESTART 自定义重启命令",
       "HIKSEMI_FAN_CC_RESTART" in a2)
    ok("仍保留重启兜底能力（restart_daemon）", "restart_daemon()" in a2)
    ok("sync ④ 提示 HIKSEMI_FAN_CC_RESTART", "HIKSEMI_FAN_CC_RESTART" in ptxt)

    # ── [15] 绑定逻辑加固：key 归一 / 两种写法统一 / 档位校验 / bind_ok ──
    sec("[15] 绑定逻辑加固（key 归一 + uid/名字统一 + 档位校验 + 失效告警）")
    b_dir = os.path.join(TMPROOT, "bindhard")
    os.makedirs(b_dir, exist_ok=True)
    b_modes = os.path.join(b_dir, "modes.json")
    b_map = os.path.join(b_dir, "hs-fan-mode-map.json")
    with open(b_modes, "w", encoding="utf-8") as f:
        json.dump(modes_ui, f, ensure_ascii=False)

    def run_bind(args):
        env = dict(os.environ, CC_MODE_CONFIG=b_modes, CC_FAN_MODE_MAP=b_map)
        p = subprocess.run([sys.executable, MAP, *args],
                           capture_output=True, text=True, env=env)
        return p.returncode, (p.stdout + p.stderr).strip()

    # ① --bind 只接受 5/2/3/4（1 档非法）
    rc, out = run_bind(["--bind", "1=whatever"])
    ok("--bind 拒绝非法档位 1（★ 无 1 档）",
       rc != 0 and "无 1 档" in out, out)
    rc, out = run_bind(["--bind", "9=x"])
    ok("--bind 拒绝档位 9", rc != 0, out)

    # ② 按名字绑定 → 存成 uid（更稳）；随后 key 类型不影响解析
    rc, out = run_bind(["--bind", "2=hs 低速 25%"])
    ok("--bind 按名字 → 解析成 uid 存盘",
       rc == 0 and json.load(open(b_map, encoding="utf-8")).get("2") == U_LOW, out)

    # ③ 绑定指向不存在的 Mode → 非 0 + 明确告警（不静默）
    rc, out = run_bind(["--bind", "4=完全不存在xyz"])
    ok("--bind 指向不存在的 Mode → 非 0 且告警",
       rc != 0 and "找不到" in out, out)

    # ④ --scan 显式标出「绑定失效」并给解除指引
    rc, out = run_bind(["--scan"])
    ok("--scan 标出绑定失效", "绑定失效" in out, out[-400:])
    ok("--scan 给出解除绑定指引", "--bind 4=" in out, out[-400:])
    ok("--scan 标出「显式绑定」来源", "显式绑定" in out, out[-400:])

    # ⑤ --explain 输出 JSON 且与 --scan 同源（reason 带依据）
    rc, out = run_bind(["--explain"])
    try:
        ex = json.loads(out)
        reasons = {e["tier"]: e["reason"] for e in ex}
        ok("--explain 输出四档 JSON", set(reasons) == {5, 2, 3, 4}, out[:200])
        ok("--explain 档位 2 reason=bind", reasons.get(2) == "bind", str(reasons))
        ok("--explain 档位 4 绑定失效(bind_ok=False)",
           any(e["tier"] == 4 and e["bind_ok"] is False for e in ex), out[:400])
    except Exception as e:
        ok("--explain 可解析为 JSON", False, f"{e}: {out[:200]}")

    # ⑥ 解除绑定后回落到名字匹配（reason 不再是 bind）
    run_bind(["--bind", "4="])
    rc, out = run_bind(["--explain"])
    ex = {e["tier"]: e for e in json.loads(out)}
    ok("解除绑定后 reason=name（回落到名字匹配）",
       ex[4]["reason"] == "name" and ex[4]["bind_value"] == "", out[:300])

    # ── [16] "模式改了但 config.toml 没变" 修复 ──────────────────────────────
    #  核心：① ensure_apply_on_boot 必须把键放进 [settings] 段（不是文件头！）
    #        ② config_matches_mode 能正确识别引号写法（段头 uid + 通道键）
    #        ③ 落地脚本给出 exit 5 判据 + 第二重回读
    sec("[16] apply_on_boot 与 config.toml 一致性（模式改了配置没变）")

    a3 = open(APPLY, encoding="utf-8", errors="replace").read()
    ok("apply-mode.sh 新增 ensure_apply_on_boot", "ensure_apply_on_boot()" in a3)
    ok("apply-mode.sh 新增 config_matches_mode", "config_matches_mode()" in a3)
    ok("落地前确保 apply_on_boot=true（配置段位正确）",
       "ensure_apply_on_boot" in a3.replace("ensure_apply_on_boot()", ""))
    ok("config.toml 不符时非零退出（exit 5 判据）", "exit 5" in a3)
    ok("★ 键写进 [settings] 段而非文件头（TOML 语义）",
       "find_sec" in a3 and 'lines.insert(start' in a3)
    ok("识别注释里的 apply_on_boot（不误判）",
       r'^[ \t]*apply_on_boot[ \t]*=[ \t]*true[ \t]*(#.*)?$' in a3)
    ok("probe sync 新增 ⑦ 环",
       "⑦ config.toml 与 modes.json 是否一致" in ptxt)
    ok("probe ⑦ 有 NG 时给出结论（模式改了配置没跟上）",
       "模式改了但 config.toml 没跟上" in ptxt)
    ok("probe ⑦ 提示同步新版 apply-mode.sh（v4：stop→写→start）",
       "v4 切档 = stop daemon" in ptxt or "stop daemon → 写 config.toml → start" in ptxt)
    ok("probe ④ 标注需 stop/start 通道（v4 官方顺序）",
       "stop → 写 config.toml → start" in ptxt or "stop→写→start" in ptxt)

    # 提取 ensure_apply_on_boot 独立可执行体，做行为验证
    fn_m = re.search(r"ensure_apply_on_boot\(\) \{.*?\n\}\n", a3, flags=re.S)
    ok("ensure_apply_on_boot 可提取", bool(fn_m))

    if fn_m:
        eh = os.path.join(TMPROOT, "fn_ab.sh")
        with open(eh, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nset -uo pipefail\n"
                    'CONFIG_TOML="$1"\n' + fn_m.group(0) +
                    "\nensure_apply_on_boot; echo rc=$?\n")

        def run_ab(content):
            p = os.path.join(TMPROOT, "ab.toml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            r = subprocess.run(["bash", eh, p], capture_output=True, text=True)
            return r.returncode, open(p, encoding="utf-8").read()

        # ① 已有 true（在段内）→ 文件不被改动
        c = '[devices]\nfoo = 1\n\n[settings]\napply_on_boot = true\n'
        rc, after = run_ab(c)
        ok("① apply_on_boot 已 true → 不改动文件", after == c, after)

        # ② [settings] 存在但缺键 → 插进 [settings] 段内
        rc, after = run_ab('[devices]\nfoo = 1\n\n[settings]\ntimeout = 5\n')
        lines = after.splitlines()
        si = lines.index("[settings]")
        ok("② 键插进 [settings] 段内（紧跟段头，非文件头）",
           lines[si + 1] == "apply_on_boot = true", after)

        # ③ 无 [settings] 段 → 追加新段
        rc, after = run_ab('[devices]\nfoo = 1\n')
        ok("③ 无 [settings] → 末尾追加 [settings] 段",
           "[settings]" in after and after.rstrip().endswith("apply_on_boot = true"),
           after)

        # ④ false → 就地改 true
        rc, after = run_ab('[settings]\napply_on_boot = false\n')
        ok("④ false → 就地改 true",
           "apply_on_boot = true" in after and "false" not in after, after)

        # ⑤ 注释里的 apply_on_boot 不得被当成真实键
        rc, after = run_ab('# apply_on_boot = true 说明\n[settings]\nx = 1\n')
        body = after.split("[settings]", 1)[1]
        ok("⑤ 注释不误判，真实键落在 [settings] 段内",
           "apply_on_boot = true" in body, after)

        # ⑥ 幂等
        rc, a1 = run_ab('[settings]\ntimeout = 5\n')
        rc, a2 = run_ab(a1)
        ok("⑥ ensure_apply_on_boot 幂等", a1 == a2, a2)

    # config_matches_mode 行为验证（引号写法必须认）
    cm_m = re.search(r"config_matches_mode\(\) \{.*?\n\}\n", a3, flags=re.S)
    ok("config_matches_mode 可提取", bool(cm_m))
    if cm_m:
        ch = os.path.join(TMPROOT, "fn_cm.sh")
        with open(ch, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nset -uo pipefail\n"
                    'MODE_CONFIG="$1"; CONFIG_TOML="$2"; MODE_UID="$3"\n' +
                    cm_m.group(0) + "\nconfig_matches_mode; echo rc=$?\n")
        cm_modes = os.path.join(TMPROOT, "cm_modes.json")
        with open(cm_modes, "w", encoding="utf-8") as f:
            json.dump({"current_active_mode": "A", "modes": [
                {"uid": "A", "name": "curve",
                 "all_device_settings": {"dev1": {"fan3": {"profile_uid": "PC"},
                                                   "fan2": {"profile_uid": "PK"}}}},
                {"uid": "B", "name": "fixed",
                 "all_device_settings": {"dev1": {"fan3": {"speed_fixed": 25},
                                                   "fan2": {"speed_fixed": 25}}}}]}, f)

        def run_cm(cfg, uid):
            p = os.path.join(TMPROOT, "cm.toml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(cfg)
            r = subprocess.run(["bash", ch, cm_modes, p, uid],
                               capture_output=True, text=True)
            return r.stdout.strip()

        ok("⑦ 引号段头+引号通道 判一致（★ 修过的假阴性）",
           run_cm('[device-settings."dev1"]\n"fan3" = { profile_uid = "PC" }\n'
                  '"fan2" = { profile_uid = "PK" }\n', "A") == "rc=0")
        ok("⑧ 内容不符时判不一致",
           run_cm('[device-settings."dev1"]\n"fan3" = { speed_fixed = 25 }\n'
                  '"fan2" = { speed_fixed = 25 }\n', "A") == "rc=1")
        ok("⑨ Fixed 档位自比对一致",
           run_cm('[device-settings."dev1"]\n"fan3" = { speed_fixed = 25 }\n'
                  '"fan2" = { speed_fixed = 25 }\n', "B") == "rc=0")
        ok("⑩ 空 Mode 视为不一致（激活无意义）",
           run_cm('[device-settings."dev1"]\n"fan3" = { speed_fixed = 25 }\n',
                  "ZZZ") == "rc=1")

    # ── [17] 切档 = stop daemon → 写 config.toml + modes.json → start ───────
    #  核心（v4）：① daemon 拥有 config.toml，运行中改会被覆盖 ⇒ 必须 stop→写→start
    #        ② write_device_settings 把 Mode 内容写进 [device-settings]
    #        ③ 与 config_matches_mode 端到端一致（写完即 matches rc=0）
    #        ④ 无 stop/start 通道时，--restart / 环境变量可兜底整条 restart
    sec("[17] 切档 stop → 写 config.toml → start（官方顺序）")

    a4 = open(APPLY, encoding="utf-8", errors="replace").read()
    ok("apply-mode.sh 新增 write_device_settings", "write_device_settings()" in a4)
    ok("★ v4：apply_once 里先 stop 再写（cc_ctl_run stop + STOPPED 标记）",
       "cc_ctl_run stop" in a4 and "STOPPED=1" in a4)
    ok("★ v4：写完再 start（cc_ctl_run start 受 STOPPED 控制）",
       "cc_ctl_run start" in a4 and 'if [ "$STOPPED" = "1" ]' in a4)
    ok("★ v4：无 stop/start 通道时回退到整条 restart（elif RESTART）",
       'elif [ "$RESTART" = "1" ]; then' in a4)
    ok("写两个文件：modes.json（模式层）+ config.toml（生效层）",
       '"$MODE_CONFIG"' in a4 and "write_device_settings" in a4)
    ok("支持 --restart / --no-restart 参数",
       "--restart)" in a4 and "--no-restart)" in a4)
    ok("支持 HIKSEMI_FAN_CC_RESTART_ON_APPLY 环境变量",
       "HIKSEMI_FAN_CC_RESTART_ON_APPLY" in a4)
    ok("不再使用旧的 exit 3/4（切档不依赖重启）",
       "exit 3" not in a4 and "exit 4" not in a4)

    w_m = re.search(r"write_device_settings\(\) \{.*?\n\}\n", a4, flags=re.S)
    ok("write_device_settings 可提取", bool(w_m))
    if w_m:
        wh = os.path.join(TMPROOT, "fn_w.sh")
        with open(wh, "w", encoding="utf-8", newline="\n") as f:
            f.write("#!/usr/bin/env bash\nset -uo pipefail\n"
                    'MODE_CONFIG="$1"; CONFIG_TOML="$2"; MODE_UID="$3"\n' +
                    w_m.group(0) + "\nwrite_device_settings; echo rc=$?\n")
        w_modes = os.path.join(TMPROOT, "w_modes.json")
        with open(w_modes, "w", encoding="utf-8") as f:
            json.dump({"current_active_mode": "std", "modes": [
                {"uid": "std", "name": "hs 标准 55%",
                 "all_device_settings": {"DEV1": {"fan3": {"speed_fixed": 55},
                                                  "fan2": {"speed_fixed": 55}}}},
                {"uid": "turbo", "name": "hs 强劲 100%",
                 "all_device_settings": {"DEV1": {"fan3": {"speed_fixed": 100},
                                                  "fan2": {"speed_fixed": 100}}}},
                {"uid": "smart", "name": "hs 智能",
                 "all_device_settings": {"DEV1": {"fan3": {"profile_uid": "CP"},
                                                  "fan2": {"profile_uid": "KP"}}}}]}, f)

        def run_w(cfg, uid):
            p = os.path.join(TMPROOT, "w.toml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(cfg)
            r = subprocess.run(["bash", wh, w_modes, p, uid],
                               capture_output=True, text=True)
            last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
            return last, open(p, encoding="utf-8").read()

        cfg0 = ('[devices]\nx = 1\n\n[device-settings."DEV1"]\n'
                '"fan3" = { profile_uid = "CP" }\n'
                '"fan2" = { profile_uid = "KP" }\n\n[[profiles]]\nuid = "0"\n\n'
                '[settings]\napply_on_boot = true\n')

        rc, after = run_w(cfg0, "std")
        ok("⑪ Fixed 档写入 speed_fixed",
           rc == "rc=0" and 'speed_fixed = 55' in after, after)
        ok("⑫ 写入后覆盖旧的 profile_uid（不残留）",
           'profile_uid = "CP"' not in after, after)
        ok("⑬ 其它段（[devices]/[[profiles]]/[settings]）原样保留",
           '[devices]' in after and '[[profiles]]' in after
           and 'apply_on_boot = true' in after, after)
        rc, after = run_w(cfg0, "turbo")
        ok("⑭ 切强劲 → speed_fixed = 100",
           'speed_fixed = 100' in after and 'speed_fixed = 55' not in after, after)
        rc, after = run_w(cfg0, "smart")
        ok("⑮ 切智能 → 写回 profile_uid（曲线）",
           'profile_uid = "CP"' in after and 'speed_fixed' not in after, after)

        # 端到端：write → matches 必一致
        cm_m2 = re.search(r"config_matches_mode\(\) \{.*?\n\}\n", a4, flags=re.S)
        if cm_m2:
            ch2 = os.path.join(TMPROOT, "fn_cm2.sh")
            with open(ch2, "w", encoding="utf-8", newline="\n") as f:
                f.write("#!/usr/bin/env bash\nset -uo pipefail\n"
                        'MODE_CONFIG="$1"; CONFIG_TOML="$2"; MODE_UID="$3"\n' +
                        cm_m2.group(0) + "\nconfig_matches_mode; echo rc=$?\n")
            p = os.path.join(TMPROOT, "e2e.toml")
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(cfg0)
            subprocess.run(["bash", wh, w_modes, p, "turbo"], capture_output=True)
            r = subprocess.run(["bash", ch2, w_modes, p, "turbo"],
                               capture_output=True, text=True)
            ok("⑯ 端到端：write 之后 matches 必为 rc=0（写完即一致）",
               r.stdout.strip() == "rc=0", r.stdout.strip())

finally:
    shutil.rmtree(TMPROOT, ignore_errors=True)

print()
print("=" * 60)
print(f" 结果：{PASS} 通过 / {FAIL} 失败")
if FAIL:
    print(" 失败项：")
    for f in FAILED:
        print(f"   - {f}")
print("=" * 60)
sys.exit(1 if FAIL else 0)

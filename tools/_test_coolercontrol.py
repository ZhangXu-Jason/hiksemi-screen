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

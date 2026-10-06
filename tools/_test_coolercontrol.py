#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_test_coolercontrol.py —— CoolerControl 配置渲染器回归测试

覆盖 render-config.py 的核心行为（这些都是「错了就静默不生效/风扇失控」的关键路径）：
  1. 双风扇默认渲染：pwm1→CPU 曲线、pwm2→机箱曲线，两通道都进 device-settings 与每个 Mode
  2. 单风扇 --single-channel：只剩一路，不再出现 pwm2
  3. --no-graph：两条 Graph 都被裁掉，智能档指向 Fixed 兜底
  4. --no-case-graph：只保留 CPU 曲线，pwm2 走 Fixed 兜底
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
    ok("device-settings 覆盖 pwm1+pwm2", set(chans.keys()) == {"pwm1", "pwm2"},
       str(set(chans.keys())))
    ok("pwm1 智能档 → hs-cpu-graph", chans["pwm1"].get("profile_uid") == "hs-cpu-graph")
    ok("pwm2 智能档 → hs-case-graph", chans["pwm2"].get("profile_uid") == "hs-case-graph")
    ok("hs-smart 两路分别指向两条曲线",
       smart_chans(modes) == {"pwm1": "hs-cpu-graph", "pwm2": "hs-case-graph"},
       str(smart_chans(modes)))
    mc = all_mode_channels(modes)
    ok("每个 Mode 都覆盖 pwm1+pwm2",
       all(v == {"pwm1", "pwm2"} for v in mc.values()), str(mc))
    ok("无残留占位符", not re.findall(r"@[A-Z_]+@", cfg + mtext),
       str(set(re.findall(r"@[A-Z_]+@", cfg + mtext))))
    ok("CPU 曲线温度源 = i3-N305",
       any("i3-N305" in str(p.get("temp_source", {}).get("temp_name", ""))
           for p in doc["profiles"] if p.get("uid") == "hs-cpu-graph"))
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
    ok("device-settings 只有 pwm1", set(chans.keys()) == {"pwm1"}, str(set(chans.keys())))
    ok("单风扇智能档 → hs-cpu-graph",
       smart_chans(modes) == {"pwm1": "hs-cpu-graph"}, str(smart_chans(modes)))
    ok("机箱曲线已移除", "hs-case-graph" not in graph_uids(doc))
    ok("机箱兜底 profile 已移除",
       not any(p["uid"] == "hs-case-fallback" for p in doc["profiles"]))
    ok("每个 Mode 只覆盖 pwm1",
       all(v == {"pwm1"} for v in all_mode_channels(modes).values()))

    # ── 3. --no-graph ───────────────────────────────────────────────────
    sec("[3] --no-graph（两路都走 Fixed 兜底）")
    d = os.path.join(TMPROOT, "nog")
    rc, out = run_render(["--host", "hs-afs-r1", "--no-graph"], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    ok("没有任何 Graph profile", graph_uids(doc) == set(), str(graph_uids(doc)))
    ok("智能档两路都指向兜底",
       smart_chans(modes) == {"pwm1": "hs-smart-fallback", "pwm2": "hs-case-fallback"},
       str(smart_chans(modes)))
    ok("仍覆盖双通道", all(v == {"pwm1", "pwm2"}
                      for v in all_mode_channels(modes).values()))

    # ── 4. --no-case-graph ──────────────────────────────────────────────
    sec("[4] --no-case-graph（只保留 CPU 曲线）")
    d = os.path.join(TMPROOT, "nogc")
    rc, out = run_render(["--host", "hs-afs-r1", "--no-case-graph"], d)
    ok("rc = 0", rc == 0, out[-400:])
    doc, modes, cfg, mtext = load(d)
    ok("只剩 CPU 曲线", graph_uids(doc) == {"hs-cpu-graph"}, str(graph_uids(doc)))
    ok("pwm1 走曲线、pwm2 走兜底",
       smart_chans(modes) == {"pwm1": "hs-cpu-graph", "pwm2": "hs-case-fallback"},
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
    ok("pwm2 智能档 → hs-case-fallback",
       smart_chans(modes).get("pwm2") == "hs-case-fallback",
       str(smart_chans(modes)))

    # ── 6. 校验器能抓出坏配置 ────────────────────────────────────────────
    sec("[6] 校验器负例（坏配置必须被拒）")
    # 6a. 首点温度 ≠ temp_min
    bad = open(TPL_CONFIG, encoding="utf-8").read()
    bad = (bad.replace("@DEVICE_UID@", "a" * 64)
              .replace("@CPU_CHANNEL@", "pwm1").replace("@CASE_CHANNEL@", "pwm2")
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
        "@DEVICE_UID@", "a" * 64).replace("@CPU_CHANNEL@", "pwm1").replace(
        "@CASE_CHANNEL@", "pwm2").replace("@CPU_SMART_PROFILE@", "hs-cpu-graph").replace(
        "@CASE_SMART_PROFILE@", "hs-case-graph"))
    modes_obj.pop("_comment", None)
    # 删掉 hs-smart 里的 pwm2
    for m in modes_obj["modes"]:
        if m["uid"] == "hs-smart":
            for dev in m["all_device_settings"].values():
                dev.pop("pwm2", None)
    expect_die(lambda: rc_mod.validate_modes(modes_obj, expect_channels=("pwm1", "pwm2")),
               "拒绝「Mode 漏掉 pwm2 通道」")

    # ── 7. --list-hosts / 参数校验 ──────────────────────────────────────
    sec("[7] CLI 行为")
    p = subprocess.run([sys.executable, RENDER, "--list-hosts"],
                       capture_output=True, text=True)
    ok("--list-hosts rc=0", p.returncode == 0)
    ok("--list-hosts 含 hs-afs-r1", "hs-afs-r1" in p.stdout)
    ok("--list-hosts 显示机箱通道 pwm2", "pwm2" in p.stdout)
    p = subprocess.run([sys.executable, RENDER, "--host", "no-such-host"],
                       capture_output=True, text=True)
    ok("未知机型 rc≠0", p.returncode != 0)
    ok("未知机型给出可选列表", "hs-afs-r1" in (p.stdout + p.stderr))
    p = subprocess.run([sys.executable, RENDER,
                        "--device-uid", "short", "--cpu-channel", "pwm1"],
                       capture_output=True, text=True)
    ok("非法 device-uid rc≠0", p.returncode != 0)
    p = subprocess.run([sys.executable, RENDER, "--host", "hs-afs-r1",
                        "--cpu-channel", "pwm1", "--case-channel", "pwm1"],
                       capture_output=True, text=True)
    ok("双通道同名 rc≠0", p.returncode != 0)
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

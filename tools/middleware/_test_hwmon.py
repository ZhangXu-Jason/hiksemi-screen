#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hwmon 温度传感器解析测试（温度「永远不变」bug 的回归防线）
================================================================
背景（教训 89）：data-mapping.json 里温度配置写的是 "chip": "auto"，
而旧版 resolve_hwmon 把 chip 当【字面芯片名】做 `chip not in name` 匹配，
"auto" 不在任何 hwmon 设备名里 ⇒ 所有设备被跳过 ⇒ 恒定返回 default
⇒ 小屏 CPU/机箱温度永远显示 45/38 不变（而 cpu/内存走实时采集器，正常变化）。

覆盖：
  · chip 字面名匹配 + scale 换算（milli-°C → °C）
  · ★ chip="auto" 必须真的自动挑选（旧 bug 的直接回归）
  · chip 列表按优先级尝试，末尾 auto 兜底
  · auto 跳过值为 0 / 负数 / 非数字的设备
  · exclude 在 auto 阶段生效（防止 CPU 与机箱温度取到同一个源）
  · 匹配不到 / 目录为空 → 用 default
  · ★ 端到端：传感器数值变化后，状态帧的温度必须跟着变（不是恒定值）
  · ★ 两个温度字段不得来自同一个传感器

用法： python3 _test_hwmon.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import middleware as mw

PASS = FAIL = 0

# 各 hwmon 设备布局（hwmon0=coretemp 45℃ / hwmon1=i915 38℃ /
#                    hwmon2=it8613 无 temp1_input / hwmon3=acpitz 值 0 无效）
LAYOUT = {
    "hwmon0": {"name": "coretemp", "temp1_input": "45000"},
    "hwmon1": {"name": "i915", "temp1_input": "38000"},
    "hwmon2": {"name": "it8613", "temp2_input": "41000"},   # 故意没有 temp1_input
    "hwmon3": {"name": "acpitz", "temp1_input": "0"},       # 无效值，应被跳过
}


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def make_hwmon(layout=None, empty=False):
    root = tempfile.mkdtemp(prefix="hwmon_")
    if not empty:
        for dev, files in (layout or LAYOUT).items():
            d = os.path.join(root, dev)
            os.makedirs(d, exist_ok=True)
            for fn, val in files.items():
                with open(os.path.join(d, fn), "w") as f:
                    f.write(val)
    return root


def write_val(root, dev, field, val):
    with open(os.path.join(root, dev, field), "w") as f:
        f.write(str(val))


def main():
    saved_root = mw.HWMON_ROOT
    saved_probed = mw._hwmon_probed
    saved_logged = mw._hwmon_logged
    try:
        root = make_hwmon()
        mw.HWMON_ROOT = root
        mw._hwmon_probed = False
        mw._hwmon_logged = set()

        print("\n【1】chip 字面名匹配 + scale 换算")
        v = mw.resolve_hwmon(
            {"chip": "coretemp", "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("coretemp → 45℃（45000 × 0.001）", v == 45.0, f"实际 {v}")
        v = mw.resolve_hwmon(
            {"chip": "i915", "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("i915 → 38℃", v == 38.0, f"实际 {v}")

        print("\n【2】★ chip=\"auto\" 必须真的自动挑选（旧 bug 回归：曾恒返回 default）")
        v = mw.resolve_hwmon(
            {"chip": "auto", "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("auto 选中了真实设备（不是 default -1）", v == 45.0, f"实际 {v}")
        v = mw.resolve_hwmon(
            {"field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("chip 省略 = auto，同样生效", v == 45.0, f"实际 {v}")

        print("\n【3】chip 列表按优先级尝试")
        v = mw.resolve_hwmon(
            {"chip": ["i915", "coretemp"], "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("列表首项优先（i915 → 38 而非 coretemp 45）", v == 38.0, f"实际 {v}")
        v = mw.resolve_hwmon(
            {"chip": ["nope", "coretemp"], "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("首项不存在则顺延到次项", v == 45.0, f"实际 {v}")
        v = mw.resolve_hwmon(
            {"chip": ["nope", "auto"], "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("末尾 auto 兜底生效", v == 45.0, f"实际 {v}")

        print("\n【4】无效值处理（0 / 负 / 非数字 一律跳过）")
        v = mw.resolve_hwmon(
            {"chip": ["acpitz"], "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("值为 0 的 acpitz 被跳过 → default", v == -1, f"实际 {v}")
        v = mw.resolve_hwmon(
            {"chip": ["acpitz", "auto"], "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("0 值设备后 auto 续选到 coretemp", v == 45.0, f"实际 {v}")
        v = mw.resolve_hwmon(
            {"chip": ["it8613"], "field": "temp1_input", "scale": 0.001, "default": -1}, {})
        check("设备缺该 field（it8613 无 temp1_input）→ default", v == -1, f"实际 {v}")

        print("\n【5】exclude：auto 阶段避开指定源（防两个温度取同一设备）")
        v = mw.resolve_hwmon(
            {"chip": ["auto"], "exclude": ["coretemp"], "field": "temp1_input",
             "scale": 0.001, "default": -1}, {})
        check("排除 coretemp 后 auto 选中 i915 → 38", v == 38.0, f"实际 {v}")

        print("\n【6】匹配不到 / 空目录 → default")
        v = mw.resolve_hwmon(
            {"chip": "nope", "field": "temp1_input", "scale": 0.001, "default": 42}, {})
        check("芯片名不存在 → default 42", v == 42, f"实际 {v}")
        empty_root = make_hwmon(empty=True)
        mw.HWMON_ROOT = empty_root
        mw._hwmon_probed = False
        v = mw.resolve_hwmon(
            {"chip": ["coretemp", "auto"], "field": "temp1_input", "scale": 0.001, "default": 42}, {})
        check("hwmon 目录为空 → default 42", v == 42, f"实际 {v}")
        shutil.rmtree(empty_root, ignore_errors=True)
        mw.HWMON_ROOT = root
        mw._hwmon_probed = False

        print("\n【7】★ 端到端：传感器值变化 → 状态帧温度必须跟着变")
        spec = {
            "source": "builder", "name": "dev_status", "fanmode": "state",
            "cpu_temp": {"source": "hwmon", "chip": ["coretemp", "x86_pkg_temp", "k10temp", "auto"],
                         "field": "temp1_input", "scale": 0.001, "default": 45, "_label": "CPU温度"},
            "temperature": {"source": "hwmon", "chip": ["i915", "it8613", "auto"],
                            "exclude": ["coretemp"],
                            "field": "temp1_input", "scale": 0.001, "default": 38, "_label": "机箱温度"},
        }

        class C:
            def __init__(self, d):
                self.d = d

            def get(self, k, default=None):
                return self.d.get(k, default)

        ctx = {"collectors": C({"cpu_percent": 12.3, "mem_percent": 40.1,
                                "net_up": 10.0, "net_down": 20.0})}

        r1 = mw.build_dev_status(spec, ctx)
        check("首次：CPU 温度 = 45（来自 coretemp 45000）", r1["cpu_temp"] == 45, f"实际 {r1['cpu_temp']}")
        check("首次：机箱温度 = 38（来自 i915 38000）", r1["temperature"] == 38, f"实际 {r1['temperature']}")
        check("★ 两个温度不同源（值不相等）", r1["cpu_temp"] != r1["temperature"],
              f"cpu={r1['cpu_temp']} case={r1['temperature']}")
        check("cpu/内存仍是裸数值（非字符串）",
              isinstance(r1["cpu"], int) and isinstance(r1["memory"], int),
              f"{type(r1['cpu']).__name__}/{type(r1['memory']).__name__}")

        # 改传感器数值（模拟真机升温）
        write_val(root, "hwmon0", "temp1_input", 55000)   # 45 → 55
        write_val(root, "hwmon1", "temp1_input", 48000)   # 38 → 48
        r2 = mw.build_dev_status(spec, ctx)
        check("★ 传感器升温后 CPU 温度跟着变（45 → 55）", r2["cpu_temp"] == 55, f"实际 {r2['cpu_temp']}")
        check("★ 传感器升温后机箱温度跟着变（38 → 48）", r2["temperature"] == 48, f"实际 {r2['temperature']}")
        check("★ 温度确实发生了变化（不是恒定 default）",
              (r2["cpu_temp"], r2["temperature"]) != (r1["cpu_temp"], r1["temperature"]),
              f"{r1['cpu_temp']}/{r1['temperature']} → {r2['cpu_temp']}/{r2['temperature']}")

        # 降温
        write_val(root, "hwmon0", "temp1_input", 39000)
        r3 = mw.build_dev_status(spec, ctx)
        check("★ 降温也同步（55 → 39）", r3["cpu_temp"] == 39, f"实际 {r3['cpu_temp']}")

        print("\n【8】仓库默认配置本身必须有效（防配置回退成裸 \"auto\" 之外的死值）")
        cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data-mapping.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        spec_cfg = cfg["actions"]["get_hw_resource_info"]
        for k in ("cpu_temp", "temperature"):
            s = spec_cfg[k]
            chip = s.get("chip")
            ok_chip = (chip == "auto") or (isinstance(chip, list) and "auto" in chip)
            check(f"{k}.chip 含 auto 兜底", ok_chip, f"实际 {chip}")
            check(f"{k}.field 存在", bool(s.get("field")), str(s))
            check(f"{k}.default 存在", "default" in s, str(s))
    finally:
        mw.HWMON_ROOT = saved_root
        mw._hwmon_probed = saved_probed
        mw._hwmon_logged = saved_logged

    print("=" * 46)
    # ★ 结尾必须是「结果：N 通过 / M 失败」—— tools/test.sh 靠这一行统计
    print(f" 结果：{PASS} 通过 / {FAIL} 失败 / 共 {PASS + FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

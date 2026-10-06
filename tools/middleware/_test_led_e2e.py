#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LED 端到端测试（HTTP 全链路，桩 I2C）
==========================================
不起真 TLS：直接构造 Handler 语义的请求走 build_response，
再叠加桩 _i2c_io 验证"屏幕操作 → 中间件 → I2C 硬件"闭环。

覆盖：
  · set_light_theme / set_atmo_light / set_atmo_color → 硬件寄存器
  · get_light_theme / get_atmo_light ← 硬件真值
  · 写后读一致（往返）
  · 客户端 4 种枚举 → 硬件模式 + RGB 全映射

用法： python3 _test_led_e2e.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import middleware as mw

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


# ── 复用 _test_led_i2c 的桩 ──
from _test_led_i2c import FakeDevice, FakeI2CIO, _DummyLed


def build(action, params=None, body=None):
    """模拟 Handler 调 build_response"""
    return mw.build_response(action, params or {}, body or {})


def main():
    print("=" * 60)
    print(" LED 端到端测试（HTTP 语义 + 桩 I2C）")
    print("=" * 60)

    # 加载真实配置（data-mapping.json）
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data-mapping.json")
    mw.CTX["config"] = mw.Config(cfg_path)

    # 桩 I2C：总线 1 挂 0x26
    dev = FakeDevice(0x26, present=True, init={0x90: 0x00})
    io = FakeI2CIO({1: dev})
    mw._i2c_io = io
    mw._I2C_BUS_CACHE["bus"] = 1
    mw._I2C_BUS_CACHE["checked_at"] = 9e18
    mw.LED_HW_BACKEND = "i2c"

    # LED 状态用临时文件
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    led = mw.LedState(path=p)
    mw.CTX["led"] = led

    # ── 1. set_atmo_light 关灯 → 硬件 off ──
    print("\n【1】set_atmo_light light_mode=0 → I2C 0x90=0x0")
    dev.writes.clear()
    r = build("set_atmo_light", {"light_mode": 0}, {})
    check("返回 code=0", r.get("code") == 0, r)
    check("硬件 0x90=0x0", dev.regs.get(0x90) == 0x00, dev.regs)

    # ── 2. 屏幕选「白色」→ 先 RGB 后 solid ──
    print("\n【2】set_atmo_color 白 → RGB(255,255,255) + 0x90=0x1")
    led.update(status=1, light_mode=9)   # 先确保开灯状态（§1 关了灯）
    dev.writes.clear()
    r = build("set_atmo_color", {},
              {"color_id": 3, "color_r": 255, "color_g": 255, "color_b": 255})
    check("返回 code=0", r.get("code") == 0, r)
    regs = [w[0] for w in dev.writes]
    check("先写 RGB 后写模式", regs and regs[-1] == 0x90 and 0x92 in regs, regs)
    check("R/G/B = 255/255/255",
          (dev.regs.get(0x92), dev.regs.get(0x93), dev.regs.get(0x94)) == (255, 255, 255))
    check("模式 = solid(0x1)", dev.regs.get(0x90) == 0x01)

    # ── 3. 读接口反映硬件真值 ──
    print("\n【3】get_light_theme 读硬件真值")
    r = build("get_light_theme", {}, {})
    check("status=1（开）", r.get("status") == 1, r)
    check("color_id=3（白）", r.get("color_id") == 3, r)

    # ── 4. set_atmo_light 开灯（light_mode=9）──
    print("\n【4】set_atmo_light light_mode=9 → 开")
    dev.regs[0x90] = 0x00     # 先强制关
    r = build("set_atmo_light", {"light_mode": 9}, {})
    check("返回 code=0", r.get("code") == 0, r)
    check("硬件被点亮（0x90 != 0）", dev.regs.get(0x90) != 0x00, dev.regs)

    # ── 5. 全枚举 → 硬件模式映射 ──
    print("\n【5】客户端枚举 → 硬件模式全映射")
    cases = [
        (1000, 0x02, "随主题→rainbow"),
        (1001, 0x03, "动态色→breathing"),
        (1002, 0x01, "蓝(bug色)→solid"),
        (1003, 0x01, "白→solid"),
        (1004, 0x01, "黄→solid"),
    ]
    for enum, expect_mode, desc in cases:
        cid = enum - 1000
        e = mw.LED_EFFECT_COLOR.get(enum)
        _, rr, gg, bb = e
        mw._apply_i2c({"status": 1, "color_id": cid,
                       "color_r": rr, "color_g": gg, "color_b": bb})
        got = dev.regs.get(0x90)
        check(f"{enum} {desc} → 0x90=0x{expect_mode:02x}",
              got == expect_mode, f"实际 0x{got:02x}")
        if expect_mode == 0x01:
            check(f"  {enum} RGB 落地 = ({rr},{gg},{bb})",
                  (dev.regs.get(0x92), dev.regs.get(0x93), dev.regs.get(0x94))
                  == (rr, gg, bb))

    # ── 6. 写后读往返一致 ──
    print("\n【6】写后读往返一致（黄色 1004）")
    mw._apply_i2c({"status": 1, "color_id": 4, "color_r": 255, "color_g": 90, "color_b": 0})
    r = build("get_atmo_light", {}, {})
    check("读回 color_id=4", r.get("color_id") == 4, r)
    check("读回 status=1", r.get("status") == 1, r)

    # 清理
    try:
        os.remove(p)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f" 结果：{PASS} 通过 / {FAIL} 失败 / 共 {PASS + FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LED 状态逻辑测试（不依赖真机）
================================
验证 middleware.py 的 LED 读写接口是否与客户端契约一致：
  · get_light_theme / get_atmo_light 返回 {code,status,mode,color_id}
  · set_atmo_light?light_mode=N  → status 0/1
  · set_light_theme?mode=N       → mode
  · set_atmo_color POST JSON     → color_id/r/g/b
  · 持久化：写后重启 LedState 仍能读回
  · 客户端解析复刻：status/mode/color_id → LightEffect 枚举

用法： python3 _test_led_state.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import middleware as mw

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def fresh_led():
    """每个用例用独立临时状态文件"""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    return mw.LedState(path=path), path


def parse_client(status, mode, color_id):
    """★ 逐字复刻客户端 get_light_theme 的解析逻辑"""
    if status == 0:
        return 999                       # LightEffectTurnOff
    if mode == 0:
        return 1000                      # LightEffectMatchTheme
    return color_id + 1000               # 1000~1004


def main():
    print("=" * 60)
    print(" LED 状态逻辑测试")
    print("=" * 60)

    # ── 1. 默认状态 ──
    print("\n【1】默认状态")
    led, path = fresh_led()
    st = led.get()
    check("默认 status=1（开启）", st["status"] == 1)
    check("默认 mode=1", st["mode"] == 1)
    check("默认 color_id=0", st["color_id"] == 0)
    r = mw.build_led_state({}, {"led": led})
    check("读接口返回 code=0", r["code"] == 0)
    check("读接口含 status/mode/color_id",
          all(k in r for k in ("status", "mode", "color_id")))

    # ── 2. set_atmo_light ──
    print("\n【2】set_atmo_light（开关灯）")
    led, path = fresh_led()
    ctx = {"led": led, "_params": {"light_mode": "9"}, "_body": {}}
    r = mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
    check("返回 code=0", r == 0)
    check("light_mode=9 → status=1", led.get()["status"] == 1)
    ctx["_params"] = {"light_mode": "0"}
    mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
    check("light_mode=0 → status=0（关灯）", led.get()["status"] == 0)

    # ── 3. set_light_theme ──
    print("\n【3】set_light_theme（模式）")
    led, path = fresh_led()
    ctx = {"led": led, "_params": {"mode": "0"}, "_body": {}}
    mw.resolve_led_write({"_write": "set_light_theme"}, ctx)
    check("mode=0 → 随主题", led.get()["mode"] == 0)
    ctx["_params"] = {"mode": "1"}
    mw.resolve_led_write({"_write": "set_light_theme"}, ctx)
    check("mode=1 → 其他", led.get()["mode"] == 1)

    # ── 4. set_atmo_color（POST body）──
    print("\n【4】set_atmo_color（POST JSON body）")
    led, path = fresh_led()
    ctx = {"led": led, "_params": {}, "_body": {"color_id": 2, "color_r": 30,
                                                "color_g": 180, "color_b": 25}}
    r = mw.resolve_led_write({"_write": "set_atmo_color"}, ctx)
    check("返回 code=0", r == 0)
    st = led.get()
    check("color_id=2", st["color_id"] == 2)
    check("★ 1002蓝原样透传 (30,180,25) == 源码 bug 值",
          (st["color_r"], st["color_g"], st["color_b"]) == (30, 180, 25))

    # ── 5. 全枚举颜色映射一致性 ──
    print("\n【5】LightEffect 全枚举 → RGB 映射一致性")
    for eid, (cid, r_, g_, b_) in mw.LED_EFFECT_COLOR.items():
        led, path = fresh_led()
        ctx = {"led": led, "_params": {}, "_body": {"color_id": cid, "color_r": r_,
                                                    "color_g": g_, "color_b": b_}}
        mw.resolve_led_write({"_write": "set_atmo_color"}, ctx)
        st = led.get()
        ok = (st["color_id"], st["color_r"], st["color_g"], st["color_b"]) == (cid, r_, g_, b_)
        check(f"枚举 {eid} → color_id={cid} rgb=({r_},{g_},{b_})", ok,
              f"实际 {st}")

    # ── 5b. ★★ set_atmo_color 不得改变开关状态（真机 bug）──
    print("\n【5b】★★ set_atmo_color 只改颜色、不动开关（修复点颜色先关灯）")
    # 场景复刻客户端 setLightEffect：当前关灯 → 先设色 → 再开灯
    led, path = fresh_led()
    mw.resolve_led_write({"_write": "set_atmo_light"},
                         {"led": led, "_params": {"light_mode": "0"}, "_body": {}})
    check("① 先关灯 → status=0", led.get()["status"] == 0)
    # ② 设色（body 里客户端【不带 status】，但我们强制保留原 status）
    mw.resolve_led_write({"_write": "set_atmo_color"},
                         {"led": led, "_params": {},
                          "_body": {"color_id": 0, "color_r": 123,
                                    "color_g": 123, "color_b": 123}})
    st = led.get()
    check("② 设色后 status 仍为 0（未被误改成 1）", st["status"] == 0, st)
    check("② 设色后 color_id 已更新为 0", st["color_id"] == 0, st)
    # ③ 再开灯
    mw.resolve_led_write({"_write": "set_atmo_light"},
                         {"led": led, "_params": {"light_mode": "9"}, "_body": {}})
    check("③ 开灯后 status=1", led.get()["status"] == 1)

    # 反向：亮灯时改颜色 → status 保持 1
    led, path = fresh_led()
    mw.resolve_led_write({"_write": "set_atmo_light"},
                         {"led": led, "_params": {"light_mode": "9"}, "_body": {}})
    mw.resolve_led_write({"_write": "set_atmo_color"},
                         {"led": led, "_params": {},
                          "_body": {"color_id": 3, "color_r": 255,
                                    "color_g": 255, "color_b": 255}})
    st = led.get()
    check("亮灯时改色 → status 仍为 1", st["status"] == 1, st)
    check("亮灯时改色 → color_id=3", st["color_id"] == 3, st)

    # ── 6. 持久化（模拟重启）──
    print("\n【6】持久化：写后重建 LedState 能读回")
    led, path = fresh_led()
    ctx = {"led": led, "_params": {"light_mode": "0"}, "_body": {}}
    mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
    ctx = {"led": led, "_params": {}, "_body": {"color_id": 3, "color_r": 255,
                                                "color_g": 255, "color_b": 255}}
    mw.resolve_led_write({"_write": "set_atmo_color"}, ctx)
    led2 = mw.LedState(path=path)          # 重建 = 模拟重启
    st = led2.get()
    check("重启后 status 保留 =0", st["status"] == 0)
    check("重启后 color_id 保留 =3", st["color_id"] == 3)
    check("重启后 rgb 保留 =(255,255,255)",
          (st["color_r"], st["color_g"], st["color_b"]) == (255, 255, 255))

    # ── 7. 客户端解析复刻 ──
    print("\n【7】客户端 get_light_theme 解析复刻")
    cases = [
        ("关闭",       dict(status=0, mode=1, color_id=0), 999),
        ("随主题",     dict(status=1, mode=0, color_id=0), 1000),
        ("蓝(bug绿)",  dict(status=1, mode=1, color_id=2), 1002),
        ("白",         dict(status=1, mode=1, color_id=3), 1003),
        ("黄",         dict(status=1, mode=1, color_id=4), 1004),
    ]
    for label, r, want in cases:
        got = parse_client(r["status"], r["mode"], r["color_id"])
        check(f"{label}: status={r['status']} mode={r['mode']} cid={r['color_id']} → {want}",
              got == want, f"实际 {got}")

    # ── 8. 往返一致性（写→读→客户端解析）──
    print("\n【8】往返一致性：模拟屏幕选色 → 写 → 读 → 复现枚举")
    for eid in (999, 1000, 1001, 1002, 1003, 1004):
        led, path = fresh_led()
        if eid == 999:
            ctx = {"led": led, "_params": {"light_mode": "0"}, "_body": {}}
            mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
        else:
            if eid == 1000:
                ctx = {"led": led, "_params": {"mode": "0"}, "_body": {}}
                mw.resolve_led_write({"_write": "set_light_theme"}, ctx)
                ctx = {"led": led, "_params": {"light_mode": "9"}, "_body": {}}
                mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
            else:
                cid, r_, g_, b_ = mw.LED_EFFECT_COLOR[eid]
                ctx = {"led": led, "_params": {"light_mode": "9"}, "_body": {}}
                mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
                ctx = {"led": led, "_params": {}, "_body": {"color_id": cid, "color_r": r_,
                                                            "color_g": g_, "color_b": b_}}
                mw.resolve_led_write({"_write": "set_atmo_color"}, ctx)
        resp = mw.build_led_state({}, {"led": led})
        back = parse_client(resp["status"], resp["mode"], resp["color_id"])
        check(f"枚举 {eid} 往返一致（读到 {back}）", back == eid)

    # ── 9. 异常输入健壮性 ──
    print("\n【9】异常输入健壮性")
    led, path = fresh_led()
    ctx = {"led": led, "_params": {"light_mode": "abc"}, "_body": {}}
    try:
        mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
        check("非法 light_mode 不崩", True)
    except Exception as e:
        check("非法 light_mode 不崩", False, str(e))
    ctx = {"led": led, "_params": {}, "_body": {"color_id": None}}
    try:
        r = mw.resolve_led_write({"_write": "set_atmo_color"}, ctx)
        check("空 color_id 不崩且返回 code=0", r == 0)
    except Exception as e:
        check("空 color_id 不崩", False, str(e))
    ctx = {"led": None, "_params": {}, "_body": {}}
    r = mw.resolve_led_write({"_write": "set_atmo_light"}, ctx)
    check("led 缺失时安全返回 code=0", r == 0)

    # ── 10. apply_to_hardware 默认 no-op ──
    print("\n【10】apply_to_hardware 默认 no-op")
    check("LED_HW_BACKEND == 'none'", mw.LED_HW_BACKEND == "none")
    check("apply_to_hardware 返回 True", mw.apply_to_hardware(dict(mw.LED_DEFAULTS)) is True)

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print(f" 结果：{PASS} 通过 / {FAIL} 失败 / 共 {PASS + FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

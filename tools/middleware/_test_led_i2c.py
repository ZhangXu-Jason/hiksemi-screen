#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
I2C 灯光后端测试（桩测试，不需要真机）
==========================================
用假的 _i2c_io 注入点（模拟 /dev/i2c-N + 0x26 从设备）验证：
  · _i2c_probe_bus 动态探测（不硬编码总线号）
  · _apply_i2c 的寄存器映射与【写入顺序】（先 RGB 后模式）
  · led_read_hardware 反解
  · build_led_state 硬件真值优先
  · 无 I2C 通道时退回内部持久化状态

用法： python3 _test_led_i2c.py
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


class FakeDevice:
    """模拟一个 I2C 从设备（如 0x26 RGB 控制器）"""

    def __init__(self, addr, present=True, init=None):
        self.addr = addr
        self.present = present
        self.regs = dict(init or {})
        self.writes = []          # [(reg, value), ...] 顺序敏感
        self._pending = 0

    def select(self):
        if not self.present:
            raise OSError(6, "No such device or address")

    def write(self, data):
        if not self.present:
            raise OSError(6, "ENXIO")
        if len(data) == 2:
            self.regs[data[0]] = data[1]
            self.writes.append((data[0], data[1]))
        elif len(data) == 1:
            self._pending = data[0]

    def read(self, n):
        if not self.present:
            raise OSError(6, "ENXIO")
        return bytes([self.regs.get(self._pending, 0)])[:n]


class FakeI2CIO:
    """替换 middleware._i2c_io —— 完全内存实现，不碰真 os/fcntl"""

    def __init__(self, devices):
        self.devices = devices      # {bus_no: FakeDevice}
        self._fds = {}              # fd -> device
        self._counter = 100
        self.opened = []

    def list_buses(self):
        return sorted(b for b, d in self.devices.items() if d.present)

    def open(self, bus):
        dev = self.devices.get(bus)
        if dev is None or not dev.present:
            raise OSError(2, "No such file or directory")
        self._counter += 1
        self._fds[self._counter] = dev
        self.opened.append(bus)
        return self._counter

    def close(self, fd):
        self._fds.pop(fd, None)

    def select(self, fd):
        dev = self._fds.get(fd)
        if dev is None:
            raise OSError(9, "Bad file descriptor")
        dev.select()

    def write(self, fd, data):
        dev = self._fds.get(fd)
        if dev is None:
            raise OSError(9, "Bad file descriptor")
        dev.write(data)

    def read(self, fd, n):
        dev = self._fds.get(fd)
        if dev is None:
            raise OSError(9, "Bad file descriptor")
        return dev.read(n)


class _DummyLed:
    def get(self):
        return dict(mw.LED_DEFAULTS)


def main():
    print("=" * 60)
    print(" I2C 灯光后端测试（桩）")
    print("=" * 60)
    real_os = os

    # ── 1. 动态探测：总线 0 有别的设备（0x50），总线 1 才是 0x26 ──
    #     注：FakeDevice(0x50) 的 select() 也会成功，所以把 0x50 设为
    #     present 但让它 select 失败（模拟"总线上不是我们要的地址"）。
    print("\n【1】动态探测总线（★ 不硬编码总线号）")

    class WrongAddrDevice(FakeDevice):
        def select(self):
            raise OSError(6, "No such device or address")   # 不是 0x26

    dev0 = WrongAddrDevice(0x50, present=True)              # 总线 0：有设备但不是灯控
    dev1 = FakeDevice(0x26, present=True, init={0x90: 0x00})
    io = FakeI2CIO({0: dev0, 1: dev1})
    mw._i2c_io = io
    mw._I2C_BUS_CACHE["bus"] = None

    found = mw._i2c_probe_bus()
    check("探测到总线 1（跳过无 0x26 的总线 0）", found == 1, f"实际 {found}")

    # ── 1b. 只写寄存器（读返回 ENXIO）也要能命中（回退路径）──
    print("\n【1b】读 0x90 失败但写成功 → 仍应命中（只写寄存器回退）")

    class WriteOnlyDevice(FakeDevice):
        def read(self, n):
            raise OSError(6, "ENXIO 只写寄存器")

    wo = WriteOnlyDevice(0x26, present=True)
    io2 = FakeI2CIO({1: wo})
    mw._i2c_io = io2
    mw._I2C_BUS_CACHE["bus"] = None
    found = mw._i2c_probe_bus()
    check("只写设备也能被探测到", found == 1, f"实际 {found}")

    # 复原主桩
    mw._i2c_io = io

    # ── 2. 关灯 ──
    print("\n【2】关灯 → 0x90=0x0")
    dev1.writes.clear()
    mw._I2C_BUS_CACHE["bus"] = 1
    mw._I2C_BUS_CACHE["checked_at"] = 9e18
    ok = mw._apply_i2c({"status": 0, "color_id": 3,
                        "color_r": 255, "color_g": 255, "color_b": 255})
    check("_apply_i2c 返回 True", ok is True)
    check("写入 (0x90, 0x00) 关灯",
          (mw.LED_I2C_REG_MODE, 0x00) in dev1.writes, dev1.writes)

    # ── 3. 固定色 → 先 RGB 后模式（顺序敏感）──
    print("\n【3】固定色 → 写入顺序必须是【先 RGB 后模式】")
    dev1.writes.clear()
    mw._apply_i2c({"status": 1, "color_id": 3,
                   "color_r": 255, "color_g": 255, "color_b": 255})
    regs = [w[0] for w in dev1.writes]
    check("写到寄存器 0x92/0x93/0x94",
          all(r in regs for r in (0x92, 0x93, 0x94)), regs)
    check("模式寄存器 0x90 在最后", regs and regs[-1] == 0x90, regs)
    check("模式值 = 0x1（solid）", dev1.regs.get(0x90) == 0x01, dev1.regs)
    check("R=255 G=255 B=255",
          (dev1.regs.get(0x92), dev1.regs.get(0x93), dev1.regs.get(0x94))
          == (255, 255, 255))
    check("RGB 写在模式之前",
          regs.index(0x92) < regs.index(0x90) and
          regs.index(0x93) < regs.index(0x90) and
          regs.index(0x94) < regs.index(0x90), regs)

    # ── 4. 随主题 → rainbow；动态色 → breathing ──
    print("\n【4】随主题=rainbow(0x2) / 动态色=breathing(0x3)")
    mw._apply_i2c({"status": 1, "color_id": 0,
                   "color_r": 0, "color_g": 0, "color_b": 0})
    check("color_id=0 → 0x90=0x2（彩虹）", dev1.regs.get(0x90) == 0x02, dev1.regs)
    mw._apply_i2c({"status": 1, "color_id": 1,
                   "color_r": 0, "color_g": 0, "color_b": 0})
    check("color_id=1 → 0x90=0x3（呼吸）", dev1.regs.get(0x90) == 0x03, dev1.regs)

    # ── 5. 硬件回读反解 ──
    print("\n【5】硬件回读 → 客户端枚举")
    dev1.regs.update({0x90: 0x02, 0x92: 0, 0x93: 0, 0x94: 0})
    hw = mw.led_read_hardware()
    check("读回 (mode,r,g,b)", hw == (0x02, 0, 0, 0), hw)

    mw.LED_HW_BACKEND = "i2c"
    mw._I2C_BUS_CACHE["bus"] = 1
    mw._I2C_BUS_CACHE["checked_at"] = 9e18

    r = mw.build_led_state({}, {"led": _DummyLed()})
    check("rainbow → status=1,mode=0,color_id=0（随主题）",
          (r["status"], r["mode"], r["color_id"]) == (1, 0, 0), r)

    dev1.regs.update({0x90: 0x01, 0x92: 255, 0x93: 255, 0x94: 255})
    r = mw.build_led_state({}, {"led": _DummyLed()})
    check("solid 白(255,255,255) → color_id=3", r["color_id"] == 3, r)

    dev1.regs.update({0x92: 30, 0x93: 180, 0x94: 25})
    r = mw.build_led_state({}, {"led": _DummyLed()})
    check("solid (30,180,25) → color_id=2（客户端 bug 色也能识回）",
          r["color_id"] == 2, r)

    dev1.regs.update({0x90: 0x00})
    r = mw.build_led_state({}, {"led": _DummyLed()})
    check("off → status=0（关）", r["status"] == 0, r)

    # ── 6. 无 I2C 时退回内部状态 ──
    print("\n【6】无 I2C 通道 → 退回内部持久化状态")
    fd, p = tempfile.mkstemp(suffix=".json")
    real_os.close(fd)
    real_os.remove(p)
    led = mw.LedState(path=p)
    led.update(color_id=4, color_r=255, color_g=90, color_b=0, status=1)

    io.devices[1] = FakeDevice(0x26, present=False)     # 设备消失
    mw._I2C_BUS_CACHE["bus"] = None
    mw._I2C_BUS_CACHE["checked_at"] = 0
    check("探测返回 None", mw._i2c_probe_bus() is None)

    mw.LED_HW_BACKEND = "none"
    r = mw.build_led_state({}, {"led": led})
    check("退回内部状态 color_id=4", r["color_id"] == 4, r)

    # 清理
    try:
        real_os.remove(p)
    except OSError:
        pass

    print("\n" + "=" * 60)
    print(f" 结果：{PASS} 通过 / {FAIL} 失败 / 共 {PASS + FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

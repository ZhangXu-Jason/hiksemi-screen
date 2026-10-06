#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_test_set_power.py —— 关机/重启（set_power）专项测试

覆盖：
  1. 契约：power_mode=0 → 关机命令；power_mode=1 → 重启命令
  2. 响应：resolver 只返回 code 值(0)，不会嵌套 {"code":{"code":0}}
  3. 非阻塞：resolver 立即返回，不等待真正执行（否则 HTTP 回包前进程已死）
  4. 命令回退链：systemctl → 直接命令 → shutdown，谁先存在用谁
  5. 非法/未知 power_mode 不触发任何动作
  6. mapping 配置正确（set_power 存在且 source=set_power）

运行： python3 _test_set_power.py
"""
import json
import os
import sys
import importlib.util
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
passed = failed = 0


def ok(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✓ {msg}")
    else:
        failed += 1
        print(f"  ✗ {msg}")


def load_mw():
    spec = importlib.util.spec_from_file_location("mw", os.path.join(HERE, "middleware.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["mw"] = m
    spec.loader.exec_module(m)
    return m


mw = load_mw()

print("=" * 60)
print(" set_power（关机/重启）测试")
print("=" * 60)

# ── 1. 契约：0=关机，1=重启 ──
print("\n[1] power_mode 契约")
ok(0 in mw.POWER_ACTIONS, "POWER_ACTIONS 含 0")
ok(1 in mw.POWER_ACTIONS, "POWER_ACTIONS 含 1")
ok(mw.POWER_ACTIONS[0]["name"] == "关机", "0 → 关机")
ok(mw.POWER_ACTIONS[1]["name"] == "重启", "1 → 重启")
ok(any(c[0] == "systemctl" and c[1] == "poweroff"
       for c in mw.POWER_ACTIONS[0]["cmds"]), "关机首选 systemctl poweroff")
ok(any(c[0] == "systemctl" and c[1] == "reboot"
       for c in mw.POWER_ACTIONS[1]["cmds"]), "重启首选 systemctl reboot")

# ── 2. resolver 返回标量 0 ──
print("\n[2] resolver 返回标量 code")
triggered = []


class _FakeThread:
    def __init__(self, target=None, args=(), daemon=None, name=None, **kw):
        self.target, self.args, self.name = target, args, name

    def start(self):
        triggered.append(self.name)


_real_thread = mw.threading.Thread
mw.threading.Thread = _FakeThread


def _call(power_mode):
    ctx = {"_params": {"power_mode": power_mode}}
    return mw.resolve_set_power({"source": "set_power"}, ctx)


r0 = _call(0)
r1 = _call(1)
ok(r0 == 0, f"power_mode=0 返回 0（实际 {r0!r}）")
ok(r1 == 0, f"power_mode=1 返回 0（实际 {r1!r}）")
ok(isinstance(r0, int) and not isinstance(r0, dict),
   "返回值是 int，不是 dict（避免 {\"code\":{\"code\":0}} 嵌套）")
ok(len(triggered) == 2, f"两次调用各起了一个后台线程（实际 {len(triggered)}）")
ok(triggered == ["power-0", "power-1"], f"线程名区分关/重启（{triggered}）")

# ── 3. 非阻塞 ──
print("\n[3] 非阻塞（立即返回）")
t0 = time.time()
_call(0)
dt = time.time() - t0
ok(dt < 0.5, f"resolver 立即返回（{dt*1000:.1f}ms）")

# ── 4. 非法值不触发 ──
print("\n[4] 非法 / 未知 power_mode")
n_before = len(triggered)
for bad in [None, "", "abc", 2, -1, 99]:
    _call(bad)
ok(len(triggered) == n_before, f"非法值 6 例均未触发动作（实际新增 {len(triggered)-n_before}）")

# ── 5. 命令回退链 ──
print("\n[5] 命令回退链")
real_which = mw.shutil.which
real_popen = mw.subprocess.Popen
calls = []


def fake_which(name):
    # 模拟：只有 shutdown 存在（systemctl/reboot 都没有）
    return "/sbin/" + name if name == "shutdown" else None


def fake_popen(cmd, **kw):
    calls.append(cmd)
    return object()


mw.shutil.which = fake_which
mw.subprocess.Popen = fake_popen
okk, used = mw._run_power_cmd(mw.POWER_ACTIONS[1]["cmds"])
ok(okk and used.startswith("shutdown"), f"回退到 shutdown（实际 {used!r}）")
ok(calls == [["shutdown", "-r", "now"]], "回退命令参数正确")
mw.shutil.which = real_which
mw.subprocess.Popen = real_popen

# 全都找不到
mw.shutil.which = lambda n: None
okk2, used2 = mw._run_power_cmd(mw.POWER_ACTIONS[0]["cmds"])
ok(not okk2 and used2 == "", "无可用命令时返回 (False, '')")
mw.shutil.which = real_which

# ── 6. mapping 配置 ──
print("\n[6] data-mapping.json 配置")
mw.threading.Thread = _real_thread
mapping = json.load(open(os.path.join(HERE, "data-mapping.json"), encoding="utf-8"))
acts = mapping["actions"]
ok("set_power" in acts, "set_power 已配置")
ok(acts.get("set_power", {}).get("code", {}).get("source") == "set_power",
   "set_power.code.source == 'set_power'")
ok("set_power" in mw.RESOLVERS, "RESOLVERS 已注册 set_power")

# ── 7. 端到端 build_response ──
print("\n[7] build_response 端到端")


class _FakeCfg:
    def actions(self):
        return acts

    def defaults(self):
        return mapping["defaults"]["unknown_action"]


mw.CTX["config"] = _FakeCfg()
mw.CTX["led"] = None
resp = mw.build_response("set_power", params={"power_mode": "1"})
ok(resp == {"code": 0}, f"响应为 {{'code': 0}}（实际 {resp!r}）")
ok(not isinstance(resp.get("code"), dict), "code 不是 dict")

print("\n" + "=" * 60)
print(f" 结果：{passed} 通过 / {failed} 失败")
print("=" * 60)
sys.exit(1 if failed else 0)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络 builder 单元测试 —— 用桩模拟飞牛OS 的多种网卡环境。

为什么需要：
    build_eth_list 的 bug 只在真实 Linux 环境暴露（Windows 没有 /sys/class/net），
    改完代码无法在开发机验证。这里用假文件系统 + 假命令把各种场景钉死。

运行：
    python3 _test_eth_builder.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("mw", os.path.join(HERE, "middleware.py"))
mw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mw)

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {detail}" if detail else ""))


def make_fs(ifaces):
    """建一个假的 /sys/class/net 树。
    ifaces: {name: {"device":bool, "operstate":str, "speed":str, "type":str,
                    "bridge":bool, "address":str}}
    """
    root = tempfile.mkdtemp(prefix="fakenet-")
    for name, meta in ifaces.items():
        dev = os.path.join(root, name)
        os.makedirs(dev)
        if meta.get("device"):
            os.makedirs(os.path.join(dev, "device"))
        for k in ("operstate", "speed", "type", "address"):
            if k in meta:
                with open(os.path.join(dev, k), "w") as f:
                    f.write(str(meta[k]))
        if meta.get("bridge"):
            os.makedirs(os.path.join(dev, "bridge"))
        if meta.get("bonding"):
            os.makedirs(os.path.join(dev, "bonding"))
    return root


def patch(mw, net_root, addrs=None, gws=None, dns=("192.168.1.1", ""),
          default_route=None, allow_all=False):
    """把 middleware 的 glob/文件读取指向假 fs，并桩掉命令。
    default_route: 模拟 /proc/net/route 的默认路由网卡名。
    """
    orig_glob = mw.glob.glob

    def fake_glob(pat):
        if pat.startswith("/sys/class/net/"):
            return orig_glob(os.path.join(net_root, "*"))
        return orig_glob(pat)

    mw.glob.glob = fake_glob

    addrs = addrs or {}
    gws = gws or {}

    def fake_addrs(name):
        if name in addrs:
            return tuple(addrs[name])
        return ("", "", "")

    def fake_gw(name):
        return gws.get(name, "")

    def fake_dns():
        return dns

    def fake_ipv4(name):
        return (addrs.get(name) or ("",))[0]

    def fake_has_gw(name):
        if default_route:
            return name == default_route
        return bool(gws.get(name))

    def fake_default_route():
        return default_route

    mw._iface_addrs = fake_addrs
    mw._iface_gateway = fake_gw
    mw._iface_dns = fake_dns
    mw._iface_ipv4 = fake_ipv4
    mw._iface_has_gateway = fake_has_gw
    mw._default_route_iface = fake_default_route
    return orig_glob


SPEC = {
    "source": "builder", "name": "eth_list", "max": 2,
    "include": [], "relaxed": False,
    "exclude": ["lo", "docker*", "veth*", "br-*", "virbr*", "tailscale*",
                "tun*", "tap*", "wg*", "zt*", "dummy*", "vmnet*", "vnet*"],
}


def run_case(title, ifaces, addrs, gws, expect_names, spec_extra=None,
             default_route=None):
    print(f"\n{'='*72}\n{title}\n{'='*72}")
    root = make_fs(ifaces)
    orig = patch(mw, root, addrs, gws, default_route=default_route)
    sp = dict(SPEC)
    sp.update(spec_extra or {})
    try:
        res = mw.build_eth_list(sp, {})
    finally:
        mw.glob.glob = orig
        shutil.rmtree(root, ignore_errors=True)
    got = [e["eth_name"] for e in res["eth"]]
    check(f"入选网卡 = {expect_names}", got == expect_names, f"实际 {got}")
    return res


try:
    # ── 场景 1：标准双物理网卡（飞牛OS 命名：enp2s0-ovs + enp3s0-ovs）──
    res = run_case(
        "场景 1 · 双物理网卡（enp2s0-ovs + enp3s0-ovs）",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp2s0-ovs": {"device": True, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"},
         "enp3s0-ovs": {"device": True, "operstate": "down", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:02"}},
        {"enp2s0-ovs": ("192.168.8.100", "240e:1234::1", "255.255.255.0"),
         "enp3s0-ovs": ("", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs", "enp3s0-ovs"],
        default_route="enp2s0-ovs",
    )
    e0 = res["eth"][0]
    check("eth_name 正确（含 -ovs 后缀）", e0["eth_name"] == "enp2s0-ovs")
    check("ip 正确", e0["ip"] == "192.168.8.100")
    check("ipv6 正确", e0["ipv6"] == "240e:1234::1")
    check("gw 正确", e0["gw"] == "192.168.8.1")
    check("connected=1（up）", e0["connected"] == 1)
    check("10 字段齐全", all(k in e0 for k in
        ["eth_name","ip","ipv6","dns1","dns2","gw","mask","method","mac","connected"]))
    e1 = res["eth"][1]
    check("down 网卡 connected=0（无IP无网关）", e1["connected"] == 0)
    check("网关网卡排第一", res["eth"][0]["eth_name"] == "enp2s0-ovs")

    # ── 场景 2：operstate=unknown 但有 IP（最常见误判）──
    res = run_case(
        "场景 2 · operstate=unknown 但链路实际连通（★ 旧代码在此误判为未联网）",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp2s0-ovs": {"device": True, "operstate": "unknown", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs"],
        default_route="enp2s0-ovs",
    )
    check("unknown+有IP → connected=1（修复点）", res["eth"][0]["connected"] == 1)

    # ── 场景 3：★ 飞牛OS 真实场景 —— 物理口就叫 enp2s0-ovs ──
    res = run_case(
        "场景 3 · ★ 飞牛OS 真实命名：物理口叫 enp2s0-ovs（不能被排除）",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp2s0-ovs": {"device": True, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"},
         "enp3s0-ovs": {"device": True, "operstate": "down", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:02"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0"),
         "enp3s0-ovs": ("", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs", "enp3s0-ovs"],
        default_route="enp2s0-ovs",
    )
    e0 = res["eth"][0]
    check("★ enp2s0-ovs 被纳入（旧代码在此排除 → 网络页无数据）",
          e0["eth_name"] == "enp2s0-ovs")
    check("★ eth_name 保留完整名字（含 -ovs 后缀）",
          e0["eth_name"] == "enp2s0-ovs")
    check("ip 正确", e0["ip"] == "192.168.8.100")
    check("connected=1", e0["connected"] == 1)
    check("有默认路由的排第一", res["eth"][0]["eth_name"] == "enp2s0-ovs")

    # ── 场景 3b：仅有 enp2s0-ovs 一张卡（飞牛OS 单网口）──
    res = run_case(
        "场景 3b · ★ 飞牛OS 单网口：只有 enp2s0-ovs",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp2s0-ovs": {"device": True, "operstate": "unknown", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "240e::1", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs"],
        default_route="enp2s0-ovs",
    )
    e = res["eth"][0]
    check("单网口也被纳入", e["eth_name"] == "enp2s0-ovs")
    check("unknown + 有IP → connected=1", e["connected"] == 1)
    check("ipv6 正常", e["ipv6"] == "240e::1")

    # ── 场景 3c：device 软链接消失（OVS 接管）仍要纳入 ──
    res = run_case(
        "场景 3c · device 软链接消失（被 OVS 接管）→ 仍纳入",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp2s0-ovs": {"device": False, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs"],
        default_route="enp2s0-ovs",
    )
    check("device 缺失不影响纳入", res["eth"][0]["eth_name"] == "enp2s0-ovs")

    # ── 场景 3d：内核标记为 bridge 的虚拟接口默认排除 ──
    res = run_case(
        "场景 3d · 内核标记 bridge 的虚拟接口（默认）应排除",
        {"lo": {"operstate": "unknown", "type": "772"},
         "ovs-br0": {"device": False, "operstate": "up", "type": "1",
                     "bridge": True},
         "enp2s0-ovs": {"device": True, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs"],
        default_route="enp2s0-ovs",
    )
    check("bridge 默认被排除，物理口保留",
          res["eth"][0]["eth_name"] == "enp2s0-ovs")

    # ── 场景 3e：relaxed=true 时连 bridge 也纳入（排障用）──
    res = run_case(
        "场景 3e · relaxed=true 时不排除 bridge（排障模式）",
        {"lo": {"operstate": "unknown", "type": "772"},
         "ovs-br0": {"device": False, "operstate": "up", "type": "1",
                     "bridge": True},
         "enp2s0-ovs": {"device": True, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs", "ovs-br0"],
        {"relaxed": True},
        default_route="enp2s0-ovs",
    )

    # ── 场景 4：include 白名单强制纳入 ──
    res = run_case(
        "场景 4 · include 白名单强制纳入",
        {"lo": {"operstate": "unknown", "type": "772"},
         "weird0": {"device": True, "operstate": "up", "type": "1",
                    "address": "aa:bb:cc:00:00:01"}},
        {"weird0": ("192.168.8.100", "", "255.255.255.0")},
        {"weird0": "192.168.8.1"},
        ["weird0"],
        {"include": ["weird0"]},
        default_route="weird0",
    )
    check("include 生效", res["eth"][0]["eth_name"] == "weird0")

    # ── 场景 5：include 优先级高于 exclude ──
    res = run_case(
        "场景 5 · include 白名单优先级高于 exclude",
        {"lo": {"operstate": "unknown", "type": "772"},
         "docker0": {"device": False, "operstate": "up", "type": "1",
                     "address": "aa:bb:cc:00:00:01"}},
        {"docker0": ("172.17.0.1", "", "255.255.0.0")},
        {},
        ["docker0"],
        {"include": ["docker0"]},
    )
    check("include 压过 exclude", res["eth"][0]["eth_name"] == "docker0")

    # ── 场景 6：docker/veth/br 必须被排除，-ovs 物理口保留 ──
    res = run_case(
        "场景 6 · 虚拟接口（docker/veth/br-）排除，enp2s0-ovs 保留",
        {"lo": {"operstate": "unknown", "type": "772"},
         "docker0": {"device": False, "operstate": "up", "type": "1"},
         "veth1a2b": {"device": False, "operstate": "up", "type": "1"},
         "br-abc123": {"device": False, "operstate": "up", "type": "1"},
         "enp2s0-ovs": {"device": True, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs"],
        default_route="enp2s0-ovs",
    )

    # ── 场景 7：只有一张网卡（eth2 缺失）──
    res = run_case(
        "场景 7 · 只有一张网卡 → 数组长度 1（客户端 eth2 用默认模板）",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp2s0-ovs": {"device": True, "operstate": "up", "speed": "1000",
                        "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp2s0-ovs": ("192.168.8.100", "", "255.255.255.0")},
        {"enp2s0-ovs": "192.168.8.1"},
        ["enp2s0-ovs"],
        default_route="enp2s0-ovs",
    )
    check("数组长度 1", len(res["eth"]) == 1)

    # ── 场景 8：排序 —— 有网关 > 有IP > up ──
    res = run_case(
        "场景 8 · 排序：有网关的排第一（不受名字字典序影响）",
        {"lo": {"operstate": "unknown", "type": "772"},
         "enp0s1": {"device": True, "operstate": "up", "speed": "1000",
                    "type": "1", "address": "aa:bb:cc:00:00:0a"},
         "zzz0": {"device": True, "operstate": "up", "speed": "1000",
                  "type": "1", "address": "aa:bb:cc:00:00:01"}},
        {"enp0s1": ("192.168.9.5", "", "255.255.255.0"),
         "zzz0": ("192.168.8.100", "", "255.255.255.0")},
        {"zzz0": "192.168.8.1"},
        ["zzz0", "enp0s1"],
        default_route="zzz0",
    )
    check("有默认网关的排第一（名字靠后也优先）",
          res["eth"][0]["eth_name"] == "zzz0")

    # ── 场景 9：掩码计算 ──
    check("_prefix_to_mask(24) = 255.255.255.0",
          mw._prefix_to_mask(24) == "255.255.255.0")
    check("_prefix_to_mask(16) = 255.255.0.0",
          mw._prefix_to_mask(16) == "255.255.0.0")
    check("_prefix_to_mask(8) = 255.0.0.0",
          mw._prefix_to_mask(8) == "255.0.0.0")
    check("_prefix_to_mask(32) = 255.255.255.255",
          mw._prefix_to_mask(32) == "255.255.255.255")

    # ── 场景 10：★ 名字无关性 —— 无论网卡叫什么，判定结果一致 ──
    for label, name in [("eth0", "eth0"), ("enp2s0", "enp2s0"),
                        ("enp2s0-ovs", "enp2s0-ovs"),
                        ("eno1", "eno1"), ("weird-name", "weird-name")]:
        r = run_case(
            f"场景 10 · 网卡名 {label!r} 不应影响判定",
            {"lo": {"operstate": "unknown", "type": "772"},
             name: {"device": True, "operstate": "up", "speed": "1000",
                    "type": "1", "address": "aa:bb:cc:00:00:01"}},
            {name: ("192.168.8.100", "", "255.255.255.0")},
            {name: "192.168.8.1"},
            [name],
            default_route=name,
        )
        check(f"  {label} → 纳入且 eth_name 一致",
              r["eth"][0]["eth_name"] == name)

except Exception:
    import traceback
    print("\n❌ 测试脚本自身抛异常（下面是完整堆栈）：")
    traceback.print_exc()
    FAIL.append("脚本异常，后续场景未执行")
finally:
    print(f"\n{'='*72}")
    print(f"结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  ✗", f)
    print("=" * 72)
    sys.exit(1 if FAIL else 0)

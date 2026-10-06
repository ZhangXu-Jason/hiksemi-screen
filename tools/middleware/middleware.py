#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hiksemi-sub 服务中间件 (Service Middleware)
=============================================
在飞牛OS（或任意 Linux）上监听 https://127.0.0.1:20443，
把「操作系统真实数据」按 data-mapping.json 配置的映射规则，
组装成 hiksemi 小彩屏程序期望的 REST 响应。

架构：
    hiksemi-sub (Electron 小彩屏)
        │  HTTPS GET /rest/1.x/...?action=xxx&access_token=...
        ▼
    middleware.py ──读取──▶ data-mapping.json  （★ 字段级映射配置）
        │ 采集                     （static / file / command / hwmon / collector / builder）
        ▼
    操作系统（/proc、/sys/class/hwmon、/sys/class/net、/sys/block、df、命令）

依赖：Python 3.8+ 纯标准库，无第三方包。
配置：程序字段 ↔ 系统数据源 的对应关系全部写在 data-mapping.json；
      改配置即时生效（按 mtime 热重载），无需重启服务。

用法：
    cd /usr/local/hiksemi-sub/middleware
    sudo python3 middleware.py                 # 使用默认路径
    python3 middleware.py --config xxx.json    # 指定配置
    python3 middleware.py --listen 0.0.0.0     # 允许外部访问（默认仅本机）

证书：
    首次运行前需在脚本同目录生成自签证书（客户端 rejectUnauthorized=false，
    任意证书均可通过）：
        openssl req -x509 -newkey rsa:2048 -nodes \
            -keyout key.pem -out cert.pem -days 3650 -subj "/CN=127.0.0.1"
"""

import glob
import json
import os
import re
import select
import shutil
import ssl
import subprocess
import sys
import threading
import time

try:
    import fcntl          # ★ I2C 硬件控制需要（Linux 专用；Windows 开发机无）
except ImportError:
    fcntl = None
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(BASE, "data-mapping.json")
DEFAULT_CERT = os.path.join(BASE, "cert.pem")
DEFAULT_KEY = os.path.join(BASE, "key.pem")

# ★ 版本号：每次有行为变更就递增。日志启动段会打印，
#   用于判断「真机上跑的是哪一版」——排查"改了没生效"时第一眼就看它。
MIDDLEWARE_VERSION = "2026.10.05-h（散热风扇 4 档：加载/修改/保存 + CoolerControl 落地）"


def _code_fingerprint():
    """本文件的大小 + mtime，用于确认部署的确实是新版。

    返回形如 "91413B @10-05 01:38"；读取失败时返回 "N/A"。
    """
    try:
        st = os.stat(os.path.abspath(__file__))
        mt = time.strftime("%m-%d %H:%M", time.localtime(st.st_mtime))
        return f"{st.st_size}B @{mt}"
    except Exception:
        return "N/A"


LOG_LOCK = threading.Lock()

# ============================================================
# 日志级别（★ 减少刷屏）
# ------------------------------------------------------------
# 客户端每 2 秒轮询一次，若每个请求 + 每次响应都打日志，
# 1 小时就能刷出 4000+ 行 / 近 500KB，且绝大多数是重复内容。
#
# 级别（由环境变量 HIKSEMI_LOG 控制，默认 normal）：
#   quiet  → 只打启动/错误/状态变更（写入类 action），请求流水全静默
#   normal → 启动/错误/状态变更 + 写入类 + 周期性摘要（默认）
#   debug  → 全部打印（含每个请求/响应），排障时用
# ============================================================
LOG_LEVEL = os.environ.get("HIKSEMI_LOG", "normal").strip().lower()
if LOG_LEVEL not in ("quiet", "normal", "debug"):
    LOG_LEVEL = "normal"

_LV = {"quiet": 0, "normal": 1, "debug": 2}


def log(msg, level="normal"):
    """level: 'quiet' 基础信息 / 'normal' 常规 / 'debug' 仅排障"""
    if _LV[LOG_LEVEL] < _LV.get(level, 1):
        return
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with LOG_LOCK:
        print(f"[{ts}] {msg}", flush=True)


# ★ 高频轮询 action 的请求流水抑制：
#   这些是客户端定时拉取的只读查询，内容不变就没必要每次记。
#   改为「首次打印 + 每 N 次摘要一行」，可把日志量降 90% 以上。
_QUIET_POLL_ACTIONS = {
    "get_sys_info", "get_off_time", "get_light_theme",
    "get_atmo_light", "get_hw_resource_info", "get_event",
    "get_space_list", "get_eth_info_v2", "get_network_mode",
    "get_version", "get_update_version_desc",
}
_poll_stat = {}          # action -> 累计次数
_POLL_SUMMARY_EVERY = 100   # 每 N 次打一行摘要（normal 级别）


def _poll_tick(action):
    """返回 True 表示本次应打印完整行；False 表示已抑制（仅计数）"""
    n = _poll_stat.get(action, 0) + 1
    _poll_stat[action] = n
    if LOG_LEVEL == "debug":
        return True
    if n == 1:
        return True                       # 首次必打（便于确认 action 通）
    if LOG_LEVEL == "normal" and n % _POLL_SUMMARY_EVERY == 0:
        return "summary"                  # 摘要行
    return False


# ★ 重复错误去重：同一类错误（按前缀归并）只完整报一次，
#   后续每 N 次汇总一行 —— 防止高频路径（如每 2s 的 led_read）刷屏。
_err_seen = {}          # key -> 累计次数
_ERR_REPEAT_EVERY = 50


def log_err(key, msg, level="quiet"):
    """错误日志（带去重）。

    key  : 错误类别（相同 key 视为同类，例如 "led_read"）
    msg  : 完整错误文本
    level: 级别（默认 quiet = 始终可见，但同类只报一次+计数）
    """
    n = _err_seen.get(key, 0) + 1
    _err_seen[key] = n
    if n == 1:
        log(msg, level=level)
        if level == "quiet":
            log(f"  （同类错误后续将合并计数，不再逐条打印）", level=level)
    elif n % _ERR_REPEAT_EVERY == 0:
        log(f"[{key}] 同类错误已发生 {n} 次（最近: {msg[:100]}）", level=level)


# ============================================================
# 一、周期采集器（后台线程刷新缓存，请求时直接取现值）
# ============================================================

class Collectors:
    """内置采集器：cpu_percent / mem_percent / net_up / net_down / load1"""

    def __init__(self, cfg):
        col = cfg.get("collectors", {})
        self.interval = float(col.get("interval", 2))
        self.ifaces = col.get("net_ifaces", [])   # 空 = 自动取所有非 lo
        self._cache = {}
        self._lock = threading.Lock()
        self._cpu_prev = None
        self._net_prev = None   # (ts, {iface: (rx, tx)})
        self._stop = threading.Event()

    # ---- 单次采样 ----
    @staticmethod
    def _cpu_sample():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        v = list(map(int, parts))
        idle = v[3] + (v[4] if len(v) > 4 else 0)
        return idle, sum(v)

    @staticmethod
    def _mem_percent():
        total = avail = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    avail = int(line.split()[1])
        if total and avail is not None and total > 0:
            return max(0.0, min(100.0, 100.0 * (1 - avail / total)))
        return 0.0

    @staticmethod
    def _net_sample(ifaces):
        out = {}
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                parts = line.split(":")
                name = parts[0].strip()
                if name == "lo":
                    continue
                if ifaces and name not in ifaces:
                    continue
                fields = parts[1].split()
                out[name] = (int(fields[0]), int(fields[8]))  # rx_bytes, tx_bytes
        return out

    # ---- 采集循环 ----
    def _loop(self):
        err_count = 0          # 连续异常计数
        err_first = None       # 首次异常消息（用于抑制刷屏）
        while not self._stop.is_set():
            try:
                # CPU
                idle, total = self._cpu_sample()
                if self._cpu_prev:
                    di = idle - self._cpu_prev[0]
                    dt = total - self._cpu_prev[1]
                    pct = 100.0 * (1 - di / dt) if dt > 0 else 0.0
                    self._set("cpu_percent", max(0.0, min(100.0, pct)))
                self._cpu_prev = (idle, total)
                # 内存
                self._set("mem_percent", self._mem_percent())
                # 网络
                now = time.time()
                cur = self._net_sample(self.ifaces)
                if self._net_prev:
                    pt, pcur = self._net_prev
                    dt = now - pt
                    if dt > 0:
                        rx = sum(v[0] for v in cur.values()) - sum(v[0] for v in pcur.values())
                        tx = sum(v[1] for v in cur.values()) - sum(v[1] for v in pcur.values())
                        self._set("net_down", max(0.0, rx / dt / 1024))  # KB/s
                        self._set("net_up", max(0.0, tx / dt / 1024))
                self._net_prev = (now, cur)
                # 负载
                with open("/proc/loadavg") as f:
                    self._set("load1", float(f.read().split()[0]))
                # 恢复正常
                if err_count:
                    log(f"[collector] 采样已恢复正常（此前连续失败 {err_count} 次）", level="quiet")
                    err_count = 0
                    err_first = None
            except Exception as e:
                msg = str(e)
                if err_count == 0:
                    # 首次异常：完整报出 + 排查提示
                    log(f"[collector] 采样异常: {msg}", level="quiet")
                    log(f"[collector] ⚠ 后续同类异常将静默（仅在恢复时提示）", level="quiet")
                    if "/proc" in msg or "/sys" in msg:
                        log("[collector]   提示：该主机没有 /proc 或 /sys 虚拟文件系统？"
                            "（中间件需在 Linux 上运行）", level="quiet")
                    err_first = msg
                elif err_count in (10, 100, 1000):
                    log(f"[collector] 采样已连续失败 {err_count} 次，最近错误: {msg}", level="quiet")
                err_count += 1
            self._stop.wait(self.interval)

    def _set(self, name, val):
        with self._lock:
            self._cache[name] = val

    def get(self, name):
        with self._lock:
            return self._cache.get(name)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        log(f"[collector] 已启动（间隔 {self.interval}s，网卡: {self.ifaces or '自动'}）", level="quiet")


# ============================================================
# 二、数据源解析器（对应 data-mapping.json 里的 source 类型）
# ============================================================

def _postprocess(val, spec):
    """统一后处理链：regex → scale → cast → format
    （scale 在 cast 之前：先做物理量换算如 milli-°C→°C，再取整）"""
    # 正则提取
    rx = spec.get("regex")
    if rx is not None:
        m = re.search(rx, str(val))
        if m:
            val = m.group(1) if m.groups() else m.group(0)
        else:
            return spec.get("default")
    # 缩放（如 milli-°C → °C）
    scale = spec.get("scale")
    if scale is not None:
        try:
            val = float(val) * float(scale)
        except (TypeError, ValueError):
            return spec.get("default")
    # 数值转换
    cast = spec.get("cast")
    try:
        if cast == "int":
            val = int(round(float(val)))
        elif cast == "float":
            val = float(val)
    except (TypeError, ValueError):
        return spec.get("default")
    # 格式化（值是数字字符串时先数值化，让 {:.0f}% 之类模板可用）
    fmt = spec.get("format")
    if fmt:
        fv = val
        if isinstance(fv, str):
            try:
                fv = float(fv)
            except ValueError:
                return fv  # 非数字字符串不做格式化，原样返回
        try:
            return fmt.format(fv)
        except (TypeError, ValueError, IndexError):
            return spec.get("default", str(val))
    return val


def resolve_static(spec, ctx):
    return spec.get("value")


def resolve_file(spec, ctx):
    path = spec.get("path", "")
    try:
        with open(path) as f:
            val = f.read().strip()
        return _postprocess(val, spec)
    except Exception:
        return spec.get("default")


def resolve_command(spec, ctx):
    cmd = spec.get("command", "")
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=spec.get("timeout", 5))
        if r.returncode != 0:
            return spec.get("default")
        return _postprocess(r.stdout.strip(), spec)
    except Exception:
        return spec.get("default")


def resolve_hwmon(spec, ctx):
    """在 /sys/class/hwmon 下按 chip 名匹配传感器文件。
    例：{"source":"hwmon","chip":"coretemp","field":"temp1_input","scale":0.001}
    chip 可省略（取第一个含该 field 的）；field 默认 temp1_input。"""
    chip = (spec.get("chip") or "").lower()
    field = spec.get("field", "temp1_input")
    try:
        for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
            try:
                name = ""
                name_path = os.path.join(d, "name")
                if os.path.exists(name_path):
                    with open(name_path) as f:
                        name = f.read().strip().lower()
                if chip and chip not in name:
                    continue
                p = os.path.join(d, field)
                if not os.path.exists(p):
                    continue
                with open(p) as f:
                    return _postprocess(f.read().strip(), spec)
            except OSError:
                continue
    except Exception:
        pass
    return spec.get("default")


def resolve_collector(spec, ctx):
    """ctx 为全局 CTX 字典，采集器实例挂在 ctx['collectors']。
    取到原始数值后同样走 _postprocess（format/cast/default 生效）"""
    coll = ctx.get("collectors")
    val = coll.get(spec.get("name")) if coll else None
    if val is None:
        return spec.get("default")
    return _postprocess(val, spec)


# ---- 主网卡自动探测（解决 eth0 / enp2s0 / enp2s0-ovs 硬编码问题）----
# ★★★ 铁律：网卡名必须从系统发现，不能硬编码，也不能靠名字后缀猜物理/虚拟 ★★★
#
# 踩过的坑：曾把 "*-ovs" 写进排除名单，理由是「OVS 虚拟桥，关掉 OVS 就没了」。
# 但飞牛OS 上物理口就是叫 enp2s0-ovs —— 那个后缀是系统命名的一部分，
# 不是"虚拟桥"的标志。结果真正联网的网卡被主动排除 → 网络页永远无数据。
#
# 正解：排除名单**只放确定的虚拟接口**；「哪张网卡有意义」完全由
# 客观事实决定（有默认路由 > 有 IP > operstate 为 up），与名字无关。
_MAC_EXCLUDE_PATTERNS = [
    "lo",              # 回环
    "docker*",         # docker 网桥
    "veth*",           # 容器 veth pair
    "br-*",            # docker 自定义网桥
    "virbr*",          # libvirt 虚拟网桥
    "tailscale*",      # tailscale 隧道
    "tun*", "tap*",    # VPN 隧道
    "wg*",             # wireguard
    "zt*",             # zerotier
    "dummy*",          # dummy 接口
    "vmnet*", "vnet*", # 虚拟机网卡
    "sit*", "gre*", "ip6tnl*",  # 各种隧道
    "kube-*", "cni*", "flannel*", "cali*",  # k8s
]


def _all_ifaces():
    """列出所有网卡名。
    ★ 返回顺序不能作为「优先级」依据 —— 调用方必须按客观事实排序。
    """
    return [os.path.basename(d) for d in sorted(glob.glob("/sys/class/net/*"))]


def _is_excluded_iface(name, extra=None):
    """是否属于确定要排除的接口类型（只按名字判断虚拟接口）。"""
    pats = list(extra or []) + _MAC_EXCLUDE_PATTERNS
    return any(re.fullmatch(p.replace("*", ".*"), name) for p in pats)


def _default_route_iface():
    """解析默认路由所在的网卡名。多级兜底：
       ① /proc/net/route 的 Destination=00000000 行（最可靠、无外部依赖）
       ② `ip route show default`
    ★ 飞牛OS 的默认路由可能挂在 enp2s0-ovs 上，这里不做任何名字过滤。
    """
    try:
        with open("/proc/net/route") as f:
            next(f)
            for ln in f:
                cols = ln.split()
                if len(cols) >= 2 and cols[1] == "00000000":
                    return cols[0]
    except Exception:
        pass
    try:
        r = subprocess.run(["ip", "-4", "route", "show", "default"],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"\bdev\s+(\S+)", r.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _iface_operstate(name):
    try:
        with open(f"/sys/class/net/{name}/operstate") as f:
            return f.read().strip()
    except Exception:
        return ""


def _iface_ipv4(name):
    """取该网卡 IPv4（不依赖 `ip` 命令，直接读 /proc/net/fib_trie 兜底）。"""
    # ① ip 命令
    try:
        r = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", name],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"inet\s+([\d.]+)", r.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    # ② /proc/net/fib_trie
    try:
        with open("/proc/net/fib_trie") as f:
            lines = f.read().splitlines()
        for i, ln in enumerate(lines):
            m = re.match(r"\s*\|\S+\s+(\d+\.\d+\.\d+\.\d+)/32", ln)
            if not m:
                continue
            ip = m.group(1)
            if ip.startswith(("127.", "169.254.")):
                continue
            for j in range(i + 1, min(i + 4, len(lines))):
                if re.search(r"\b" + re.escape(name) + r"\b", lines[j]):
                    return ip
    except Exception:
        pass
    return ""


def _iface_has_gateway(name):
    """该网卡是否承载默认路由（最能说明「这张卡在联网」的客观事实）。"""
    if _default_route_iface() == name:
        return True
    try:
        r = subprocess.run(["ip", "-4", "route", "show", "default", "dev", name],
                           capture_output=True, text=True, timeout=3)
        return "default" in r.stdout
    except Exception:
        return False


def score_iface(name):
    """★ 网卡「有意义程度」打分：返回 (网关, IP, MAC, up, 名字) 元组，越小越靠前。

    判定依据全部是客观事实，与接口名无关：

        [0] 0 = 有默认路由   → 真正联网的那张，必须排第一
        [1] 0 = 有 IPv4 地址
        [2] 0 = 有有效 MAC    ← ★ 新增：影子接口的 MAC 常为全 0
        [3] 0 = operstate ∈ {up, unknown}
        [4] 接口名（仅用于稳定排序）

    ★ 不再检查 -ovs / -br 等名字后缀 —— 飞牛OS 物理口就叫 enp2s0-ovs。

    ★★ 为什么加 MAC 维度：飞牛OS 上 OVS 接管后会残留一个同名「影子接口」
       （如 enp2s0，而真口是 enp2s0-ovs）。影子接口无 IPv4、无网关、MAC 常为
       全 0。仅靠 (网关, IP, up) 打分时，影子口若处于 up 态可能与真口打平，
       一旦真口被 exclude 误杀，就会兜底选中影子口 → IP 全空 → 屏幕显示 --。
       把「有无有效 MAC」纳入打分，可在排序层面把影子口压到真口之后。
    """
    oper = _iface_operstate(name)
    has_gw = 0 if _iface_has_gateway(name) else 1
    has_ip = 0 if _iface_ipv4(name) else 1
    mac = _read_sysfs("/sys/class/net/%s/address" % name, "").lower()
    has_mac = 0 if (mac and mac != "00:00:00:00:00:00") else 1
    is_up = 0 if oper in ("up", "unknown") else 1
    return (has_gw, has_ip, has_mac, is_up, name)


def pick_primary_iface(extra_exclude=None):
    """按优先级挑选主网卡名（供 MAC 探测等场景使用）。

    优先级（全部基于客观事实）：
       1) 有默认路由的网卡      ← 最可靠
       2) 有 IPv4 地址的网卡
       3) operstate 为 up / unknown 的网卡
       4) 其余候选的第一个

    ★ 不按接口名后缀猜物理/虚拟。飞牛OS 的 enp2s0-ovs 只要有默认路由，
      它就是主网卡。
    """
    cands = [n for n in _all_ifaces() if not _is_excluded_iface(n, extra_exclude)]
    if not cands:
        return None
    cands.sort(key=score_iface)
    return cands[0]


def resolve_mac(spec, ctx):
    """读主网卡 MAC。配置：{"source":"mac","iface":"eth0"} 可显式指定；
    不指定则自动探测（见 pick_primary_iface）。"""
    iface = spec.get("iface")
    if not iface:
        iface = pick_primary_iface(spec.get("exclude"))
    if not iface:
        return spec.get("default", "00:11:22:33:44:55")
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            mac = f.read().strip()
        return _postprocess(mac, spec) if mac else spec.get("default")
    except Exception:
        return spec.get("default", "00:11:22:33:44:55")


# ============================================================
# 三、复合构造器（需要遍历多个系统对象组装列表的场景）
# ============================================================

def _fmt_size_gb(size_bytes):
    """字节数 → 海康风格容量字符串（4.0TB / 500GB）"""
    tb = size_bytes / (1000 ** 4)
    if tb >= 0.95:
        return f"{tb:.1f}TB"
    gb = size_bytes / (1000 ** 3)
    return f"{gb:.0f}GB" if gb >= 10 else f"{gb:.1f}GB"


def _num(val, dflt=0.0):
    """安全转 float（None/乱码 → dflt）"""
    try:
        return float(val)
    except (TypeError, ValueError):
        return dflt


def _disk_sort_key(name):
    """★ 物理盘位排序：SATA/sd* 在前，NVMe 在后。

    原因：/sys/block 的字典序是 ["nvme0n1", "sda", "sdb", ...]，
    "n" < "s" 所以 NVMe 会被排到 1 号盘位。但机箱物理盘位 1~4 通常是 SATA，
    NVMe 插在后面的 M.2 槽位。直接 sorted() 会导致盘位全部错位。

    排序规则：
      0 - sd* / hd* / vd*（SATA/USB/虚拟块设备）→ 盘位 1,2,3,...
      1 - nvme*n*（M.2 NVMe）                    → 接在 SATA 之后
      2 - 其它（mmcblk / md / dm 等）            → 最后
    同类内按自然序（sda<sdb<...，nvme0n1<nvme0n2<nvme1n1）。
    ★ 排序键一致性铁律：所有分支必须返回【同长度、同位置同类型】的元组。
      否则 Python 3 在比较到不同类型元素时会抛
      TypeError: '<' not supported between instances of 'str' and 'int'。
      这里统一为 4 元组，且每个位置类型固定：
        [0] int  (0=SATA / 1=NVMe / 2=其它)
        [1] int  (控制器/次序号)
        [2] int  (次要序，SATA 用盘符转数字)
        [3] str  (原始设备名，兜底保证全序)
    """
    m = re.match(r"^nvme(\d+)n(\d+)$", name)
    if m:
        return (1, int(m.group(1)), int(m.group(2)), name)
    m = re.match(r"^(sd|hd|vd)([a-z]+)$", name)
    if m:
        # 盘符转数字：a→1, b→2, ... z→26；多字母时按 26 进制累加
        letters = m.group(2)
        idx = 0
        for ch in letters:
            idx = idx * 26 + (ord(ch) - ord("a") + 1)
        return (0, 0, idx, name)
    return (2, 0, 0, name)


def _iter_block_devices(pattern):
    """按物理盘位顺序遍历 /sys/block 下匹配 pattern 的块设备。"""
    devs = []
    for dev in glob.glob("/sys/block/*"):
        name = os.path.basename(dev)
        if re.fullmatch(pattern, name):
            devs.append((_disk_sort_key(name), dev))
    devs.sort(key=lambda t: t[0])
    return [d for _, d in devs]


def build_disk_manage(spec, ctx):
    """生成设备状态帧里的盘位数组（客户端字段：port/disk_state/smart_status/
    disk_cap(字节数)/disk_form）。客户端会 changeCap(disk_cap) 自行换算容量。"""
    pattern = spec.get("pattern", r"sd[a-z]|nvme[0-9]+n[0-9]+")
    out = []
    port = 1
    for dev in _iter_block_devices(pattern):
        try:
            with open(os.path.join(dev, "size")) as f:
                size = int(f.read().strip()) * 512
            rota_p = os.path.join(dev, "queue/rotational")
            rota = os.path.exists(rota_p) and open(rota_p).read().strip() == "1"
            state = "normal"
            sp = os.path.join(dev, "device/state")
            if os.path.exists(sp):
                st = open(sp).read().strip()
                state = "sleep" if st in ("suspended", "sleeping", "standby") else "normal"
            out.append({
                "port": port,
                "disk_state": state,
                "smart_status": "normal",
                "disk_cap": size,
                "disk_form": "HDD" if rota else "SSD",
            })
            port += 1
        except Exception:
            continue
    return out


def build_dev_status(spec, ctx):
    """★ 首页设备状态帧（action=get_hw_resource_info，走 1.2/config 长连接）。

    客户端 devStatusInit 真实解析逻辑（从 index-*.js 逆向）：
        r && r.cpu && r.cpu >= 0        ← cpu 必须是数字，字符串会被丢弃！
        devSta.cpu      = `${r.cpu}`           数字
        devSta.memory   = `${r.memory}`        数字
        devSta.upload   = `${changeSpeed(r.upload)}`   数字 KB/s，客户端自己格式化
        devSta.download = `${changeSpeed(r.download)}` 数字 KB/s
        devSta.Fanmode  = r.fanmode            小写！
        devSta.cpuTemp  = `${r.cpu_temp}`      下划线！
        devSta.devTemp  = `${r.temperature}`
        diskList        = r.disk_manage        盘位数组

    ★ 关键教训：cpu 返回 "12%" 这类字符串会让 `r.cpu >= 0` 在 JS 里
      变成 NaN>=0 → false，整帧被丢弃，屏幕永远不刷新。
      所以这里必须返回「裸数值」，格式化交给客户端。
    """
    c = ctx.get("collectors")
    get = c.get if c else (lambda k: None)

    # CPU / 内存：裸数值（百分比数字，不带 %）
    cpu = _num(get("cpu_percent"), 0.0)
    mem = _num(get("mem_percent"), 0.0)
    # 网速：裸数值 KB/s（客户端 changeSpeed 负责变成 "012.0KB/s"）
    up = _num(get("net_up"), 0.0)
    down = _num(get("net_down"), 0.0)

    # 温度：按配置的 hwmon 规则取，取不到用 default
    def _temp(field_spec):
        fs = spec.get(field_spec)
        if isinstance(fs, dict):
            v = resolve_hwmon(fs, ctx)
            return int(_num(v, fs.get("default", 0)))
        return int(_num(fs, 0))

    # 风扇档位（fanmode）：★ 动态读 FanState，保证 UI 回显与最近设置一致。
    #   客户端按 `fanmode === FAN_LIST[].id` 严格相等匹配（5/2/3/4）。
    #   若配置里显式给了 static 值（数字），则优先用配置（便于临时固定排障）。
    fan = ctx.get("fan")
    cfg_fan = spec.get("fanmode")
    if isinstance(cfg_fan, (int, float)) and not (isinstance(cfg_fan, dict)):
        fanmode = _normalize_fan_mode(int(cfg_fan))
    elif isinstance(cfg_fan, dict) and cfg_fan.get("source") == "static":
        fanmode = _normalize_fan_mode(cfg_fan.get("value", FAN_MODE_DEFAULT))
    elif fan is not None:
        fanmode = fan.mode()
    else:
        fanmode = FAN_MODE_DEFAULT

    return {
        "code": 0,
        "cpu": round(cpu),
        "memory": round(mem),
        "upload": round(up, 1),
        "download": round(down, 1),
        "fanmode": fanmode,
        "cpu_temp": _temp("cpu_temp"),
        "temperature": _temp("temperature"),
        # 盘位列表：客户端做 diskList = r.disk_manage
        "disk_manage": build_disk_manage(spec.get("disk_manage") or {}, ctx),
    }


def build_disk_list(spec, ctx):
    """扫描 /sys/block 按配置的设备通配，生成 diskList（6 盘位）。
    配置：{"pattern":"sd[a-f]|nvme[0-9]n[0-9]+", "ports":6}"""
    pattern = spec.get("pattern", r"sd[a-z]|nvme[0-9]+n[0-9]+")
    ports = int(spec.get("ports", 6))
    disks = {p: {"port": p, "isExist": 0} for p in range(1, ports + 1)}
    port_idx = 1
    for dev in _iter_block_devices(pattern):
        if port_idx > ports:
            break
        try:
            with open(os.path.join(dev, "size")) as f:
                size = int(f.read().strip()) * 512
            rota = os.path.exists(os.path.join(dev, "queue/rotational")) and \
                open(os.path.join(dev, "queue/rotational")).read().strip() == "1"
            state = "normal"
            sp = os.path.join(dev, "device/state")
            if os.path.exists(sp):
                st = open(sp).read().strip()
                state = "normal" if st == "running" else ("sleep" if st in ("suspended", "sleeping") else "normal")
            disks[port_idx] = {
                "port": port_idx, "isExist": 1, "isSleep": 0,
                "status": state, "diskCap": _fmt_size_gb(size),
                "diskForm": "HDD" if rota else "SSD",
            }
            port_idx += 1
        except Exception:
            continue
    return {"code": 0, "diskList": list(disks.values())}


def _read_sysfs(path, dflt=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return dflt


def _iface_addrs(name):
    """返回 (ipv4, ipv6, mask)。多级兜底：
       ① `ip -o addr show dev X`（首选，信息最全）
       ② /proc/net/fib_trie + /sys/class/net/X/... （无 ip 命令时）
    ★ 不能只依赖 `ip` 命令：systemd 服务里 PATH 可能不含 /sbin，
      那样 ipv4 会静默为空 → 页面 IP 行显示 "--"。
    飞牛OS 网卡常只有 IPv6 链路本地地址，必须跳过 fe80:: 否则
      页面会显示一串无意义的 fe80 地址。
    """
    ipv4 = ipv6 = mask = ""
    # ── ① ip 命令 ──
    try:
        r = subprocess.run(["ip", "-o", "addr", "show", "dev", name],
                           capture_output=True, text=True, timeout=3)
        for ln in r.stdout.splitlines():
            m = re.search(r"\binet6?\s+([0-9a-fA-F:.]+)(?:/(\d+))?", ln)
            if not m:
                continue
            addr, plen = m.group(1), m.group(2)
            if "inet6" in ln:
                low = addr.lower()
                if low.startswith("fe80") or low == "::1":
                    continue                      # 跳过链路本地 / 回环
                if not ipv6:
                    ipv6 = addr
            else:
                if not ipv4:
                    ipv4 = addr
                    if plen:
                        mask = _prefix_to_mask(int(plen))
    except Exception:
        pass
    # ── ② /proc/net/fib_trie 兜底（仅在 ip 命令失败时用）──
    if not ipv4:
        try:
            with open("/proc/net/fib_trie") as f:
                lines = f.read().splitlines()
            cur = None
            for i, ln in enumerate(lines):
                m = re.match(r"\s*\|\S+\s+(\d+\.\d+\.\d+\.\d+)/32", ln)
                if m:
                    cur = m.group(1)
                    continue
                if cur is None:
                    continue
                # 往下找 3 行内是否出现本接口名
                for j in range(i + 1, min(i + 4, len(lines))):
                    if re.search(r"\b" + re.escape(name) + r"\b", lines[j]):
                        if not cur.startswith("127.") and not cur.startswith("169.254."):
                            ipv4 = cur
                        break
                if ipv4:
                    break
                cur = None
        except Exception:
            pass
    # ── ③ 兜底掩码 ──
    if not mask:
        mask = "255.255.255.0"
    return ipv4, ipv6, mask


def _prefix_to_mask(plen):
    try:
        plen = max(0, min(32, int(plen)))
    except Exception:
        return "255.255.255.0"
    bits = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
    return ".".join(str((bits >> s) & 0xFF) for s in (24, 16, 8, 0))


def _iface_gateway(name):
    """读该网卡的默认网关。优先 `ip route`，失败再读 /proc/net/route。"""
    try:
        r = subprocess.run(["ip", "-4", "route", "show", "dev", name],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"default via ([\d.]+)", r.stdout)
        if m:
            return m.group(1)
    except Exception:
        pass
    # /proc/net/route 兜底：Destination=00000000 即默认路由，Gateway 是小端 hex
    try:
        with open("/proc/net/route") as f:
            next(f)
            for ln in f:
                c = ln.split()
                if len(c) >= 3 and c[0] == name and c[1] == "00000000":
                    h = c[2]
                    if len(h) == 8:
                        b = [str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0)]
                        return ".".join(b)
    except Exception:
        pass
    return ""


def _iface_dns():
    """读 DNS（/etc/resolv.conf 的 nameserver）。飞牛OS 上通常是网关或 127.0.0.53。"""
    out = []
    try:
        with open("/etc/resolv.conf") as f:
            for ln in f:
                m = re.match(r"\s*nameserver\s+(\S+)", ln)
                if m:
                    out.append(m.group(1))
    except Exception:
        pass
    return (out + ["", ""])[:2]


def _is_virtual_by_sysfs(name, dev):
    """判断是否**确定**的虚拟接口（只用内核明确标记，不猜名字）。

    返回 (is_virtual, 原因)。★ 注意这套判定**不包含** -ovs 后缀检查：
    飞牛OS 的物理口就是 enp2s0-ovs，后缀是系统命名的一部分。
    """
    if name == "lo":
        return True, "回环"
    if os.path.exists(os.path.join(dev, "bridge")):
        return True, "内核标记为 bridge"
    if os.path.exists(os.path.join(dev, "bonding")):
        return True, "内核标记为 bond"
    # veth pair 有 iflink 且与 ifindex 不同
    iflink = _read_sysfs(os.path.join(dev, "iflink"), "")
    ifindex = _read_sysfs(os.path.join(dev, "ifindex"), "")
    if iflink and ifindex and iflink != ifindex:
        return True, "veth pair（iflink≠ifindex）"
    # tun/tap 的 type 是 65534
    if _read_sysfs(os.path.join(dev, "type"), "") == "65534":
        return True, "tun/tap"
    return False, ""


def build_eth_list(spec, ctx):
    """生成 eth 列表。配置：
        {"exclude":["lo","docker0","veth*"], "max":2, "include":[], "relaxed":false}

    客户端 net-status 组件（index-6fd73a6e.js）真实契约（逐字实测）：
        DEFAULT_ETH_ITEM = {eth_name, ip, ipv6, dns1, dns2, gw, mask, method, mac, connected}
        te.eth1 = Object.assign({}, DEFAULT_ETH_ITEM, e[0])      // e = 本接口的 eth 数组
        te.eth2 = Object.assign({}, DEFAULT_ETH_ITEM, e[1])
        d.value = !!eth1.connected || !!eth2.connected           // 顶部「正常/未联网」
        「网口N」卡片显示条件： ethN.connected
        ip / ipv6 为空时显示 "--"

    ★★★ 四条铁律 ★★★
      1) 【网卡名必须从系统发现】绝不硬编码 eth0 / enp2s0，
         也绝不用名字后缀判断物理或虚拟。飞牛OS 的物理口叫 enp2s0-ovs，
         曾把它写进 exclude 导致网络页永远无数据。
      2) 【数组不能为空】有物理网卡就绝不返回 []。为空时
         Object.assign({}, DEFAULT, undefined) 会跳过 undefined，
         结果顶部误显示「正常」但 IP 全是 "--"。
      3) 【connected 判定放宽】operstate 可能是 unknown，不能只认 up；
         有 IP 或有默认网关也算连通。
      4) 【IP 读取多级兜底】systemd 下 PATH 可能没有 /sbin，
         不能只依赖 `ip` 命令，必须退到 /proc/net/fib_trie 等。

    「哪张网卡有意义」完全由客观事实决定（见 score_iface）：
        有默认路由 > 有 IPv4 > operstate 为 up。
    """
    exclude = spec.get("exclude", [])
    include = spec.get("include", []) or []
    relaxed = bool(spec.get("relaxed", False))
    max_ports = int(spec.get("max", 2))

    def excluded(name):
        return any(re.fullmatch(p.replace("*", ".*"), name) for p in exclude)

    candidates, skipped = [], []
    for dev in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(dev)
        # include 白名单优先级最高（跳过一切过滤）
        if name in include:
            candidates.append(name)
            continue
        if excluded(name):
            skipped.append(f"{name}(exclude)")
            continue
        if _is_excluded_iface(name):
            skipped.append(f"{name}(虚拟接口名单)")
            continue
        virt, why = _is_virtual_by_sysfs(name, dev)
        if virt and not relaxed:
            skipped.append(f"{name}({why})")
            continue
        candidates.append(name)

    # ★ 按客观事实排序：有网关 → 有 IP → 有 MAC → up → 接口名
    candidates.sort(key=score_iface)

    # ★★★ 硬保障：持有 IPv4 或默认路由的接口【绝不能被漏掉】★★★
    #   把 qualify 的口强制提到最前，即使它不在候选里（例如被 exclude 误杀）。
    #   理由：屏幕要显示的就是「谁真的联网」，这是唯一不可妥协的判据。
    essential = []
    for dev in sorted(glob.glob("/sys/class/net/*")):
        n = os.path.basename(dev)
        try:
            has_ip = bool(_iface_ipv4(n))
            has_gw = bool(_iface_has_gateway(n))
        except Exception:
            has_ip = has_gw = False
        if has_ip or has_gw:
            essential.append(n)
    essential.sort(key=score_iface)
    for n in reversed(essential):
        if n in candidates:
            candidates.remove(n)
        candidates.insert(0, n)
        if n not in include:
            skipped.append(f"{n}(★持有IPv4/网关，强制纳入)")

    eths = []
    dns1, dns2 = _iface_dns()
    for name in candidates[:max_ports]:
        dev = os.path.join("/sys/class/net", name)
        oper = _read_sysfs(os.path.join(dev, "operstate"), "down")
        ipv4, ipv6, mask = _iface_addrs(name)
        gw = _iface_gateway(name)

        # ★ connected 放宽：up/unknown 都算通；拿到 IP 或网关也算通
        if oper in ("up", "unknown") or ipv4 or gw:
            connected = 1
        else:
            connected = 0

        try:
            speed = int(_read_sysfs(os.path.join(dev, "speed"), "0"))
        except ValueError:
            speed = 0

        eths.append({
            "eth_name": name,                        # ★ 从系统发现，非硬编码
            "ip":       ipv4,
            "ipv6":     ipv6,
            "dns1":     dns1,
            "dns2":     dns2,
            "gw":       gw,
            "mask":     mask,
            "method":   0,                           # 0=DHCP
            "mac":      _read_sysfs(os.path.join(dev, "address"), ""),
            "connected": connected,
            # 兼容旧字段（无副作用）
            "port":     len(eths) + 1,
            "status":   oper,
            "speed":    f"{speed}M" if speed > 0 else "0M",
        })

    if skipped:
        log(f"  [eth] 跳过接口: {', '.join(skipped)}", level="quiet")
    log(f"  [eth] 从系统发现 {len(candidates)} 张候选，入选 {len(eths)} 张: "
        + ", ".join(f"{e['eth_name']}({'up' if e['connected'] else 'down'},"
                    f"{e['ip'] or '-'})" for e in eths), level="quiet")
    return {"code": 0, "eth": eths}


def _iface_ip(name):
    """读网卡 IP（优先 /proc/net/fib_trie 太绕，用 hostname -I 过滤）"""
    try:
        r = subprocess.run(f"ip -4 -o addr show dev {name}".split(),
                           capture_output=True, text=True, timeout=3)
        m = re.search(r"inet\s+([\d.]+)", r.stdout)
        return m.group(1) if m else ""
    except Exception:
        return ""


def build_space_list(spec, ctx):
    """生成存储空间列表（action=get_space_list，走 1.2/quota）。
    ★ 客户端只接受 space_state ∈ {clean, degraded, syncing}，
      其它值（如 "normal"）会被过滤掉导致列表为空。这里统一映射为 clean。
    配置：{"mounts":["/vol1","/vol2"]}；未配置则自动探测 /vol* 挂载点。"""
    mounts = spec.get("mounts")
    if not mounts:
        try:
            with open("/proc/mounts") as f:
                mounts = sorted({ln.split()[1] for ln in f
                                 if re.match(r"^/dev/\S+\s+/vol\d+", ln)})
        except Exception:
            mounts = []
    spaces = []
    for i, mnt in enumerate(mounts, 1):
        try:
            st = os.statvfs(mnt)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            # 客户端硬性要求：clean / degraded / syncing
            state = "clean" if free > 0 else "degraded"
            spaces.append({
                "space_index": i,
                "space_path": mnt,
                "space_state": state,
                "space_name": f"存储空间{i}",
            })
        except OSError:
            continue
    return {"code": 0, "space_list": spaces}


def build_space_manage(spec, ctx):
    """★ action=space_manage（1.2/config，普通 GET）真实响应结构：
        {"code":0,
         "space_list":[{"disk_info":[
              {"port":1,"disk_state":"normal","smart_status":"normal",
               "disk_cap":4000000000000,"disk_form":"HDD"}, ...]}],
         "unused_disk_list":[{"port":3,"disk_power":0,"disk_cap":...,"disk_form":"SSD"}]}

    客户端 getDiskListInfo 遍历 space_list[].disk_info[]，用
    changeCap(disk_cap) 自行换算容量。注意 disk_cap 是「字节数」。
    """
    pattern = spec.get("pattern", r"sd[a-z]|nvme[0-9]+n[0-9]+")
    disk_info = []
    port = 1
    for dev in _iter_block_devices(pattern):
        try:
            with open(os.path.join(dev, "size")) as f:
                size = int(f.read().strip()) * 512
            rota_p = os.path.join(dev, "queue/rotational")
            rota = os.path.exists(rota_p) and open(rota_p).read().strip() == "1"
            state = "normal"
            sp = os.path.join(dev, "device/state")
            if os.path.exists(sp):
                st = open(sp).read().strip()
                state = "sleep" if st in ("suspended", "sleeping", "standby") else "normal"
            disk_info.append({
                "port": port,
                "disk_state": state,
                "smart_status": "normal",
                "disk_cap": size,
                "disk_form": "HDD" if rota else "SSD",
            })
            port += 1
        except Exception:
            continue
    out = {"code": 0, "space_list": [{"disk_info": disk_info}], "unused_disk_list": []}
    # 兼容旧前端：额外给出扁平 diskList（无副作用）
    out["diskList"] = [
        {"port": d["port"], "isExist": 1,
         "isSleep": 1 if d["disk_state"] == "sleep" else 0,
         "status": d["smart_status"], "diskCap": _fmt_size_gb(d["disk_cap"]),
         "diskForm": d["disk_form"]}
        for d in disk_info
    ]
    return out


def build_update_desc(spec, ctx):
    """升级检查：固定返回「无新版本」（-2099）"""
    return {"code": spec.get("code", -2099), "msg": "no new version"}


# ============================================================
# ★★★ LED / 氛围灯状态管理（第 5 轮探测后的务实方案）★★★
# ------------------------------------------------------------
# 【背景】5 轮探测结论：HS-AFS-R1 的氛围灯【没有任何可用软件控制入口】
#   · /sys/class/leds 只有 6 个 igc-* 网口灯（max_brightness=1，非氛围灯）
#   · /boot/board.json 不存在 → led-set / set_gpio-init 服务全部空跑
#   · system_setled.sh 全文 396 行，纯 sysfs 批量配置器，无 GPIO/WMI 直控
#   · 官方 trim_miniscreen.service 是 disabled 且脚本不存在
#   · 现场无任何进程持有 LED 设备
#   · 仅剩 GPIO / ACPI WMI 私有通道，需深度逆向，暂不投入
#
# 【方案】把 LED 设置当作【应用级持久化状态】处理：
#   · 读接口(get_atmo_light/get_light_theme) → 返回内部持久化状态
#   · 写接口(set_atmo_light/set_light_theme/set_atmo_color) → 写内部状态文件
#   这样屏幕 UI 的「设置能存住、关闭再打开回显一致」需求 100% 满足；
#   将来若打通硬件通道，只需在 apply_to_hardware() 补一层，上层接口不变。
#
# 【状态文件】/var/lib/hiksemi-sub/led.json（无权限则退回脚本同目录）
#   {
#     "status": 1,        // 0=关闭, 非0=开启
#     "mode":   1,        // 0=随主题, 1=其他
#     "color_id": 2,      // 0~4（对应 LightEffect-1000）
#     "color_r": 30, "color_g": 180, "color_b": 25,
#     "light_mode": 9     // set_atmo_light 收到的最新 light_mode
#   }
# ============================================================

LED_STATE_CANDIDATES = [
    "/var/lib/hiksemi-sub/led.json",          # 首选（标准状态目录）
    os.path.join(BASE, ".led-state.json"),    # 兜底（脚本同目录）
]

# 客户端 LightEffect 枚举 → (color_id, r, g, b)
# ★ 注意 1002「蓝」在客户端源码里就是 (30,180,25) —— 实为绿色，属源码 bug，
#   必须【原样透传】，不得"纠正"，否则屏幕显示与实际不一致。
LED_EFFECT_COLOR = {
    1000: (0, 123, 123, 123),
    1001: (1, 123, 123, 123),
    1002: (2, 30, 180, 25),      # ★ 源码 bug：名为蓝实为绿，原样保留
    1003: (3, 255, 255, 255),
    1004: (4, 255, 90, 0),
}

# 硬件落地后端：none / i2c / sysfs_multi
#   auto 探测：启动时若找到 I2C 上的 0x26 设备 → "i2c"
LED_HW_BACKEND = "none"

# ============================================================
# ★★★ I2C RGB 灯协议（逆向自原厂 config_callback / install_init）
# ------------------------------------------------------------
# 从设备 0x26，寄存器：
#   0x90 = 模式：0x0 关 / 0x1 静态单色常亮 / 0x2 彩虹 / 0x3 呼吸
#   0x92 = R (0~255)
#   0x93 = G (0~255)
#   0x94 = B (0~255)
# 设置顺序（原厂做法）：先写 RGB(0x92/93/94)，再写模式(0x90)。
# ============================================================
LED_I2C_ADDR = 0x26
LED_I2C_REG_MODE = 0x90
LED_I2C_REG_R = 0x92
LED_I2C_REG_G = 0x93
LED_I2C_REG_B = 0x94

LED_I2C_MODE_OFF = 0x0        # 关闭
LED_I2C_MODE_SOLID = 0x1      # 静态单色常亮
LED_I2C_MODE_RAINBOW = 0x2    # 彩虹（动态）
LED_I2C_MODE_BREATHING = 0x3  # 呼吸

# i2c 总线探测缓存（避免每次写都扫一遍）
_I2C_BUS_CACHE = {"bus": None, "checked_at": 0}
_I2C_BUS_CACHE_TTL = 30       # 秒

# Linux i2c-dev ioctl
I2C_SLAVE = 0x0703


# ── 低层 I2C 原语（单一注入点，便于桩测试；真机走 os/fcntl）──
# 测试可整体替换 _i2c_io 对象来模拟 /dev/i2c-N。
class _I2CIO:
    """真实环境的 I2C I/O 实现（Linux i2c-dev）"""

    @staticmethod
    def list_buses():
        """返回存在的 I2C 总线号列表（来自 /dev/i2c-*）"""
        out = []
        for dev in sorted(glob.glob("/dev/i2c-*")):
            m = re.search(r"/dev/i2c-(\d+)$", dev)
            if m:
                out.append(int(m.group(1)))
        return out

    @staticmethod
    def open(bus):
        return os.open(f"/dev/i2c-{bus}", os.O_RDWR)

    @staticmethod
    def close(fd):
        os.close(fd)

    @staticmethod
    def select(fd):
        """把 fd 选中到从设备地址（ioctl I2C_SLAVE）；失败抛 OSError"""
        fcntl.ioctl(fd, I2C_SLAVE, LED_I2C_ADDR)

    @staticmethod
    def write(fd, data):
        os.write(fd, bytes(data))

    @staticmethod
    def read(fd, n):
        return os.read(fd, n)


_i2c_io = _I2CIO


def _i2c_probe_bus(verbose=False, prefer_read=True):
    """★ 动态探测哪条 I2C 总线挂着 RGB 控制器（0x26）。

    判据（与原厂 install_init 一致）：能成功访问 0x26 的 0x90 寄存器。
    绝不硬编码总线号 —— 总线号随内核/硬件版本漂移。

    探测策略（★ 分级，避免"读失败=设备不存在"的误判）：
      1) 先 open + ioctl(I2C_SLAVE,0x26) —— 这一步失败基本说明该总线无此设备；
      2) 再尝试【读】0x90 —— 成功即命中（最强证据）；
      3) 读失败时【回退到写】0x90（写回读到的值或 0）—— 因为不少灯控寄存器
         是"只写"的（原厂用的就是 i2cset -f 强制写），读不到不代表设备不在；
         写成功同样认定命中。

    verbose=True 时把每条总线的失败原因打进日志（排查用）。
    """
    buses = _i2c_io.list_buses()
    if verbose:
        log(f"[led] I2C 探测：发现总线 {buses or '（无 /dev/i2c-*）'}", level="quiet")
    if not buses:
        # 无设备节点：多半是 i2c-dev 模块没加载
        if verbose:
            log("[led] /dev/i2c-* 不存在 → 可能 i2c_dev 模块未加载"
                "（尝试 modprobe i2c_dev）或未安装 i2c-tools", level="quiet")
        return None
    for bus in buses:
        fd = None
        try:
            fd = _i2c_io.open(bus)
        except OSError as e:
            if verbose:
                log(f"[led] /dev/i2c-{bus}: open 失败 → {e}"
                    + ("（★注意是否权限不足，非 root 可能 EACCES）"
                       if getattr(e, "errno", None) == 13 else ""), level="quiet")
            continue
        try:
            # ① 选中从设备：这一步失败=该总线无此设备
            _i2c_io.select(fd)
        except OSError as e:
            if verbose:
                log(f"[led] /dev/i2c-{bus}: 选中 0x{LED_I2C_ADDR:02x} 失败 → {e}", level="quiet")
            _i2c_io.close(fd)
            continue
        # ② 尝试读 0x90（最强证据）
        try:
            _i2c_io.write(fd, [LED_I2C_REG_MODE])   # 1 字节=只设寄存器指针，无副作用
            _i2c_io.read(fd, 1)
            if verbose:
                log(f"[led] /dev/i2c-{bus}: ✅ 命中 0x{LED_I2C_ADDR:02x}（读成功）", level="quiet")
            _i2c_io.close(fd)
            return bus
        except OSError as e:
            if verbose:
                log(f"[led] /dev/i2c-{bus}: 读 0x90 失败（{e}）→ 回退到指针写探测", level="quiet")
        # ③ 回退：只写 1 字节寄存器指针（无副作用，不改变灯效）
        try:
            _i2c_io.write(fd, [LED_I2C_REG_MODE])
            if verbose:
                log(f"[led] /dev/i2c-{bus}: ✅ 命中 0x{LED_I2C_ADDR:02x}"
                    "（写指针成功，寄存器可能只写）", level="quiet")
            _i2c_io.close(fd)
            return bus
        except OSError as e:
            if verbose:
                log(f"[led] /dev/i2c-{bus}: ❌ 读写均失败 → {e}", level="quiet")
        finally:
            try:
                _i2c_io.close(fd)
            except OSError:
                pass
    return None


def _i2c_bus():
    """取 I2C 总线号（带 TTL 缓存）"""
    now = time.time()
    if _I2C_BUS_CACHE["bus"] is not None and \
       now - _I2C_BUS_CACHE["checked_at"] < _I2C_BUS_CACHE_TTL:
        return _I2C_BUS_CACHE["bus"]
    bus = _i2c_probe_bus()
    _I2C_BUS_CACHE["bus"] = bus
    _I2C_BUS_CACHE["checked_at"] = now
    if bus is not None:
        log(f"[led] I2C 灯控总线探测到: /dev/i2c-{bus}（从设备 0x{LED_I2C_ADDR:02x}）", level="quiet")
    return bus


def _i2c_wreg(bus, reg, value):
    """写一个 8 位寄存器：fd ← [reg, value]"""
    fd = _i2c_io.open(bus)
    try:
        _i2c_io.select(fd)
        _i2c_io.write(fd, [reg & 0xFF, value & 0xFF])
    finally:
        _i2c_io.close(fd)


def _i2c_rreg(bus, reg):
    """读一个 8 位寄存器"""
    fd = _i2c_io.open(bus)
    try:
        _i2c_io.select(fd)
        _i2c_io.write(fd, [reg & 0xFF])
        return _i2c_io.read(fd, 1)[0]
    finally:
        _i2c_io.close(fd)


def _ensure_i2c_dev_module():
    """★ 确保 i2c-dev 内核模块已加载（否则 /dev/i2c-* 不存在）。

    原厂 install_init 里就是这么做的。中间件以 root 启动时自动补上，
    非 root 时静默失败（后续日志会提示）。
    """
    if glob.glob("/dev/i2c-*"):
        return True
    # 先看模块是否已在（可能已加载但无设备）
    try:
        with open("/proc/modules", encoding="utf-8") as f:
            if "i2c_dev" in f.read():
                return bool(glob.glob("/dev/i2c-*"))
    except OSError:
        pass
    try:
        subprocess.run(["modprobe", "i2c_dev"], check=False,
                       timeout=10, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    time.sleep(0.3)
    ok = bool(glob.glob("/dev/i2c-*"))
    if ok:
        log("[led] 已自动加载 i2c_dev 模块（/dev/i2c-* 出现）", level="quiet")
    return ok


def led_detect_backend():
    """启动时探测硬件后端；返回 'i2c' 或 'none'"""
    global LED_HW_BACKEND
    if fcntl is None:
        log("[led] 当前平台无 fcntl，跳过 I2C 后端", level="quiet")
        LED_HW_BACKEND = "none"
        return LED_HW_BACKEND
    # ★ 先确保 i2c-dev 模块加载（原厂 install_init 同款做法）
    _ensure_i2c_dev_module()
    # ★ 启动时用 verbose 探测：把每条总线的失败原因写进日志（排查必需）
    bus = _i2c_probe_bus(verbose=True)
    _I2C_BUS_CACHE["bus"] = bus
    _I2C_BUS_CACHE["checked_at"] = time.time()
    if bus is not None:
        LED_HW_BACKEND = "i2c"
        log(f"[led] ✅ I2C 灯控总线 = /dev/i2c-{bus}（从设备 0x{LED_I2C_ADDR:02x}）", level="quiet")
    else:
        LED_HW_BACKEND = "none"
        log("[led] ❌ 未探测到 I2C 灯控 → 仅应用级状态。"
            "排查：① lsmod | grep i2c_dev ② ls /dev/i2c-* "
            "③ sudo i2cdetect -l ④ 中间件是否 root（非 root 需 udev 授权）", level="quiet")
    return LED_HW_BACKEND


def _apply_i2c(state, persist=True):
    """★ 把 LED 状态通过 I2C 落地到硬件。

    映射规则（客户端枚举语义 → 硬件模式）：
      关闭(status==0)                 → 0x90 = 0（off）
      随主题(color_id==0)             → 0x90 = 2（rainbow，动态）
      动态色(color_id==1)             → 0x90 = 3（breathing）
      固定色(color_id 2/3/4)          → 先写 RGB(0x92/93/94)，再 0x90 = 1（solid）

    ★ 顺序严格照原厂：先 RGB 后模式。
    """
    bus = _i2c_bus()
    if bus is None:
        return False
    try:
        on = int(state.get("status", 1)) != 0
        cid = int(state.get("color_id", 0))
        r = int(state.get("color_r", 0)) & 0xFF
        g = int(state.get("color_g", 0)) & 0xFF
        b = int(state.get("color_b", 0)) & 0xFF

        if not on:
            _i2c_wreg(bus, LED_I2C_REG_MODE, LED_I2C_MODE_OFF)
            log("[led] I2C → 关灯 (0x90=0x0)", level="quiet")
            return True

        if cid == 0:
            # 随主题 → 彩虹动态
            _i2c_wreg(bus, LED_I2C_REG_MODE, LED_I2C_MODE_RAINBOW)
            log("[led] I2C → 随主题/彩虹 (0x90=0x2)", level="quiet")
        elif cid == 1:
            # 动态色 → 呼吸
            _i2c_wreg(bus, LED_I2C_REG_MODE, LED_I2C_MODE_BREATHING)
            log("[led] I2C → 动态色/呼吸 (0x90=0x3)", level="quiet")
        else:
            # 固定色：先 RGB 再 solid
            _i2c_wreg(bus, LED_I2C_REG_R, r)
            _i2c_wreg(bus, LED_I2C_REG_G, g)
            _i2c_wreg(bus, LED_I2C_REG_B, b)
            time.sleep(0.1)
            _i2c_wreg(bus, LED_I2C_REG_MODE, LED_I2C_MODE_SOLID)
            log(f"[led] I2C → 固定色 solid rgb=({r},{g},{b}) (0x92/93/94 + 0x90=0x1)", level="quiet")
        return True
    except Exception as e:
        log(f"[led] I2C 写入失败: {e}", level="quiet")
        return False


def led_read_hardware():
    """★ 从硬件读回真实状态。

    返回 (mode, r, g, b)；失败返回 None。
    可用于「读接口反映硬件真值」—— 但读回的是硬件状态，
    与客户端枚举需再映射（见 parse_hw_to_client）。
    """
    bus = _i2c_bus()
    if bus is None:
        return None
    try:
        mode = _i2c_rreg(bus, LED_I2C_REG_MODE)
        r = _i2c_rreg(bus, LED_I2C_REG_R)
        g = _i2c_rreg(bus, LED_I2C_REG_G)
        b = _i2c_rreg(bus, LED_I2C_REG_B)
        return (mode, r, g, b)
    except Exception as e:
        log_err("led_read", f"[led] I2C 读取失败: {e}")
        return None


LED_DEFAULTS = {
    "status": 1,
    "mode": 1,
    "color_id": 0,
    "color_r": 123,
    "color_g": 123,
    "color_b": 123,
    "light_mode": 9,
}


# ============================================================
# ★★★ 散热风扇（4 档）状态 + 外部应用落地
# ------------------------------------------------------------
# 客户端契约（逆向自 index-be27a12a.js，详见 docs/fan-control.md）：
#   · UI 四档 id（= 发给后端的 fan_mode）：智能=5 / 低速=2 / 标准=3 / 强劲=4
#     ★ 取值不连续，没有 1。
#   · 写：GET /rest/1.1/config?action=set_hardware&fan_mode={5|2|3|4}，成功判据 code===0
#   · 读：【无专用读接口】。回显靠设备状态帧 get_hw_resource_info 的 fanmode 字段
#         （客户端严格相等 fanmode === FAN_LIST[].id）。
#
# 本模块负责「加载 / 修改 / 保存」三件事，且④档值在此集中定义：
#   · 加载：启动时从状态文件读回上次档位（无效则回默认「智能」）
#   · 修改：set_hardware 写入 → 规范化 → 落盘
#   · 保存：文件持久化（线程安全 + mtime 感知外部人工改动）
#
# ★ 外部应用落地：
#   飞牛OS 上风扇的真实调速通道确定后，用统一钩子 apply_to_external_app() 落地。
#   已接入【CoolerControl】适配器（HIKSEMI_FAN_COOLERCONTROL=1）——把 4 档翻译成
#   /etc/coolercontrol/{config.toml,modes.json} 的 4 个 Mode，见 docs/coolercontrol.md
#   与 tools/coolercontrol/。也支持直接指定外部命令（HIKSEMI_FAN_APPLY_CMD）
#   或 sysfs PWM 节点（HIKSEMI_FAN_PWM_PATH）作为兜底。
#   优先级：CMD > CoolerControl > sysfs PWM > 空实现。上层接口完全不动。
# ============================================================

# UI 四档 → 展示名（同时是合法 fan_mode 白名单，顺序即 UI 顺序）
FAN_MODES = {
    5: "智能",
    2: "低速",
    3: "标准",
    4: "强劲",
}
FAN_MODE_VALID = tuple(FAN_MODES.keys())          # (5, 2, 3, 4)
FAN_MODE_DEFAULT = 5                              # 默认「智能」

# 状态文件（首选标准状态目录，兜底脚本同目录）
FAN_STATE_CANDIDATES = [
    "/var/lib/hiksemi-sub/fan.json",              # 首选
    os.path.join(BASE, ".fan-state.json"),        # 兜底
]

# ★ 外部应用钩子：若设置该环境变量，则每次档位变化时执行该命令落地到系统。
#   占位符 {mode} / {name} 会被替换为当前 fan_mode 与档位名。
#   例：HIKSEMI_FAN_APPLY_CMD="/usr/local/bin/pwm-set {mode}"
FAN_APPLY_CMD_ENV = "HIKSEMI_FAN_APPLY_CMD"
# ★ CoolerControl 通道：置为 1/true 时启用内置适配器。
#   适配器逻辑 = 调用 <工具目录>/coolercontrol/apply-mode.sh {mode}
#   （见 tools/coolercontrol/ 与 docs/coolercontrol.md）
FAN_COOLERCONTROL_ENV = "HIKSEMI_FAN_COOLERCONTROL"
# 可覆盖 apply-mode.sh 的路径（默认自动定位）
FAN_COOLERCONTROL_SCRIPT_ENV = "HIKSEMI_FAN_COOLERCONTROL_SCRIPT"
#   也可直接指定 sysfs PWM 节点（兜底）：写入按档位换算后的 PWM 值
FAN_ATTR_PWM_ENV = "HIKSEMI_FAN_PWM_PATH"

# 四档 → 预留的 PWM 占空比（0–255，典型 8-bit sysfs）。仅在配置了
# HIKSEMI_FAN_PWM_PATH 时使用；「智能」交由外部/内核自行调速，不直接写死。
FAN_MODE_PWM = {
    2: 64,        # 低速
    3: 140,       # 标准
    4: 255,       # 强劲
}

# apply-mode.sh 的默认搜索路径（相对本脚本目录 tools/middleware/ 的上一级）
FAN_COOLERCONTROL_SCRIPT_CANDIDATES = [
    os.path.join(os.path.dirname(BASE), "coolercontrol", "apply-mode.sh"),
    os.path.join(BASE, "coolercontrol", "apply-mode.sh"),
    "/screen/tools/coolercontrol/apply-mode.sh",
]


def _normalize_fan_mode(v):
    """把任意输入规范化为合法档位（5/2/3/4）。无法解析时用默认「智能」5。

    ★ 不做"最近邻夹取"：四档间不存在数值大小关系（5 不是最大），
      夹取会产生错误档位，故非法值一律回默认档。
    ★ 也拒绝非整数（如 2.9）——避免 "2.9 被截断成 2" 这类静默误判。
    """
    if isinstance(v, bool):            # bool 是 int 子类，显式排除
        return FAN_MODE_DEFAULT
    if isinstance(v, float) and not v.is_integer():
        return FAN_MODE_DEFAULT
    try:
        n = int(v)
    except (TypeError, ValueError):
        return FAN_MODE_DEFAULT
    return n if n in FAN_MODE_VALID else FAN_MODE_DEFAULT


class FanState:
    """散热风扇档位内部状态（线程安全、文件持久化、mtime 感知外部改动）"""

    def __init__(self, path=None):
        self.path = path or self._pick_path()
        self._lock = threading.Lock()
        self._data = {"fan_mode": FAN_MODE_DEFAULT}
        self._mtime = 0
        self.load()

    def _pick_path(self):
        for p in FAN_STATE_CANDIDATES:
            d = os.path.dirname(p)
            try:
                os.makedirs(d, exist_ok=True)
                if os.access(d, os.W_OK):
                    return p
            except Exception:
                continue
        return FAN_STATE_CANDIDATES[-1]

    def load(self):
        """加载：从文件读回上次档位（无效 → 默认「智能」）"""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "fan_mode" in raw:
                self._data["fan_mode"] = _normalize_fan_mode(raw["fan_mode"])
            self._mtime = os.path.getmtime(self.path)
        except Exception as e:
            log(f"[fan] 状态文件读取失败（用默认值）: {e}", level="quiet")

    def _save_locked(self):
        """保存：原子写入（tmp + os.replace）"""
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
        except Exception as e:
            log(f"[fan] 状态文件写入失败: {e}", level="quiet")

    def get(self):
        """读：返回当前档位（dict）。人工改了文件也能感知。"""
        with self._lock:
            try:
                if os.path.exists(self.path) and \
                   os.path.getmtime(self.path) != self._mtime:
                    self.load()
            except Exception:
                pass
            return dict(self._data)

    def mode(self):
        """便捷读：直接返回 fan_mode 整数"""
        return int(self.get().get("fan_mode", FAN_MODE_DEFAULT))

    def update(self, fan_mode):
        """修改 + 保存 + 触发外部应用落地。返回 (新状态, 是否变化)。"""
        new_mode = _normalize_fan_mode(fan_mode)
        with self._lock:
            prev = int(self._data.get("fan_mode", FAN_MODE_DEFAULT))
            self._data["fan_mode"] = new_mode
            self._save_locked()
            state = dict(self._data)
        log(f"[fan] 档位已保存 fan_mode={new_mode}"
            f"（{FAN_MODES.get(new_mode, '未知')}）"
            f"{'（未变化）' if prev == new_mode else f'（原 {prev}）'}", level="quiet")
        # ★ 外部应用落地（无论是否变化都重放一次，保证与系统一致）
        apply_to_external_app(prev, state)
        return state, prev != new_mode


def _resolve_coolercontrol_script():
    """定位 apply-mode.sh：优先环境变量，其次默认候选路径。返回路径或 None。"""
    p = os.environ.get(FAN_COOLERCONTROL_SCRIPT_ENV, "").strip()
    if p:
        return p if os.path.isfile(p) else None
    for c in FAN_COOLERCONTROL_SCRIPT_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def _coolercontrol_enabled():
    """HIKSEMI_FAN_COOLERCONTROL 是否开启（1/true/yes/on 均可）。"""
    v = os.environ.get(FAN_COOLERCONTROL_ENV, "").strip().lower()
    return v in ("1", "true", "yes", "on")


def apply_to_external_app(prev_mode, state):
    """★ 把风扇档位落地到【外部应用 / 系统】的钩子。

    按优先级尝试，全部不可用时安全退化为「仅应用级状态」：

      1) HIKSEMI_FAN_APPLY_CMD 指定的外部命令（{mode}/{name} 占位）
      2) HIKSEMI_FAN_COOLERCONTROL=1 → 调用内置 CoolerControl 适配器
         （coolercontrol/apply-mode.sh {mode}，写入 config.toml/modes.json）
      3) HIKSEMI_FAN_PWM_PATH 指定的 sysfs PWM 节点（按 FAN_MODE_PWM 写值）
      4) 都没有 → 空实现（仅记录/回显，不改硬件）

    返回 True 表示"已尝试落地"。★ 任何异常都不向上抛，避免影响 REST 响应。
    """
    mode = int(state.get("fan_mode", FAN_MODE_DEFAULT))
    name = FAN_MODES.get(mode, "未知")

    # ── ① 外部命令钩子（用户显式指定，优先级最高）──
    cmd_tpl = os.environ.get(FAN_APPLY_CMD_ENV, "").strip()
    if cmd_tpl:
        try:
            cmd = cmd_tpl.replace("{mode}", str(mode)).replace("{name}", name)
            r = subprocess.run(cmd, shell=True, capture_output=True, timeout=10)
            if r.returncode == 0:
                log(f"[fan] 外部命令已执行: {cmd}", level="quiet")
                return True
            err = (r.stderr or b"").decode(errors="replace").strip()
            log(f"[fan] ⚠ 外部命令失败(rc={r.returncode}): {cmd}"
                + (f" — {err}" if err else ""), level="quiet")
        except Exception as e:
            log(f"[fan] ⚠ 外部命令异常: {e}", level="quiet")
        return False

    # ── ② CoolerControl 适配器 ──
    if _coolercontrol_enabled():
        script = _resolve_coolercontrol_script()
        if not script:
            log(f"[fan] ⚠ 已启用 {FAN_COOLERCONTROL_ENV} 但找不到 apply-mode.sh"
                f"（候选: {FAN_COOLERCONTROL_SCRIPT_CANDIDATES}）", level="quiet")
            return False
        try:
            # 超时放宽到 30s：apply-mode.sh 内部可能重启 daemon
            r = subprocess.run([script, str(mode)], capture_output=True, timeout=30)
            out = (r.stdout or b"").decode(errors="replace").strip()
            err = (r.stderr or b"").decode(errors="replace").strip()
            if r.returncode == 0:
                tail = (err or out).splitlines()[-1] if (err or out) else ""
                log(f"[fan] CoolerControl 已切换: {name}({mode})"
                    + (f" — {tail}" if tail else ""), level="quiet")
                return True
            log(f"[fan] ⚠ CoolerControl 切换失败(rc={r.returncode}): {script} {mode}"
                + (f" — {err}" if err else ""), level="quiet")
        except Exception as e:
            log(f"[fan] ⚠ CoolerControl 适配器异常: {e}", level="quiet")
        return False

    # ── ③ sysfs PWM 节点（兜底）──
    pwm_path = os.environ.get(FAN_ATTR_PWM_ENV, "").strip()
    if pwm_path:
        # 「智能」不直接写死 PWM（交由内核/EC 自调），仅刷新其余档
        if mode in FAN_MODE_PWM:
            try:
                with open(pwm_path, "w") as f:
                    f.write(str(FAN_MODE_PWM[mode]))
                log(f"[fan] 已写 sysfs PWM: {pwm_path} = {FAN_MODE_PWM[mode]}"
                    f"（{name}）", level="quiet")
                return True
            except Exception as e:
                log(f"[fan] ⚠ sysfs PWM 写入失败({pwm_path}): {e}", level="quiet")
                return False
        log(f"[fan] 「{name}」不写 PWM（交由外部/内核调速）", level="quiet")
        return True

    # ── ③ 空实现（仅应用级状态）──
    log(f"[fan] 未配置外部应用通道 → 仅保存档位（{name}={mode}），不落地硬件",
        level="debug")
    return True


# ============================================================
# ★★★ 自动息屏（熄屏时间）状态
# ------------------------------------------------------------
# 客户端契约（逆向自 index-be27a12a.js）：
#   读：get_off_time&cmdfrom=devsub → 客户端取 res.xp_time（标量秒数！）
#   写：set_off_time&xp_time={n}&cmdfrom=devsub → 期望 res.code===0
#   ★ 合法值：30 / 60 / 120 / 300（秒）；客户端列表 AUTO_CLOSE_LIST
#
# ★ 关键：客户端【只发偏好】，真正 "到点熄屏" 必须由服务端做 ——
#   监视空闲 → echo 1 > /sys/class/graphics/fb0/blank；有输入 → echo 0。
#   原厂 config_callback 的 set_screen_power() 就是这么实现的。
# ============================================================
OFF_TIME_CANDIDATES = [
    "/var/lib/hiksemi-sub/off-time.json",       # 首选
    os.path.join(BASE, ".off-time-state.json"),  # 兜底
]

# 合法档位（秒）。非法值会被夹到最近档位
OFF_TIME_VALID = (30, 60, 120, 300)
OFF_TIME_DEFAULT = 120          # 默认 2 分钟

# 屏幕开关：原厂实现（config_callback set_screen_power）
# 1 = 黑屏（关显示）/ 0 = 亮屏。优先 fb0，兜底自动找其它 fbN。
SCREEN_BLANK_PATH = "/sys/class/graphics/fb0/blank"
SCREEN_BLANK_FALLBACKS = [
    "/sys/class/graphics/fb0/blank",
    "/sys/class/graphics/fb1/blank",
]

# ★★★ 多 framebuffer 场景（本机实测 3 个 fb）：fb0 很可能不是小屏！
#   小屏通常是后面那个、且带 DSI 连接器。要【按连接器类型】挑，不能靠编号。
#   判据优先级：分辨率匹配 > DSI 连接器 > eDP/LVDS > 无信息 > fb0
_FB_CONNECTOR_PRIORITY = ("dsi", "edp", "lvds", "mipi", "dpi")

# ★★★ 小屏分辨率（物理事实，最强判据！）
#   程序内按 376×960 竖装；硬件标称 960×376。旋转属 drm 层面，
#   fb 的 virtual_size 可能是任意一种。两种都认。
PANEL_SIZES = {(376, 960), (960, 376)}

# 背光通道（亮度的真相在这里：原厂走 intel_backlight，值域 0–96000）
_BACKLIGHT_GLOBS = [
    "/sys/class/backlight/*/brightness",
    "/sys/class/backlight/*/bl_power",
]


def _fb_virtual_size(blank_path):
    """读该 fb 的 virtual_size，返回 (w, h)；失败返回 None。

    ★ 这是识别小屏【最可靠】的判据 —— 分辨率是物理事实，
      不像连接器名要靠路径推断、也不像"写 blank 看反应"那样会误判。
    """
    fbdir = _fb_device_of(blank_path)
    for base in (fbdir, os.path.realpath(fbdir)):
        try:
            with open(os.path.join(base, "virtual_size")) as f:
                raw = f.read().strip()
            a, b = raw.split(",")[:2]
            return (int(a), int(b))
        except Exception:
            continue
    return None


def _fb_name(blank_path):
    """读该 fb 的 name（如 mipi-dsi / inteldrmfb / vkms）"""
    fbdir = _fb_device_of(blank_path)
    for base in (fbdir, os.path.realpath(fbdir)):
        try:
            with open(os.path.join(base, "name")) as f:
                return f.read().strip()
        except Exception:
            continue
    return ""


def _fb_device_of(blank_path):
    """从 /sys/class/graphics/fbN/blank 反推 fbN 目录路径"""
    return os.path.dirname(blank_path)


def _fb_connector_name(blank_path):
    """尽力读出该 fb 对应的连接器名（DSI-1 / HDMI-A-1 / eDP-1 ...）

    路径很绕，逐级尝试；返回小写字符串，失败返回 ""。
    """
    fbdir = _fb_device_of(blank_path)
    # 1) .../graphics/fb0 → 真实设备目录（解 symlink）
    real = os.path.realpath(fbdir)          # /sys/devices/.../graphics/fb0
    devdir = os.path.dirname(real)          # .../drm/card0 或平台设备
    names = []
    # 2) 同级 / 上级找 drm 连接器目录名（含 DSI / HDMI 等关键字）
    for root in (devdir, os.path.dirname(devdir), os.path.dirname(os.path.dirname(devdir))):
        try:
            for entry in os.listdir(root):
                low = entry.lower()
                if any(k in low for k in ("dsi", "hdmi", "edp", "lvds", "vga", "dpi", "mipi")):
                    names.append(entry)
        except Exception:
            continue
        if names:
            break
    # 3) 兜底：读 fb 的 name 文件（如 "mipi-dsi"）
    for nf in (os.path.join(fbdir, "name"), os.path.join(real, "name")):
        try:
            with open(nf) as f:
                nm = f.read().strip().lower()
            if nm:
                names.append(nm)
        except Exception:
            pass
    return " ".join(names).lower()


def _score_blank_path(blank_path):
    """给候选 blank 路径打分，越高越可能是小屏。

    ★★★ 打分优先级（分辨率 > 连接器）：
      1. virtual_size 恰好等于小屏分辨率（376×960 / 960×376）→ 1000 分
         —— 物理事实，最强、最不会错
      2. 连接器含 DSI → 100 分；eDP/LVDS → 90...
      3. 明确是 HDMI/VGA → -50 分（肯定不是小屏）
      4. 都无信息 → 按编号（fb0 常是虚拟/主显，略微压低）
    """
    # ① 分辨率匹配（最强判据）
    size = _fb_virtual_size(blank_path)
    if size in PANEL_SIZES:
        return 1000
    # ② 连接器
    conn = _fb_connector_name(blank_path)
    for i, kw in enumerate(_FB_CONNECTOR_PRIORITY):
        if kw in conn:
            return 100 - i * 10
    # ③ 明确不是小屏的
    if any(k in conn for k in ("hdmi", "vga", "dvi")):
        return -50
    # ④ 无信息 → 按编号
    base = os.path.basename(os.path.dirname(blank_path))
    num = 0
    for ch in base:
        if ch.isdigit():
            num = num * 10 + int(ch)
    return 1 + num          # fb1 → 2 分，fb0 → 1 分


def _blank_candidates():
    """列出全部 blank 候选（去重），按"更像小屏"排序"""
    import glob as _g
    cands = []
    for p in SCREEN_BLANK_FALLBACKS + sorted(_g.glob("/sys/class/graphics/fb*/blank")):
        if p not in cands and os.path.exists(p):
            cands.append(p)
    cands.sort(key=_score_blank_path, reverse=True)
    return cands


def _resolve_blank_path():
    """返回最可能是小屏的 blank 路径（DSI 优先，其次其它 fbN）"""
    cands = _blank_candidates()
    return cands[0] if cands else None


def _read_blank(path):
    """读回 blank 当前值；失败返回 None"""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


class OffTimeState:
    """自动息屏时长（线程安全、文件持久化、mtime 感知外部改动）"""

    def __init__(self, path=None):
        self.path = path or self._pick_path()
        self._lock = threading.Lock()
        self._data = {"xp_time": OFF_TIME_DEFAULT}
        self._mtime = 0
        self.load()

    def _pick_path(self):
        for p in OFF_TIME_CANDIDATES:
            d = os.path.dirname(p)
            try:
                os.makedirs(d, exist_ok=True)
                if os.access(d, os.W_OK):
                    return p
            except Exception:
                continue
        return OFF_TIME_CANDIDATES[-1]

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict) and "xp_time" in raw:
                self._data["xp_time"] = _normalize_off_time(raw["xp_time"])
            self._mtime = os.path.getmtime(self.path)
        except Exception as e:
            log(f"[off_time] 状态文件读取失败（用默认值）: {e}", level="quiet")

    def _save_locked(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
        except Exception as e:
            log(f"[off_time] 状态文件写入失败: {e}", level="quiet")

    def get(self):
        """读：返回当前 xp_time"""
        with self._lock:
            try:
                if os.path.exists(self.path) and \
                   os.path.getmtime(self.path) != self._mtime:
                    self.load()
            except Exception:
                pass
            return dict(self._data)

    def update(self, **kw):
        """写：更新并落盘；返回完整状态"""
        with self._lock:
            if "xp_time" in kw:
                self._data["xp_time"] = _normalize_off_time(kw["xp_time"])
            self._save_locked()
            return dict(self._data)


def _normalize_off_time(v):
    """把任意输入夹到合法档位（30/60/120/300）；无法解析时用默认值"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return OFF_TIME_DEFAULT
    if n in OFF_TIME_VALID:
        return n
    # 夹到最近档位
    return min(OFF_TIME_VALID, key=lambda x: abs(x - n))


def build_off_time(spec, ctx):
    """读接口 builder：get_off_time。

    ★ 契约修正（关键 bug）：
      客户端 getCloseScreenPeriod() 取的是 res.xp_time（标量），
      而旧实现返回的是 off_time:[]（数组）→ 客户端拿到 undefined → UI 显示 "--"。
      正确响应：{"code": 0, "xp_time": <秒>}
    """
    st = ctx.get("off_time")
    data = st.get() if st else {"xp_time": OFF_TIME_DEFAULT}
    return {"code": 0, "xp_time": int(data.get("xp_time", OFF_TIME_DEFAULT))}


def resolve_off_time_write(spec, ctx):
    """写接口 resolver：set_off_time。

    客户端：set_off_time&xp_time={n}（n=30/60/120/300），期望响应 code=0。
    返回【字段值】(0)，避免 {"code":{"code":0}} 嵌套。
    """
    st = ctx.get("off_time")
    if st is None:
        return 0
    params = ctx.get("_params") or {}
    v = params.get("xp_time")
    if v is None:
        v = params.get("time")
    if v is None:
        return 0
    try:
        st.update(xp_time=v)
        # ★ 用户刚设完档 → 从此刻重新计时；若当前是黑屏，顺便亮回来，
        #   否则会出现「刚设完就立刻熄屏」的诡异体验。
        _IDLE["last_active"] = time.time()
        if _IDLE.get("blanked"):
            _screen_set(False)
            _IDLE["blanked"] = False
            log("[off_time] 设置后亮屏（重置计时）", level="quiet")
        log(f"[off_time] 已设置自动息屏 = {st.get()['xp_time']}s（计时已重置）", level="quiet")
    except Exception as e:
        log(f"[off_time] 设置失败: {e}", level="quiet")
    return 0


# ============================================================
# ★★★ 关机 / 重启（set_power）
# ------------------------------------------------------------
# 客户端契约（逆向自 index-be27a12a.js + index-6fd73a6e.js）：
#   GET /rest/1.1/config?action=set_power&power_mode={n}&cmdfrom=devsub
#     power_mode = 0 → 关机（确认关机弹窗 L() → ft(0)）
#     power_mode = 1 → 重启（确认重启弹窗 F() → ft(1)）
#   期望响应 {code: 0}；网络异常时客户端仅对 e===1(重启) 兜底 r({code:0})。
#
# ★ 这是【真正让机器断电/重启】的动作 —— 必须由服务端执行：
#     关机：systemctl poweroff（回退 poweroff / shutdown -h now）
#     重启：systemctl reboot  （回退 reboot  / shutdown -r now）
#   ⚠ 绝不能同步执行（否则 HTTP 回包前进程就被杀了，客户端收不到 code=0）
#     → 延迟 1.5s + 完全脱离父进程（start_new_session）后台触发。
# ============================================================
POWER_ACTIONS = {
    0: {
        "name": "关机",
        "cmds": [
            ["systemctl", "poweroff"],
            ["poweroff"],
            ["shutdown", "-h", "now"],
        ],
    },
    1: {
        "name": "重启",
        "cmds": [
            ["systemctl", "reboot"],
            ["reboot"],
            ["shutdown", "-r", "now"],
        ],
    },
}

# 触发前留给 HTTP 回包的时间（秒）
POWER_ARM_DELAY = 1.5


def _run_power_cmd(cmds):
    """依次尝试候选命令；成功启动即返回实际命令（后台、脱离进程组）。

    返回 (ok, cmd_str)；ok=True 表示至少一条命令被成功 fork。
    """
    for cmd in cmds:
        exe = shutil.which(cmd[0])
        if not exe:
            continue
        try:
            # start_new_session=True → 脱离本进程会话，父进程退出/被杀也不影响
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True, " ".join(cmd)
        except Exception as e:
            log(f"[power] 命令启动失败 {' '.join(cmd)}: {e}", level="quiet")
            continue
    return False, ""


def _picked_power_cmd():
    """返回当前会实际使用的关机命令（仅用于启动自检展示）"""
    for cmd in POWER_ACTIONS[0]["cmds"]:
        if shutil.which(cmd[0]):
            return cmd[0]
    return "无"


def _delayed_power(which, delay=POWER_ARM_DELAY):
    """延迟后执行关机/重启（守护线程内调用）"""
    time.sleep(delay)
    info = POWER_ACTIONS.get(which)
    if not info:
        return
    ok, used = _run_power_cmd(info["cmds"])
    if ok:
        log(f"[power] ★ 已触发{info['name']}：{used}", level="quiet")
    else:
        log(f"[power] ✗ {info['name']}失败：找不到可用命令"
            f"（需 root 或 polkit 权限；候选: "
            f"{' / '.join(c[0] for c in info['cmds'])}）", level="quiet")


def resolve_set_power(spec, ctx):
    """写接口 resolver：set_power（关机 / 重启）。

    这是【字段级 resolver】：只返回 code 字段的值(0)，副作用在此处触发。
    参数 power_mode：0=关机，1=重启。
    """
    params = ctx.get("_params") or {}
    raw = params.get("power_mode")
    if raw is None:
        raw = params.get("mode")
    try:
        which = int(raw)
    except (TypeError, ValueError):
        log(f"[power] ⚠ 非法 power_mode={raw!r}，忽略", level="quiet")
        return 0
    if which not in POWER_ACTIONS:
        log(f"[power] ⚠ 未知 power_mode={which}（仅 0=关机 / 1=重启），忽略", level="quiet")
        return 0
    info = POWER_ACTIONS[which]
    log(f"[power] 收到{info['name']}请求（power_mode={which}）"
        f"→ {POWER_ARM_DELAY}s 后执行", level="quiet")
    # ★ 异步触发：先回包，再动作
    threading.Thread(target=_delayed_power, args=(which,),
                     daemon=True, name=f"power-{which}").start()
    return 0


# ============================================================
# ★★★ 自动息屏：空闲监视 + 屏幕开关
# ------------------------------------------------------------
# 客户端只发偏好（set_off_time&xp_time=N），真正"到点熄屏"由服务端做：
#   · 空闲达到 xp_time 秒 → echo 1 > /sys/class/graphics/fb0/blank（黑屏）
#   · 一旦有触摸/按键输入   → echo 0 > .../blank（亮屏）并重置计时
#
# 输入来源（按可用性回退）：
#   1) /dev/input/event*  —— 读取事件（需 root 权限）；活动即重置空闲
#   2) xprintidle（若装了 x11-utils）—— 通过 X 空闲毫秒数判断
#   3) 都不可用 → 只保存设置，不实际熄屏（日志提示）
# ============================================================
_IDLE = {"last_active": time.time(), "blanked": False, "input_devs": None,
         "blank_fail_warned": False}


_SCREEN_APPLY_CACHE = {"path": None}      # 已验证【真实生效】的 blank 路径
_BACKLIGHT_SAVED = {}                     # 熄屏前的背光值，用于还原
_SCREEN_BLANKED = {"v": False}            # 当前是否处于"背光已熄"状态
_DPMS_UNWRITABLE_WARNED = set()           # 已提示过"不可写"的 DPMS 节点（避免刷屏）


def _write_blank(path, blank):
    """写 blank 并【回读校验】。返回 True 表示确认生效。

    ★★★ 关键教训：open() 不抛异常 ≠ 生效！
      某些 framebuffer 接受了写入但屏幕纹丝不动（写到了错误/无效的 fb）。
      必须回读比对，否则会误报「已写入」而屏幕依旧亮。
    """
    val = "1" if blank else "0"
    try:
        with open(path, "w") as f:
            f.write(val)
    except Exception as e:
        log(f"[off_time] ✗ blank 写入异常({path}): {e}", level="quiet")
        return False
    got = _read_blank(path)
    if got is None:
        # 读不回来（部分内核 blank 不可读）→ 乐观认为成功，但记一笔
        log(f"[off_time] ⚠ blank 回读失败({path})，无法确认生效", level="quiet")
        return True
    if got != val:
        log(f"[off_time] ✗ blank 写入【未生效】({path}): 写 {val} 但回读为 {got}", level="quiet")
        return False
    return True


def _backlight_paths():
    """列出所有背光可写节点 [(path, kind), ...]，kind = brightness / bl_power"""
    import glob as _g
    out = []
    for pat in _BACKLIGHT_GLOBS:
        for p in sorted(_g.glob(pat)):
            if os.access(p, os.W_OK):
                out.append((p, os.path.basename(p)))
    return out


def _screen_set_backlight(blank):
    """背光通道（★ 本机唯一被实测证明有效的屏控手段）。

    原厂走 `/sys/class/backlight/intel_backlight/brightness`（值域 0–96000）。
    诊断实测：`68160 → 0 → 68160` 写入/还原全部成功。

    ★ 双写保险：
       · `brightness = 0`      → 背光完全熄灭
       · `bl_power  = 4`       → FB_BLANK_POWERDOWN（部分驱动只认这个）
    亮屏时两者都还原。
    """
    paths = _backlight_paths()
    if not paths:
        return False
    ok_cnt = 0
    for p, kind in paths:
        try:
            if blank:
                if p not in _BACKLIGHT_SAVED:
                    cur = _read_sysfs_str(p)
                    if cur is None:
                        cur = "96000"
                    _BACKLIGHT_SAVED[p] = cur
                val = "4" if kind == "bl_power" else "0"
                with open(p, "w") as f:
                    f.write(val)
            else:
                if p in _BACKLIGHT_SAVED:
                    val = "0" if kind == "bl_power" else _BACKLIGHT_SAVED[p]
                    with open(p, "w") as f:
                        f.write(val)
                else:
                    continue
            # ★ 回读校验：sysfs 写入成功不代表生效（教训 57）
            got = _read_sysfs_str(p)
            if got is not None and got.strip() == val:
                ok_cnt += 1
            elif got is not None:
                log(f"[off_time] ✗ 背光未生效({p}): 写 {val} 但回读 {got.strip()}", level="quiet")
        except Exception as e:
            log(f"[off_time] 背光写入失败({p}): {e}", level="quiet")
    if ok_cnt:
        log(f"[off_time] 背光通道{'熄屏' if blank else '亮屏'}"
            f"（{ok_cnt}/{len(paths)} 个节点已确认写入并回读生效）", level="quiet")
    return ok_cnt > 0


def _read_sysfs_str(path):
    """读 sysfs 文本，失败返回 None"""
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None


def _drm_connectors():
    """列出 DRM 连接器目录（/sys/class/drm/cardN-XXX），返回 [(dir, name), ...]

    ★ 小屏若是 DSI 输出，会出现在这里。DPMS 是 DRM 层面关屏的标准接口。
    """
    import glob as _g
    out = []
    for d in sorted(_g.glob("/sys/class/drm/card[0-9]*-*")):
        name = os.path.basename(d).split("-", 1)[-1]
        out.append((d, name))
    return out


def _screen_set_dpms(blank):
    """DPMS 通道：写 /sys/class/drm/cardN-XXX/dpms。

    "Off" = 关闭输出（屏幕黑）；"On" = 打开。
    ★ 只对状态为 connected 且是我们目标的连接器操作。

    ★ 注意：DPMS 只是【备用】通道（P2），背光（P1）才是本机唯一验证有效的手段。
      很多驱动把 sysfs `dpms` 设为 root 也不可写（EACCES）——**这是正常现象**，
      不应每次熄屏都刷错误日志。故：只在【首次】遇到不可写时记一条 warning，
      之后静默跳过（用一个模块级 set 去重）。
    """
    acted = False
    for d, name in _drm_connectors():
        st = ""
        try:
            with open(os.path.join(d, "status")) as f:
                st = f.read().strip()
        except Exception:
            continue
        if st != "connected":
            continue
        dpms = os.path.join(d, "dpms")
        if not os.path.exists(dpms):
            continue
        try:
            with open(dpms, "w") as f:
                f.write("Off" if blank else "On")
            acted = True
            log(f"[off_time] DPMS {name} → {'Off' if blank else 'On'}", level="quiet")
        except PermissionError:
            # root 也不可写 → 该驱动的 sysfs 不支持，属正常；只提示一次
            if dpms not in _DPMS_UNWRITABLE_WARNED:
                _DPMS_UNWRITABLE_WARNED.add(dpms)
                log(f"[off_time] DPMS {name} 不可写（内核/驱动不支持 root 写入）"
                    f"，已跳过（不影响背光熄屏）", level="quiet")
        except Exception as e:
            if dpms not in _DPMS_UNWRITABLE_WARNED:
                _DPMS_UNWRITABLE_WARNED.add(dpms)
                log(f"[off_time] DPMS {name} 写入失败({dpms}): {e}（已跳过）", level="quiet")
    return acted


def _screen_set(blank):
    """屏幕开关总入口：True=黑屏，False=亮屏。

    ★★★ 真机三轮验证得出的最终结论（2026-10-05）：

    事实 1：本机只有【一个】fb0，且 virtual_size=376,960 —— **fb0 就是小屏**，
            路径没错。写 fb0/blank 回读确实变成 1，但**屏幕不黑**。
    事实 2：**根因是 X server 通过 DRM 直接渲染**。程序跑在 X 上，
            X 已把画面直接提交给 DRM/KMS；而 `/sys/class/graphics/fb0/blank`
            只作用于 **framebuffer console 层**，管不到 X 的渲染输出
            ⇒ 写它当然不生效（回读=1 只是 fb 层的记录）。
    事实 3：**背光通道物理可用且立竿见影** —— 诊断实测
            `/sys/class/backlight/intel_backlight/brightness`
            `68160 → 0 → 68160` 写入与还原全部成功。

    ⇒ 因此策略调整为【背光优先】，blank 仅作补充：

      P1【背光】brightness=0（真正让屏幕变暗；原厂也是这条路）
      P2【DPMS】/sys/class/drm/.../dpms = Off（DRM 层面关输出）
      P3【blank】fb0/blank=1（framebuffer console 层，X 下多半无效，聊胜于无）

    返回 True 表示【至少一条通道确实执行成功】。
    """
    ok_any = False

    # ── P1：背光（首选！这是唯一被实测证明能让屏幕变暗的通道） ──
    try:
        if _screen_set_backlight(blank):
            ok_any = True
            _SCREEN_BLANKED["v"] = bool(blank)
    except Exception as e:
        log(f"[off_time] 背光通道异常: {e}", level="quiet")

    # ── P2：DPMS ──
    try:
        if _screen_set_dpms(blank):
            ok_any = True
    except Exception as e:
        log(f"[off_time] DPMS 通道异常: {e}", level="quiet")

    # ── P3：blank（分辨率匹配的 fb 优先） ──
    try:
        cands = _blank_candidates()
        exact = [p for p in cands if _fb_virtual_size(p) in PANEL_SIZES]
        for p in (exact or cands):
            if _write_blank(p, blank):
                if _SCREEN_APPLY_CACHE.get("path") != p:
                    _SCREEN_APPLY_CACHE["path"] = p
                    log(f"[off_time] blank 已写 {p}"
                        f"（virtual_size={_fb_virtual_size(p)}）", level="quiet")
                ok_any = True
                break
    except Exception as e:
        log(f"[off_time] blank 通道异常: {e}", level="quiet")

    if not ok_any:
        log("[off_time] ✗ 所有屏控通道均失败", level="quiet")
    return ok_any


def _find_input_devices():
    """找可读的输入设备（触摸屏/按键/鼠标）

    返回 [(path, fd, name, is_touch_like), ...]
      · name          —— 从 /sys/class/input/<ev>/device/name 读
      · is_touch_like —— 设备名含触摸/键盘等关键字（用于日志与优先级）
    """
    devs = []
    try:
        for p in sorted(glob.glob("/dev/input/event*")):
            try:
                fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
            except OSError:
                continue
            name = ""
            try:
                ev = os.path.basename(p)
                with open(f"/sys/class/input/{ev}/device/name") as f:
                    name = f.read().strip()
            except Exception:
                pass
            low = name.lower()
            touch_like = any(k in low for k in
                             ("touch", "mouse", "keyboard", "key", "button",
                              "wheel", "pen", "stylus", "tablet"))
            devs.append((p, fd, name, touch_like))
    except Exception:
        pass
    return devs


# ---- Linux input 事件解析（只认「真实用户操作」，丢弃噪声）----
# struct input_event（64 位内核）: time(16B) + type(u16) + code(u16) + value(s32) = 24B
#   type: EV_SYN=0  EV_KEY=1  EV_REL=2  EV_ABS=3  EV_MSC=4  EV_SW=5
#   ★ 关键：EV_SYN(SYN_REPORT=0) 是每帧结束标记，【本身不代表操作】。
#     一帧正常操作 = 若干 EV_ABS/EV_KEY + 1 个 EV_SYN。
#     有些触摸控制器即使无人碰也会持续发 SYN_REPORT（心跳/噪声），
#     若把「任何字节」都当活动，空闲计时永远累不起来 → 熄屏永远不触发。
_INPUT_EVENT_SIZE = 24
_EV_SYN, _EV_KEY, _EV_REL, _EV_ABS = 0, 1, 2, 3
_EV_MSC, _EV_SW = 4, 5
# 触摸坐标轴（ABS_MT_POSITION_X/Y 等）——这些才是"手指在动"
_ABS_TOUCH_CODES = {0x35, 0x36, 0x3a, 0x3b, 0x39, 0x30, 0x31, 0x32, 0x33, 0x34, 0x37, 0x38}
_KEY_BTN_TOUCH = 0x14a          # BTN_TOUCH：手指按下/抬起
_KEY_BTN_TOOL_FINGER = 0x145    # BTN_TOOL_FINGER


def _parse_input_events(data, strict=True):
    """解析 input_event 缓冲区，判断是否含「真实用户操作」。

    strict=True （默认）：
        只认 EV_KEY（按键/触摸按下/抬起）与 EV_ABS 中的触摸坐标变化；
        **忽略 EV_SYN / EV_MSC 等**——它们会被噪声设备持续产生。
    strict=False：
        退化为「有任何事件即算活动」（兼容不支持区分的设备）。

    返回 True 表示检测到真实操作。
    """
    if not data:
        return False
    if not strict:
        return True
    n = len(data) // _INPUT_EVENT_SIZE
    for i in range(n):
        off = i * _INPUT_EVENT_SIZE
        etype = int.from_bytes(data[off + 16:off + 18], "little")
        code = int.from_bytes(data[off + 18:off + 20], "little")
        value = int.from_bytes(data[off + 20:off + 24], "little", signed=True)
        if etype == _EV_KEY:
            # 任何按键类事件（含触摸按下/抬起）都算真操作。
            # 但 BTN_TOUCH 的 value==0（抬起）也算（一次完整点击的结束）。
            return True
        if etype == _EV_ABS and code in _ABS_TOUCH_CODES:
            # 触摸坐标变化：value != 0 才算（0 常是初始化/噪声）
            if value != 0:
                return True
        # EV_REL（鼠标移动）也认
        if etype == _EV_REL and value != 0:
            return True
        # EV_SYN / EV_MSC / EV_SW 一律忽略
    return False


def _idle_loop(off_time):
    """后台线程：监视空闲，到点熄屏；有输入则亮屏。
    ★ 输入来源按可用性回退：
      1) /dev/input/event*（读取事件；活动即唤醒。需 root 读权限）
      2) xprintidle（X 空闲毫秒数，装了 x11-utils 时）
    防抖：
      · 熄屏只写一次（blanked 标记），避免反复写 sysfs
      · 唤醒后立即重置计时，避免刚亮又马上黑
    """
    global _IDLE
    path = _resolve_blank_path()
    log(f"[off_time] 空闲监视启动: blank 路径={path}  "
        f"当前档位={off_time.get().get('xp_time')}s", level="quiet")
    # ★★★ 完整列出所有 fb 的真相（分辨率/名称/连接器/blank值）
    #    —— 这是判断"小屏到底是哪个 fb"的唯一可靠依据，务必看这里！
    all_cands = _blank_candidates()
    log(f"[off_time] 发现 {len(all_cands)} 个 framebuffer：", level="quiet")
    for i, c in enumerate(all_cands):
        vs = _fb_virtual_size(c)
        mark = "★小屏(分辨率匹配)" if vs in PANEL_SIZES else (
            "  ★选中" if i == 0 else "")
        log(f"[off_time]   {c}", level="quiet")
        log(f"[off_time]      virtual_size={vs}  name={_fb_name(c)!r}  "
            f"连接器={_fb_connector_name(c) or '未知'}  "
            f"blank={_read_blank(c)}{mark}", level="quiet")
    if not any(_fb_virtual_size(c) in PANEL_SIZES for c in all_cands):
        log(f"[off_time] ⚠⚠ 没有任何 fb 的分辨率匹配小屏 {sorted(PANEL_SIZES)}！", level="quiet")
        log("[off_time]     → 说明小屏的 framebuffer 未由内核创建，"
            "写 blank 大概率无效（需改用背光/DPMS 通道）", level="quiet")
    # 背光节点（真正能让屏幕变暗的通道）
    bl = _backlight_paths()
    log(f"[off_time] 背光节点 {len(bl)} 个: "
        + (", ".join(f"{p}" for p, _ in bl) if bl else "无"), level="quiet")
    if not bl:
        log("[off_time]     ⚠ 无背光节点 → blank 无效时无后备通道", level="quiet")
    # DRM 连接器（DPMS 通道，对 DSI 小屏有效）
    drm = _drm_connectors()
    if drm:
        log(f"[off_time] DRM 连接器 {len(drm)} 个：", level="quiet")
        for d, name in drm:
            st = "?"
            try:
                with open(os.path.join(d, "status")) as f:
                    st = f.read().strip()
            except Exception:
                pass
            dpms = os.path.join(d, "dpms")
            can = "可写" if os.path.exists(dpms) and os.access(dpms, os.W_OK) else "不可写"
            log(f"[off_time]   {name}  status={st}  dpms={can}", level="quiet")
    devs = _find_input_devices()
    _IDLE["input_devs"] = devs
    use_xprintidle = False
    if devs:
        for p, _fd, name, tl in devs:
            log(f"[off_time]   输入设备 {p}  name={name!r}"
                f"{'  ← 触摸/键鼠' if tl else ''}", level="quiet")
    elif shutil.which("xprintidle"):
        use_xprintidle = True
        log("[off_time] 空闲监视输入源: xprintidle", level="quiet")
    else:
        log("[off_time] ⚠ 无可读输入设备也无 xprintidle → 仅保存设置，不实际熄屏", level="quiet")
        log("[off_time]   排查：ls -l /dev/input/event*；"
            "是否以 root 运行；或 apt install x11-utils", level="quiet")
        return
    # ★ 屏幕真实状态：读 blank 并【同步】内部 blanked（修复"启动时已黑屏
    #   但内部以为是亮的"状态不一致 —— 那会导致屏幕一直黑着不恢复）
    blank_now = _read_blank(path) if path else None
    if blank_now is not None:
        log(f"[off_time] 当前 fb blank 值 = {blank_now}（0=亮 1=黑）[{path}]", level="quiet")
    else:
        log(f"[off_time] ⚠ 读 blank 失败: {path}", level="quiet")
    if blank_now == "1":
        _IDLE["blanked"] = True
        log("[off_time] ⚠ 屏幕当前是黑屏状态（可能上次异常退出未复位）"
            "→ 主动亮屏一次", level="quiet")
        _screen_set(False)
        _IDLE["blanked"] = False

    # ★ 计时基准 = 监视启动时刻（不是模块导入时刻！）
    _IDLE["last_active"] = time.time()
    _seen_timeout = off_time.get().get("xp_time", OFF_TIME_DEFAULT)
    _last_hb = time.time()          # ★ 心跳按【墙钟时间】计，不用 tick 计数
    _noise_warned = False
    while True:
        try:
            timeout = int(off_time.get().get("xp_time", OFF_TIME_DEFAULT))
        except Exception:
            timeout = OFF_TIME_DEFAULT

        # ★ 档位变化 → 重置计时：避免刚改完档就立刻按旧时长熄屏
        if timeout != _seen_timeout:
            _IDLE["last_active"] = time.time()
            log(f"[off_time] 档位变化 {_seen_timeout}s → {timeout}s，计时重置", level="quiet")
            _seen_timeout = timeout

        active = False

        # ── 路径 A：读 /dev/input/event* ──
        if devs:
            # ★ 用 select 等待，有输入立即返回（不再盲等 0.5s，唤醒更快）
            try:
                rl, _, _ = select.select([fd for _, fd, _, _ in devs], [], [], 0.5)
                for fd in rl:
                    try:
                        data = os.read(fd, 4096)
                        # ★★ 关键：解析事件，只认真实操作（EV_KEY/触摸坐标），
                        #    忽略 EV_SYN 等噪声 —— 否则触摸屏持续发 SYN 会导致
                        #    "已空闲"永远为 0、熄屏永远不触发。
                        if _parse_input_events(data):
                            active = True
                        elif data and not _noise_warned:
                            name = next((n for _p, f, n, _t in devs if f == fd), "?")
                            log(f"[off_time] 检测到噪声事件（{name!r}），已忽略"
                                f"（非真实操作，不计入活动）", level="quiet")
                            _noise_warned = True
                    except BlockingIOError:
                        pass
                    except OSError as e:
                        pid = next((q for q, f, _n, _t in devs if f == fd), None)
                        log(f"[off_time] 输入设备 {pid} 异常({e})，移除", level="quiet")
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        devs = [d for d in devs if d[1] != fd]
                        _IDLE["input_devs"] = devs
            except (OSError, ValueError) as e:
                # fd 全部失效或 select 参数问题 → 重扫设备
                log(f"[off_time] select 异常({e})，重新扫描输入设备", level="quiet")
                for _p, fd, _n, _t in devs:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                devs = _find_input_devices()
                _IDLE["input_devs"] = devs
                if not devs:
                    log("[off_time] ⚠ 输入设备全部失效 → 退回 xprintidle/停摆", level="quiet")
                    if not shutil.which("xprintidle"):
                        return
                    use_xprintidle = True
        else:
            # ── 路径 B：xprintidle ──
            try:
                r = subprocess.run(["xprintidle"], capture_output=True,
                                   text=True, timeout=3)
                idle_ms = int((r.stdout or "0").strip() or 0)
                if idle_ms < 800:
                    active = True
            except Exception:
                pass
            time.sleep(0.5)

        # ── 根据活动状态决策 ──
        if active:
            _IDLE["last_active"] = time.time()
            if _IDLE["blanked"]:
                ok = _screen_set(False)
                _IDLE["blanked"] = False
                log(f"[off_time] 检测到输入 → 亮屏"
                    f"（{'✅ 已确认生效' if ok else '✗ 未能生效'}）", level="quiet")
        else:
            idle = time.time() - _IDLE["last_active"]
            if idle >= timeout and not _IDLE["blanked"]:
                ok = _screen_set(True)
                if ok:
                    _IDLE["blanked"] = True
                    log(f"[off_time] 空闲 {int(idle)}s ≥ {timeout}s → 熄屏"
                        f"（✅ 已确认写入并回读生效）", level="quiet")
                else:
                    log(f"[off_time] ✗ 空闲 {int(idle)}s ≥ {timeout}s，但熄屏"
                        f"【未生效】（候选 fb: "
                        f"{', '.join(_blank_candidates()) or '无'}）", level="quiet")
                    # ★ 未生效就不要把 blanked 置 True —— 否则内部以为黑了、
                    #   实际还亮着，状态永久脱节，且不会再重试。
                    _IDLE["blanked"] = False
                    _IDLE["blank_fail_warned"] = True

        # 心跳：按【墙钟时间】每 ~30s 一次（★ 不能用 tick 计数 —— 有输入时
        #       select 会立即返回，循环转速远超预期，tick 计数会让心跳刷屏）
        _now = time.time()
        if _now - _last_hb >= 30:
            _last_hb = _now
            idle_now = int(_now - _IDLE["last_active"])
            log(f"[off_time] 心跳: 档位={timeout}s 已空闲={idle_now}s "
                f"黑屏中={_IDLE['blanked']} 输入设备数={len(devs)}", level="quiet")
        # 注：路径 A 的 0.5s 节流由 select 超时提供；路径 B 在分支内 sleep


def start_idle_monitor(off_time):
    """启动空闲监视线程（守护）"""
    if _resolve_blank_path() is None:
        log("[off_time] ⚠ 未找到 /sys/class/graphics/fb*/blank → "
            "只保存设置，不实际熄屏（如硬件支持请检查内核 framebuffer）", level="quiet")
        return None
    t = threading.Thread(target=_idle_loop, args=(off_time,),
                         daemon=True, name="off-time-monitor")
    t.start()
    return t

class LedState:
    """LED 内部状态（线程安全，文件持久化，mtime 感知外部改动）"""

    def __init__(self, path=None):
        self.path = path or self._pick_path()
        self._lock = threading.Lock()
        self._data = dict(LED_DEFAULTS)
        self._mtime = 0
        self.load()

    def _pick_path(self):
        for p in LED_STATE_CANDIDATES:
            d = os.path.dirname(p)
            try:
                os.makedirs(d, exist_ok=True)
                if os.access(d, os.W_OK):
                    return p
            except Exception:
                continue
        return LED_STATE_CANDIDATES[-1]

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            merged = dict(LED_DEFAULTS)
            if isinstance(raw, dict):
                merged.update({k: raw[k] for k in LED_DEFAULTS if k in raw})
            self._data = merged
            self._mtime = os.path.getmtime(self.path)
        except Exception as e:
            log(f"[led] 状态文件读取失败（用默认值）: {e}", level="quiet")

    def _save_locked(self):
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
            self._mtime = os.path.getmtime(self.path)
        except Exception as e:
            log(f"[led] 状态文件写入失败: {e}", level="quiet")

    def get(self):
        """读：返回当前状态副本。★ 作为唯一真值来源供读接口使用"""
        with self._lock:
            # 外部（人工）改了文件则重新加载
            try:
                if os.path.exists(self.path) and os.path.getmtime(self.path) != self._mtime:
                    self.load()
            except Exception:
                pass
            return dict(self._data)

    def update(self, **kw):
        """写：更新若干字段并落盘，同时触发硬件落地"""
        with self._lock:
            changed = {k: v for k, v in kw.items() if k in LED_DEFAULTS and v is not None}
            if not changed:
                return dict(self._data)
            self._data.update(changed)
            self._save_locked()
            state = dict(self._data)
        apply_to_hardware(state)
        log(f"[led] 状态已更新 {changed} → {json.dumps(state, ensure_ascii=False)}", level="quiet")
        return state


def apply_to_hardware(state):
    """★ 把 LED 内部状态落地到硬件。

    后端由 LED_HW_BACKEND 决定：
      · "i2c"          → 通过 /dev/i2c-N 写 0x26（★ 本机真实通道）
      · "sysfs_multi"  → 标准 sysfs 多色 LED（备用）
      · "none"         → 空实现（无可用通道时）
    返回 True 表示"处理完成"。
    """
    if LED_HW_BACKEND == "i2c":
        return _apply_i2c(state)
    if LED_HW_BACKEND == "sysfs_multi":
        return _apply_sysfs_multi(state)
    return True


def _apply_sysfs_multi(state):
    """sysfs 多色 LED 落地（预留）。

    ★ 必须读 multi_index 定颜色通道顺序，逐通道拼装 multi_intensity，
      不可假设 R G B 顺序 —— 照抄官方 system_setled.sh 的 led_color_set()。
    """
    name = os.environ.get("HIKSEMI_LED_SYSFS", "").strip()
    if not name:
        return True
    base = f"/sys/class/leds/{name}"
    if not (os.path.exists(f"{base}/multi_intensity") and os.path.exists(f"{base}/multi_index")):
        log(f"[led] sysfs_multi: {base} 无 multi_intensity/multi_index，跳过", level="quiet")
        return True
    try:
        max_b = int(_read_sysfs(f"{base}/max_brightness", "255") or 255)
        channels = _read_sysfs(f"{base}/multi_index", "red green blue").split()
        on = int(state.get("status", 1)) != 0
        vals = []
        for ch in channels:
            v = 0 if not on else int(state.get("color_" + ch[0], 0))  # r/g/b
            vals.append(min(v, max_b))
        with open(f"{base}/multi_intensity", "w") as f:
            f.write(" ".join(str(v) for v in vals))
        with open(f"{base}/brightness", "w") as f:
            f.write(str(max_b if on else 0))
        log(f"[led] sysfs_multi 已写 {name} → {vals}", level="quiet")
    except Exception as e:
        log(f"[led] sysfs_multi 写失败: {e}", level="quiet")
    return True


def build_led_state(spec, ctx):
    """读接口 builder：get_light_theme / get_atmo_light。

    ★ 契约（逆向自客户端 index-be27a12a.js）：
      get_light_theme 响应字段：{code, status, mode, color_id}
        客户端解析：status===0 → 关闭(999)
                    mode===0   → 随主题(1000)
                    否则        → color_id + 1000
      get_atmo_light  响应字段：{code, status, mode, color_id}

    ★★ 硬件真值优先：若能通过 I2C 读回硬件状态，则以【硬件为准】
      反解出 status/mode/color_id，这样屏幕显示与灯光实际永远一致。
      读不到硬件时退回内部持久化状态。
    """
    led = ctx.get("led")
    st = led.get() if led else dict(LED_DEFAULTS)

    if LED_HW_BACKEND == "i2c":
        hw = led_read_hardware()
        if hw is not None:
            mode_hw, r, g, b = hw
            if mode_hw == LED_I2C_MODE_OFF:
                return {"code": 0, "status": 0, "mode": int(st.get("mode", 1)),
                        "color_id": int(st.get("color_id", 0))}
            if mode_hw == LED_I2C_MODE_RAINBOW:
                # 彩虹 → 对应「随主题」(color_id=0, mode=0)
                return {"code": 0, "status": 1, "mode": 0, "color_id": 0}
            if mode_hw == LED_I2C_MODE_BREATHING:
                # 呼吸 → 对应「动态色」(color_id=1)
                return {"code": 0, "status": 1, "mode": 1, "color_id": 1}
            # solid → 按硬件 RGB 反查 color_id
            return {"code": 0, "status": 1, "mode": int(st.get("mode", 1)),
                    "color_id": _rgb_to_color_id(r, g, b)}

    return {
        "code": 0,
        "status": int(st.get("status", 1)),
        "mode": int(st.get("mode", 1)),
        "color_id": int(st.get("color_id", 0)),
    }


def _rgb_to_color_id(r, g, b):
    """由硬件 RGB 反查客户端 color_id（取最接近的已知色）。

    ★ 注意 1002「蓝」在硬件上实际是 (30,180,25)（源码 bug），
      所以这里按【客户端实际下发的值】做匹配，而非"直觉上的颜色"。
    """
    best, best_d = 0, 1 << 30
    for cid, (_cid, cr, cg, cb) in LED_EFFECT_COLOR.items():
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < best_d:
            best, best_d = _cid, d
    return best


def resolve_led_write(spec, ctx):
    """★ 字段级 resolver：处理写接口的副作用，返回【该字段的值】。

    注意：build_response 是「逐字段」解析，本函数返回的是 `code` 字段的值（0），
    而不是完整的 {code:0} —— 后者会导致响应变成 {"code":{"code":0}}。

    客户端写入契约（逆向自 index-be27a12a.js）：
      set_atmo_light   ?light_mode={n}   n=0 关灯 / n=9 开灯 → 更新 status/light_mode
      set_light_theme  ?mode={n}         n=0 随主题 / n=1 其他 → 更新 mode
      set_atmo_color   POST JSON body    {color_id,r,g,b}     → 更新颜色四元组
    """
    generic = spec.get("_write")
    if not generic:
        # ★ 字段级 resolver 只会拿到【字段 spec】；action 级 _write 需从上下文取
        generic = ((ctx.get("_action_spec") or {}).get("_write"))
    led = ctx.get("led")
    if led is None:
        return 0
    params = ctx.get("_params") or {}
    body = ctx.get("_body") or {}
    try:
        if generic == "set_atmo_light":
            lm = params.get("light_mode")
            if lm is not None:
                lm = int(lm)
                # light_mode=0 → 关闭；其它（如 9）→ 开启
                led.update(light_mode=lm, status=(0 if lm == 0 else 1))
        elif generic == "set_light_theme":
            m = params.get("mode")
            if m is not None:
                led.update(mode=int(m))
        elif generic == "set_atmo_color":
            # ★★ 关键：set_atmo_color 【不改变开关状态】！
            #    客户端流程（setLightEffect）是「先设颜色、再开灯」：
            #      当前 status===0（关灯）→ setupLightEffectColor(r)（此时 light_mode 仍为 0）
            #                              → setupLightEffectStatus(9)（真正开灯）
            #    若设色时照旧照 status 关灯，硬件会被先写 0x90=0x0，
            #    日志表现为「点了颜色反而先关灯」。
            #    正确语义：颜色接口只管颜色四元组，开关由 set_atmo_light 决定。
            cid = body.get("color_id", params.get("color_id"))
            r = body.get("color_r", params.get("color_r"))
            g = body.get("color_g", params.get("color_g"))
            b = body.get("color_b", params.get("color_b"))
            kw = {}
            if cid is not None:
                kw["color_id"] = int(cid)
            if r is not None:
                kw["color_r"] = int(r)
            if g is not None:
                kw["color_g"] = int(g)
            if b is not None:
                kw["color_b"] = int(b)
            if kw:
                # 强制保留当前 status：只在硬件层面更新颜色，
                # 不因 body 里可能存在的 status=0 而关灯。
                kw["status"] = int(led.get().get("status", 1))
                led.update(**kw)
                log(f"[led] 颜色已更新（保持开关状态={kw['status']}）", level="quiet")
    except Exception as e:
        log(f"[led] 写接口 {generic} 处理异常: {e}", level="quiet")
    return 0


def resolve_set_hardware(spec, ctx):
    """★ 字段级 resolver：散热风扇档位 set_hardware。

    客户端契约（逆向自 index-be27a12a.js 的 setupFanMode）：
      GET /rest/1.1/config?action=set_hardware&fan_mode={5|2|3|4}&cmdfrom=devsub
      成功判据：响应 code === 0（否则界面弹「设置失败」）

    四档 id（★不连续，无 1）：
      5=智能（默认） / 2=低速 / 3=标准 / 4=强劲

    本函数把档位【规范化 → 落盘（FanState）→ 触发外部应用钩子】，
    并返回 code 字段的值(0)。真正的硬件调速由 apply_to_external_app() 预留。
    """
    fan = ctx.get("fan")
    params = ctx.get("_params") or {}
    raw = params.get("fan_mode")
    if raw is None:
        raw = params.get("mode")

    mode = _normalize_fan_mode(raw)
    if str(raw) != str(mode) or (raw is not None and raw != "" and not str(raw).lstrip("-").isdigit()):
        log(f"[fan] ⚠ 非法 fan_mode={raw!r} → 规范化为 {mode}"
            f"（{FAN_MODES.get(mode, '未知')}）", level="quiet")

    if fan is None:
        # 无状态实例（理论不会发生）：仍返回 0，避免界面报「设置失败」
        log("[fan] ⚠ 缺少 FanState 实例，仅回 code=0（未保存）", level="quiet")
        return 0
    try:
        fan.update(mode)
    except Exception as e:
        log(f"[fan] 档位写入异常: {e}", level="quiet")
    return 0


BUILDERS = {
    "disk_list": build_disk_list,
    "disk_manage": build_disk_manage,
    "space_manage": build_space_manage,
    "dev_status": build_dev_status,
    "eth_list": build_eth_list,
    "space_list": build_space_list,
    "update_desc": build_update_desc,
    "led_state": build_led_state,
    "off_time": build_off_time,          # ★ 自动息屏时长（读）
}

RESOLVERS = {
    "static": resolve_static,
    "file": resolve_file,
    "command": resolve_command,
    "hwmon": resolve_hwmon,
    "collector": resolve_collector,
    "mac": resolve_mac,          # ★ 主网卡 MAC 自动探测（不依赖 eth0）
    "led_write": resolve_led_write,   # ★ LED 写接口（持久化到内部状态）
    "off_time_write": resolve_off_time_write,   # ★ 自动息屏时长（写）
    "set_power": resolve_set_power,   # ★ 关机 / 重启（power_mode: 0=关机 1=重启）
    "set_hardware": resolve_set_hardware,   # ★ 散热风扇档位（fan_mode: 5/2/3/4）
}


# ============================================================
# 四、配置加载（mtime 热重载）
# ============================================================

class Config:
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.mtime = 0
        self.data = {}
        self.load()

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            self.data = json.load(f)
        self.mtime = os.path.getmtime(self.path)
        n = len(self.data.get("actions", {}))
        log(f"[config] 已加载 {self.path}（{n} 个 action）")

    def maybe_reload(self):
        # 配置文件被移动/删除时不刷屏，只在状态变化时提示一次
        if not os.path.exists(self.path):
            if not getattr(self, "_missing_warned", False):
                log(f"[config] ⚠ 配置文件不存在：{self.path}（沿用内存中的旧配置）")
                log(f"[config]   若已移动目录，请重启服务： systemctl restart hiksemi-sub")
                self._missing_warned = True
            return
        if getattr(self, "_missing_warned", False):
            log(f"[config] 配置文件已恢复：{self.path}")
            self._missing_warned = False
        try:
            if os.path.getmtime(self.path) != self.mtime:
                self.load()
        except Exception as e:
            log(f"[config] 热重载失败（沿用旧配置）: {e}")

    def server(self):
        return self.data.get("server", {})

    def actions(self):
        return self.data.get("actions", {})

    def defaults(self):
        return self.data.get("defaults", {}).get("unknown_action", {"code": 0})


# ============================================================
# 五、HTTP 服务
# ============================================================

CTX = {}


def build_response(action, params=None, body=None):
    cfg = CTX["config"]
    actions = cfg.actions()
    spec = actions.get(action)
    if spec is None:
        log(f"  [warn] 未配置的 action: {action}，返回默认值")
        return dict(cfg.defaults())
    # ★ 请求级局部上下文：避免多线程下全局 CTX 互相覆盖（ThreadingHTTPServer）
    ctx = dict(CTX)
    ctx["_params"] = params or {}
    ctx["_body"] = body or {}
    ctx["_action_spec"] = spec          # ★ 字段级 resolver 需读 action 级信号（如 _write）
    # builder 型：整体由构造器生成
    if spec.get("source") == "builder":
        fn = BUILDERS.get(spec.get("name"))
        return fn(spec, ctx) if fn else dict(cfg.defaults())
    # 字段型：逐字段按映射解析
    out = {}
    for key, fs in spec.items():
        if key.startswith("_"):
            continue
        try:
            fn = RESOLVERS.get(fs.get("source"))
            out[key] = fn(fs, ctx) if fn else fs.get("default")
        except Exception as e:
            log(f"  [warn] 字段 {key} 解析失败: {e}")
            out[key] = fs.get("default")
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # 自己打日志

    def _respond(self, action, params=None, body=None):
        CTX["config"].maybe_reload()
        payload = build_response(action, params=params, body=body)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

        # ★ 响应日志分级：
        #   · 写入类（set_*）→ 完整内容 + quiet 级别（始终可见，状态变更必须可追溯）
        #   · 高频只读轮询（get_sys_info / get_off_time 等）→ 仅 debug
        #   · 网络类 → 完整内容（字段少一个页面就 "--"）
        #   · 其余 → 仅 debug
        _is_write = action.startswith("set_") or action in ("opt_screen_theme",)
        _is_net = action in ("get_eth_info_v2", "get_network_mode", "space_manage")
        if _is_write:
            log(f"  → {action} 响应: {json.dumps(payload, ensure_ascii=False)}",
                level="quiet")
        elif _is_net:
            # ★ 网络类 action 打【完整】响应体（仅 debug，避免轮询刷屏）：
            #   这几个字段少一个页面就显示 "--"，排障时必须能一眼看到
            #   eth 数组的真实内容（含 ip/connected）。
            log(f"  → {action} 响应({len(body)}B) 完整内容:\n"
                f"      {payload and json.dumps(payload, ensure_ascii=False)}",
                level="debug")
        else:
            log(f"  → {action} 响应({len(body)}B): "
                f"{payload and json.dumps(payload, ensure_ascii=False)[:160]}",
                level="debug")

    # ------------------------------------------------------------
    # 长连接（multipart --myboundary 分帧）
    # ------------------------------------------------------------
    # 客户端 StreamDataParser 期望的帧格式（首帧必须含 boundary）：
    #   --myboundary\r\nContent-type: application/json\r\nContent-Length: <n>\r\n\r\n<n 字节 JSON>
    #
    # ★★★ 帧与帧之间【不能有任何 \r\n 分隔】—— 必须严格首尾相接 ★★★
    #
    # 客户端解析器（index-be27a12a.js @410034 StreamDataParser）的真实算法：
    #   BoundaryRegexp = /--myboundary\r\nContent-type: application\/json\r\nContent-Length: (\d+)\r\n\r\n/
    #   m = cachedData.toString().match(BoundaryRegexp)   // 正则不含任何前后的 \r\n
    #   cachedData = cachedData.slice(Buffer.byteLength(m[0]))  // 只切掉 boundary 头
    #   nextReceiveBodyLength = +m[1]                      // 按 Content-Length 切 JSON
    #   _tryParseData(): JSON.parse(cachedData.slice(0, nextReceiveBodyLength))
    #                    cachedData = cachedData.slice(nextReceiveBodyLength)   // 精确切 n 字节
    #
    # 也就是说它只认「boundary 头 + 恰好 n 字节 JSON」这两段，多一个字节都不行：
    #   · 帧首加 \r\n → 切掉 boundary 头后剩 "\r\n{JSON}"，JSON 前多 2 字节，
    #                  切 n 字节会把 JSON 尾部 "}" 留下 → 与下一帧 boundary 粘连 → 崩
    #   · 帧尾加 \r\n → 切掉 JSON 后剩 "\r\n"，成为下一轮的垃圾前缀 → 累积错位 → 崩
    #   实测（tools/stream_final_verify.js，逐字节复刻客户端解析器）：
    #       帧首带 \r\n  ：6 帧 → 解析出 0 帧
    #       帧尾带 \r\n  ：6 帧 → 解析出 1 帧
    #       首尾都不带  ：6 帧 → 解析出 6 帧，残留 0 字节 ✅
    #
    # 这也解释了为什么 mock 时代「设备状态页」也没数据：mock 走一次性 JSON，
    # 连 boundary 都没有，StreamDataParser 首块即 onerror（而调用方没传 onerror，
    # 于是静默丢弃）。当时看到有数据的只有主题页动画和硬盘页盘位。
    #
    # 按 cfg 的 stream 配置周期推送；客户端 60s 无数据则断开重连。
    def _respond_stream(self, action):
        cfg = CTX["config"]
        stream_cfg = cfg.data.get("stream", {})
        interval = float(stream_cfg.get("interval", 2))
        # 允许按 action 覆盖推送间隔
        per_action = stream_cfg.get("overrides", {})
        interval = float(per_action.get(action, interval))

        self.send_response(200)
        self.send_header("Content-Type", "multipart/mixed; boundary=myboundary")
        self.send_header("Connection", "keep-alive")
        self.send_header("Cache-Control", "no-cache")
        # 无 Content-Length + HTTP/1.1 → chunked，便于长连接持续推送
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        log(f"STREAM {action} 开始推送（间隔 {interval}s）")

        def send_frame(payload):
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            # ★ 帧首、帧尾都【不加 \r\n】，严格首尾相接 —— 与客户端
            #   Content-Length 切分精确对齐（见上方注释的实测数据）。
            frame = b"--myboundary\r\nContent-type: application/json\r\nContent-Length: " \
                    + str(len(raw)).encode() + b"\r\n\r\n" + raw
            # chunked 编码：<hex 长度>\r\n<数据>\r\n  （这层 \r\n 属于 HTTP 传输层，
            # 会被 got 解掉，不会进入客户端 StreamDataParser 的 cachedData）
            self.wfile.write(f"{len(frame):x}".encode() + b"\r\n" + frame + b"\r\n")
            self.wfile.flush()

        try:
            # 立即推首帧，避免首帧缺失触发客户端 onerror
            first = build_response(action)
            send_frame(first)
            # ★ 首帧内容仅 debug 打印（长连接每 2s 一帧，全打会刷屏）
            log(f"STREAM {action} 首帧: "
                f"{json.dumps(first, ensure_ascii=False)[:180]}", level="debug")
            _frames = 1
            while True:
                time.sleep(interval)
                cfg.maybe_reload()
                send_frame(build_response(action))
                _frames += 1
                # ★ 每 100 帧打一行心跳（normal），证明长连接还活着
                if _frames % 100 == 0:
                    log(f"STREAM {action} 已推送 {_frames} 帧（连接正常）")
        except (BrokenPipeError, ConnectionResetError, OSError):
            log(f"STREAM {action} 客户端断开（共推送 {_frames} 帧）")
        except Exception as e:
            log(f"STREAM {action} 异常终止: {e}")

    def _handle(self, method):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        action = (qs.get("action") or [""])[0]
        params = {k: v[0] for k, v in qs.items() if v}
        cfg = CTX["config"]
        stream_cfg = cfg.data.get("stream", {})
        mode = stream_cfg.get("mode", "auto")   # auto | always | never
        in_stream = action and action in stream_cfg.get("actions", [])

        # ★ 请求流水：只读高频轮询默认静默（首次 + 每 N 次摘要），
        #   写入类 action 与 debug 级别始终打印。
        write_like = action in ("set_off_time", "set_power", "set_light_theme",
                                "set_atmo_light", "set_atmo_color",
                                "set_ezviz_server", "opt_screen_theme")
        if write_like:
            # ★ 写入操作必须始终可见（含 quiet 级别）
            log(f"{method} {parsed.path}  action={action}  （写入操作）", level="quiet")
        elif action in _QUIET_POLL_ACTIONS:
            r = _poll_tick(action)
            if r is True:
                log(f"{method} {parsed.path}  action={action}  模式={mode}  "
                    f"[轮询类，后续同类将合并计数]")
            elif r == "summary":
                log(f"[poll] {action} 已轮询 {_poll_stat[action]} 次（只读查询，已合并）")
        else:
            log(f"{method} {parsed.path}  action={action}  模式={mode}")

        # 自适应判定：
        #   always → 该 action 一律走长连接
        #   never  → 一律走一次性 JSON（排障用；不确定协议时的安全档）
        #   auto   → 命中 stream.actions 才走长连接，否则一次性 JSON
        if in_stream and mode != "never":
            self._respond_stream(action)
        else:
            if in_stream and mode == "never":
                log(f"  [info] {action} 属长连接 action，但 mode=never，改发一次性 JSON")
            self._respond(action, params=params)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        action = (qs.get("action") or [""])[0]
        params = {k: v[0] for k, v in qs.items() if v}
        body = {}
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except Exception:
                    body = {}
        except Exception:
            pass
        log(f"POST {parsed.path}  action={action}  body={json.dumps(body, ensure_ascii=False)}")
        self._respond(action, params=params, body=body)


def _fatal(msg):
    """致命错误：写 stdout/stderr 并**立即 flush**（否则重定向到日志文件时会丢）"""
    print(f"[FATAL] {msg}", flush=True)
    sys.stderr.write(f"[FATAL] {msg}\n")
    sys.stderr.flush()


# ============================================================
# ★★★ 背光看门狗：防止进程退出时屏幕停留在全黑
#   背光被写到 0 后，若中间件崩溃/被 kill 而不恢复 → 屏幕永久黑，
#   用户会以为设备坏了。故注册退出钩子强制还原。
# ============================================================
def _restore_backlight_on_exit(*_a):
    """退出时把背光还原（幂等，可重复调用）"""
    if not _SCREEN_BLANKED.get("v"):
        return
    try:
        _screen_set_backlight(False)
        _SCREEN_BLANKED["v"] = False
        print("[off_time] 退出钩子：已还原背光（防止屏幕停留在全黑）", flush=True)
    except Exception:
        pass


def _install_backlight_watchdog():
    """注册 atexit + SIGTERM/SIGINT/SIGHUP 钩子，确保背光被还原"""
    import atexit
    atexit.register(_restore_backlight_on_exit)
    try:
        import signal as _sig

        def _handler(signum, _frame):
            _restore_backlight_on_exit()
            # 还原后按默认行为退出
            _sig.signal(signum, _sig.SIG_DFL)
            os.kill(os.getpid(), signum)

        for s in ("SIGTERM", "SIGINT", "SIGHUP"):
            if hasattr(_sig, s):
                try:
                    _sig.signal(getattr(_sig, s), _handler)
                except Exception:
                    pass
    except Exception:
        pass


def _led_probe_cli():
    """`--led-probe`：独立探测 I2C 灯控，打印逐总线诊断后退出。"""
    print("=" * 60)
    print(" LED I2C 硬件探测")
    print("=" * 60)
    print(f"  fcntl 模块   : {'有' if fcntl is not None else '无（非 Linux？）'}")
    euid = os.geteuid() if hasattr(os, "geteuid") else "N/A"
    print(f"  当前 euid    : {euid}（0=root）")
    raw = sorted(glob.glob("/dev/i2c-*"))
    print(f"  /dev/i2c-*   : {raw or '（不存在）'}")
    if fcntl is None:
        print("\n  ❌ 无 fcntl，无法使用 I2C")
        return 1
    loaded = _ensure_i2c_dev_module()
    print(f"  i2c_dev 模块 : {'已加载/可用' if loaded else '未加载且无法自动加载'}")
    raw = sorted(glob.glob("/dev/i2c-*"))
    print(f"  加载后 /dev  : {raw or '（仍不存在）'}")
    print("-" * 60)
    print("  逐总线探测 0x26：")
    bus = _i2c_probe_bus(verbose=True)
    print("-" * 60)
    if bus is not None:
        print(f"  ✅ 命中：/dev/i2c-{bus} 上的 0x26")
        hw = None
        try:
            fd = _i2c_io.open(bus)
            try:
                _i2c_io.select(fd)
                _i2c_io.write(fd, [LED_I2C_REG_MODE])
                mode = _i2c_io.read(fd, 1)[0]
                hw = mode
            finally:
                _i2c_io.close(fd)
        except OSError as e:
            print(f"  ⚠ 回读 0x90 失败：{e}")
        if hw is not None:
            names = {0x0: "关", 0x1: "solid 常亮", 0x2: "rainbow 彩虹", 0x3: "breathing 呼吸"}
            print(f"  当前模式 0x90 = 0x{hw:02x}（{names.get(hw, '未知')}）")
        print("\n  → 中间件启动时应能识别 I2C 后端")
        return 0
    print("  ❌ 未命中任何总线")
    print("\n  排查建议：")
    print("    1) 确认设备存在： ls /dev/i2c-*")
    print("    2) 扫描总线：    sudo i2cdetect -l")
    print("    3) 手动验证：    sudo i2cget -f -y 1 0x26 0x90")
    print("    4) 若只有 root 能访问，请以 root 运行中间件（或加 udev 规则）")
    return 1


def main():
    import argparse
    ap = argparse.ArgumentParser(description="hiksemi-sub 服务中间件")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--cert", default=DEFAULT_CERT)
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--listen", default=None, help="默认取配置文件 server.listen")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--check", action="store_true", help="只做自检后退出（不启动服务）")
    ap.add_argument("--led-probe", action="store_true",
                    help="只探测 LED I2C 硬件后端并退出（排查用）")
    args = ap.parse_args()

    if args.led_probe:
        return _led_probe_cli()

    # 打印完整运行环境：日志里看不到任何输出的首要排查依据
    log("=" * 56)
    log("hiksemi-sub 服务中间件启动")
    # ★ 版本与指纹：判断"真机跑的是哪一版代码"的唯一依据。
    #   排查「改了没生效」时，先看这两行，再看【配置里 action 数量】。
    log(f"  中间件版本  : {MIDDLEWARE_VERSION}")
    log(f"  代码指纹    : {_code_fingerprint()}")
    log(f"  Python      : {sys.version.split()[0]}  ({sys.executable})")
    log(f"  工作目录    : {os.getcwd()}")
    log(f"  脚本目录    : {BASE}", level="quiet")

    # ── 启动自检：配置文件 ──
    cfg_path = os.path.abspath(args.config)
    log(f"  配置文件    : {cfg_path}")
    if not os.path.exists(cfg_path):
        _fatal(f"配置文件不存在：{cfg_path}")
        print(f"        脚本所在目录：{BASE}", flush=True)
        print("        请确认 data-mapping.json 与 middleware.py 同目录，", flush=True)
        print("        或用 --config /完整/路径/data-mapping.json 指定。", flush=True)
        sys.exit(1)
    try:
        with open(cfg_path, encoding="utf-8") as f:
            json.load(f)
    except Exception as e:
        _fatal(f"配置文件无法解析：{cfg_path}\n        {e}")
        sys.exit(1)

    # ── 启动自检：证书 ──
    log(f"  证书        : {os.path.abspath(args.cert)}")
    if not (os.path.exists(args.cert) and os.path.exists(args.key)):
        _fatal(f"缺少证书（{args.cert} / {args.key}）")
        print("        在脚本同目录执行：", flush=True)
        print('        openssl req -x509 -newkey rsa:2048 -nodes '
              '-keyout key.pem -out cert.pem -days 3650 -subj "/CN=127.0.0.1"', flush=True)
        sys.exit(1)

    # ── 启动自检：ssl 模块（精简发行版可能缺失）──
    try:
        import ssl as _ssl_test  # noqa: F401
    except ImportError:
        _fatal("Python 缺少 ssl 模块（apt install -y python3-openssl）")
        sys.exit(1)

    cfg = Config(cfg_path)
    srv = cfg.server()
    listen = args.listen or srv.get("listen", "127.0.0.1")
    port = args.port or int(srv.get("port", 20443))
    log(f"  监听地址    : https://{listen}:{port}")
    log(f"  stream.mode : {cfg.data.get('stream', {}).get('mode', 'auto')}")
    log(f"  流式 action : {cfg.data.get('stream', {}).get('actions', [])}")

    CTX["config"] = cfg
    collectors = Collectors(cfg.data)
    CTX["collectors"] = collectors
    collectors.start()

    # ★ LED 内部状态（读接口真值来源 + 写接口落地）
    led = LedState()
    CTX["led"] = led
    # ★ 探测 LED 硬件后端（I2C 0x26）；命中则后续写操作直接落地到硬件
    backend = led_detect_backend()
    log(f"LED 状态文件: {led.path}")
    log(f"LED 当前状态: {json.dumps(led.get(), ensure_ascii=False)}")
    log(f"LED 硬件后端: {backend}"
        + ("（I2C 0x26，读写均落地硬件）" if backend == "i2c" else "（无 I2C 通道，仅应用级状态）"))
    if backend == "i2c":
        hw = led_read_hardware()
        log(f"LED 硬件回读: mode/r/g/b = {hw}")

    # ★ 自动息屏状态（读接口真值 + 写接口落地 + 空闲监视）
    off_time = OffTimeState()
    CTX["off_time"] = off_time
    log(f"自动息屏状态文件: {off_time.path}")
    _bp = _resolve_blank_path()
    log(f"自动息屏当前值: {off_time.get()['xp_time']}s"
        + (f"（屏幕开关: {_bp}）" if _bp else
           "（⚠ 未找到 fb*/blank，仅保存设置、不实际熄屏）"))
    start_idle_monitor(off_time)
    _install_backlight_watchdog()      # ★ 防止退出时屏幕停在全黑

    # ★ 散热风扇档位状态（set_hardware 写 + 设备状态帧 fanmode 读）
    fan = FanState()
    CTX["fan"] = fan
    _fan_mode = fan.mode()
    log(f"风扇状态文件  : {fan.path}")
    log(f"风扇当前档位  : fan_mode={_fan_mode}"
        f"（{FAN_MODES.get(_fan_mode, '未知')}）"
        f"  合法档位: {'/'.join(str(k) + '=' + FAN_MODES[k] for k in FAN_MODE_VALID)}")
    _fan_cmd = os.environ.get(FAN_APPLY_CMD_ENV, "").strip()
    _fan_cc = _coolercontrol_enabled()
    _fan_cc_script = _resolve_coolercontrol_script() if _fan_cc else None
    _fan_pwm = os.environ.get(FAN_ATTR_PWM_ENV, "").strip()
    if _fan_cmd:
        log(f"风扇外部应用  : ✅ 外部命令钩子（{FAN_APPLY_CMD_ENV}={_fan_cmd}）")
    elif _fan_cc and _fan_cc_script:
        log(f"风扇外部应用  : ✅ CoolerControl（{_fan_cc_script} {{mode}}）")
    elif _fan_cc:
        log(f"风扇外部应用  : ⚠ 已启用 {FAN_COOLERCONTROL_ENV} 但找不到 apply-mode.sh"
            f"（候选: {FAN_COOLERCONTROL_SCRIPT_CANDIDATES}）")
    elif _fan_pwm:
        log(f"风扇外部应用  : ✅ sysfs PWM 节点（{FAN_ATTR_PWM_ENV}={_fan_pwm}）")
    else:
        log("风扇外部应用  : ⚠ 未配置（仅保存 + 回显档位，不改硬件）"
            f" —— 可选 {FAN_COOLERCONTROL_ENV}=1（CoolerControl）或 "
            f"{FAN_APPLY_CMD_ENV} / {FAN_ATTR_PWM_ENV}")

    # ★ 功能就绪摘要：一眼看清 LED / 熄屏 / 关机重启 三条通道是否都启用。
    #   排查「某功能没反应」时，先看这里有没有对应 ✅。
    _pw_ok = bool(shutil.which("systemctl") or shutil.which("poweroff")
                  or shutil.which("reboot") or shutil.which("shutdown"))
    log("-" * 56)
    log("功能就绪摘要：", level="quiet")
    log(f"  LED 灯控   : {'✅ I2C 硬件' if backend == 'i2c' else '⚠ 仅应用级（无 I2C 通道）'}")
    if _bp:
        _fb_cnt = len(_blank_candidates())
        _sel_conn = _fb_connector_name(_bp) or "未知"
        log(f"  自动息屏   : ✅ 可实际熄屏（选中 {_bp}，连接器={_sel_conn}"
            f"{'，共 ' + str(_fb_cnt) + ' 个 fb 已按 DSI 优先排序' if _fb_cnt > 1 else ''}）")
    else:
        log(f"  自动息屏   : ⚠ 仅保存设置（无 fb*/blank）")
    _bl_n = len(_backlight_paths())
    log(f"  屏控兜底   : {'✅ 背光通道 ' + str(_bl_n) + ' 个节点' if _bl_n else '⚠ 无背光节点（blank 无效时无后备）'}")
    log(f"  关机/重启  : {'✅ 可用（' + _picked_power_cmd() + '）' if _pw_ok else '❌ 无可用命令'}")
    _fan_hook = ("外部命令" if _fan_cmd
                 else ("CoolerControl" if (_fan_cc and _fan_cc_script)
                       else ("sysfs PWM" if _fan_pwm else None)))
    log(f"  风扇档位   : {'✅ 可落地（' + _fan_hook + '）' if _fan_hook else '⚠ 仅保存/回显（外部通道未配置）'}"
        f"，当前 {_fan_mode}={FAN_MODES.get(_fan_mode, '?')}")
    log(f"  action 数  : {len(CTX['config'].actions())} 个"
        f"（含 set_power: {'是' if 'set_power' in CTX['config'].actions() else '否 ← 配置过旧！'}）")
    log("-" * 56)

    if args.check:
        log("自检完成（--check，不启动服务）")
        sys.exit(0)

    # 端口占用 / 权限等绑定错误要明确报出来，否则日志里只有 traceback
    try:
        httpd = ThreadingHTTPServer((listen, port), Handler)
    except OSError as e:
        _fatal(f"无法绑定 {listen}:{port} → {e}")
        print("        常见原因：端口被占用（ss -lntp | grep 20443）", flush=True)
        print("                  或另一个中间件/mock 实例已在运行", flush=True)
        sys.exit(1)

    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(args.cert, args.key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    except Exception as e:
        _fatal(f"HTTPS 证书加载失败：{e}")
        sys.exit(1)

    log(f"中间件已启动: https://{listen}:{port}", level="quiet")
    log(f"脚本目录:     {BASE}", level="quiet")
    log(f"数据映射配置: {cfg_path}（改动即时生效）", level="quiet")
    log(f"证书:         {os.path.abspath(args.cert)}", level="quiet")
    log(f"日志级别:     {LOG_LEVEL}（quiet/normal/debug，用 HIKSEMI_LOG 环境变量调整）", level="quiet")
    if LOG_LEVEL != "debug":
        log("  说明：只读轮询请求已合并计数，日志仅保留启动/错误/状态变更", level="quiet")
    log("按 Ctrl+C 停止", level="quiet")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("已停止", level="quiet")
        httpd.shutdown()


if __name__ == "__main__":
    main()

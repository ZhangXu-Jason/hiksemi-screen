#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
散热风扇（4 档）测试
==========================================
覆盖：
  · FanState 默认值 / 四档写入 / 非法值回默认 / 持久化 / 线程安全
  · _normalize_fan_mode（5/2/3/4 白名单，★ 不夹取）
  · set_hardware resolver（写 fan_mode，返回 code=0，非嵌套）
  · 设备状态帧 fanmode 动态返回（客户端 FAN_LIST[].id 严格相等匹配）
  · apply_to_external_app 钩子（外部命令 / CoolerControl / sysfs PWM / 空实现 + 优先级）
  · 客户端 FAN_LIST 契约复刻（id 5/2/3/4）

用法： python3 _test_fan.py
"""
import inspect
import os
import sys
import tempfile
import threading
import time

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


def make_state():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    return mw.FanState(path=p), p


IS_WINDOWS = os.name == "nt"


def make_fake_script(body, suffix=".sh"):
    """生成一个可被 subprocess.run([script, mode]) 直接执行的假脚本。

    ★ 真机（Linux）上就是带 shebang + 可执行位的 .sh；
      Windows 上无法直接 exec .sh（WinError 193），故生成一个 .cmd 垫片，
      由它调用 git-bash 转交真正的 .sh。这样测试在两端行为一致。
    返回 (可执行路径, 需要清理的辅助文件列表)。
    """
    tmpdir = tempfile.mkdtemp()
    sh = os.path.join(tmpdir, "apply-mode" + suffix)
    with open(sh, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env bash\n" + body)
    os.chmod(sh, 0o755)
    if not IS_WINDOWS:
        return sh, [sh]
    # Windows：找 bash，生成 .cmd 垫片
    import shutil as _sh
    bash = _sh.which("bash") or _sh.which("sh")
    if not bash:
        # 无 bash 可用 → 退化为 python 垫片（仍能模拟 rc 与参数）
        py = os.path.join(tmpdir, "apply-mode.py")
        with open(py, "w", encoding="utf-8") as f:
            f.write("import sys\nprint('fake', file=sys.stderr)\n")
        cmd = os.path.join(tmpdir, "apply-mode.cmd")
        with open(cmd, "w", encoding="ascii", newline="\r\n") as f:
            f.write("@echo off\n")
            f.write(f'"{sys.executable}" "{py}" %*\n')
        return cmd, [sh, py, cmd]
    cmd = os.path.join(tmpdir, "apply-mode.cmd")
    with open(cmd, "w", encoding="ascii", newline="\r\n") as f:
        f.write("@echo off\n")
        f.write(f'"{bash}" "{sh}" %*\n')
    return cmd, [sh, cmd]


def main():
    print("=" * 60)
    print(" 散热风扇（4 档）测试")
    print("=" * 60)

    cfg = mw.Config(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data-mapping.json"))
    mw.CTX["config"] = cfg

    # ── 1. 四档常量契约 ──
    print("\n【1】四档常量（★ 取值不连续，无 1）")
    check("FAN_MODES 键 = (5,2,3,4)", set(mw.FAN_MODES.keys()) == {5, 2, 3, 4}, mw.FAN_MODES)
    check("顺序 5/2/3/4", tuple(mw.FAN_MODES.keys()) == (5, 2, 3, 4))
    check("5=智能", mw.FAN_MODES[5] == "智能")
    check("2=低速", mw.FAN_MODES[2] == "低速")
    check("3=标准", mw.FAN_MODES[3] == "标准")
    check("4=强劲", mw.FAN_MODES[4] == "强劲")
    check("默认 = 5（智能）", mw.FAN_MODE_DEFAULT == 5)
    check("合法元组 FAN_MODE_VALID", mw.FAN_MODE_VALID == (5, 2, 3, 4))
    check("★ 1 不是合法档", 1 not in mw.FAN_MODE_VALID)

    # ── 2. 默认值 ──
    print("\n【2】默认值")
    st, p = make_state()
    check("默认 fan_mode = 5", st.get()["fan_mode"] == 5, st.get())
    check("mode() 便捷读 = 5", st.mode() == 5)

    # ── 3. 四档写入 + 持久化 ──
    print("\n【3】四档写入 + 持久化")
    for v in (5, 2, 3, 4):
        st.update(v)
        check(f"写入 {v} 生效", st.get()["fan_mode"] == v, st.get())
    st2 = mw.FanState(path=p)
    check("重新加载后仍为 4（持久化）", st2.get()["fan_mode"] == 4, st2.get())

    # ── 4. 非法值 → 默认档（★ 不夹取） ──
    print("\n【4】非法值回默认档（★ 不按数值夹取）")
    for raw in (0, 1, 6, 7, 99, -1, "abc", None, "", 2.9):
        r = mw._normalize_fan_mode(raw)
        check(f"_normalize({raw!r}) → 5", r == 5, f"得到 {r}")
    # 字符串数字应被接受
    for raw, exp in ((("5"), 5), (("2"), 2), (("4"), 4), ((4.0), 4)):
        r = mw._normalize_fan_mode(raw)
        check(f"_normalize({raw!r}) → {exp}", r == exp, f"得到 {r}")

    # ── 5. update 返回 (状态, 是否变化) ──
    print("\n【5】update 返回值语义")
    st.update(3)
    state, changed = st.update(3)
    check("重复写同档 → changed=False", changed is False, changed)
    check("state 含 fan_mode", state.get("fan_mode") == 3, state)
    state, changed = st.update(4)
    check("换档 → changed=True", changed is True, changed)

    # ── 6. mtime 感知外部改动 ──
    print("\n【6】mtime 感知外部人工改动")
    import json
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"fan_mode": 2}, f)
    import time as _t
    _t.sleep(0.02)
    # ★ 平台差异（偶发）：Windows/MSYS 上两次相邻写入偶尔拿到【相同/过期的 mtime】，
    #   于是 get() 的「mtime 变了才重载」判定不触发 → 本断言偶发失败（非脚本 bug）。
    #   真机 Linux（ext4/btrfs/ZFS，纳秒级 mtime）+ 人工改文件（数秒甚至更久之后）
    #   不会遇到这种情况。这里把 mtime 显式往后拨，让「文件确实变了」在任何平台都成立。
    _now = _t.time() + 2
    os.utime(p, (_now, _now))
    check("外部改文件后 get() 读到 2", st.get()["fan_mode"] == 2, st.get())

    # ── 7. 损坏文件回默认 ──
    print("\n【7】损坏/异常文件容错")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{ not json ")
    st3 = mw.FanState(path=p)
    check("损坏 JSON → 默认 5", st3.get()["fan_mode"] == 5, st3.get())
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"fan_mode": 999}, f)
    st4 = mw.FanState(path=p)
    check("非法档位文件 → 默认 5", st4.get()["fan_mode"] == 5, st4.get())

    # ── 8. 线程安全 ──
    print("\n【8】线程安全（并发写不崩）")
    st5, p5 = make_state()
    errs = []

    def worker(v):
        try:
            for _ in range(20):
                st5.update(v)
        except Exception as e:  # noqa
            errs.append(e)

    ts = [threading.Thread(target=worker, args=(v,)) for v in (5, 2, 3, 4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("并发写无异常", not errs, errs)
    check("最终值仍是合法档", st5.get()["fan_mode"] in mw.FAN_MODE_VALID, st5.get())
    os.remove(p5)

    # ── 9. resolver：set_hardware ──
    print("\n【9】resolver: set_hardware")
    st6, p6 = make_state()
    mw.CTX["fan"] = st6
    ctx6 = dict(mw.CTX)                # ★ 真实调用时 ctx = dict(CTX)
    ctx6["_params"] = {"fan_mode": "4"}
    r = mw.resolve_set_hardware({}, ctx6)
    check("返回 code 字段值 0", r == 0, r)
    check("★ 返回标量（非 {code:0} 嵌套）", not isinstance(r, dict), r)
    check("fan_mode 已写入 4", st6.get()["fan_mode"] == 4, st6.get())

    ctx6["_params"] = {"fan_mode": "99"}
    r = mw.resolve_set_hardware({}, ctx6)
    check("非法值仍返回 0", r == 0, r)
    check("非法值 → 默认 5", st6.get()["fan_mode"] == 5, st6.get())

    ctx6["_params"] = {}
    r = mw.resolve_set_hardware({}, ctx6)
    check("缺参数 → 返回 0", r == 0, r)

    # 无 FanState 实例也不崩
    saved = mw.CTX.pop("fan", None)
    try:
        ctx_no = {"fan": None, "_params": {"fan_mode": "2"}}
        r = mw.resolve_set_hardware({}, ctx_no)
        check("无 FanState 实例 → 仍返回 0（不弹设置失败）", r == 0, r)
    finally:
        if saved is not None:
            mw.CTX["fan"] = saved

    check("resolver 已注册", "set_hardware" in mw.RESOLVERS)

    # ── 10. 设备状态帧 fanmode 动态返回 ──
    print("\n【10】设备状态帧 fanmode 动态返回")
    spec = cfg.actions().get("get_hw_resource_info", {})
    check("配置 fanmode = 'state'", spec.get("fanmode") == "state", spec.get("fanmode"))
    for v in (5, 2, 3, 4):
        st6.update(v)
        frame = mw.build_dev_status(spec, mw.CTX)
        check(f"fanmode 随 FanState 返回 {v}", frame["fanmode"] == v, frame.get("fanmode"))
    # 配置可覆盖为固定值（排障）
    spec_fixed = dict(spec)
    spec_fixed["fanmode"] = 3
    frame = mw.build_dev_status(spec_fixed, mw.CTX)
    check("配置数字 → 覆盖为 3", frame["fanmode"] == 3, frame.get("fanmode"))
    spec_static = dict(spec)
    spec_static["fanmode"] = {"source": "static", "value": 2}
    frame = mw.build_dev_status(spec_static, mw.CTX)
    check("配置 {source:static,value:2} → 2", frame["fanmode"] == 2, frame.get("fanmode"))
    check("fanmode 是 int（客户端严格相等需要）",
          isinstance(frame["fanmode"], int), type(frame["fanmode"]))

    # ── 11. 客户端 FAN_LIST 契约复刻 ──
    print("\n【11】客户端 FAN_LIST 严格相等匹配复刻")
    FAN_LIST_IDS = (5, 2, 3, 4)   # 来自 index-be27a12a.js @610765

    def client_match(fanmode):
        if fanmode and fanmode > 0:
            for i in FAN_LIST_IDS:
                if fanmode == i:
                    return i
            return None            # 不匹配 → 保持原值
        return 5                   # 回退智能

    for v in (5, 2, 3, 4):
        check(f"fanmode={v} 能匹配到 UI 档", client_match(v) == v)
    check("★ fanmode=1（旧硬编码值）匹配失败", client_match(1) is None)
    check("fanmode=0 回退智能", client_match(0) == 5)

    # ── 12. 外部应用预留钩子 ──
    print("\n【12】外部应用预留钩子 apply_to_external_app")
    check("apply_to_external_app 可调用", callable(mw.apply_to_external_app))
    src = inspect.getsource(mw.apply_to_external_app)
    check("支持 HIKSEMI_FAN_APPLY_CMD 外部命令", "FAN_APPLY_CMD_ENV" in src)
    check("支持 HIKSEMI_FAN_PWM_PATH sysfs", "FAN_ATTR_PWM_ENV" in src)
    check("含空实现兜底（无通道时不报错）", "return True" in src)
    check("FAN_MODE_PWM 预留映射存在", hasattr(mw, "FAN_MODE_PWM"))

    # 空实现：无环境变量时应返回 True 且不抛
    old = os.environ.pop(mw.FAN_APPLY_CMD_ENV, None)
    old2 = os.environ.pop(mw.FAN_ATTR_PWM_ENV, None)
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 4})
        check("无配置 → 空实现返回 True", r is True, r)
    finally:
        if old is not None:
            os.environ[mw.FAN_APPLY_CMD_ENV] = old
        if old2 is not None:
            os.environ[mw.FAN_ATTR_PWM_ENV] = old2

    # 外部命令钩子：用 echo 验证占位符替换（★ 命令与断言全 ASCII，避开 Windows 重定向编码）
    fd, outp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    os.environ[mw.FAN_APPLY_CMD_ENV] = f"echo {{mode}} > {outp}"
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 4})
        check("外部命令返回 True", r is True, r)
        with open(outp, encoding="utf-8", errors="replace") as f:
            got = f.read().strip().strip("\r\n")
        check("占位符 {mode} 替换为 4", got == "4", repr(got))
    finally:
        os.environ.pop(mw.FAN_APPLY_CMD_ENV, None)
        os.remove(outp)

    # 外部命令失败 → 返回 False 且不抛
    os.environ[mw.FAN_APPLY_CMD_ENV] = "false"
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 2})
        check("外部命令失败 → False（不抛）", r is False, r)
    finally:
        os.environ.pop(mw.FAN_APPLY_CMD_ENV, None)

    # sysfs PWM 钩子：写临时文件验证值
    fd2, pwmp = tempfile.mkstemp(suffix=".pwm")
    os.close(fd2)
    os.environ[mw.FAN_ATTR_PWM_ENV] = pwmp
    try:
        mw.apply_to_external_app(2, {"fan_mode": 4})
        with open(pwmp, encoding="utf-8") as f:
            v = f.read().strip()
        check("sysfs PWM 写入 255（强劲）", v == "255", v)
        mw.apply_to_external_app(3, {"fan_mode": 2})
        with open(pwmp, encoding="utf-8") as f:
            v = f.read().strip()
        check("sysfs PWM 写入 64（低速）", v == "64", v)
        # 智能档不写 PWM
        with open(pwmp, "w") as f:
            f.write("SENTINEL")
        mw.apply_to_external_app(3, {"fan_mode": 5})
        with open(pwmp, encoding="utf-8") as f:
            v = f.read().strip()
        check("「智能」不写 PWM（保持原值）", v == "SENTINEL", v)
    finally:
        os.environ.pop(mw.FAN_ATTR_PWM_ENV, None)
        os.remove(pwmp)

    # ── 12b. CoolerControl 适配器分支 ──
    print("\n【12b】CoolerControl 适配器分支")
    check("FAN_COOLERCONTROL_ENV 常量存在",
          getattr(mw, "FAN_COOLERCONTROL_ENV", "") == "HIKSEMI_FAN_COOLERCONTROL")
    check("FAN_COOLERCONTROL_SCRIPT_ENV 常量存在",
          getattr(mw, "FAN_COOLERCONTROL_SCRIPT_ENV", "")
          == "HIKSEMI_FAN_COOLERCONTROL_SCRIPT")
    check("脚本候选路径含 coolercontrol/apply-mode.sh",
          any(p.endswith(os.path.join("coolercontrol", "apply-mode.sh"))
              for p in mw.FAN_COOLERCONTROL_SCRIPT_CANDIDATES),
          mw.FAN_COOLERCONTROL_SCRIPT_CANDIDATES)
    check("_coolercontrol_enabled 可调用", callable(mw._coolercontrol_enabled))
    check("_resolve_coolercontrol_script 可调用",
          callable(mw._resolve_coolercontrol_script))

    # 开关语义：1/true/yes/on 视为开，其余为关
    for val, want in (("1", True), ("true", True), ("TRUE", True),
                      ("yes", True), ("on", True),
                      ("0", False), ("false", False), ("", False), ("no", False)):
        if val:
            os.environ[mw.FAN_COOLERCONTROL_ENV] = val
        else:
            os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)
        check(f"开关 '{val}' → {want}", mw._coolercontrol_enabled() is want)
    os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)

    # 用一个假 apply-mode.sh 验证：脚本被调用、收到档位参数、rc=0 → True
    fd_arg, argfile = tempfile.mkstemp(suffix=".arg")
    os.close(fd_arg)
    os.remove(argfile)
    fake_cc, clean_cc = make_fake_script(
        f'echo "$1" > "{argfile}"\n'
        "echo applied-to-coolercontrol >&2\n"
        "exit 0\n")
    os.environ[mw.FAN_COOLERCONTROL_ENV] = "1"
    os.environ[mw.FAN_COOLERCONTROL_SCRIPT_ENV] = fake_cc
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 3})
        check("CoolerControl 分支返回 True", r is True, r)
        got = ""
        if os.path.exists(argfile):
            with open(argfile, encoding="utf-8", errors="replace") as f:
                got = f.read().strip().strip("\r\n")
        check("apply-mode.sh 收到档位参数 3", got == "3", repr(got))
    finally:
        os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)
        os.environ.pop(mw.FAN_COOLERCONTROL_SCRIPT_ENV, None)
        for p in clean_cc + [argfile]:
            if os.path.exists(p):
                os.remove(p)

    # 脚本非零退出 → False 且不抛
    fake_cc2, clean_cc2 = make_fake_script("exit 7\n")
    os.environ[mw.FAN_COOLERCONTROL_ENV] = "1"
    os.environ[mw.FAN_COOLERCONTROL_SCRIPT_ENV] = fake_cc2
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 4})
        check("脚本 rc=7 → False（不抛）", r is False, r)
    finally:
        os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)
        os.environ.pop(mw.FAN_COOLERCONTROL_SCRIPT_ENV, None)
        for p in clean_cc2:
            if os.path.exists(p):
                os.remove(p)

    # 启用但脚本不存在 → False 且不抛（不能静默当成功）
    os.environ[mw.FAN_COOLERCONTROL_ENV] = "1"
    os.environ[mw.FAN_COOLERCONTROL_SCRIPT_ENV] = "/nonexistent/apply-mode.sh"
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 4})
        check("启用但脚本缺失 → False（不抛）", r is False, r)
    finally:
        os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)
        os.environ.pop(mw.FAN_COOLERCONTROL_SCRIPT_ENV, None)

    # ★ 优先级：显式 HIKSEMI_FAN_APPLY_CMD 必须压过 CoolerControl
    fd_p, pref_out = tempfile.mkstemp(suffix=".txt")
    os.close(fd_p)
    fake_cc3, clean_cc3 = make_fake_script(
        f'echo CC-CALLED > "{pref_out}"\nexit 0\n')
    os.environ[mw.FAN_COOLERCONTROL_ENV] = "1"
    os.environ[mw.FAN_COOLERCONTROL_SCRIPT_ENV] = fake_cc3
    os.environ[mw.FAN_APPLY_CMD_ENV] = "true"
    try:
        r = mw.apply_to_external_app(5, {"fan_mode": 2})
        check("优先级：显式 CMD 生效", r is True, r)
        check("优先级：CoolerControl 未被调用",
              not os.path.exists(pref_out) or
              os.path.getsize(pref_out) == 0,
              "CC 被误调用")
    finally:
        os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)
        os.environ.pop(mw.FAN_COOLERCONTROL_SCRIPT_ENV, None)
        os.environ.pop(mw.FAN_APPLY_CMD_ENV, None)
        for p in clean_cc3 + [pref_out]:
            if os.path.exists(p):
                os.remove(p)

    # 优先级：CoolerControl 压过 sysfs PWM
    fd_pw, pw_out = tempfile.mkstemp(suffix=".pwm")
    os.close(fd_pw)
    fake_cc4, clean_cc4 = make_fake_script("exit 0\n")
    with open(pw_out, "w") as f:
        f.write("UNTOUCHED")
    os.environ[mw.FAN_COOLERCONTROL_ENV] = "1"
    os.environ[mw.FAN_COOLERCONTROL_SCRIPT_ENV] = fake_cc4
    os.environ[mw.FAN_ATTR_PWM_ENV] = pw_out
    try:
        mw.apply_to_external_app(5, {"fan_mode": 4})
        with open(pw_out, encoding="utf-8") as f:
            v = f.read().strip()
        check("优先级：CoolerControl 压过 sysfs PWM（PWM 未被写）",
              v == "UNTOUCHED", v)
    finally:
        os.environ.pop(mw.FAN_COOLERCONTROL_ENV, None)
        os.environ.pop(mw.FAN_COOLERCONTROL_SCRIPT_ENV, None)
        os.environ.pop(mw.FAN_ATTR_PWM_ENV, None)
        for p in clean_cc4 + [pw_out]:
            if os.path.exists(p):
                os.remove(p)

    # ── 13. update 触发外部钩子（端到端串联） ──
    print("\n【13】update 自动触发外部钩子")
    fd3, outp3 = tempfile.mkstemp(suffix=".txt")
    os.close(fd3)
    st7, p7 = make_state()
    os.environ[mw.FAN_APPLY_CMD_ENV] = f"echo {{mode}} > {outp3}"
    try:
        st7.update(4)
        with open(outp3, encoding="utf-8", errors="replace") as f:
            v = f.read().strip().strip("\r\n")
        check("update 后外部应用收到 mode=4", v == "4", repr(v))
    finally:
        os.environ.pop(mw.FAN_APPLY_CMD_ENV, None)
        os.remove(outp3)
        os.remove(p7)

    # ── 14. 状态文件路径候选 ──
    print("\n【14】状态文件路径")
    check("优先 /var/lib/hiksemi-sub/fan.json",
          mw.FAN_STATE_CANDIDATES[0] == "/var/lib/hiksemi-sub/fan.json",
          mw.FAN_STATE_CANDIDATES[0])
    check("兜底在脚本同目录", mw.FAN_STATE_CANDIDATES[1].endswith(".fan-state.json"))

    # ── 15. ★ CoolerControl 反向同步（CC → 小屏）─────────────────────────
    #   用户在 Web UI 里自己建了 Mode（uid 是随机 UUID，不是 hs-smart），
    #   然后在 UI 里手动切档 —— 小屏回显必须跟上，否则就是「不同步」。
    print("\n【15】CoolerControl 反向同步（CC → 小屏）")
    import json as _json
    import shutil as _sh
    _ccdir = tempfile.mkdtemp()
    _modes_p = os.path.join(_ccdir, "modes.json")
    _map_p = os.path.join(_ccdir, "hs-fan-mode-map.json")
    U_SMART = "1a700658-89d7-4d03-8443-c9260d1604d9"
    U_LOW = "57070995-90a9-41cc-b0dd-d88c7f4aac34"
    U_MINE = "deadbeef-0000-1111-2222-333344445555"
    _modes = {"modes": [
        {"uid": U_SMART, "name": "hs 智能（CPU 曲线 + 机箱曲线）", "all_device_settings": {}},
        {"uid": U_LOW, "name": "hs 低速 25%", "all_device_settings": {}},
        {"uid": U_MINE, "name": "我的自定义模式", "all_device_settings": {}},
    ], "current_active_mode": U_LOW}

    def _write_modes(cur):
        _modes["current_active_mode"] = cur
        with open(_modes_p, "w", encoding="utf-8") as f:
            _json.dump(_modes, f, ensure_ascii=False)
        os.utime(_modes_p, (time.time() + 1, time.time() + 1))

    _saved_env = (os.environ.get("HIKSEMI_FAN_COOLERCONTROL"),
                  os.environ.get(mw.CC_MODE_CONFIG_ENV),
                  os.environ.get(mw.CC_FAN_MODE_MAP_ENV))
    try:
        os.environ["HIKSEMI_FAN_COOLERCONTROL"] = "1"
        os.environ[mw.CC_MODE_CONFIG_ENV] = _modes_p
        os.environ[mw.CC_FAN_MODE_MAP_ENV] = _map_p
        mw._CC_REV_CACHE["sig"] = None

        _write_modes(U_LOW)
        mw._CC_REV_CACHE["sig"] = None
        check("UUID Mode（名字「hs 低速…」）反查出档位 2",
              mw._cc_current_fan_mode() == 2, mw._cc_current_fan_mode())

        _write_modes(U_SMART)
        mw._CC_REV_CACHE["sig"] = None
        check("切到「hs 智能…」反查出档位 5",
              mw._cc_current_fan_mode() == 5, mw._cc_current_fan_mode())

        _write_modes(U_MINE)
        mw._CC_REV_CACHE["sig"] = None
        check("名字无关（我的自定义模式）→ None（不乱猜，保持本地）",
              mw._cc_current_fan_mode() is None, mw._cc_current_fan_mode())

        # ── 显式绑定后即可识别（等价于 apply-mode.sh --bind 2=<uid>）──
        with open(_map_p, "w", encoding="utf-8") as f:
            _json.dump({"2": U_MINE}, f)
        os.utime(_map_p, (time.time() + 2, time.time() + 2))
        mw._CC_REV_CACHE["sig"] = None
        check("--bind 后自定义 Mode 反查出档位 2",
              mw._cc_current_fan_mode() == 2, mw._cc_current_fan_mode())

        # ── FanState.mode() 会把 CC 侧的值同步回本地（屏幕回显跟着变）──
        _write_modes(U_LOW)
        mw._CC_REV_CACHE["sig"] = None
        st8, p8 = make_state()
        st8.update(5)                       # 屏幕当前在「智能」
        _write_modes(U_LOW)                 # 用户在 CC UI 里手动切到「低速」
        mw._CC_REV_CACHE["sig"] = None
        try:
            check("mode() 反向同步：本地 5 → CC 的 2", st8.mode() == 2, st8.mode())
            check("同步后已持久化（重开仍是 2）",
                  mw.FanState(path=p8).mode(sync_external=False) == 2)
        finally:
            if os.path.exists(p8):
                os.remove(p8)

        # 关掉开关 → 不再读 CC（避免没装 CoolerControl 时空转）
        os.environ["HIKSEMI_FAN_COOLERCONTROL"] = "0"
        mw._CC_REV_CACHE["sig"] = None
        check("未启用 CoolerControl 时不做反向同步",
              mw._cc_current_fan_mode() is None)
    finally:
        for k, v in zip(("HIKSEMI_FAN_COOLERCONTROL",
                         mw.CC_MODE_CONFIG_ENV, mw.CC_FAN_MODE_MAP_ENV), _saved_env):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mw._CC_REV_CACHE["sig"] = None
        _sh.rmtree(_ccdir, ignore_errors=True)

    # ── 17. ★★ 自我回滚防护（屏幕切档 → 写 modes.json → 反向同步弹回）──────
    #   真机 bug：屏幕切到「低速」后，中间件调 apply-mode.sh 把 modes.json 的
    #   current_active_mode 写成「低速」；随后状态帧的 _cc_current_fan_mode()
    #   读到这个「低速」，而本地状态也是「低速」→ 看似没问题……
    #   ★ 但真正的坑是：如果本地状态被别的路径写回旧值（或 apply 与读取
    #     交错），反向同步就会拿 CC 的值覆盖本地，表现为「切了一下又弹回去」。
    #   防护 = update() 打自写窗口标记；窗口内读到 == 目标档位 ⇒ 不回滚。
    print("\n【17】自我回滚防护（屏幕切档不被反向同步弹回）")
    import json as _json2
    import shutil as _sh2
    _ccdir2 = tempfile.mkdtemp()
    _modes_p2 = os.path.join(_ccdir2, "modes.json")
    _map_p2 = os.path.join(_ccdir2, "hs-fan-mode-map.json")
    _U_LOW2 = "aaaaaaaa-1111-2222-3333-444455556666"
    _U_STD2 = "bbbbbbbb-1111-2222-3333-444455556666"
    _modes2 = {"modes": [
        {"uid": _U_STD2, "name": "hs 标准 55%", "all_device_settings": {}},
        {"uid": _U_LOW2, "name": "hs 低速 25%", "all_device_settings": {}},
    ], "current_active_mode": _U_STD2}

    def _write_modes2(cur):
        _modes2["current_active_mode"] = cur
        with open(_modes_p2, "w", encoding="utf-8") as f:
            _json2.dump(_modes2, f, ensure_ascii=False)
        os.utime(_modes_p2, (time.time() + 3, time.time() + 3))

    _saved_env2 = (os.environ.get("HIKSEMI_FAN_COOLERCONTROL"),
                   os.environ.get(mw.CC_MODE_CONFIG_ENV),
                   os.environ.get(mw.CC_FAN_MODE_MAP_ENV),
                   os.environ.get(mw.CC_SELF_WRITE_ENV))
    try:
        os.environ["HIKSEMI_FAN_COOLERCONTROL"] = "1"
        os.environ[mw.CC_MODE_CONFIG_ENV] = _modes_p2
        os.environ[mw.CC_FAN_MODE_MAP_ENV] = _map_p2
        mw._CC_REV_CACHE["sig"] = None
        mw._CC_SELF_WRITE["mode"] = None
        mw._CC_SELF_WRITE["until"] = 0.0

        st9, p9 = make_state()
        try:
            # 初始：屏幕在「标准」，CC 也在标准
            st9.update(3)
            _write_modes2(_U_STD2)
            mw._CC_REV_CACHE["sig"] = None
            check("起始态：屏幕=3 与 CC=标准 一致", st9.mode() == 3, st9.mode())

            # ★ 模拟「屏幕切到低速」：update(2) → 打自写标记 → 外部把
            #   modes.json 写成低速（apply-mode.sh 的行为）
            st9.update(2)
            check("update(2) 后本地状态=2", st9.get()["fan_mode"] == 2,
                  st9.get()["fan_mode"])
            check("update(2) 已打自写标记",
                  mw._CC_SELF_WRITE["mode"] == 2,
                  mw._CC_SELF_WRITE["mode"])
            check("自写窗口已开启", mw._cc_in_self_write_window())

            _write_modes2(_U_LOW2)          # apply-mode.sh 写文件
            mw._CC_REV_CACHE["sig"] = None
            # ★ 关键断言：窗口内反向同步读到 2 == 自写目标 ⇒ 不得回滚
            check("★ 自写窗口内：mode() 保持 2（不被弹回 3）",
                  st9.mode() == 2, st9.mode())
            check("★ 自写窗口内：本地状态仍为 2",
                  st9.get()["fan_mode"] == 2, st9.get()["fan_mode"])

            # ★ 对照：窗口过期后，用户真的在 CC UI 手动切了 → 必须同步
            mw._CC_SELF_WRITE["until"] = 0.0        # 强制窗口过期
            _write_modes2(_U_STD2)                  # 用户在 CC 切回标准
            mw._CC_REV_CACHE["sig"] = None
            check("窗口过期后：CC 手动切回标准 → 屏幕跟上 =3",
                  st9.mode() == 3, st9.mode())

            # ★ 边界：窗口开着但 CC 切到了【另一个】档 → 是真外部操作，要同步
            st9.update(2)                           # 屏幕切低速（打标记）
            check("再次切档：标记=2", mw._CC_SELF_WRITE["mode"] == 2,
                  mw._CC_SELF_WRITE["mode"])
            mw._CC_REV_CACHE["sig"] = None
            _write_modes2(_U_STD2)                  # 但 CC 侧是标准（≠目标）
            mw._CC_REV_CACHE["sig"] = None
            check("窗口内但读到≠目标值 → 判定为外部切换，同步为 3",
                  st9.mode() == 3, st9.mode())

            # 窗口秒数可用环境变量覆盖
            os.environ[mw.CC_SELF_WRITE_ENV] = "0"
            check("窗口秒数可被 HIKSEMI_FAN_CC_SELF_WRITE_SEC 覆盖为 0",
                  mw._cc_self_write_window() == 0.0, mw._cc_self_write_window())
            os.environ.pop(mw.CC_SELF_WRITE_ENV, None)
            check("不设环境变量时用默认 12s",
                  mw._cc_self_write_window() == mw.CC_SELF_WRITE_WINDOW,
                  mw._cc_self_write_window())
        finally:
            if os.path.exists(p9):
                os.remove(p9)
    finally:
        for k, v in zip(("HIKSEMI_FAN_COOLERCONTROL",
                         mw.CC_MODE_CONFIG_ENV, mw.CC_FAN_MODE_MAP_ENV,
                         mw.CC_SELF_WRITE_ENV), _saved_env2):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mw._CC_REV_CACHE["sig"] = None
        mw._CC_SELF_WRITE["mode"] = None
        mw._CC_SELF_WRITE["until"] = 0.0
        _sh2.rmtree(_ccdir2, ignore_errors=True)

    # ── 16. 版本号 ──
    print("\n【16】版本号已递增")
    check("版本含 散热风扇/风扇", "风扇" in mw.MIDDLEWARE_VERSION, mw.MIDDLEWARE_VERSION)

    if os.path.exists(p):
        os.remove(p)
    print("\n" + "=" * 60)
    print(f" 结果：{PASS} 通过 / {FAIL} 失败 / 共 {PASS + FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

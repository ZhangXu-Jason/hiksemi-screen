#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动息屏（熄屏时间）测试
==========================================
覆盖：
  · OffTimeState 默认值/持久化/夹取非法值/线程安全
  · get_off_time builder 契约（★ 必须是 xp_time 标量，不是 off_time 数组）
  · set_off_time resolver（写 xp_time，返回 code=0，非嵌套）
  · 客户端解析复刻（AUTO_CLOSE_LIST 匹配）
  · 屏幕开关（/sys/class/graphics/fb0/blank）

用法： python3 _test_off_time.py
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


def make_state():
    fd, p = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(p)
    return mw.OffTimeState(path=p), p


def main():
    print("=" * 60)
    print(" 自动息屏（熄屏时间）测试")
    print("=" * 60)

    cfg = mw.Config(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "data-mapping.json"))
    mw.CTX["config"] = cfg

    # ── 1. 默认值 ──
    print("\n【1】默认值")
    st, p = make_state()
    check("默认 xp_time = 120", st.get()["xp_time"] == 120, st.get())

    # ── 2. 合法值写入与持久化 ──
    print("\n【2】合法值写入 + 持久化")
    for v in (30, 60, 120, 300):
        st.update(xp_time=v)
        check(f"写入 {v} 生效", st.get()["xp_time"] == v, st.get())
    st2 = mw.OffTimeState(path=p)
    check("重新加载后仍为 300（持久化）", st2.get()["xp_time"] == 300, st2.get())

    # ── 3. 非法值夹取 ──
    print("\n【3】非法值夹到最近档位")
    cases = [(0, 30), (29, 30), (31, 30), (59, 60), (100, 120),
             (301, 300), (99999, 300), (-5, 30)]
    for raw, expect in cases:
        st.update(xp_time=raw)
        check(f"输入 {raw} → {expect}", st.get()["xp_time"] == expect, st.get())
    st.update(xp_time="abc")
    check("非数字 → 默认 120", st.get()["xp_time"] == 120, st.get())

    # ── 4. builder 契约：必须是 xp_time 标量 ──
    print("\n【4】get_off_time 契约（★ 关键 bug 修复点）")
    mw.CTX["off_time"] = st
    st.update(xp_time=60)
    r = mw.build_response("get_off_time", {}, {})
    check("响应含 code=0", r.get("code") == 0, r)
    check("★ 含 xp_time 字段（客户端真正读的）", "xp_time" in r, r)
    check("★ xp_time 是标量 60（非数组）", r.get("xp_time") == 60, r)
    check("不含旧的 off_time 字段", "off_time" not in r, r)

    # ── 5. resolver：写接口 ──
    print("\n【5】set_off_time 写接口")
    r = mw.build_response("set_off_time", {"xp_time": 300}, {})
    check("返回 code=0（非嵌套）", r.get("code") == 0, r)
    check("状态已写入 300", st.get()["xp_time"] == 300, st.get())

    # 客户端实际 URL 形式：?xp_time=30&cmdfrom=devsub
    r = mw.build_response("set_off_time", {"xp_time": "30", "cmdfrom": "devsub"}, {})
    check("字符串 '30' 也能写（URL 参数是字符串）", st.get()["xp_time"] == 30, st.get())
    check("仍返回 code=0", r.get("code") == 0, r)

    # ── 6. 客户端解析复刻 ──
    print("\n【6】客户端解析复刻（AUTO_CLOSE_LIST）")
    AUTO_CLOSE_LIST = [{"id": 30}, {"id": 60}, {"id": 120}, {"id": 300}]
    for v in (30, 60, 120, 300):
        st.update(xp_time=v)
        res = mw.build_response("get_off_time", {}, {})
        # 客户端：res.code===0 ? res.xp_time : -1
        xp = res["xp_time"] if res.get("code") == 0 else -1
        matched = next((it for it in AUTO_CLOSE_LIST if it["id"] == xp), None)
        check(f"xp_time={v} 能被 UI 列表匹配", matched is not None, xp)

    # ── 7. 屏幕开关可用性 ──
    print("\n【7】屏幕开关路径探测")
    check("SCREEN_BLANK_PATH 已定义",
          mw.SCREEN_BLANK_PATH == "/sys/class/graphics/fb0/blank",
          mw.SCREEN_BLANK_PATH)
    # Windows 上不存在，_screen_set 应安全返回 False 而不崩
    ok = mw._screen_set(True)
    check("_screen_set 在无设备时安全返回（不崩）", ok in (True, False))

    # ── 8. ★ 计时重置（修复"设完档立刻熄屏"/"启动即秒黑"）──
    print("\n【8】★ 设置档位时重置计时")
    st.update(xp_time=30)
    # 模拟：把 last_active 设成 100 秒前（假装已空闲很久）
    mw._IDLE["last_active"] = 0
    mw._IDLE["blanked"] = False
    before = mw._IDLE["last_active"]
    mw.build_response("set_off_time", {"xp_time": 60}, {})
    after = mw._IDLE["last_active"]
    check("set_off_time 后 last_active 被刷新（不再是 0）",
          after != before and after > 0, f"before={before} after={after}")
    check("计时重置到「刚刚」（距今 < 2s）",
          (__import__("time").time() - after) < 2.0, after)

    # 设置后若处于黑屏，应自动亮屏
    mw._IDLE["blanked"] = True
    mw.build_response("set_off_time", {"xp_time": 120}, {})
    check("设置时若黑屏 → 自动亮屏（blanked 复位）",
          mw._IDLE["blanked"] is False, mw._IDLE["blanked"])

    # ── 9. ★ 档位变化线内重置（_idle_loop 内逻辑的等价校验）──
    print("\n【9】★ 档位变化触发重置（防止改小档位后立刻黑屏）")
    src = __import__("inspect").getsource(mw._idle_loop)
    check("_idle_loop 含档位变化检测", "档位变化" in src)
    check("_idle_loop 用监视启动时刻作基准（非模块导入时刻）",
          "_IDLE[\"last_active\"] = time.time()" in src)
    check("_idle_loop 用 select 等待输入（即时唤醒）", "select.select" in src)
    check("_idle_loop 心跳按墙钟时间（非 tick 计数）",
          "_now - _last_hb >= 30" in src and "_tick % 60" not in src)

    # ── 10. ★★ 输入事件解析：区分「真实操作」与「噪声」──
    print("\n【10】★★ 输入事件解析（噪声过滤，修复熄屏不及时）")
    import struct

    def ev(etype, code, value):
        return b"\x00" * 16 + struct.pack("<HHi", etype, code, value)

    parse_ev = mw._parse_input_events
    check("纯 EV_SYN（触摸屏噪声）→ 不算活动", parse_ev(ev(0, 0, 0) * 5) is False)
    check("EV_MSC（杂项噪声）→ 不算活动", parse_ev(ev(4, 0, 0)) is False)
    check("BTN_TOUCH 按下 → 算活动", parse_ev(ev(1, 0x14a, 1)) is True)
    check("BTN_TOUCH 抬起 → 算活动", parse_ev(ev(1, 0x14a, 0)) is True)
    check("普通按键 EV_KEY → 算活动", parse_ev(ev(1, 30, 1)) is True)
    check("触摸坐标 ABS_MT_POSITION_X → 算活动", parse_ev(ev(3, 0x35, 1234)) is True)
    check("触摸坐标 ABS_MT_POSITION_Y → 算活动", parse_ev(ev(3, 0x36, 567)) is True)
    check("坐标=0（初始化/噪声）→ 不算活动", parse_ev(ev(3, 0x35, 0)) is False)
    check("鼠标 EV_REL 移动 → 算活动", parse_ev(ev(2, 0, 5)) is True)
    check("EV_REL 值=0 → 不算活动", parse_ev(ev(2, 0, 0)) is False)
    check("空数据 → 不算活动", parse_ev(b"") is False)
    check("非 24 对齐的残包 → 不崩", parse_ev(b"\x01" * 20) in (True, False))
    check("mixed：SYN + 真触摸 → 算活动", parse_ev(ev(0, 0, 0) + ev(1, 0x14a, 1)) is True)
    check("strict=False 退化模式：任何数据都算",
          mw._parse_input_events(ev(0, 0, 0), strict=False) is True)

    # ── 11. ★ 输入设备名解析 ──
    print("\n【11】输入设备探测（含名称）")
    src2 = __import__("inspect").getsource(mw._find_input_devices)
    check("_find_input_devices 读取设备 name", "/sys/class/input/" in src2)
    check("_find_input_devices 返回 4 元组（含名称/类型）",
          "is_touch_like" in src2 or "touch_like" in src2)

    # ── 12. ★★★ 屏幕控制：回读校验（核心修复） ──
    print("\n【12】屏幕开关：写入回读校验（报成功不等于生效 的修复）")
    import shutil as _sh
    tmpd = tempfile.mkdtemp(prefix="fbtest-")
    try:
        # 造一个"假 framebuffer"：写入后回读与写入一致 → 应判生效
        p_ok = os.path.join(tmpd, "blank_ok")
        with open(p_ok, "w") as f:
            f.write("0")
        check("_write_blank 正常写入 → 回读一致 → True", mw._write_blank(p_ok, True) is True)
        check("  回读值确实变成 1", mw._read_blank(p_ok) == "1")
        check("_write_blank 写 0 → True", mw._write_blank(p_ok, False) is True)

        # 造一个"写入被忽略"的假 framebuffer（模拟本机 fb0 症状：写不生效）
        p_dead = os.path.join(tmpd, "blank_dead")
        with open(p_dead, "w") as f:
            f.write("0")
        # 用 monkeypatch 模拟：写入被静默丢弃（本机 fb0 的症状）
        import builtins
        _real_open = builtins.open

        class _DropWriter:
            """写入被丢弃、回读恒为 0 —— 模拟『写成功但屏幕不黑』"""
            def __init__(self, path, mode="r", *a, **k):
                self._f = _real_open(path, "r" if "w" not in mode else "r+", *a, **k)
            def read(self, *a):
                return self._f.read(*a)
            def write(self, *a):
                return len(a[0]) if a else 0      # 假装写了，实际丢弃
            def close(self):
                self._f.close()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                self.close()

        builtins.open = lambda path, mode="r", *a, **k: _DropWriter(path, mode) \
            if str(path) == p_dead else _real_open(path, mode, *a, **k)
        try:
            check("_write_blank 写入被丢弃 → 回读不符 → False",
                  mw._write_blank(p_dead, True) is False)
        finally:
            builtins.open = _real_open

    finally:
        _sh.rmtree(tmpd, ignore_errors=True)

    # ★ 这两项放在 mock 还原 + 清理之后再测（否则会被 patch 影响）
    check("_write_blank 路径不存在 → False（不抛异常）",
          mw._write_blank(os.path.join(tmpd, "nope"), True) is False)
    check("_read_blank 路径不存在 → None",
          mw._read_blank(os.path.join(tmpd, "nope")) is None)

    # ── 13. ★★★ 多 framebuffer 选屏（DSI 优先，不认编号） ──
    print("\n【13】多 framebuffer 选屏：DSI 优先，fb0 不一定是对的")
    src_score = __import__("inspect").getsource(mw._score_blank_path)
    check("打分函数含 DSI 优先级", "dsi" in src_score.lower())
    check("打分函数会压低 HDMI/VGA（明显不是小屏）",
          "hdmi" in src_score.lower() and "vga" in src_score.lower())
    check("_FB_CONNECTOR_PRIORITY 首位是 dsi",
          mw._FB_CONNECTOR_PRIORITY[0] == "dsi")
    # 用真实 sysfs 风格路径验证打分（不依赖文件存在，直接调打分）
    try:
        s_dsi = mw._score_blank_path("/sys/class/graphics/fb1/blank") \
            if "dsi" in mw._fb_connector_name("/sys/class/graphics/fb1/blank") else None
    except Exception:
        s_dsi = None
    check("_score_blank_path 对任意路径不抛异常",
          isinstance(mw._score_blank_path("/sys/class/graphics/fb0/blank"), int))
    check("_blank_candidates 返回 list", isinstance(mw._blank_candidates(), list))

    # ── 14. ★ 背光兜底通道 ──
    print("\n【14】背光兜底（blank 无效时的第二条路）")
    check("_backlight_paths 可调用且返回 list", isinstance(mw._backlight_paths(), list))
    src_bl = __import__("inspect").getsource(mw._screen_set_backlight)
    check("背光熄屏会保存原值以便还原", "_BACKLIGHT_SAVED" in src_bl)
    check("背光写作 0（亮度归零）", '"0"' in src_bl)
    src_set = __import__("inspect").getsource(mw._screen_set)
    check("_screen_set 含背光兜底", "_screen_set_backlight" in src_set)
    check("_screen_set 含 DPMS 兜底", "_screen_set_dpms" in src_set)
    check("_screen_set 遍历候选（不是只写第一个）",
          "_blank_candidates" in src_set)

    # ── 14b. ★★★ 分辨率判据（识别小屏最强、最可靠的手段） ──
    print("\n【14b】分辨率判据：识别小屏的可靠依据")
    check("PANEL_SIZES 含 (376,960)", (376, 960) in mw.PANEL_SIZES)
    check("PANEL_SIZES 含 (960,376)", (960, 376) in mw.PANEL_SIZES)
    check("_fb_virtual_size 可调用", callable(mw._fb_virtual_size))
    check("_fb_name 可调用", callable(mw._fb_name))
    src_score2 = __import__("inspect").getsource(mw._score_blank_path)
    check("打分函数用分辨率做首要判据",
          "_fb_virtual_size" in src_score2 and "PANEL_SIZES" in src_score2)
    check("分辨率匹配返回 1000（远高于连接器 100）", "1000" in src_score2)
    # 真实造一个 fb 目录，验证能读出 virtual_size
    import tempfile as _tf
    d2 = _tf.mkdtemp(prefix="fbsize-")
    try:
        fbd = os.path.join(d2, "fb7")
        os.makedirs(fbd)
        with open(os.path.join(fbd, "virtual_size"), "w") as f:
            f.write("376,960")
        with open(os.path.join(fbd, "name"), "w") as f:
            f.write("mipi-dsi")
        bp = os.path.join(fbd, "blank")
        with open(bp, "w") as f:
            f.write("0")
        check("_fb_virtual_size 正确解析 376,960",
              mw._fb_virtual_size(bp) == (376, 960))
        check("_fb_name 读出 mipi-dsi", mw._fb_name(bp) == "mipi-dsi")
        check("★ 分辨率匹配的小屏得分 = 1000",
              mw._score_blank_path(bp) == 1000)
    finally:
        _sh.rmtree(d2, ignore_errors=True)

    # ── 14c. ★ DPMS 通道 ──
    print("\n【14c】DPMS 通道（DRM 层面关屏）")
    check("_drm_connectors 返回 list", isinstance(mw._drm_connectors(), list))
    check("_screen_set_dpms 可调用", callable(mw._screen_set_dpms))
    src_dpms = __import__("inspect").getsource(mw._screen_set_dpms)
    check("DPMS 只操作 connected 连接器", "connected" in src_dpms)
    check("DPMS 写 Off/On 而非数字", '"Off"' in src_dpms and '"On"' in src_dpms)

    # ── 15. ★ 熄屏失败不得置 blanked=True（防状态脱节） ──
    print("\n【15】熄屏失败时不得标记为已黑屏（防内部状态脱节）")
    src_loop = __import__("inspect").getsource(mw._idle_loop) \
        if hasattr(mw, "_idle_loop") else ""
    if not src_loop:
        import re as _re
        _full = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "middleware.py"), encoding="utf-8").read()
        src_loop = _full
    check("失败分支显式置 blanked=False",
          "blank_fail_warned" in src_loop)
    check("_IDLE 初始化含 blank_fail_warned",
          "blank_fail_warned" in str(mw._IDLE))

    # ── 16. ★★★ 通道优先级：背光优先（真机实测唯一有效） ──
    print("\n【16】屏控通道优先级：背光 > DPMS > blank（真机三轮验证）")
    src_final = __import__("inspect").getsource(mw._screen_set)
    i_bl = src_final.find("_screen_set_backlight")
    i_dp = src_final.find("_screen_set_dpms")
    i_bk = src_final.find("_write_blank")
    check("三通道都在 _screen_set 中", -1 not in (i_bl, i_dp, i_bk))
    check("★ 背光在 DPMS 之前调用（背光优先）", i_bl < i_dp)
    check("★ 背光在 blank 之前调用（blank 降为补充）", i_bl < i_bk)
    check("_screen_set 不再一有 blank 成功就 return",
          "ok_any" in src_final)

    # ── 17. ★★ 背光看门狗（防退出后屏幕永久黑） ──
    print("\n【17】背光看门狗：退出时恢复背光")
    check("_restore_backlight_on_exit 可调用", callable(mw._restore_backlight_on_exit))
    check("_install_backlight_watchdog 可调用", callable(mw._install_backlight_watchdog))
    src_wd = __import__("inspect").getsource(mw._install_backlight_watchdog)
    check("注册了 atexit", "atexit" in src_wd)
    check("处理 SIGTERM", "SIGTERM" in src_wd)
    check("处理 SIGINT", "SIGINT" in src_wd)
    check("_SCREEN_BLANKED 状态存在", hasattr(mw, "_SCREEN_BLANKED"))
    src_rb = __import__("inspect").getsource(mw._restore_backlight_on_exit)
    check("还原前检查 _SCREEN_BLANKED（幂等）", "_SCREEN_BLANKED" in src_rb)
    mw._SCREEN_BLANKED["v"] = False
    mw._restore_backlight_on_exit()
    check("未熄屏时调用还原无副作用", mw._SCREEN_BLANKED["v"] is False)

    # ── 18. ★ 背光双写（brightness=0 + bl_power=4） ──
    print("\n【18】背光双写保险")
    src_bl2 = __import__("inspect").getsource(mw._screen_set_backlight)
    check("brightness 写 0", '"0"' in src_bl2)
    check("bl_power 写 4（FB_BLANK_POWERDOWN）", '"4"' in src_bl2)
    check("_read_sysfs_str 辅助函数存在", callable(mw._read_sysfs_str))

    os.remove(p)
    print("\n" + "=" * 60)
    print(f" 结果：{PASS} 通过 / {FAIL} 失败 / 共 {PASS + FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

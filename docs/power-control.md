# 关机 / 重启（set_power）控制文档

> 适用：hiksemi-sub 移植到飞牛OS（fnOS）后，控制中心「关机」「重启」按钮点击无反应的问题。  
> 相关文件：`tools/middleware/middleware.py`、`tools/middleware/data-mapping.json`、`tools/diag.sh`（`--power`）

---

## 1. 现象

点击控制中心底部「关机」/「重启」，弹出确认框，点「确认」后**按钮无任何反应**：设备既不关机也不重启，界面也无提示。

---

## 2. 根因

客户端点击确认后确实**发出了请求**，但服务端（中间件）**没有为 `set_power` 配置任何处理**，落进了 `defaults.unknown_action`，只回了一个 `{"code": 0}`：

| 环节          | 实际情况                                                                     |
| ----------- | ------------------------------------------------------------------------ |
| 客户端         | `GET /rest/1.1/config?action=set_power&power_mode={0\|1}&cmdfrom=devsub` |
| 中间件 mapping | **无 `set_power` 条目** → 命中 `defaults.unknown_action`                      |
| 响应          | `{"code": 0}`（客户端认为成功）                                                   |
| 副作用         | **无**（没人去关机）                                                             |

所以表现为「点了没反应」，但客户端侧还以为是成功的。

---

## 3. 客户端契约（逆向结果）

来源：`resources/app/dist/assets/index-be27a12a.js`（接口定义）+ `index-6fd73a6e.js`（UI 按钮）。

```js
// index-be27a12a.js —— 接口定义
setDevTurnOff = function (e) {
  const i = encodeURI(
    `${httpHeader}/rest/1.1/config?access_token=${makeToken()}&action=set_power&power_mode=${e}&cmdfrom=devsub`
  );
  ...
  httpGet(i, timeOut).then(s => { ... r(s); })   // 直接把响应回给调用方
};

// index-6fd73a6e.js —— 按钮 → 确认弹窗 → 调用
function $() { h.value = !0 }                 // 点「关机」→ 打开确认框
function B() { r.value = !0 }                 // 点「重启」→ 打开确认框
async function L() { h.value = !1; ft(0) }    // 确认关机 → ft(0)
async function F() { r.value = !1; ft(1) }    // 确认重启 → ft(1)
// 其中：import { ac as ft } from "./index-be27a12a.js"  ← ac 即 setDevTurnOff
```

**契约摘要：**

| 项目             | 值                                                |
| -------------- | ------------------------------------------------ |
| 方法             | **GET**（`httpGet`）                               |
| 路径             | `/rest/1.1/config`                               |
| 参数             | `action=set_power`、`power_mode`、`cmdfrom=devsub` |
| `power_mode=0` | **关机**                                           |
| `power_mode=1` | **重启**                                           |
| 期望响应           | `{"code": 0}`                                    |

> ⚠️ 注意客户端 `.catch` 里对 `e===1`（重启）有兜底 `r({code:0})`——因为重启时连接可能先断。关机没有这个兜底，所以**必须真正回包成功后**才执行动作。

---

## 4. 实现要点

### 4.1 必须异步执行（关键）

这是最容易踩的坑：**如果同步执行 `systemctl poweroff`，HTTP 响应还没发出去，进程就已经被杀了**，客户端收不到 `code=0`，界面会卡在「正在关机」。

实现方式：**先回包，延迟 1.5 秒，再用脱离会话的子进程执行**。

```python
POWER_ARM_DELAY = 1.5

def resolve_set_power(spec, ctx):
    params = ctx.get("_params") or {}
    which = int(params.get("power_mode"))
    # ★ 异步：立即返回，由后台线程延迟执行
    threading.Thread(target=_delayed_power, args=(which,),
                     daemon=True, name=f"power-{which}").start()
    return 0        # 只是 code 字段的值
```

```python
def _run_power_cmd(cmds):
    for cmd in cmds:
        if not shutil.which(cmd[0]):
            continue
        subprocess.Popen(cmd, stdout=DEVNULL, stderr=DEVNULL,
                         start_new_session=True)   # ★ 脱离会话，父进程退出也不影响
        return True, " ".join(cmd)
    return False, ""
```

`start_new_session=True` 很重要——它让子进程脱离当前会话/进程组，不会被中间件的退出或重启连带杀掉。

### 4.2 命令回退链

不硬绑定 `systemctl`，按可用性依次尝试：

| 动作 | 回退链                                                   |
| -- | ----------------------------------------------------- |
| 关机 | `systemctl poweroff` → `poweroff` → `shutdown -h now` |
| 重启 | `systemctl reboot` → `reboot` → `shutdown -r now`     |

### 4.3 配置

`data-mapping.json`：

```json
"set_power": {
  "code": { "source": "set_power" }
}
```

**注意**：`resolve_set_power` 是**字段级 resolver**，返回的是 `code` 字段的值（`0`），不是 `{"code":0}`——否则响应会变成 `{"code":{"code":0}}`，客户端 `res.code === 0` 判定失败。

---

## 5. 运行验证

### 5.1 日志关键字

点击「关机」/「重启」后，日志应出现：

```
GET /rest/1.1/config  action=set_power  模式=auto
[power] 收到关机请求（power_mode=0）→ 1.5s 后执行
  → set_power 响应: {"code": 0}
[power] ★ 已触发关机：systemctl poweroff
```

### 5.2 真机验收

1. 点「关机」→ 确认 → **1~2 秒后设备断电**
2. 点「重启」→ 确认 → **1~2 秒后设备重启**

### 5.3 排障

若点击后日志有 `[power] ✗ 关机失败`，说明命令不可用或权限不足：

```bash
sudo bash tools/diag.sh --power
```

重点看输出：

- `【4】中间件是否 root 运行`：中间件必须 **root**
- `【1】关机/重启命令可用性`：`systemctl` / `poweroff` / `reboot` 是否齐全
- `【2】systemd 运行状态`：`is-system-running` 是否为 `running` / `degraded`

> 飞牛OS 是 Debian 12，`systemctl` 正常可用，服务以 `User=root` 运行，通常一次即可生效。

---

## 6. 测试覆盖

`tools/middleware/_test_set_power.py`（21 项）：

- 契约：`0=关机`、`1=重启`、首选命令正确
- resolver 返回标量 `0`（不嵌套）
- **非阻塞**：resolver 立即返回（实测 ≈0ms）
- 非法/未知 `power_mode`（`None`/`""`/`abc`/`2`/`-1`/`99`）不触发动作
- 命令回退链（仅 `shutdown` 存在时正确回退；全无时返回 `(False, "")`）
- mapping 配置 + `RESOLVERS` 注册
- `build_response` 端到端返回 `{"code": 0}`

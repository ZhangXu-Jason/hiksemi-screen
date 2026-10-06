# hiksemi-screen

> 把海康「小彩屏」HS-AFS-R1（飞牛OS / Debian 12）随附的 Electron 桌面程序，移植到通用 Linux 上运行。

**思路一句话：二进制原封不动，只替换它依赖的那个本地 REST 服务。**

---

## 背景与原理

海康 HS-AFS-R1 小彩屏出厂运行飞牛OS（fnOS，基于 Debian 12），界面上跑的是一个
Electron 程序（`hiksemi-sub`）。它**不直接读系统**，而是一律通过本机 HTTPS 服务取数：

```
hiksemi-sub (Electron)
      │  HTTPS  GET /rest/1.x/<action>?access_token=…&cmdfrom=devsub
      ▼
https://127.0.0.1:20443        ← 出厂时由厂商服务提供
```

本项目**不修改、不重打包**那个 Electron 二进制，而是用一个纯标准库 Python 写的
**中间件**顶替 `127.0.0.1:20443`：按 `data-mapping.json` 里声明的字段级映射规则
（`static` / `file` / `command` / `hwmon` / `collector` / `builder` 六类数据源），
把真实操作系统数据组装成客户端期望的响应格式（数据源、字段契约均从客户端包逆向得到）。

于是这个程序就能在**任何 Linux** 上跑起来，并且小屏上显示的是**本机真实数据**
（CPU、内存、磁盘、网络、温度、风扇……）。

## 功能

| 能力 | 说明 |
|---|---|
| 显示与业务数据 | CPU 占用 / 内存 / 存储容量与读写 / 网络接口信息（IP、速率、连接状态） |
| 灯控 | I2C 从设备 `0x26`：开关、静态色、彩虹、呼吸（RGB 先写、模式后写） |
| 息屏 | 走背光节点（`backlight/*/brightness=0` + `bl_power=4`），带看门狗精确还原 |
| 电源 | 关机 / 重启（先回包再异步执行） |
| 定时息屏 | 空闲计时（解析 `input_event`，过滤 `EV_SYN` 噪声），支持超时设置 |
| 风扇 | 四档（智能 / 低速 / 标准 / 强劲）→ 对接 [CoolerControl](https://gitlab.com/coolercontrol/coolercontrol)，**双风扇**（CPU + 机箱）各自温度曲线 |
| 开机自启 | systemd 单元 + kiosk 独占 tty1；含开机时序避让与监控服务补挂 |

## 环境要求

- Linux（已在 **fnOS / Debian 12 / Intel N100** 上实机验证）
- `python3` ≥ 3.8（**纯标准库**，零第三方依赖）
- Xorg / `xinit`、一个窗口管理器（脚本默认 `openbox`）
- `openssl`（首次安装生成自签证书；客户端不校验证书，任意证书均可）
- 可选：CoolerControl（想真正驱动风扇时）

## 快速开始

```bash
# 1) 先把海康那个 Electron 程序放到一个目录，例如 /screen/hiksemi-sub
#    ★ 本仓库不包含该程序（版权归海康），请从你自己的设备上获取。

# 2) 进入工具目录，跑安装器（自动完成：生成证书、渲染配置、装 systemd 单元）
cd /screen/tools
sudo bash install-autostart.sh --dir /screen/hiksemi-sub

# 常用参数
#   --dir <程序目录>              程序所在目录（不给则自动搜索常见位置）
#   --tools-dir <目录>            工具落地目录（默认 /screen/tools）
#   --fan-coolercontrol auto|on|off   是否接入 CoolerControl 风扇控制
#   --dry-run                     只打印将要执行的动作，不改系统
#   uninstall                     卸载（含恢复 getty@tty1）

# 3) 起效
sudo systemctl restart hiksemi-sub
systemctl status hiksemi-sub --no-pager
```

## 风扇控制（CoolerControl）

界面四档风扇默认**只保存状态、不驱动硬件**。想真正调速，安装时加
`--fan-coolercontrol on`（或 `auto` 自动探测），随后渲染配置：

```bash
sudo /screen/tools/coolercontrol/render-config.py --host hs-afs-r1
#   --single-channel      单风扇机型
#   --no-case-graph       机箱风扇走固定转速
#   --no-graph            两路都用固定转速兜底
```

详见 [`docs/coolercontrol.md`](docs/coolercontrol.md) 与
[`tools/coolercontrol/README.md`](tools/coolercontrol/README.md)。

> ⚠️ 双风扇机型请确认 `pwmN` 与风扇的对应关系（CPU / 机箱）；接反了就交换
> `--cpu-channel` / `--case-channel`。

## 排障与自检

```bash
sudo bash tools/test.sh            # 全量回归（516 断言）
bash tools/test.sh middleware      # 只跑中间件
bash tools/diag.sh                 # 一键体检（链路 / I2C / 屏 / 电源 / 网络 / 日志）
bash tools/probe.sh                # 只读探测（环境 / I2C / 风扇）
```

日志：`/var/log/hiksemi-sub/`（`kiosk.log`、`middleware.log`、`xorg.log`、`app.log`）。
日志级别用 `HIKSEMI_LOG=quiet|normal|debug` 控制。

## 目录结构

```
tools/
├── install-autostart.sh      # 安装 / 卸载（systemd 单元渲染、证书、风扇接入）
├── kiosk.sh                  # X 启动后的会话入口（清屏、WM、中间件、程序本体）
├── hiksemi-sub.service       # systemd 单元模板（@APP_DIR@ / @TOOLS_DIR@ / @KIOSK@ 占位符）
├── diag.sh / probe.sh / test.sh
├── decode-hslog.py           # 客户端日志解码
├── middleware/
│   ├── middleware.py         # ★ 本地 REST 服务（纯标准库）
│   ├── data-mapping.json     # ★ 字段级映射配置（热重载）
│   └── _test_*.py            # 中间件单元测试（347 断言）
└── coolercontrol/
    ├── config.toml           # 模板（含 @占位符@，渲染后落到 /etc/coolercontrol/）
    ├── modes.json            # 四档模式定义
    ├── render-config.py      # 模板渲染器（含契约校验）
    ├── apply-mode.sh         # 切档落地
    └── probe-coolercontrol.sh
docs/                         # 8 份专题文档（见下）
```

## 文档

| 文档 | 内容 |
|---|---|
| [`docs/port-to-fnos.md`](docs/port-to-fnos.md) | 移植总述：为什么要替换本地服务、契约从哪来 |
| [`docs/deploy-guide.md`](docs/deploy-guide.md) | 部署与故障速查 |
| [`docs/local-rest-api.md`](docs/local-rest-api.md) | 客户端调用的 REST 接口清单 |
| [`docs/led-control.md`](docs/led-control.md) | 灯控 I2C 协议 |
| [`docs/off-time-control.md`](docs/off-time-control.md) | 定时息屏 |
| [`docs/power-control.md`](docs/power-control.md) | 关机 / 重启 |
| [`docs/fan-control.md`](docs/fan-control.md) | 风扇四档 |
| [`docs/coolercontrol.md`](docs/coolercontrol.md) | CoolerControl 集成（双风扇曲线） |

## 本仓库不包含什么

- **海康官方程序本体**：`hiksemi-sub` 可执行文件、`resources/`（含前端资源）、
  Electron/Chromium 运行时等一律不入库（版权归海康，且体积 300M+）。
  请从你自己的设备上获取并放到 `APP_DIR`。
- **私钥与证书**：`cert.pem` / `key.pem` 由安装器首次运行时自动生成。

## 免责声明

- 本项目仅包含**自研的中间件、脚本与文档**，不含任何海康威视的二进制或前端资源；
  `hiksemi-sub` 等名称与商标归其各自所有者。
- 接口契约来源于对**自己持有的设备**上的客户端程序的观察分析，仅用于个人设备的
  互操作与学习研究。请遵守你所在地区的法律法规及设备许可协议，自行承担使用风险。
- 作者不对因使用本项目导致的设备损坏、数据丢失或保修失效负责。

## 许可证

[MIT](LICENSE)

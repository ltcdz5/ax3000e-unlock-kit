# 小米路由器解锁套件（xiaomi-router-unlock-kit）：SSH 解锁 + 去广告/DNS 定制 + 监控面板

[![CI](https://github.com/ltcdz5/xiaomi-router-unlock-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/ltcdz5/xiaomi-router-unlock-kit/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/ltcdz5/xiaomi-router-unlock-kit)](https://github.com/ltcdz5/xiaomi-router-unlock-kit/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> 核心在小米 AX3000E（RN07）上实测验证，**中继/AP** 与 **主路由** 双模式全覆盖。
> ⚠️ 仅供学习与自有设备折腾使用，风险自负。**升级固件 = 解锁全部报废**——解锁后第一件事：管理页关闭自动升级。
> 本套件由 AI 辅助生成，所有功能均经自有设备实测验证后才发布，使用前请自行评估。

## 适配设备

| 设备 / 拓扑 | 状态 | 说明 |
|---|---|---|
| AX3000E（RN07）固件 ≤ 1.0.24 | ✅ 1.0.24 全支持（实测基准） | 1.0.24 为官方最新版、全套实测；**更低版本理论上同样可解锁**（注入漏洞旧固件即存在），未实测，路径/接口需按实机校准 |
| 同代 IPQ 平台（AX3000T 等） | ⚠️ 思路一致 | 同为 start_binding 注入解锁，需实机校准 |

## 目录结构

```
panel/    router_monitor_ap.py        AP 面板（核心 + monitor_web.py 服务层 + monitor_page.html 前端）
          router_monitor_ax3000e.py   主路由面板（端口转发/QoS/DHCP/防火墙，安全基线与 AP 面板一致）
          Start-*.bat                 Windows 一键启动器（自动装依赖，见下）
路由器面板.exe                          AP 模式桌面客户端（源码 desktop/RouterPanel.cs）：自动扫描 + 独立 Edge 窗口 + 关窗即清理
router/   auto_ssh.sh                 ★ v5 自愈脚本（放 /data/auto_ssh/，install 注册开机钩子）
          configs/                    各配置持久副本（upstreams/noipv6/microsoft/logqueries）
deploy/   一键部署.bat / .py          SSH 通了以后一键部署 DNS/去广告/自愈
docs/     自救手册.md                  SSH 失效时的分步自救流程
tests/    test_security.py            本地单元测试（注入拒绝/白名单/转义/门禁，CI 执行）
tools/    unlock_wizard.py            解锁向导（GUI/CLI，自动登录+注入+验口+接力部署）
          一键体检.bat / kit_doctor.py  自动发现路由器 IP + 状态体检 + 自动修复启动器 IP，列出手动待办
          migration_pack.py           迁移包：整套配置/自愈/列表缓存导出，换机时一键导入
```

## 定位与边界

这是**单设备、单固件版本的运维工具包**，不是通用固件方案：面向 AX3000E（RN07）官方固件 **≤ 1.0.24**（1.0.24 为当前官方最新版、全套实测基准；更低版本理论可用、未实测），核心价值是 SSH 解锁 + 三层自愈、DNS/去广告定制、本地 Web 面板。几条边界：

- **不是刷机项目**：不刷 OpenWrt / ImmortalWrt，全程保留小米官方固件，只在原厂系统上做 SSH 解锁与配置定制——要刷机请另寻教程；但只要 DNS/去广告/自愈这些需求，保留原厂固件 + 本套件是风险更低的路
- **WiFi 设置有天花板**：信道/功率为即时切换（重启丢失），持久化会被官方框架覆盖——面板只做即时切换，持久设置请用小米管理页；QCA 闭源驱动无公开的后台扫描开关，周期性扫描延迟尖刺无法根治
- **面板依赖一台常开的 PC**：面板持有 root SSH 凭据跑在电脑上，电脑关机监控即断；路由器侧零常驻负载
- **去广告拦截点在 DNS 层（中继模式的必然选择）**：ipset/防火墙方案需要阻断点位于网关，中继模式下网关是上级路由，故仅 DNS 劫持有效；内存占用约 5-15MB，已 NXDOMAIN 化降低客户端重试

## 快速开始

**最简路径**：到 [Releases](../../releases) 下载 zip 解压 → 按第 1 步解锁 → 双击根目录 `路由器面板.exe`（自动发现路由器 + 输 SSH 密码 + 独立窗口打开面板）。不想用 exe 也可双击 `panel/` 里的 `.bat` 启动器：
- `Start-AP-panel.bat` —— 中继/AP 模式：WiFi + DNS 去广告 + 监控
- `Start-MainRouter-panel.bat` —— 主路由模式：另含端口转发 / QoS / DHCP 静态绑定 / 防火墙 / UPnP

### 1 · 解锁 SSH（不丢配置）

**新手路径**：`python tools/unlock_wizard.py`——输 IP 和管理页密码自动完成登录、注入、验口，成功后可一键接力部署（无图形环境自动转命令行交互）。

**手动路径**：浏览器登录管理页，从地址栏复制 `;stok=` 后面的字符串，替换 `<IP>`/`<STOK>` 后逐条执行：

```powershell
$B="http://<IP>/cgi-bin/luci/;stok=<STOK>/api/xqsystem/start_binding"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Anvram%20set%20ssh_en%3D1'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Anvram%20set%20telnet_en%3D1'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Anvram%20commit'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Ased%20-i%20's%2Fchannel%3D.*%2Fchannel%3D%22debug%22%2Fg'%20%2Fetc%2Finit.d%2Fdropbear'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0A%2Fetc%2Finit.d%2Fdropbear%20start'"
```

`Test-NetConnection <IP> -Port 22` 应为 True。

⚠️ 网上常见的 `api/misystem/arn_switch` 在 RN07 上是**假接口**——返回 `{"code":0}` 但不执行任何命令，请认准上面的 `xqsystem/start_binding`。
ℹ️ 注入链只对**锁定状态**设备的首次解锁生效；已解锁设备重跑会返回 `code:1541` 且不执行（2026-08-30 实测，浏览器/API 两种 stok 均如此），无副作用，直接进第 2 步即可。

### 2 · 改密码 & 固化自愈

```powershell
ssh -oHostKeyAlgorithms=+ssh-rsa -oPubkeyAcceptedAlgorithms=+ssh-rsa root@<IP>
```

```sh
echo -e '你的密码\n你的密码' | passwd root
mkdir -p /data/auto_ssh          # 上传本仓库 router/auto_ssh.sh 到这里
chmod +x /data/auto_ssh/auto_ssh.sh
/bin/sh /data/auto_ssh/auto_ssh.sh install     # 注册 firewall.include 开机钩子
grep -q auto_ssh /etc/crontabs/root || echo '* * * * * /bin/sh /data/auto_ssh/auto_ssh.sh' >> /etc/crontabs/root
/etc/init.d/cron restart
```

### 3 · 一键部署插件

Windows 下运行 `deploy/一键部署.bat <IP>`：DNS 上游、anti-AD 去广告、抖音定向、禁 IPv6 一次落齐，持久到 `/data/`。

### 4 · 启动面板

双击启动器，首次自动装依赖，按提示输 IP 和 SSH 密码即可：

| 模式 | 启动器 | 地址 |
|---|---|---|
| 中继/AP（日常推荐） | 根目录 `路由器面板.exe`（独立窗口、自动扫描、关窗即清理）或 `panel/Start-AP-panel.bat` | 127.0.0.1:8787 |
| 主路由（端口转发/QoS/DHCP/防火墙） | `panel/Start-MainRouter-panel.bat` | 127.0.0.1:8788 |

命令行等价：`set ROUTER_PASSWD=密码 && python panel/router_monitor_ap.py`。
依赖只有 `requirements.txt`（paramiko **<5**，5.0 移除了 ssh-rsa，连不上老 dropbear；3.x/4.x 实测均可）。

## 安全基线（两个面板一致）

- 默认**只监听 127.0.0.1**；要局域网访问必须显式 `--lan --token <令牌>`，缺令牌拒启
- 所有请求过 **Host 回环校验**（防 DNS rebinding）+ **Origin/Referer 同源校验**（防 CSRF）
- `--lan` 模式启用 **HTTP Basic 令牌**（浏览器原生弹框，凭证自动附带所有请求）
- 写动作参数**全白名单**：IP/MAC/端口/信道/功率/域名/租期/带宽均有正则+范围检查，杜绝 uci 注入
- 前端所有路由器返回字段经 HTML 转义渲染，防存储型 XSS（DHCP 主机名等不可信字段）

## 三层自愈（重启不怕）

| 层 | 触发 | 说明 |
|---|---|---|
| 1 | firewall.include | 每次防火墙重载执行自愈脚本 |
| 2 | cron 每分钟 | 固件偶尔重建 crontab 会清掉这行——丢了补一行即可 |
| 3 | 脚本内重试 | 开机早期失败则后台每 10s 重试，最长 10 分钟 |

v5 特性：毫秒级防重复触发；去广告列表本地缓存、超 48h 才联网刷新（下载不达标绝不覆盖在用缓存）；动作全写 syslog（`grep auto_ssh /tmp/messages`）。

重置后恢复顺序：解锁 → 一键部署 → 从 `router/configs/` 恢复各文件到 `/data/` → `auto_ssh.sh install`。

## 常见问题

- **SSH 又没了？** → 按 [docs/自救手册.md](docs/自救手册.md) 四连 curl 复活，不用重置。
- **IP 老漂移？** → 上级路由 DHCP 里按 MAC 绑静态地址，一劳永逸。
- **中继模式下设备没去广告效果？** → 上级路由 DHCP 下发的 DNS 要指向本机 IP。
- **为什么不能升固件？** → 新固件封堵注入路径并清掉自愈，锁死 1.0.24 是底线。

## 版本记录

| 版本 | 要点 |
|---|---|
| v2.0 | 完整形态：双面板单一服务层（回环/Basic/同源校验）、去广告双列表自动更新、配置备份/恢复五道防线、解锁向导 + 一键体检（自动升级检测/关闭、DNS 链路检测）+ 实机探针、设备识别解耦、双 CI 发布通道、26 项测试 |
| v1.0 | 首版：SSH 解锁 + v4 三层自愈 + 双面板 + 一键部署；确认 arn_switch 为假接口、start_binding 为真 |

## 致谢

- [lemoeo/AX6S](https://github.com/lemoeo/AX6S) —— auto_ssh 原型与「firewall.include 固化」思路来源，本仓库在其基础上重写
- 社区教程分享者 —— `xqsystem/start_binding` 注入方法来自 AX3000E/T 相关公开文章，非本项目首创；[kjqq/AX3000E](https://github.com/kjqq/AX3000E) 是本机型较早的公开教程
- [TG-Twilight/AWAvenue-Ads-Rule](https://github.com/TG-Twilight/AWAvenue-Ads-Rule) —— 去广告补充列表来源
- [juewuy/ShellCrash](https://github.com/juewuy/ShellCrash) —— 官方固件跑 Clash 的流行方案，常与本套件配合

License: MIT

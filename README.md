# 小米 AX3000E 双模式套件：SSH 解锁 + 去广告/DNS 定制 + 监控面板

> 适用小米 AX3000E（RN07），同代 IPQ 平台（AX3000T 等）思路一致。
> 官方固件 **1.0.24**；**中继/AP** 与 **主路由** 两种模式均覆盖，SSH 解锁与自愈体系通用。
> ⚠️ 仅供学习与自有设备折腾使用，风险自负。**升级固件 = 解锁全部报废。**

## 目录结构

```
panel/    router_monitor_ap.py        AP 面板（核心 + monitor_web.py 服务层 + monitor_page.html 前端）
          router_monitor_ax3000e.py   主路由面板（端口转发/QoS/DHCP/防火墙，安全基线与 AP 面板一致）
          Start-*.bat                 Windows 一键启动器（自动装依赖，见下）
router/   auto_ssh.sh                 ★ v5 自愈脚本（放 /data/auto_ssh/，install 注册开机钩子）
          configs/                    各配置持久副本（upstreams/bytedance/noipv6/microsoft/logqueries）
deploy/   一键部署.bat / .py          SSH 通了以后一键部署 DNS/去广告/自愈
docs/     自救手册.md                  SSH 失效时的分步自救流程
```

## 快速开始

**最简路径**：到 [Releases](../../releases) 下载 zip 解压 → 按第 1 步解锁 → 双击 `panel/` 里对应模式的启动器。

### 1 · 解锁 SSH（不丢配置）

浏览器登录管理页，从地址栏复制 `;stok=` 后面的字符串，替换 `<IP>`/`<STOK>` 后逐条执行：

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
| 中继/AP（日常推荐） | `panel/Start-AP-panel.bat` | 127.0.0.1:8787 |
| 主路由（端口转发/QoS/DHCP/防火墙） | `panel/Start-MainRouter-panel.bat` | 127.0.0.1:8788 |

命令行等价：`set ROUTER_PASSWD=密码 && python panel/router_monitor_ap.py`。
依赖只有 `requirements.txt`（paramiko **<4**，5.x 移除了 ssh-rsa，连不上老 dropbear）。

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

| 日期 | 版本 | 要点 |
|---|---|---|
| 08-27 | v1.0 | 首版：v4 自愈脚本 + 双面板 + 冷启动演练通过；确认 arn_switch 为假接口 |
| 08-28 | v2.1→v2.3 | 面板五连更：Origin/Host 校验、降载与命中率、备份/下载、去广告开关联动 |
| 08-28 | v2.3.1 | 去广告换源：停更的 yhosts → AWAvenue；自愈升 v5（48h 缓存 + 每日 04:30 刷新 + 双镜像回退） |
| 08-28 | v2.4→v2.5.0 | 备份/恢复五道防线、custom.conf 持久化、恢复白名单收紧 |
| 08-28 | v2.5.1 | 主路由面板安全基线对齐：默认 127.0.0.1、--lan+令牌、uci 参数白名单、JS 转义 |
| 08-28 | v2.6.0 | --lan 认证改 HTTP Basic（修回归）；内联 JSON XSS 逃逸；开箱即用 zip + 双启动器 |

## 致谢

- [lemoeo/AX6S](https://github.com/lemoeo/AX6S) —— auto_ssh 原型与「firewall.include 固化」思路来源，本仓库在其基础上重写
- 社区教程分享者 —— `xqsystem/start_binding` 注入方法来自 AX3000E/T 相关公开文章，非本项目首创；[kjqq/AX3000E](https://github.com/kjqq/AX3000E) 是本机型较早的公开教程
- [TG-Twilight/AWAvenue-Ads-Rule](https://github.com/TG-Twilight/AWAvenue-Ads-Rule) —— 去广告补充列表来源
- [juewuy/ShellCrash](https://github.com/juewuy/ShellCrash) —— 官方固件跑 Clash 的流行方案，常与本套件配合

License: MIT

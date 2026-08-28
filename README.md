# 小米 AX3000E 中继模式全套留存：SSH 解锁 + 去广告/DNS 定制 + 监控面板

> 适用：小米 AX3000E（RN07）/ 同代 IPQ 平台（AX3000T 等思路一致）。
> 官方固件 **1.0.24**，中继/AP 模式挂在上级路由之后。
> ⚠️ 仅供学习与自有设备折腾使用；操作路由器风险自负。**升级固件 = 解锁全部报废**。

## 目录结构

```
panel/    router_monitor_ap.py        中继(AP)面板·核心逻辑  ·+ monitor_web.py 服务/认证 ·+ monitor_page.html 前端
          router_monitor_ax3000e.py   主路由模式完整版面板（已对齐 AP 面板安全基线，见下方安全说明）
deploy/   oneclick_deploy.py          SSH 通了以后一键部署 DNS/去广告/自愈
          一键部署.bat                 Windows 双击入口
router/   auto_ssh.sh                 ★ v5 自愈脚本：解锁+插件重建+去广告列表自动刷新(放 /data/auto_ssh/)
          configs/                    各配置持久副本(upstreams/bytedance/noipv6/microsoft/logqueries)
docs/     自救手册.md                  SSH 失效时的分步自救流程
          使用说明-主路由面板.md        主路由模式面板说明
```

## 面板安全基线（两个面板一致）

- **默认只监听 127.0.0.1**，仅本机可访问；要局域网访问必须显式 `--lan --token <令牌>`（缺令牌直接拒启）
- 所有 POST 经 **Host 回环校验 + Origin/Referer 同源校验**，--lan 模式另加令牌（POST 头 `X-Panel-Token`，浏览器 GET 用 `?token=`）
- **全部写动作参数白名单校验**：IP/MAC/端口/信道/功率/域名/租期/带宽均有正则+范围检查，杜绝 uci 单引号拼接注入
- 前端所有路由器返回字段经 HTML 转义后渲染，防存储型 XSS（DHCP 主机名等不可信字段）
- 日常使用推荐 **AP 面板**（功能与安全迭代最活跃）；主路由面板适合切回主路由模式时临时使用

## 快速开始

### 1. 解锁 SSH（不丢配置）
1) 浏览器登录管理页，从地址栏复制 `;stok=` 后面的字符串；
2) 把 `<IP>`/`<STOK>` 替换后逐条执行：

```powershell
$B="http://<IP>/cgi-bin/luci/;stok=<STOK>/api/xqsystem/start_binding"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Anvram%20set%20ssh_en%3D1'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Anvram%20set%20telnet_en%3D1'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Anvram%20commit'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0Ased%20-i%20's%2Fchannel%3D.*%2Fchannel%3D%22debug%22%2Fg'%20%2Fetc%2Finit.d%2Fdropbear'"
curl.exe -X POST $B -d "uid=1234&key=1234'%0A%2Fetc%2Finit.d%2Fdropbear%20start'"
```

3) `Test-NetConnection <IP> -Port 22` 应为 True。

⚠️ 本机实测坑：网上常见的 `api/misystem/arn_switch` 在 RN07 上是**假接口**——返回 `{"code":0}` 但不执行任何命令。请用上面的 `xqsystem/start_binding`（key 参数注入）。

### 2. 改 root 密码 & 固化
```powershell
ssh -oHostKeyAlgorithms=+ssh-rsa -oPubkeyAcceptedAlgorithms=+ssh-rsa root@<IP>
# 登录后：
echo -e '你的密码\n你的密码' | passwd root
mkdir -p /data/auto_ssh
```
把本仓库 `router/auto_ssh.sh` 上传到 `/data/auto_ssh/auto_ssh.sh` 并注册开机钩子：
```sh
chmod +x /data/auto_ssh/auto_ssh.sh
/bin/sh /data/auto_ssh/auto_ssh.sh install      # 注册 firewall.include
grep -q auto_ssh /etc/crontabs/root || echo '* * * * * /bin/sh /data/auto_ssh/auto_ssh.sh' >> /etc/crontabs/root
/etc/init.d/cron restart
```

### 3. 部署插件（DNS 上游/去广告/抖音定向/禁 IPv6）
Windows 下直接运行 `deploy/一键部署.bat <IP>`；脚本会从 `anti-ad.net` 拉最新列表并落到 `/data/` 持久分区。

### 4. 启动监控面板
```
set ROUTER_PASSWD=你的密码 && python panel/router_monitor_ap.py   # 浏览器开 http://127.0.0.1:8787
```
依赖：`pip install -r requirements.txt`（paramiko 必须 <4，5.x 移除了 ssh-rsa，连不上老 dropbear）。
默认只监听 127.0.0.1；需局域网访问时加 `--lan --token 你的令牌`（HTTP Basic 认证，缺一拒绝启动）。

## 三层自愈体系（重启不怕）

| 层 | 触发方式 | 说明 |
|---|---|---|
| 1 | firewall.include | 每次防火墙重载执行 `/data/auto_ssh/auto_ssh.sh` |
| 2 | cron 每分钟 | 固件偶尔会重建 crontab 清掉这行——丢了就补一行 |
| 3 | 脚本内重试线程 | 开机早期若一次失败，后台每 10s 重试最长 10 分钟 |

v4 脚本特性：毫秒级防重复（marker）、广告列表用本地缓存且仅超 48h 才联网刷新、全部动作写 syslog（`grep auto_ssh /tmp/messages`）。

插件恢复顺序（重置后）：解锁 → 跑一键部署 → 从 `router/configs/` 恢复各文件到 `/data/` → `auto_ssh.sh install`。

## 常见问题

- **SSH 又突然没了？** → 按 [docs/自救手册.md](docs/自救手册.md) 分步走（远程四连 curl 即可复活，不用重置）。
- **IP 老漂移、每次失联？** → 在上级路由 DHCP 里把路由器 MAC 绑定静态地址，一劳永逸。
- **中继模式下设备没去广告效果？** → 需在上级路由把 DHCP 下发的 DNS 指向本机 IP。
- **为什么你们固件版本不能高？** → 新固件会封堵注入路径并清掉自愈；锁死 1.0.24 是底线。

## 版本记录

- 2026-08-28（v2.3.1，就地更新）：去广告换源——停更的 yhosts（上游 2025-03 归档，实测 90% 与 anti-AD 重合、仅 645 条独有）换成 AWAvenue 国内补充列表；`auto_ssh.sh` 升 v5，新增开机按 48h 年龄自动刷新 + 每日 04:30 定时刷新 + 双镜像回退，下载不达标绝不覆盖在用缓存；面板去广告卡片显示两个列表条数与新鲜度，「立即更新列表」一次刷两条。
- 2026-08-28（v2.3）：安全加固（POST Origin 校验 + Host 防 rebinding）；去广告开关真实联动 anti-AD 且可持久关闭；备份补全 /data；部署按模式选面板、CRLF 根治（.gitattributes）。
- 2026-08-28（v2.2）：面板健壮性修复——信道参数补校验（堵命令注入）、去广告下载失败不再覆盖好缓存、配置中心 20+ 次 SSH 往返合并为 1 次、屏蔽/上游删除改安全写回、网络测试跨平台、命中率统计去掉固定等待。
- 2026-08-28（v2.1）：面板降载（采集 3s 间隔+开销可视）、米家云服务一键精简（重启自动恢复）、DNS 缓存命中率。
- 2026-08-28：面板 v2（详见 Release）：配置真备份/下载、LED 手动+定时、负载与 IPv6 拦截状态、界面改版；凭据脱敏；`requirements.txt` 锁 `paramiko<4`。
- 2026-08-27：v4 自愈脚本（防重复下载+缓存刷新+重试兜底）；冷启动演练通过；发现并绕开 arn_switch 假接口问题。

## 致谢 Acknowledgements

本项目站在社区成果之上，感谢：

- [lemoeo/AX6S](https://github.com/lemoeo/AX6S) —— `auto_ssh.sh` 的原型与「firewall.include 固化」思路来源；本仓库 `router/auto_ssh.sh` 在其基础上重写（毫秒级防重复触发、去广告列表本地缓存+48h 刷新、后台重试兜底）。
- [kjqq/AX3000E](https://github.com/kjqq/AX3000E) —— 《Xiaomi 小米路由器 AX3000E 科学上网教程》，本机型上较早的公开教程，机型侧的若干坑是先从它拿到参考的。
- 社区公开教程（AX3000E / AX3000T 解锁流程的分享者们）—— `xqsystem/start_binding` key 参数注入方法来自相关文章，并非本项目首创。
- [TG-Twilight/AWAvenue-Ads-Rule](https://github.com/TG-Twilight/AWAvenue-Ads-Rule) —— 国内 App 广告/跟踪域名列表，本套件的去广告补充列表来源（原生 dnsmasq 格式，持续更新）。
- [juewuy/ShellCrash](https://github.com/juewuy/ShellCrash) —— 官方固件上运行 Clash 的流行方案，常与本套件配合使用。

License: MIT

---
name: ax3000e-router-maintainer
description: 维护小米 AX3000E（RN07，固件 1.0.24）AP/中继模式路由器的操作技能——SSH 连接与自救、三层自愈部署、DNS 去广告列表管理、监控面板运维、crontab 灾后恢复。接手该设备维护、排查"SSH 突然失效/面板连不上/列表不更新"时使用。
---

# AX3000E 路由器维护 Skill

配套仓库：https://github.com/ltcdz5/ax3000e-unlock-kit （代码、配置副本、自救手册以此为准）
配套交接文档：仓库 `docs/` 或机主桌面 `路由器维护交接-20260828.md`（含全部文件位置清单）

## 铁律（违反必出事故）

1. **禁止升级固件**。锁 1.0.24，升级=SSH 解锁与自愈全部报废。
2. **任何写路由器文件的操作，先 `df /data`**。`/etc/crontabs` 与 `/data` 同在一个仅 1.7MB 的 UBIFS 卷；卷满时截断命令成功但写入静默失败，会把 crontab 等重要文件变成 0 字节（命令还返回成功，极具迷惑性）。
3. **SSH 库 paramiko 必须 <4**（5.0 移除 ssh-rsa，本机 dropbear 2017.75 只有 ssh-rsa）。老 dropbear 不认 rsa-sha2 主机密钥协商，连接需带：
   `disabled_algorithms={'keys': ['rsa-sha2-256', 'rsa-sha2-512']}`（仓库面板脚本已内置）。
4. 不要试图用 SFTP 传文件——该 dropbear 没编译 sftp-server。走 SSH exec + `cat`/base64。
5. SSH 凭据（root 密码）只问机主，禁止写入仓库/文档/日志。

## 设备速查

| 项 | 值 |
|---|---|
| 型号/固件 | AX3000E (RN07) / 1.0.24，OpenWrt 系 + 小米栈 |
| 角色 | AP 中继，上级 K2P `192.168.2.1`；本机只管 WiFi + dnsmasq 去广告 |
| IP | `192.168.2.106`（会漂移！失联先按 MAC `<ROUTER-MAC>` 扫） |
| SSH | root@:22，密码问机主 |
| 面板 | PC 双击 `路由器面板.bat` → http://127.0.0.1:8787 |
| 内存/存储 | 186MB RAM / /data 仅 1.7MB UBIFS |

## 任务：SSH 突然连不上

1. 先判定：`ping 192.168.2.106` 不通 → IP 漂了，按 MAC 找新 IP（自救手册 §按MAC找IP）。
2. IP 对但 22 端口不通 → 看 80 端口（管理页）是否活着；活着 = dropbear 没起。
3. 恢复：`/data/auto_ssh/auto_ssh.sh` 三层自愈（firewall 钩子 / cron 每分钟 / 脚本内 10 分钟重试）通常几分钟内自己拉起来。等不到就按 `docs/自救手册.md` 用管理页 stok 走 `xqsystem/start_binding` 注入四连 curl 复活。**注意：网传 `arn_switch` 接口在本固件是假的（返回 code:0 但不执行）。**
4. 复活后检查 `/etc/crontabs/root` 是否还有 `* * * * * /bin/sh /data/auto_ssh/auto_ssh.sh` 行（固件重建 crontab 会吃掉它）。

## 任务：恢复被清空的 crontab

**先 `df /data` 腾出空间，再写！** 然后：
- 用仓库/机主桌面 `restore_crontab2.py`（base64 单命令注入 + 行数验证），或
- 参照其中 15 行权威内容手工恢复（含 4 条自定义行：ip6tables 拦截、dnsquery 截断、led 开关灯、auto_ssh 兜底）。

## 任务：管理 DNS 去广告

- 当前双列表：anti-AD（`96-antiad.conf`，主）+ yhosts（已死源，正迁 AWAvenue——接手先完成交接文档 §6）。
- 面板"更新列表"按钮 = 拉 anti-AD 最新 + 体积/条数双门槛校验，失败不覆盖在用缓存。
- 查询日志：`/tmp/dnsquery.log`（dnsmasq `log-queries` + `log-facility` 指定文件；注意 USR1 统计也转储到此文件而非 syslog）。
- 命中率：`kill -USR1 $(pidof dnsmasq)` 后 `grep 'queries forwarded' /tmp/dnsquery.log | tail -n1`。

## 任务：改配置后的持久化检查

`/tmp` 里的 dnsmasq.d 配置重启会丢，必须保证：
1. `/data/` 有同名持久副本；
2. `/data/auto_ssh/auto_ssh.sh` 的恢复块里有对应 `cp /data/xxx /tmp/dnsmasq.d/xxx` 行（新加配置文件时**必须同时改这里**，改完 `sh -n` 验语法并实际触发一次恢复路径）。

## 任务：面板开发/改参

- 代码在仓库 `panel/`（router_monitor_ap.py 入口 + monitor_web.py 页面层）；桌面同名 .py 是过时遗留，别改。
- 参数走环境变量：`ROUTER_HOST` / `ROUTER_PASSWD` / `ROUTER_USER` / `ROUTER_SSH_PORT`。
- 改完跑 `python -m py_compile` + 实际启动访问 `/api`；提 PR 前注意 `requirements.txt` 的 paramiko<4 不许动。
- 采集节奏：默认 3s 一次、单条合并 SSH 命令（别拆成多次往返，机器只有 2 核）。

## 已确认结论（别重复踩）

- "noipv6.conf" 实际是抖音域名 IPv6 置空，**不是**全局禁 IPv6；全局拦截是 crontab 里 ip6tables 那行（保护去广告不被 IPv6 DNS 绕过，保留）。
- 抖音定向 DNS（bytedance.conf）未被 dnsmasq 加载、机主确认无用、不修。
- 面板服务精简停的 3 个米家云服务的都是临时停（重启自恢复），机主接受。
- 路由器负载 ~2.0 但 CPU 大量空闲：非 CPU 瓶颈，元凶疑为每分钟级 cron 密集任务，未深查。

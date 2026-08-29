# CHANGELOG

> 版本历史（与 GitHub Releases 同步；离线查阅用）。最新在上。
> 发版惯例：大改升版本号；小修原地更新最新 release（CI 自动重建资产）。

## v2.0.0 · 完整形态：双面板 + 去广告体系 + 三层自愈 + 新手三件套

> Tag: `v2.0.0` · 2026-08-30

### 解锁与自愈

- SSH 解锁：`xqsystem/start_binding` 注入（实测结论：已解锁设备重跑返回 1541 不执行，注入链仅首次解锁有效；网传 `arn_switch` 在本机是假接口）
- `auto_ssh.sh` 三层自愈（v6）：firewall 钩子 + 每分钟 cron 兜底 + 脚本内重试；开机恢复去广告列表/查询日志/自定义屏蔽/IPv6 拦截
- `/data` 卷满防护（1.7MB UBIFS）：`/tmp 暂存→删旧→写新` 落盘、写后回读校验、写前先 `df`——根治"卷满静默清空 crontab"事故

### DNS / 去广告体系

- 双列表：anti-AD（10 万条）+ AWAvenue（883 条），开机 48h 年龄刷新 + 每日 04:30 定时 + jsDelivr/raw 双镜像回退 + 体积/行数门槛校验（下载异常不覆盖好缓存）
- 上游池实测收敛为 4 条国内精品（阿里/腾讯/114/百度）；清除被投毒与高延迟源（4.2.2.2、8.8.8.8、字节 180.184.1.1）
- DNS 查询日志持久化 + 面板实时查询记录 + 开关；命中率统计（SIGUSR1 转储，路由器侧轮询等待）
- NXDOMAIN 化（`/#`，与上游格式一致；本机 dnsmasq 2.86 嵌入版对 `/#` 仍回 0.0.0.0，无行为收益，保留仅为格式对齐）
- IPv6 FORWARD 拦截（保护 DNS 去广告）+ 生效状态实测
- 自定义屏蔽域名持久化（/data/custom.conf，重启自动回挂）

### 面板（AP / 主路由双模式）

- 双面板单一服务层 `monitor_web.py`：HTTP Basic + Host 回环校验（防 DNS rebinding）+ Origin/Referer 同源校验（防 CSRF）+ 默认仅 127.0.0.1（`--lan` 需显式令牌）
- 安全：uci 注入全堵（白名单校验×5 处）、存储型 XSS 转义、信道分段校验、恢复白名单（路径穿越/绝对路径拒绝）
- 配置真备份 + 一键恢复（五道防线：白名单精解、恢复前自动备份、/data 净增预检、分块上传回读校验、失败不重启服务）
- 功能面：LED 开关/定时、WiFi 信道功率即时切换、服务精简（米家云服务临时停止）、DNS 缓存/命中率/上游管理、端口转发/QoS/DHCP 绑定（主路由模式）、外网延迟测试
- 性能：采集单连接批处理（配置页 20+ 次往返合并为 1 次）、前端 fetch + toast + 局部刷新、页面热加载

### 新手三件套

- **解锁向导** `tools/unlock_wizard.py`（GUI/CLI）：IP+管理页密码全自动——MiWiFi 登录算法取 stok（sha256 真机验证）→ 注入 → 验 22 口 → 接力部署；1541 被拒有专门分支
- **一键体检** `tools/kit_doctor.py`：零凭据局域网指纹扫描发现路由器；固件/SSH/自愈/去广告/容量/云服务/弱密码/面板逐项检查；**固件自动升级自动检测 + `--fix` 自动关闭**（`otapred.settings.auto`）；**去广告 DNS 链路静默失效检测**；启动器 IP 自动修复；主路由模式功能分支；结尾汇总手动待办
- **实机校准探针** `deploy/device_probe.py`：只读单连接输出设备画像，新设备适配入口
- 设备识别解耦模块 `panel/device_profile.py`：机型能力表（`nvram get model`，RN07=AX3000E）+ 纯解析 + sh 注入；未验证机型功能键不盲开

### 工程与可信度

- GitHub Actions 双流水线：`ci.yml`（py_compile + sh -n + pytest 26 项）+ `release-latest.yml`（push 到 main 自动重建 zip+SHA256SUMS 覆盖最新 release）
- README（适配设备表 ≤1.0.24、"不是刷机项目"边界、双模式平权、AI 辅助声明、徽章）、Issue 模板、`panel/README.md`、`docs/自救手册.md`
- `SHA256SUMS.txt` 资产；zip 经 `git archive` 可逐字节复现；仓库更名 `xiaomi-router-unlock-kit`（旧地址重定向）+ 14 topics
- 凭据脱敏：仓库不含任何真实凭据，密码经环境变量/运行时输入
- 依赖：paramiko `<5,>=3`（4.x 真机全验证；5.0 删 ssh-rsa 无法连老 dropbear）

### 已知边界

- 固件升级 = 解锁报废（仅适用官方固件 ≤1.0.24）；体检已可自动检测/关闭自动升级
- WiFi 信道/功率为即时切换（重启丢失，官方框架覆盖）；QCA 闭源驱动无后台扫描开关
- 面板依赖一台常开 PC；中继模式去广告只能走 DNS 层（阻断点须在网关）
- 主面板查 AP 模式路由器取数慢（模式不匹配，属预期）

## v1.0.0 · AX3000E 中继模式全套（SSH自愈 v4）

> Tag: `v1.0.0` · 2026-08-27

## 小米 AX3000E 中继/AP 模式留存套件

**内含**：SSH 解锁与固化流程、v4 自愈脚本、双版监控面板、一键部署工具、去广告/DNS 配置副本、《自救手册》。

### 附件说明
`auto_ssh.sh` —— 放到路由器 `/data/auto_ssh/` 后执行：

```sh
chmod +x /data/auto_ssh/auto_ssh.sh
/bin/sh /data/auto_ssh/auto_ssh.sh install
```

其余文件按需从本页 Source code (zip) 下载。

### 特性
- 三层自愈：firewall.include + cron 兜底 + 脚本内重试（冷启动实测通过）
- 去广告列表本地缓存 + 超48小时才联网刷新
- 仅适用于官方固件 **1.0.24**；升级固件会失效。详见 README 与《自救手册》。

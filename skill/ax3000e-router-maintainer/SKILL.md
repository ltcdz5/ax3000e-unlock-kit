---
name: ax3000e-router-maintainer
description: 维护小米路由器解锁套件（xiaomi-router-unlock-kit，原 ax3000e-unlock-kit）的操作技能——AX3000E（RN07，固件 1.0.24）SSH 连接与自救、三层自愈、DNS/去广告体系、双面板架构（v3.1 共享服务层）、发版与同步流程；含 BE3600 预适配与小米 Mesh 预设计文档。接手设备维护、排查"SSH 失效/面板异常/DNS 问题"或推进新设备适配时使用。
---

# AX3000E 路由器维护 Skill（2026-08-29 00:4x 更新至 v3.1.0 后状态）

配套仓库：https://github.com/ltcdz5/xiaomi-router-unlock-kit（代码权威来源，本地克隆 `C:\Users\xutengfa\ax3000e-unlock-kit`）
配套文档：仓库 `docs/自救手册.md`；桌面 `路由器聊天记录-完整导出-20260828.md`（历史）；桌面 `面板交接-剩余任务-20260828.md`（已基本清完，可当验证清单）
预适配文档（仓库 `docs/`）：`BE3600-解锁与适配指南.md`（解锁方法 A/B/C + 套件移植清单，待实机）、`Mesh拓扑适配笔记.md`（去广告挂主节点、子节点分流自愈，预设计）
备份机：BE3600 2.5G 另有一套独立存档 `Desktop\路由器配置存档-20260827\BE3600-降级开SSH方法存档-20260829.md`，与本项目无关，勿混。

## 铁律（违反必出事故）

1. **禁止升级固件**，锁 1.0.24。升级=解锁与自愈全部报废。
2. **写路由器任何东西前先 `df /data`**。/etc/crontabs 与 /data 同在一个 1.7MB UBIFS 卷，卷满时截断"成功"但写入静默失败（crontab 曾被清空事故根因）。写文件用 base64 单命令注入或带字节回读校验的 sh_write（范本：`restore_crontab*.py`、面板 `sh_write`）。大文件（anti-AD ~780K）禁止 .new 双份落盘——先 /tmp 暂存、删旧、cat 写新。
3. **paramiko 锁 <4**（5.x 删 ssh-rsa，本机 dropbear 2017.75 只认它），连接必须带 `disabled_algorithms={'keys': ['rsa-sha2-256','rsa-sha2-512']}`。
4. **无 SFTP**——文件传输走 SSH exec + base64/tr。
5. **dropbear 有连接限流**：短时间内连续新建 SSH 连接会被拒 banner（"Error reading SSH protocol banner"）。脚本验证一律复用单连接（可 monkeypatch 面板模块的 `sh` 到已建连接）；操作间隔 ≥20-30s；被限流等 60-90s 自愈。
6. **对路由器的"成功"要自校验**：sh() 失败静默返回空串，曾导致 UI 谎报"已开启"而实际什么都没执行。写类动作必须回读断言（如 log_toggle 的 `test -f ... || echo DONE` 模式）。
7. **仓库增删只按机主明确指令**（机主令，2026-08-29）。公开仓库零凭据：推送前扫密码/stok/MAC/个人 IP。
8. **改面板 .py 必须重启面板进程**；HTML 模板由共享层每请求读盘（热加载，改完即生效）。
9. 测试加载器须先 `sys.modules["monitor_web"] = <真实模块>` 再 importlib 加载面板，避免双实例。

## 设备速查

| 项 | 值 |
|---|---|
| 型号/固件 | AX3000E (RN07) / 1.0.24，OpenWrt 系（dnsmasq 2.86 嵌入版） |
| 角色 | AP 中继，上级 K2P 192.168.2.1；本机只管 WiFi + DNS 去广告 |
| IP | 192.168.2.106（**会漂**，按 MAC `58-ea-1f-ca-0c-b4` 找；K2P 绑静态 IP 是待办） |
| SSH | root@:22，密码问机主 |
| 面板 | 桌面 `路由器面板.bat` → http://127.0.0.1:8787（AP 版）；主路由版 8788 |
| 内存/存储 | 186MB RAM / /data 1.7MB UBIFS（当前 81%） |
| 依赖 | Python 3.12 + paramiko 3.5.x；Git Bash 环境；**push 需梯子**（端口会漂，读注册表 ProxyServer 或探测 7890/7892） |

## 架构现状（v3.1.0）

- `panel/monitor_web.py` = **双面板唯一 HTTP 服务层**：认证（HTTP Basic + hmac）、Host 回环校验、Origin 同源校验、路由。纯函数 `host_ok/origin_ok/auth_ok/escape_inline_json/parse_act_body` 可单测。
- `panel/device_profile.py` = **设备识别解耦模块**：机型能力表（key = `nvram get model`，AX3000E=RN07）+ 纯解析函数；面板注入自己的 sh 采集。新机型只加表一行，不改面板；未验证机型功能键不得盲开。配套只读探针 `deploy/device_probe.py`（新设备实机校准用）。
- AP 面板：`router_monitor_ap.py`（业务）+ `monitor_page.html`（前端，fetch+toast+refreshCfg 模式）。
- 主面板：`router_monitor_ax3000e.py`——已删自有 Handler/认证（v3.0），`_page()` 服务端**零 SSH**（GET / 1.8ms），前端 JS 全 fetch。**不要复活** form/302/?msg= 与 `render_config_html`（已删除）。
- `tests/test_security.py` 21 项本地单测（不连路由器）；CI sanity = py_compile + sh -n + pytest。
- **发版流程**：改码 → 本地 pytest → commit → **机主审核通过** → push → 重建 zip + release（附 zip 与 auto_ssh.sh 资产）→ 三步同步（见下）。

## DNS 体系现状与结论

- 上游四条国内：**223.5.5.5(阿里) / 119.29.29.29(腾讯) / 114.114.114.114 / 180.76.76.76(百度)**。
  - 4.2.2.2/8.8.8.8/9.9.9.9 已清除：实测 4.2.2.2 被投毒（google 14ms 假应答 31.13.x），且 WU 域名解析到美国边缘（443 连接 196-1169ms vs 国内 9-29ms）。
  - 字节 180.184.1.1 已清除：两轮实测多秒级停顿（tlu.dl 3.4-4.2s×3）。**字节自家 DNS 对自家域名无优势**（抖音走 HTTPDNS，公共解析器拿到的 CDN 边缘一样好）。
- 拦截：anti-AD(~10 万) + AWAvenue(883) + custom，全部 `address=/x/#` 格式。**实测本机 dnsmasq 2.86 对 `/#` 仍返回 0.0.0.0/::**（与主流文档的 NXDOMAIN 语义不同，嵌入式构建差异）——别指望 NXDOMAIN 降重试，也别重复尝试这条优化。
- **查询日志可开关**：`log_toggle` action（写/删 `/data/logqueries.conf` + 93 conf，带执行后自校验）。查询记录走 `/tmp/dnsquery.log`（tmpfs），命中统计 `kill -USR1 $(pidof dnsmasq)` 后读该日志尾部。
- 抖音系域名在 ad_skip 豁免正则里（防去广告误伤取源），**必须保留**。
- 命中率 40-60% 属正常（大量唯一短 TTL 域名）；`min-cache-ttl` 已评估未采用（收益太小）。

## 任务：SSH 突然连不上

1. `ping` 不通 → IP 漂了，按 MAC 找；`/api` 80 活着 22 不通 → dropbear 没起。
2. 三层自愈（firewall 钩子 / cron 每分钟 auto_ssh 行 / 脚本内 10 分钟重试）通常自己拉起；等不到按 `docs/自救手册.md` 用管理页 stok 走 `xqsystem/start_binding` 注入四连 curl。**arn_switch 是假接口（code:0 但不执行）。**
3. 复活后检查 `/etc/crontabs/root` 仍有 auto_ssh 行（固件会重建 crontab）。

## 任务：改 DNS/去广告配置

1. 先 `df /data`；持久副本在 `/data/`，运行态在 `/tmp/dnsmasq.d/`，改完 `dnsmasq restart`。
2. anti-AD/AWAvenue 刷新走面板动作（体积/行数双门槛，不达标不覆盖缓存）；**列表已是 `/#` 格式，别再"优化"成 NXDOMAIN**（见上）。
3. 新增配置文件必须同时改 `auto_ssh.sh` 的恢复块 + `/data/` 持久副本，`sh -n` 验语法并实测触发一次。

## 任务：面板开发

- 改 .py → 必须重启面板进程；改 .html → 热生效。
- 改完：`py_compile` → `pytest tests -q` → 重启 → 浏览器实测。
- 已知限制：主面板查 AP 模式路由器取数慢（模式不匹配，GET / 曾 187-300s；现已服务端零 SSH，但 get_config 经 /api/config 仍慢）——主路由功能请在主路由模式下用。
- 长耗时按钮（更新列表/测速）应有 disabled+"更新中…"态（net_test 已有，其他未对齐——待办）。

## 任务：发版

1. commit → push（梯子！端口读注册表 ProxyServer）→ 机主已批准才 `gh release create vX.Y.Z`（zip 用 `git archive` 从 HEAD 构建 + auto_ssh.sh 单件资产）。
2. **发版三步同步**：① `tr -d '\r' < repo/router/auto_ssh.sh | md5sum` vs 路由器 live md5（**live 路径 = `/data/auto_ssh/auto_ssh.sh`**，不是 `/data/auto_ssh.sh`），不一致先备份 live（.bak_vX）再上传+`sh -n`+install；② 面板 .py 变了重启进程；③ 复核 8787 存活 / df /data / 广告域名劫持。
3. 版本惯例：大改主版本号，小修就地编辑最新 release 说明；zip 资产用 `--clobber` 跟随 HEAD（资产名随仓库更名：`xiaomi-router-unlock-kit-vX.Y.Z.zip`，旧的 `ax3000e-unlock-kit-*.zip` 在下次发版时替换）。
4. 发版必做三件可信度事：① 改 `panel/monitor_web.py` 的 `KIT_VERSION`（页脚显示，有单测门禁）；② 同步 `CHANGELOG.md`（从 release notes 汇编，离线可查）；③ 上传 `SHA256SUMS.txt` 资产（zip 用 `git archive` 从 tag 重建可逐字节复现，已实测验证）。

## 已确认结论（别重复踩/别重复试）

- arn_switch 假接口；start_binding 真注入点；/etc 是 ramfs，重启清零，全靠三层自愈。
- /data/etc（~920K）是 /etc/config、crontabs 的**活体 bind-mount 载体，不可清理**（曾被误判为备份堆积）。
- K2P 的 DNS 已指向本机（全屋去广告生效）；ipset 阻断在中继模式不适用（阻断点在上游网关）；WiFi 后台扫描无公开开关（QCA 闭源）。
- 抖音系 DNS 定向（95-bytedance.conf）**已废弃且复活载体全部删除（机主令 2026-08-29，永不再启）**：字节自家 DNS 对自家域名无优势 + 180.184.1.1 有多秒停顿。
- NXDOMAIN（/#）在本机 dnsmasq 上无行为收益（仍回 0.0.0.0）。
- 字节/4.2.2.2 的多秒停顿与投毒数据在 `maintenance-scripts/rdns_*.ps1` 的历史输出与 commit 信息里。
- 桌面遗留 `router_monitor*.py` 已归档至 `Desktop\归档\`；权威代码只有仓库 panel/。

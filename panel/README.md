# panel/ 目录说明

本地 Web 面板（持有路由器 root SSH 凭据，跑在你的电脑上；路由器侧零常驻负载）。
先跑 `tools/一键体检.bat` 确认路由器状态，再按模式启动对应面板。

## 选哪个

| 你的路由器模式 | 启动器 | 端口 | 功能 |
|---|---|---|---|
| 中继 / AP（接上级路由） | `Start-AP-panel.bat` | 8787 | WiFi + DNS 去广告 + 监控 + 备份恢复 |
| 主路由（自己拨号） | `Start-MainRouter-panel.bat` | 8788 | 另含端口转发 / QoS / DHCP 静态绑定 / 防火墙 / UPnP |

启动器会问路由器 IP 和 SSH 密码（隐藏输入），首次自动安装 paramiko（需 <5，5.0 删了 ssh-rsa）。
IP 忘了？跑 `tools/kit_doctor.py` 自动发现。

## 文件清单

| 文件 | 作用 | 能单独跑吗 |
|---|---|---|
| `router_monitor_ap.py` | AP 面板业务逻辑 | ✅（启动器就是跑它） |
| `router_monitor_ax3000e.py` | 主路由面板业务逻辑 | ✅（`--web 8788`） |
| `monitor_web.py` | 双面板共享 HTTP 服务层：认证/Host/Origin 校验/路由 | ❌ 库文件，被上面两个 import |
| `monitor_page.html` | AP 面板前端模板 | ❌ 改它即热生效，无需重启面板 |
| `Start-*.bat` | 一键启动器（装依赖+问凭据+开浏览器） | ✅ 双击 |
| `backups/` | 面板"配置备份"的本地存档（.gitignore，不入库） | — |

## 手动启动（不用启动器）

```powershell
set ROUTER_HOST=192.168.2.106      # 路由器 IP
set ROUTER_PASSWD=你的SSH密码
python router_monitor_ap.py        # 或 router_monitor_ax3000e.py --web 8788
```

可选参数：`--lan --token 口令` 绑定全网卡并启用 HTTP Basic 认证（默认仅 127.0.0.1 免认证）。

## 安全须知

- 默认只监听 127.0.0.1，电脑之外谁也访问不到；`--lan` 暴露到局域网时**必须**配 token
- 面板进程持有 root 密码：关窗口即停服务，别在公共电脑上常驻
- 改 `.py` 需重启面板生效；改 `.html` 刷新页面即生效

更多见根目录 README 与 `docs/自救手册.md`。

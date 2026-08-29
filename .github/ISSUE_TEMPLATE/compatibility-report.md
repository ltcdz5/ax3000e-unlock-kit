name: 兼容性反馈（新设备/新固件实测）
description: 你在其它小米路由器或固件版本上跑了本套件？告诉我们结果，帮兼容表成长
title: "[兼容性] <机型> <固件版本> <成功/部分成功/失败>"
labels: ["compatibility"]
---

> 一条有效反馈 = 设备画像 + 现象。先跑只读探针：`python deploy/device_probe.py <路由器IP>`，把生成的 `device_profile_*.txt` 贴上。

## 设备信息

- 机型（如 BE3600 2.5G / AX3000T）：
- `nvram get model` 输出（识别源，探针报告里有）：
- 固件版本：
- 运行模式：主路由 / AP 中继 / Mesh 主节点 / Mesh 子节点
- SSH 解锁方式：start_binding 注入 / xmir-patcher / 其它

## 哪个组件

- [ ] SSH 解锁
- [ ] auto_ssh 三层自愈
- [ ] 一键部署（deploy/）
- [ ] 面板（AP / 主路由）
- [ ] DNS/去广告

## 结果描述

<!-- 成功：哪些功能直接可用。失败：报错原文 + 探针报告 -->

```
（粘贴探针报告或日志）
```

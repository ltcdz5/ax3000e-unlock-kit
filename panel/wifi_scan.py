# -*- coding: utf-8 -*-
"""WiFi 信道扫描分析（纯解析 + sh 注入，与面板/SSH 解耦）。

- 从 `iw dev` 挑出真的在跑 AP 的接口（有 ssid + type AP），按 freq 归到 2g/5g；
- 只扫活跃 band——2.4G 关了自然拿不到数据，返回 None，UI 显示"未启用"；
- 按候选信道累加邻居 AP 的功率（dBm→mW），越低越干净，取第一个作推荐；
- 只推荐、不自动切；用户点"应用"复用现成 `wifi_channel` 动作。

QCA 闭源驱动对 down 的射频 `iw scan` 会静默返空——这不是 bug，正好让我们跳过。
"""
import re

CANDIDATES = {
    "2g": [1, 6, 11],
    "5g": [36, 40, 44, 48, 149, 153, 157, 161],
}

_FREQ_RE = re.compile(r"^\s*freq:\s*(\d+)", re.M)
_SIG_RE = re.compile(r"^\s*signal:\s*(-?[\d.]+)\s*dBm", re.M)


def freq_to_channel(freq):
    f = int(freq)
    if 2412 <= f <= 2472:
        return (f - 2407) // 5
    if f == 2484:
        return 14
    if 5170 <= f <= 5350 or 5470 <= f <= 5900:
        return (f - 5000) // 5
    return None


def band_of_freq(freq):
    f = int(freq)
    return "2g" if 2400 <= f < 2500 else ("5g" if 5150 <= f < 5950 else None)


def _sig_factor(dbm):
    """把 RSSI 归一化到 0..1：-92dBm(几乎不可闻)=0，-32dBm(贴脸)=1。"""
    return max(0.0, min(1.0, (dbm + 92.0) / 60.0))


def parse_iw_dev(text):
    """返回 [{"iface","band","freq","ssid"}]，只保留真的在跑 AP 的接口。"""
    out, cur = [], None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("Interface "):
            if cur and cur.get("ssid") and cur.get("band"):
                out.append(cur)
            cur = {"iface": s.split(None, 1)[1].split()[0], "ssid": None,
                   "freq": None, "band": None}
            continue
        if cur is None:
            continue
        if s.startswith("ssid ") and not cur["ssid"]:
            cur["ssid"] = s[5:]
        elif s.startswith("channel ") and cur["freq"] is None:
            m = re.search(r"\((\d+)\s*MHz\)", s)
            if m:
                cur["freq"] = int(m.group(1))
                cur["band"] = band_of_freq(cur["freq"])
    if cur and cur.get("ssid") and cur.get("band"):
        out.append(cur)
    return out


def parse_scan_neighbors(text):
    """从 `iw dev X scan` 原文抽 [(channel, signal_dbm)]，忽略解析不了的 BSS。"""
    blocks = re.split(r"^BSS\s[0-9a-f:]+", text or "", flags=re.M)
    out = []
    for b in blocks[1:]:
        fm = _FREQ_RE.search(b)
        if not fm:
            continue
        ch = freq_to_channel(fm.group(1))
        if ch is None:
            continue
        sm = _SIG_RE.search(b)
        out.append((ch, float(sm.group(1)) if sm else -95.0))
    return out


def rank(band, neighbors, current_channel=None):
    """候选信道按干扰功率（mW）打分，越低越好；2.4G 邻域 ±4 部分重叠按 0.4 权重。"""
    cands = CANDIDATES.get(band)
    if not cands:
        return None
    scored = []
    for c in cands:
        s, aps = 0.0, 0
        for ch, sig in neighbors:
            if ch == c:
                wgt = 1.0
                aps += 1
            elif band == "2g" and abs(ch - c) <= 4:
                wgt = 0.4
            else:
                continue
            s += wgt * _sig_factor(sig)
        scored.append({"channel": c, "score": round(s, 3), "same_ch": aps})
    scored.sort(key=lambda x: (x["score"], x["channel"]))
    if not scored:
        return None
    best = scored[0]["score"]
    # 当前信道已并列最优 → 保持不动，避免无谓切换
    cur = next((x for x in scored if x["channel"] == current_channel), None)
    if cur and cur["score"] <= best:
        rec = current_channel
    else:
        tied = [x["channel"] for x in scored if x["score"] == best]
        if current_channel is not None:
            rec = min(tied, key=lambda c: (abs(c - current_channel), c))
        else:
            rec = min(tied)
    return {
        "candidates": scored,
        "recommended": rec,
        "current": current_channel,
        "better": rec is not None and current_channel is not None and rec != current_channel,
        "neighbor_count": len(neighbors),
    }


def collect(sh, scan_timeout=25):
    """sh: 面板注入的命令执行器。返回 {"2g":rank|None, "5g":rank|None, "iface":{band:name}}。"""
    ifaces = parse_iw_dev(sh("iw dev 2>/dev/null", timeout=10))
    out, ifmap = {}, {}
    for it in ifaces:
        band = it["band"]
        ifmap.setdefault(band, it["iface"])
        if band in out:
            continue
        scan = sh("iw dev %s scan 2>/dev/null" % it["iface"], timeout=scan_timeout) or ""
        out[band] = rank(band, parse_scan_neighbors(scan), freq_to_channel(it["freq"]))
    for band in CANDIDATES:
        out.setdefault(band, None)
    out["iface"] = ifmap
    return out


def summary(res):
    """给 toast 用的一行摘要。"""
    parts = []
    for band, label in (("5g", "5G"), ("2g", "2.4G")):
        r = res.get(band)
        if not r:
            continue
        if r.get("better"):
            parts.append("%s 当前 ch%s，建议 ch%s（%d 邻居）" % (label, r["current"], r["recommended"], r["neighbor_count"]))
        else:
            parts.append("%s 当前 ch%s 已是最优（%d 邻居）" % (label, r["current"], r["neighbor_count"]))
    return " · ".join(parts) or "未扫到可用射频（对应 WiFi 可能未启用）"

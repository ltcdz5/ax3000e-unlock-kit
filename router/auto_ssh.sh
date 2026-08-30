#!/bin/sh
# auto_ssh.sh v6 (2026-08-28) — 轻量化自愈：解锁 SSH + 每次开机只构建一次 DNS 插件
# v6: anti-AD 列表 NXDOMAIN 化（0.0.0.0/空地址 → /#，客户端直接放弃不重试，降低查询量）
# 由 firewall include 触发（每次防火墙重载都会跑），所以必须毫秒级返回。
# v5: 去广告改为 anti-AD(主) + AWAvenue(国内补充) 双列表，缓存超 48h 自动联网刷新，
#     并补一条每日 refresh 定时任务；下载不达标绝不覆盖在用的好列表。

auto_ssh_dir="/data/auto_ssh"
host_key="/etc/dropbear/dropbear_rsa_host_key"
host_key_bk="${auto_ssh_dir}/dropbear_rsa_host_key"
marker="/tmp/.dns_ready"

antiad_url="https://anti-ad.net/anti-ad-for-dnsmasq.conf"
awavenue_urls="https://cdn.jsdelivr.net/gh/TG-Twilight/AWAvenue-Ads-Rule@main/Filters/AWAvenue-Ads-Rule-Dnsmasq.conf https://raw.githubusercontent.com/TG-Twilight/AWAvenue-Ads-Rule/main/Filters/AWAvenue-Ads-Rule-Dnsmasq.conf"
# 抖音系一律豁免：本机实测屏蔽这些域名会让抖音视频取源掉进黑洞回退
ad_skip="byteimg|pstatp|douyinpic|douyin|bytecdn|bytedance"
# 游戏/常用域名白名单（规则瘦身）
ad_keep="steam|epicgames|battle.*net|blizzard|origin|riotgames|microsoft|windowsupdate|doubleclick|googlead|google-analytics|googleapis|gstatic|facebook|twitter|amazon|ad.*baidu|qq.*com|taobao|tmall|alibaba|xiaomi|bilibili|youku|iqiyi|163.*com|sina|sohu|jd.*com|meituan|adservice|adnxs|adsrv|openx|rubicon|pubmatic|appnexus|outbrain|taboola"
# 游戏专用（只保留游戏平台/硬件厂商）
ad_keep_game="steam|epicgames|battle.*net|blizzard|origin|riotgames|ubi.*|minecraft|nvidia.*geforce|microsoft|windowsupdate|doubleclick|googlead|adservice"
# 去广告模式（full=全量 light=精简 game=游戏专用），通过 /data/.adblock_mode 持久化
adblock_mode="light"
[ -f /data/.adblock_mode ] && adblock_mode=$(cat /data/.adblock_mode)

stale() {  # 缓存缺失或超过 48 小时 → 需要联网刷新
    [ -s "$1" ] || return 0
    [ -n "$(find "$1" -mmin +2880 2>/dev/null)" ]
}

wait_net() {
    w=0
    while [ $w -lt 12 ]; do
        ping -c 1 -W 2 223.5.5.5 >/dev/null 2>&1 && return 0
        w=$((w+1)); sleep 5
    done
    return 1
}

ensure() {
    [ -f $host_key_bk ] && ln -sf $host_key_bk $host_key
    [ "$(nvram get telnet_en)" = 0 ] && nvram set telnet_en=1 && nvram commit
    [ "$(nvram get ssh_en)" = 0 ] && nvram set ssh_en=1 && nvram commit
    [ "$(nvram get uart_en)" = 0 ] && nvram set uart_en=1 && nvram commit
    [ "$(nvram get boot_wait)" = "off" ] && nvram set boot_wait=on && nvram commit
    if [ -z "$(pidof dropbear)" ] || [ -z "$(netstat -ntul 2>/dev/null | grep ':22 ')" ]; then
        logger -t auto_ssh "port22 missing -> starting dropbear"
        sed -i 's/channel=.*/channel="debug"/g' /etc/init.d/dropbear
        /etc/init.d/dropbear restart 2>/dev/null
        /etc/init.d/dropbear enable
    fi
}

port_up() { netstat -ntul 2>/dev/null | grep -q ':22 '; }

unlock() {
    ensure
    # 开机早期若一次没拉起来，后台每10秒重试，最长10分钟（防抖标记避免重复线程）
    if ! port_up && [ ! -f /tmp/.ssh_retrying ]; then
        touch /tmp/.ssh_retrying
        logger -t auto_ssh "retry thread armed"
        (
            i=0
            while [ $i -lt 60 ]; do
                sleep 10
                ensure
                port_up && { logger -t auto_ssh "port22 recovered by retry"; break; }
                i=$((i+1))
            done
            rm -f /tmp/.ssh_retrying
        ) &
    fi
}

refresh_antiad() {
    curl -sL "$antiad_url" -o /tmp/antiad_raw --connect-timeout 15 --max-time 90
    # 根据模式选择过滤规则
    case "$adblock_mode" in
        full) grep -vE "$ad_skip" /tmp/antiad_raw > /tmp/antiad_new 2>/dev/null ;;
        game) grep -vE "$ad_skip" /tmp/antiad_raw | grep -iE "$ad_keep_game" > /tmp/antiad_new 2>/dev/null ;;
        *)    grep -vE "$ad_skip" /tmp/antiad_raw | grep -iE "$ad_keep" > /tmp/antiad_new 2>/dev/null ;;
    esac
    rm -f /tmp/antiad_raw
    # 体积 + 条目数双重门槛：下载被截断或返回错误页时绝不覆盖在用的好列表
    if [ "$(wc -c < /tmp/antiad_new 2>/dev/null)" -le 10000 ] || [ "$(wc -l < /tmp/antiad_new 2>/dev/null)" -le 500 ]; then
        rm -f /tmp/antiad_new
        logger -t auto_ssh "anti-AD download invalid, keep cache"
        return 1
    fi
    mv -f /tmp/antiad_new /tmp/dnsmasq.d/96-antiad.conf
    # NXDOMAIN 化：0.0.0.0/空地址 → /#（客户端直接放弃，不再重试 AAAA/换协议，降低查询量）
    sed -i -E '/^address=\// { s|/0\.0\.0\.0$|/#|; s|/$|/#| }' /tmp/dnsmasq.d/96-antiad.conf
    # /data 仅 1.7MB：禁止 .new 双份落盘（会撑爆卷），先 /tmp 暂存再删旧写新
    gzip -c /tmp/dnsmasq.d/96-antiad.conf > /tmp/antiad_new.gz 2>/dev/null \
        && { rm -f /data/antiad.gz; cat /tmp/antiad_new.gz > /data/antiad.gz; rm -f /tmp/antiad_new.gz; }
    logger -t auto_ssh "anti-AD refreshed: $(wc -l < /tmp/dnsmasq.d/96-antiad.conf)"
}

refresh_awavenue() {
    for u in $awavenue_urls; do
        curl -sL "$u" -o /tmp/awv_raw --connect-timeout 15 --max-time 60
        [ "$(wc -c < /tmp/awv_raw 2>/dev/null)" -gt 12000 ] && break
        rm -f /tmp/awv_raw
    done
    [ -s /tmp/awv_raw ] || { logger -t auto_ssh "AWAvenue download failed, keep cache"; return 1; }
    grep '^address=/' /tmp/awv_raw | grep -vE "$ad_skip" > /tmp/awv_new 2>/dev/null
    rm -f /tmp/awv_raw
    if [ "$(wc -l < /tmp/awv_new 2>/dev/null)" -le 300 ]; then
        rm -f /tmp/awv_new
        logger -t auto_ssh "AWAvenue download invalid, keep cache"
        return 1
    fi
    mv -f /tmp/awv_new /tmp/dnsmasq.d/90-awavenue.conf
    gzip -c /tmp/dnsmasq.d/90-awavenue.conf > /tmp/awavenue_new.gz 2>/dev/null \
        && { rm -f /data/awavenue.gz; cat /tmp/awavenue_new.gz > /data/awavenue.gz; rm -f /tmp/awavenue_new.gz; }
    logger -t auto_ssh "AWAvenue refreshed: $(wc -l < /tmp/dnsmasq.d/90-awavenue.conf)"
}

ensure_refresh_cron() {
    # 固件偶尔重建 crontab，所以每次开机都补一次；已存在则完全不动，避免重启 cron
    grep -q 'auto_ssh.sh refresh' /etc/crontabs/root 2>/dev/null && return 0
    echo '30 4 * * * /bin/sh /data/auto_ssh/auto_ssh.sh refresh' >> /etc/crontabs/root
    /etc/init.d/cron restart 2>/dev/null
    logger -t auto_ssh "daily adblock refresh cron added"
}

apply_dns() {
    ensure_refresh_cron
    # 每次开机执行一次系统调优（重启后恢复默认，所以每次开机都要设）
    echo 32768 > /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null
    echo 2048 > /proc/sys/net/core/netdev_max_backlog 2>/dev/null
    # 本次开机已就绪则直接返回（重载不重复干活）
    if [ -f $marker ] || { [ -s /tmp/dnsmasq.d/96-antiad.conf ] && [ -s /tmp/dnsmasq.d/98-upstream.conf ] && pidof dnsmasq >/dev/null; }; then
        touch $marker; return 0
    fi

    ip6tables -I FORWARD -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null
    for svc in miwifi-roam miwifi-discovery trafficd; do killall $svc 2>/dev/null; done

    # 面板可持久关闭去广告（/data/.adblock_off），关闭时开机不拉起、并清掉运行态
    adblock_on=1
    [ -f /data/.adblock_off ] && adblock_on=0
    # yhosts（/data/adblock.hosts）上游 2025-03 已归档停更，且 90% 与 anti-AD 重合 → 不再加载
    rm -f /tmp/dnsmasq.d/99-adblock.conf
    if [ "$adblock_on" != "1" ]; then
        rm -f /tmp/dnsmasq.d/96-antiad.conf /tmp/dnsmasq.d/90-awavenue.conf
    fi
    uci set dhcp.@dnsmasq[0].allservers=1 2>/dev/null; uci commit dhcp 2>/dev/null
    # 纯统计日志（不含 log-queries，不记录每个查询，只接 SIGUSR1 转储供面板显示命中率）
    echo 'log-facility=/tmp/dnsquery.log' > /tmp/dnsmasq.d/93-stats.conf
    # OTA 固件升级黑名单：DNS 黑洞，阻止小米升级服务器连接
    echo 'address=/otapred.settings.auto/0.0.0.0' > /tmp/dnsmasq.d/99-block-ota.conf
    [ -s /data/noipv6.conf ]   && cp /data/noipv6.conf   /tmp/dnsmasq.d/92-noipv6.conf
    [ -s /data/logqueries.conf ]&& cp /data/logqueries.conf /tmp/dnsmasq.d/93-logqueries.conf
    [ -s /data/microsoft.conf ] && cp /data/microsoft.conf /tmp/dnsmasq.d/91-microsoft.conf
    [ -s /data/noresolv.conf ] && cp /data/noresolv.conf /tmp/dnsmasq.d/94-noresolv.conf
    # 抖音系定向（95-bytedance.conf）已废弃，故意不恢复——防止复活
    [ -s /data/custom.conf ]   && cp /data/custom.conf   /tmp/dnsmasq.d/97-custom.conf
    if [ -s /data/upstreams.conf ]; then cp /data/upstreams.conf /tmp/dnsmasq.d/98-upstream.conf
    else printf 'server=223.5.5.5\nserver=119.29.29.29\nserver=114.114.114.114\nserver=180.76.76.76\n' > /tmp/dnsmasq.d/98-upstream.conf
    fi
    if [ "$adblock_on" = "1" ]; then
        [ -s /data/antiad.gz ]   && zcat /data/antiad.gz   > /tmp/dnsmasq.d/96-antiad.conf   2>/dev/null
        [ -s /data/awavenue.gz ] && zcat /data/awavenue.gz > /tmp/dnsmasq.d/90-awavenue.conf 2>/dev/null
    fi
    /etc/init.d/dnsmasq restart 2>/dev/null

    if [ "$adblock_on" = "1" ] && { stale /data/antiad.gz || stale /data/awavenue.gz; }; then
        (
            wait_net || exit 0
            r=0
            stale /data/antiad.gz   && refresh_antiad   && r=1
            stale /data/awavenue.gz && refresh_awavenue && r=1
            [ "$r" = "1" ] && /etc/init.d/dnsmasq restart 2>/dev/null
        ) &
    fi
    touch $marker
}

install() {
    unlock
    [ -s $host_key_bk ] || {
        i=0
        while [ $i -le 30 ]; do
            if [ -s $host_key ]; then cp -f $host_key $host_key_bk 2>/dev/null; break; fi
            let i++; sleep 1s
        done
    }
    uci set firewall.auto_ssh=include
    uci set firewall.auto_ssh.type='script'
    uci set firewall.auto_ssh.path="${auto_ssh_dir}/auto_ssh.sh"
    uci set firewall.auto_ssh.enabled='1'
    uci commit firewall
    echo OK
}

uninstall() { uci delete firewall.auto_ssh 2>/dev/null; uci commit firewall 2>/dev/null; echo removed; }

main() {
    case "$1" in
    install)   install ;;
    uninstall) uninstall ;;
    refresh)
        # 每日定时刷新：去广告被持久关闭时不动任何东西
        [ -f /data/.adblock_off ] && exit 0
        (
            wait_net || exit 0
            r=0
            refresh_antiad   && r=1
            refresh_awavenue && r=1
            [ "$r" = "1" ] && /etc/init.d/dnsmasq restart 2>/dev/null
        ) &
        ;;
    *) unlock; apply_dns & return ;;
    esac
}
main "$@"

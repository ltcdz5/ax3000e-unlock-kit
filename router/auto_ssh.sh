#!/bin/sh
# auto_ssh.sh v2 (2026-08-27) — 轻量化自愈：解锁 SSH + 每次开机只构建一次 DNS 插件
# 由 firewall include 触发（每次防火墙重载都会跑），所以必须毫秒级返回。

auto_ssh_dir="/data/auto_ssh"
host_key="/etc/dropbear/dropbear_rsa_host_key"
host_key_bk="${auto_ssh_dir}/dropbear_rsa_host_key"
marker="/tmp/.dns_ready"

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

apply_dns() {
    # 本次开机已就绪则直接返回（重载不重复干活）
    if [ -f $marker ] || { [ -s /tmp/dnsmasq.d/96-antiad.conf ] && [ -s /tmp/dnsmasq.d/98-upstream.conf ] && pidof dnsmasq >/dev/null; }; then
        touch $marker; return 0
    fi

    ip6tables -I FORWARD -j REJECT --reject-with icmp6-adm-prohibited 2>/dev/null
    for svc in miwifi-roam miwifi-discovery trafficd; do killall $svc 2>/dev/null; done

    echo "addn-hosts=/data/adblock.hosts" > /tmp/dnsmasq.d/99-adblock.conf
    uci set dhcp.@dnsmasq[0].allservers=1 2>/dev/null; uci commit dhcp 2>/dev/null
    [ -s /data/noipv6.conf ]   && cp /data/noipv6.conf   /tmp/dnsmasq.d/92-noipv6.conf
    [ -s /data/logqueries.conf ]&& cp /data/logqueries.conf /tmp/dnsmasq.d/93-logqueries.conf
    [ -s /data/microsoft.conf ] && cp /data/microsoft.conf /tmp/dnsmasq.d/91-microsoft.conf
    [ -s /data/noresolv.conf ] && cp /data/noresolv.conf /tmp/dnsmasq.d/94-noresolv.conf
    [ -s /data/bytedance.conf ] && cp /data/bytedance.conf /tmp/dnsmasq.d/95-bytedance.conf
    if [ -s /data/upstreams.conf ]; then cp /data/upstreams.conf /tmp/dnsmasq.d/98-upstream.conf
    else printf 'server=223.5.5.5\nserver=119.29.29.29\nserver=114.114.114.114\nserver=4.2.2.2\n' > /tmp/dnsmasq.d/98-upstream.conf
    fi
    # 优先用本地缓存恢复广告表；缓存超过48小时才联网刷新
    if [ -s /data/antiad.gz ]; then zcat /data/antiad.gz > /tmp/dnsmasq.d/96-antiad.conf 2>/dev/null; refresh=1; fi
    /etc/init.d/dnsmasq restart 2>/dev/null

    age_ok=$(find /data/antiad.gz -mmin +2880 2>/dev/null)
    if [ "$refresh" != "1" ] || [ -n "$age_ok" ]; then
        (
            w=0
            while [ $w -lt 12 ]; do ping -c 1 -W 2 223.5.5.5 >/dev/null 2>&1 && break; w=$((w+1)); sleep 5; done
            curl -sL "https://anti-ad.net/anti-ad-for-dnsmasq.conf" -o /tmp/antiad_raw --connect-timeout 15 --max-time 90
            if [ "$(wc -c < /tmp/antiad_raw)" -gt 500000 ]; then
                grep -vE "byteimg|pstatp|douyinpic|douyin|bytecdn|bytedance" /tmp/antiad_raw > /tmp/dnsmasq.d/96-antiad.conf
                gzip -c /tmp/dnsmasq.d/96-antiad.conf > /data/antiad.gz.new 2>/dev/null \
                    && mv -f /data/antiad.gz.new /data/antiad.gz
                /etc/init.d/dnsmasq restart 2>/dev/null
                logger -t auto_ssh "anti-AD refreshed"
            else
                rm -f /tmp/antiad_raw
                logger -t auto_ssh "anti-AD download failed, keep cache"
            fi
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
    *) unlock; apply_dns & return ;;
    esac
}
main "$@"

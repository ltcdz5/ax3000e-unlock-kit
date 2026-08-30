# -*- coding: utf-8 -*-
"""migration_pack 纯函数测试（清单/容量/安全过滤）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.argv = ["migration_pack.py"]
import migration_pack as mp


def test_capacity_ok_and_limits():
    line = "Filesystem            Size      Used Available Use% Mounted on"
    assert mp.capacity_ok("/dev/ubi0_5 1696 1408 288 83% /data", 100 * 1024) is True
    assert mp.capacity_ok("/dev/ubi0_5 1696 1408 288 83% /data", 288 * 1024) is False  # 无余量
    assert mp.capacity_ok("", 1) is False
    assert mp.capacity_ok("/dev/ubi0_5 1696 1408 xx 83% /data", 1) is False


def test_forbidden_names_filtered():
    assert mp.safe_pack_name("/data/auto_ssh/dropbear_rsa_host_key") is None
    assert mp.safe_pack_name("/data/.hidden") is None
    assert mp.safe_pack_name("/data/auto_ssh/auto_ssh.sh") == "auto_ssh.sh"
    assert mp.safe_pack_name("/data/upstreams.conf") == "upstreams.conf"


def test_manifest_roundtrip():
    entries = {"data/a.conf": b"hello", "uci/network": b"\x00\x01binary"}
    parsed = mp.parse_manifest(mp.build_manifest(entries))
    assert set(parsed) == set(entries)
    import hashlib
    for name, b in entries.items():
        md5, size = parsed[name]
        assert md5 == hashlib.md5(b).hexdigest() and size == len(b)


def test_parse_manifest_ignores_noise():
    text = "# 迁移包清单 format=1\n随便一行\ndata/a.conf  md5=%s  size=3\n" % ("0" * 32)
    parsed = mp.parse_manifest(text)
    assert list(parsed) == ["data/a.conf"]


def test_valid_host():
    assert mp.valid_host("192.168.2.106") is True
    assert mp.valid_host("router.local") is True
    assert mp.valid_host("1.1.1.1'$(reboot)'") is False
    assert mp.valid_host("") is False

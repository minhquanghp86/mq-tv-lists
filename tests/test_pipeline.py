from update_iptv import (
    Channel,
    classify,
    clean_name,
    is_junk,
    merge,
    norm_key,
    parse_m3u,
    quality_score,
    sort_key,
)

SAMPLE = """#EXTM3U
#EXTINF:-1 tvg-id="vtv1" tvg-logo="http://x/vtv1.png" group-title="VTV",VTV1 HD
#EXTVLCOPT:http-user-agent=TestUA
http://example.com/vtv1.m3u8
#EXTINF:-1 tvg-id="vtv1" tvg-logo="http://x/vtv1.png" group-title="VTV",VTV1 FHD 8Mbps
http://example.com/vtv1-hd.m3u8
#EXTINF:-1 tvg-id="hbo" tvg-logo="http://x/hbo.png" group-title="Quốc Tế",HBO HD
http://example.com/hbo.m3u8
#EXTINF:-1 tvg-id="unknown" group-title="Khác",Kênh lạ | nguồn X
http://example.com/other.m3u8
"""


def test_parse_basic():
    channels = parse_m3u(SAMPLE, "test")
    assert len(channels) == 4
    first = channels[0]
    assert first.name == "VTV1"
    assert first.tvg_id == "vtv1"
    assert first.group == "VTV"
    assert first.url == "http://example.com/vtv1.m3u8"
    assert first.vlc_opts == ["#EXTVLCOPT:http-user-agent=TestUA"]


def test_clean_name_removes_quality_suffix():
    assert clean_name("VTV3 FHD 8Mbps") == "VTV3"
    assert clean_name("HBO HD | Nguồn A") == "HBO"


def test_quality_prefers_higher_tier():
    assert quality_score("Kênh FHD", "http://a") > quality_score("Kênh HD", "http://a")
    assert quality_score("Kênh 8K", "http://a") > quality_score("Kênh 4K", "http://a")


def test_classify_groups():
    assert classify("VTV1", "") == "VTV"
    assert classify("SCTV2", "") == "SCTV"
    assert classify("HBO", "") == "Quốc Tế"
    assert classify("Đài Phát thanh Quảng Ninh", "Địa phương") == "Địa Phương"


def test_merge_keeps_best_and_dedupes_urls():
    low = Channel(name="VTV1", url="http://a/1", key="vtv1", score=10, group="VTV")
    high = Channel(name="VTV1", url="http://a/2", key="vtv1", score=50, group="VTV")
    other = Channel(name="VTV2", url="http://a/3", key="vtv2", score=60, group="VTV")
    merged = merge([low, high, other], {})
    assert len(merged) == 2
    urls = {channel.url for channel in merged}
    assert urls == {"http://a/2", "http://a/3"}


def test_sort_orders_groups_and_numbers():
    channels = [
        Channel(name="HBO", url="http://a/h", key="hbo", group="Quốc Tế"),
        Channel(name="VTV3", url="http://a/3", key="vtv3", group="VTV"),
        Channel(name="VTV1", url="http://a/1", key="vtv1", group="VTV"),
    ]
    ordered = sorted(channels, key=sort_key)
    assert [channel.name for channel in ordered] == ["VTV1", "VTV3", "HBO"]


def test_norm_key_matches_diacritics():
    assert norm_key("Quốc Tế") == norm_key("quoc te")


def test_is_junk_filters_garbage_names():
    assert is_junk("===| TEST |===")
    assert is_junk("   ")
    assert is_junk("x")
    assert not is_junk("VTV1")
    assert not is_junk("Đài Phát thanh Quảng Ninh")


def test_merge_prefers_source_priority_when_scores_tie():
    a = Channel(name="VTV1", url="http://a/1", key="vtv1", score=50, group="VTV", priority=5)
    b = Channel(name="VTV1", url="http://b/1", key="vtv1", score=50, group="VTV", priority=1)
    merged = merge([a, b], {})
    assert len(merged) == 1
    assert merged[0].url == "http://b/1"

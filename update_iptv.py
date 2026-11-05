#!/usr/bin/env python3
"""MQTV — builder danh sách kênh IPTV.

Pipeline: tải nhiều nguồn M3U → parse → chuẩn hoá tên/tvg-id → kiểm tra kênh còn sống
→ gộp & chọn luồng tốt nhất cho mỗi kênh → phân nhóm & sắp xếp → ghi:

  vn.m3u            danh sách chính (cho app và mọi trình phát M3U)
  mq-tv.json        metadata (số kênh, nhóm, trạng thái nguồn, sha256)
  state/health.json trạng thái sức khoẻ từng kênh giữa các lần chạy

Không host nội dung video; chỉ tổng hợp link công khai.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
SOURCES_FILE = ROOT / "sources.json"
HEALTH_FILE = ROOT / "state" / "health.json"
OVERRIDES_FILE = ROOT / "channels.json"
OUTPUT_M3U = ROOT / "vn.m3u"
OUTPUT_JSON = ROOT / "mq-tv.json"

FETCH_TIMEOUT = 20
PROBE_TIMEOUT = 8
PROBE_WORKERS = 24
MAX_CONSECUTIVE_FAILS = 5
MIN_CHANNELS = 50

ICT = timezone(timedelta(hours=7))

GROUP_ORDER = [
    "VTV",
    "VTC",
    "SCTV",
    "Quốc Tế",
    "VTVcab",
    "HTV",
    "In The Box",
    "Thể Thao",
    "Địa Phương",
    "Quốc Phòng",
    "Khác",
]

GROUP_KEYWORDS = [
    ("VTVcab", ("vtvcab", "vtv cab")),
    ("VTV", ("vtv",)),
    ("VTC", ("vtc",)),
    ("SCTV", ("sctv",)),
    ("HTV", ("htv",)),
    ("In The Box", ("in the box", "inthebox", "intv")),
    ("Quốc Phòng", ("quốc phòng", "quoc phong", "antv", "qpvn")),
    ("Thể Thao", ("thể thao", "the thao", "sport", "football", "bóng đá", "golf", "tennis", "blv", "bình luận")),
    (
        "Địa Phương",
        (
            "địa phương", "dia phuong", "local", "tỉnh", "tinh",
            "thvl", "btv", "dnrt", "ptt", "ktv", "atv", "la34", "thdt", "tg",
        ),
    ),
    (
        "Quốc Tế",
        (
            "quốc tế",
            "quoc te",
            "international",
            "hbo",
            "cinemax",
            "axn",
            "warner",
            "discovery",
            "national geographic",
            "nat geo",
            "cartoon",
            "animax",
            "k+",
            "fox",
            "bbc",
            "cnn",
            "nhk",
            "tv5",
            "dw",
            "arirang",
        ),
    ),
]

QUALITY_TIERS = [
    (re.compile(r"\b8k\b", re.I), 80.0),
    (re.compile(r"\b(4k|uhd)\b", re.I), 70.0),
    (re.compile(r"\b(fhd|full\s*hd)\b", re.I), 60.0),
    (re.compile(r"\bhd\b", re.I), 50.0),
    (re.compile(r"\b(sd|360p|480p)\b", re.I), 40.0),
]

NOISE_SUFFIX = re.compile(
    r"[\s\-–|]*(?:fhd|full\s*hd|hd|sd|4k|8k|uhd|h\.?264|h\.?265|hevc|avc|"
    r"\d+(?:\.\d+)?\s*mbps|\d+\s*kbps|\d{3,4}p)\b.*$",
    re.I,
)
PIPE_SUFFIX = re.compile(r"\s*\|.*$")
BITRATE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mb(?:ps)?", re.I)
SPACES_RE = re.compile(r"\s{2,}")
ATTR_RE = re.compile(r'([a-zA-Z0-9\-]+)="([^"]*)"')
JUNK_RE = re.compile(r"^(?:[=\-_*#|~ .]+|test|xxx|demo|sample)$", re.I)

UA_FALLBACK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/129.0.0.0 Safari/537.36"


@dataclass
class Channel:
    name: str
    url: str
    tvg_id: str = ""
    logo: str = ""
    group: str = "Khác"
    vlc_opts: list[str] = field(default_factory=list)
    score: float = 0.0
    key: str = ""
    alive: bool = True
    source: str = ""
    priority: int = 10


# ── chuẩn hoá ────────────────────────────────────────────────────────
def strip_diacritics(text: str) -> str:
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn")


def norm_key(text: str) -> str:
    base = strip_diacritics(text.lower())
    base = re.sub(r"[^a-z0-9]+", "", base)
    return base


def clean_name(raw: str) -> str:
    name = NOISE_SUFFIX.sub("", raw).strip()
    name = PIPE_SUFFIX.sub("", name).strip()
    name = SPACES_RE.sub(" ", name)
    return name or raw.strip()


def quality_score(name: str, url: str) -> float:
    haystack = f"{name} {url}"
    tier = 30.0
    for pattern, value in QUALITY_TIERS:
        if pattern.search(haystack):
            tier = value
            break
    bitrate = BITRATE_RE.search(haystack)
    return tier + (float(bitrate.group(1)) if bitrate else 0.0)


def classify(name: str, group_title: str) -> str:
    haystack = f"{strip_diacritics(group_title.lower())} {norm_key(name)}"
    for group, words in GROUP_KEYWORDS:
        for word in words:
            needle = strip_diacritics(word.lower())
            if needle and needle in haystack:
                return group
    return "Khác"


# ── tải & parse ──────────────────────────────────────────────────────
def load_config() -> dict:
    if SOURCES_FILE.exists():
        return json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    return {"epg_url": "https://epg.io.vn/epg.xml", "user_agent": UA_FALLBACK, "sources": []}


def fetch(url: str, user_agent: str, timeout: int = FETCH_TIMEOUT) -> str | None:
    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": user_agent})
        response.raise_for_status()
        return response.text
    except Exception as error:  # noqa: BLE001 - log và bỏ qua nguồn lỗi
        print(f"  ⚠  tải lỗi {url}: {error}", file=sys.stderr)
        return None


def is_junk(name: str) -> bool:
    cleaned = re.sub(r"[^0-9A-Za-zÀ-ỹ]+", " ", name).strip().lower()
    if len(cleaned.replace(" ", "")) < 2:
        return True
    return cleaned in {"test", "xxx", "demo", "sample"}


def parse_m3u(text: str, source: str, priority: int = 10) -> list[Channel]:
    channels: list[Channel] = []
    pending = None
    pending_opts: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            attrs = dict(ATTR_RE.findall(line))
            title = line.split(",", 1)[1].strip() if "," in line else ""
            pending = Channel(
                name=clean_name(title),
                url="",
                tvg_id=attrs.get("tvg-id", "").strip(),
                logo=attrs.get("tvg-logo", "").strip(),
                group=attrs.get("group-title", "").strip() or "Khác",
                source=source,
                priority=priority,
            )
            pending_opts = []
            continue
        if line.startswith("#EXTVLCOPT") and pending is not None:
            pending_opts.append(line)
            continue
        if line.startswith("#"):
            continue
        if pending is not None and line.startswith(("http://", "https://", "rtsp://", "rtmp://", "udp://")):
            pending.url = line
            pending.vlc_opts = list(pending_opts)
            if pending.name:
                pending.key = norm_key(pending.name)
                pending.score = quality_score(pending.name, pending.url)
                pending.group = classify(pending.name, pending.group)
                channels.append(pending)
            pending = None
            pending_opts = []

    return channels


# ── sức khoẻ kênh ────────────────────────────────────────────────────
def load_health() -> dict:
    if HEALTH_FILE.exists():
        try:
            return json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def probe(url: str, user_agent: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    try:
        with requests.get(
            url,
            timeout=PROBE_TIMEOUT,
            headers={"User-Agent": user_agent, "Range": "bytes=0-2047"},
            stream=True,
        ) as response:
            if response.status_code >= 400:
                return False
            for chunk in response.iter_content(512):
                if chunk:
                    return True
            return response.status_code in (200, 206, 301, 302)
    except Exception:  # noqa: BLE001 - kênh chết là kết quả hợp lệ
        return False


def check_health(channels: list[Channel], user_agent: str, health: dict) -> None:
    urls = sorted({channel.url for channel in channels})
    print(f"🩺  kiểm tra {len(urls)} luồng…")
    results: dict[str, bool] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
        future_map = {pool.submit(probe, url, user_agent): url for url in urls}
        for future in concurrent.futures.as_completed(future_map):
            url = future_map[future]
            results[url] = bool(future.result())

    failed = [url for url, ok in results.items() if not ok]
    if failed:
        print(f"    thử lại {len(failed)} luồng lỗi…")

        def retry(url: str) -> bool:
            try:
                with requests.get(
                    url,
                    timeout=PROBE_TIMEOUT + 4,
                    headers={"User-Agent": user_agent},
                    stream=True,
                ) as response:
                    return response.status_code < 400
            except Exception:  # noqa: BLE001
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            for url, ok in zip(failed, pool.map(retry, failed)):
                if ok:
                    results[url] = True

    now = datetime.now(ICT).isoformat(timespec="seconds")
    for url, ok in results.items():
        entry = health.setdefault(url, {"fails": 0, "lastOk": None, "lastCheck": None})
        entry["lastCheck"] = now
        if ok:
            entry["fails"] = 0
            entry["lastOk"] = now
        else:
            entry["fails"] = int(entry.get("fails", 0)) + 1

    alive = sum(1 for ok in results.values() if ok)
    print(f"    {alive}/{len(results)} luồng phản hồi")


def keep_channel(channel: Channel, health: dict) -> bool:
    entry = health.get(channel.url, {})
    fails = int(entry.get("fails", 0))
    # chỉ loại sau 3 lần lỗi liên tiếp: tránh xoá oan kênh VN khi runner nước ngoài không vào được
    return fails < MAX_CONSECUTIVE_FAILS


# ── gộp, sắp xếp, ghi ────────────────────────────────────────────────
def apply_overrides(channels: list[Channel]) -> None:
    if not OVERRIDES_FILE.exists():
        return
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("  ⚠  channels.json không hợp lệ, bỏ qua", file=sys.stderr)
        return
    for channel in channels:
        data = overrides.get(channel.key) or overrides.get(channel.tvg_id)
        if not data:
            continue
        channel.name = data.get("name", channel.name)
        channel.group = data.get("group", channel.group)
        channel.logo = data.get("logo", channel.logo)
        channel.tvg_id = data.get("tvg_id", channel.tvg_id)


def merge(channels: list[Channel], health: dict, stats: dict | None = None) -> list[Channel]:
    best: dict[str, Channel] = {}
    seen_urls: set[str] = set()
    counter = stats if stats is not None else {}
    for channel in channels:
        if channel.url in seen_urls:
            counter["duplicate"] = counter.get("duplicate", 0) + 1
            continue
        if not keep_channel(channel, health):
            counter["dead"] = counter.get("dead", 0) + 1
            continue
        alive = int(health.get(channel.url, {}).get("fails", 1)) == 0
        channel.alive = alive
        current = best.get(channel.key)
        if current is None:
            best[channel.key] = channel
            seen_urls.add(channel.url)
            continue
        current_score = (int(current.alive), -current.priority, current.score)
        new_score = (int(alive), -channel.priority, channel.score)
        if new_score > current_score:
            seen_urls.discard(current.url)
            best[channel.key] = channel
            seen_urls.add(channel.url)
            counter["duplicate"] = counter.get("duplicate", 0) + 1
        else:
            counter["duplicate"] = counter.get("duplicate", 0) + 1
    return list(best.values())


def sort_key(channel: Channel) -> tuple:
    group_index = GROUP_ORDER.index(channel.group) if channel.group in GROUP_ORDER else len(GROUP_ORDER)
    number = 999
    match = re.search(r"(\d{1,3})", channel.name)
    if match and channel.group in ("VTV", "VTC", "SCTV", "HTV", "VTVcab"):
        number = int(match.group(1))
    return (group_index, number, channel.name.lower())


def write_outputs(
    channels: list[Channel],
    sources_report: list[dict],
    epg_url: str,
    dropped: dict | None = None,
) -> dict:
    channels.sort(key=sort_key)
    lines = [
        f'#EXTM3U url-tvg="{epg_url}" x-tvg-url="{epg_url}"',
        "# Tổng hợp tự động bởi mq-tv-lists · không host nội dung, chỉ tổng hợp link công khai",
    ]
    for channel in channels:
        lines.append(
            f'#EXTINF:-1 tvg-id="{channel.tvg_id}" tvg-logo="{channel.logo}" '
            f'group-title="{channel.group}",{channel.name}'
        )
        lines.extend(channel.vlc_opts)
        lines.append(channel.url)
    text = "\n".join(lines) + "\n"
    OUTPUT_M3U.write_text(text, encoding="utf-8")

    groups: dict[str, int] = {}
    for channel in channels:
        groups[channel.group] = groups.get(channel.group, 0) + 1

    meta = {
        "updatedAt": datetime.now(ICT).isoformat(timespec="seconds"),
        "epg": epg_url,
        "channels": len(channels),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "groups": [
            {"name": name, "count": groups[name]}
            for name in GROUP_ORDER
            if groups.get(name)
        ],
        "missingGroups": [name for name in GROUP_ORDER if not groups.get(name) and name != "Khác"],
        "unverified": sum(1 for channel in channels if channel.alive is False),
        "dropped": dropped or {},
        "sources": sources_report,
        "playlist": "https://raw.githubusercontent.com/minhquanghp86/mq-tv-lists/main/vn.m3u",
    }
    OUTPUT_JSON.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def validate(channels: list[Channel]) -> None:
    if len(channels) < MIN_CHANNELS:
        raise SystemExit(f"❌  quá ít kênh ({len(channels)} < {MIN_CHANNELS})")
    urls = [channel.url for channel in channels]
    if len(urls) != len(set(urls)):
        raise SystemExit("❌  có URL trùng trong output")
    if not all(channel.name and channel.url.startswith(("http://", "https://", "rtsp://", "rtmp://")) for channel in channels):
        raise SystemExit("❌  có kênh thiếu tên hoặc URL không hợp lệ")


def main() -> None:
    config = load_config()
    user_agent = config.get("user_agent", UA_FALLBACK)
    epg_url = config.get("epg_url", "https://epg.io.vn/epg.xml")
    sources = [source for source in config.get("sources", []) if source.get("enabled", True)]
    if not sources:
        raise SystemExit("❌  không có nguồn nào được bật trong sources.json")

    health = load_health()
    all_channels: list[Channel] = []
    report: list[dict] = []

    for index, source in enumerate(sources, 1):
        print(f"[{index}/{len(sources)}] {source['name']}: {source['url']}")
        text = fetch(source["url"], user_agent)
        parsed = parse_m3u(text, source["name"], int(source.get("priority", 10))) if text else []
        print(f"    {len(parsed)} kênh")
        report.append(
            {
                "name": source["name"],
                "url": source["url"],
                "priority": int(source.get("priority", 10)),
                "ok": bool(text),
                "channels": len(parsed),
            }
        )
        all_channels.extend(parsed)

    if not all_channels:
        raise SystemExit("❌  không tải được nguồn nào")

    junk = [channel for channel in all_channels if is_junk(channel.name)]
    usable = [channel for channel in all_channels if not is_junk(channel.name)]
    unique_by_url = {channel.url: channel for channel in usable}.values()
    candidates = list(unique_by_url)
    dropped = {"junk": len(junk)} if junk else {}
    check_health(candidates, user_agent, health)

    alive_by_source: dict[str, int] = {}
    for channel in candidates:
        if int(health.get(channel.url, {}).get("fails", 1)) == 0:
            alive_by_source[channel.source] = alive_by_source.get(channel.source, 0) + 1
    for entry in report:
        entry["alive"] = alive_by_source.get(entry["name"], 0)
        total = entry.get("channels") or 0
        entry["aliveRatio"] = round(entry["alive"] / total, 3) if total else 0.0

    merged = merge(candidates, health, dropped)
    apply_overrides(merged)
    merged = merge(merged, health)
    validate(merged)

    meta = write_outputs(merged, report, epg_url, dropped)
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(
        json.dumps(dict(sorted(health.items())), ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    print(f"✅  {meta['channels']} kênh → vn.m3u · nhóm: {[g['name'] + '(' + str(g['count']) + ')' for g in meta['groups']]}")


if __name__ == "__main__":
    main()

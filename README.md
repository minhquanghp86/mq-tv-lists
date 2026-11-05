# mq-tv-lists

Danh sách kênh IPTV cho dự án **MINH QUANG TV** — tổng hợp tự động từ nhiều nguồn công khai, **kiểm tra kênh còn sống** trước khi phát hành, cập nhật **01:00 ICT hằng ngày** bằng GitHub Actions.

## Dùng ngay

```
https://raw.githubusercontent.com/minhquanghp86/mq-tv-lists/main/vn.m3u
```

Dùng được với app MINH QUANG TV, TiviMate, OTT Navigator, Perfect Player, VLC…

## Output

| File | Nội dung |
|---|---|
| `vn.m3u` | Danh sách chính: một header `#EXTM3U` có `url-tvg`/`x-tvg-url`, mỗi kênh có `tvg-id`, `tvg-logo`, `group-title`; giữ `#EXTVLCOPT` (UA/Referer) nếu nguồn cần |
| `mq-tv.json` | Metadata: `updatedAt`, `channels`, `groups[]`, `sources[]` (kênh/nguồn/sống), `sha256`, `epg` |
| `state/health.json` | Sức khoẻ từng URL giữa các lần chạy (số lần lỗi liên tiếp, lần sống cuối) |
| `channels.json` | Ghi đè thủ công: `{"vtv1": {"name": "...", "group": "...", "logo": "...", "tvg_id": "..."}}` |

## Cách hoạt động

1. Tải mọi nguồn bật trong `sources.json` (song song, UA Chrome).
2. Parse M3U: `tvg-id`, `tvg-logo`, `group-title`, `#EXTVLCOPT`.
3. Chuẩn hoá tên (bỏ HD/FHD/4K/8K/bitrate/pipe), chuẩn hoá `tvg-id`.
4. **Kiểm tra sống**: GET `Range: bytes=0-2047`, timeout 8s, 24 luồng song song.
   - Kênh sống → giữ.
   - Kênh lỗi nhưng **từng sống** → giữ thêm 1 lần (tránh mạng chập chờn).
   - Kênh chưa từng sống hoặc lỗi 2 lần liên tiếp → loại.
5. Gộp: mỗi tên kênh giữ **1 luồng tốt nhất** (tier 8K/4K/FHD/HD/SD × bitrate × còn sống), không trùng URL.
6. Phân nhóm theo thứ tự: VTV → VTC → SCTV → Quốc Tế → VTVcab → HTV → In The Box → Thể Thao → Địa Phương → Quốc Phòng → Khác; trong nhóm sắp theo số kênh.
7. Ghi `vn.m3u`, `mq-tv.json`, `state/health.json`; CI **validate** (≥ 50 kênh, không trùng URL, đủ tên/URL) rồi commit.

## Thêm / tắt nguồn

Sửa `sources.json`:

```json
{ "name": "ten-nguon", "url": "https://…/list.m3u", "enabled": true }
```

## Chạy tại máy

```bash
pip install -r requirements-dev.txt
python update_iptv.py      # tạo vn.m3u + mq-tv.json + state/health.json
pytest -q                  # 7 test
ruff check .               # lint
```

## CI

`.github/workflows/update.yml`: cron `0 18 * * *` (01:00 ICT), `workflow_dispatch`, và chạy khi sửa `update_iptv.py`/`sources.json`/`channels.json`. Mỗi lần chạy: lint → test → build → commit output nếu đổi.

## Lưu ý

- Repo **không host nội dung video**, chỉ tổng hợp link công khai do các nguồn bên ngoài công bố.
- Danh sách phụ thuộc nguồn bên ngoài: nguồn chết sẽ tự bị loại dần, nhưng không có gì đảm bảo kênh luôn khả dụng.
- Giấy phép **MIT** cho mã nguồn của repo này.

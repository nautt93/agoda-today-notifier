# Booking Check-in Hôm nay

Ứng dụng Windows theo dõi email xác nhận từ Agoda và Expedia Partner Central, lưu booking tương lai và cảnh báo đúng ngày khách check-in.

## Tính năng

- Đọc email Agoda và Expedia qua IMAP với TLS có xác minh chứng chỉ.
- Xử lý đầy đủ booking mới, chỉnh sửa và hủy.
- Agoda và Expedia dùng chung cơ chế: chỉ hiện cảnh báo cho booking check-in hôm nay; chỉnh sửa/hủy ngày khác được cập nhật âm thầm.
- Lưu booking tương lai, không bỏ lỡ cảnh báo khi email đến trước ngày nhận phòng.
- Phân biệt Expedia Collect và Hotel/Property Collect; ưu tiên đúng khoản tiền khách sạn thực nhận.
- Hiển thị popup, phát âm thanh, lưu lịch sử và chép 9 cột sang Excel.
- Giao diện desktop tối–vàng cao cấp; nhấp đúp, Ctrl+C hoặc chuột phải để sao chép một/nhiều booking sang Excel.
- Hỗ trợ màn hình/loa F92 qua USB serial.
- OTA dùng HTTPS, SHA-256 và chữ ký Ed25519 độc lập; updater tự rollback khi thay thế thất bại.
- Mật khẩu ứng dụng được mã hóa bằng Windows DPAPI.

## Cài đặt

Tải file `.exe` mới nhất tại [Releases](https://github.com/nautt93/agoda-today-notifier/releases). Với Gmail, Yahoo hoặc iCloud, hãy dùng **mật khẩu ứng dụng**, không dùng mật khẩu tài khoản chính.

Người đang dùng bản v1.5.5 từ repo cũ cần cài v1.6.0 thủ công một lần. Từ v1.6.0, OTA mặc định chuyển hoàn toàn sang repo `nautt93`.

## Phát triển

Yêu cầu Python 3.11 trở lên:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements-build.txt
python -m pytest -q
python app.py
```

Build Windows:

```powershell
pyinstaller --noconfirm --clean BookingNotifier.spec
```

## Kiến trúc an toàn

- `booking_notifier/parsing.py`: parser và nhận diện vòng đời booking.
- `booking_notifier/state.py`: lưu trạng thái nguyên tử, lịch booking tương lai và chống trùng.
- `booking_notifier/mail_monitor.py`: kết nối IMAP, lọc header trước khi tải nội dung.
- `booking_notifier/ota_update.py`: xác minh chữ ký manifest, checksum và rollback.
- `booking_notifier/f92_device.py`: giao tiếp thiết bị F92.
- `tests/`: regression test cho các lỗi đã phát hiện ở v1.5.5.

## Bảo mật phát hành

Workflow release cần secret `OTA_SIGNING_PRIVATE_KEY`. Nếu có chứng thư code-signing tin cậy, cấu hình thêm `WINDOWS_CERTIFICATE_PFX_BASE64` và `WINDOWS_CERTIFICATE_PASSWORD` để ký Authenticode. Không commit private key hoặc file `.pfx` vào repository.

# Booking Check-in Hôm nay

Ứng dụng Windows đọc thư xác nhận mới nhất từ Agoda và Expedia Partner Central, báo ngay nếu booking check-in hôm nay, theo cơ chế bản 1.5.5.

## Tính năng

- Đọc email Agoda và Expedia qua IMAP với TLS có xác minh chứng chỉ.
- Lần đầu chỉ đọc 20 thư gần nhất; các lần sau chỉ đọc thư mới bằng mốc UID lưu trên máy. Không tải toàn bộ hộp thư hoặc tìm lại 500 thư cũ.
- Khi tắt máy/mất mạng, lần kết nối sau đọc đủ thư mới chưa xử lý, kể cả hơn 20 thư. Lưu danh sách thư cần đọc trước khi tải; mất kết nối/thoát giữa chừng không làm mất phần còn thiếu. Luôn đọc thư mới trước và báo ngay.
- Agoda và Expedia dùng chung cơ chế: email xác nhận mới → đọc ngày check-in → nếu là hôm nay, lưu cảnh báo và hiện popup ngay trước khi đọc thư tiếp theo.
- Ghép tên khách/hạng phòng xuống nhiều dòng; ưu tiên phần HTML đầy đủ thay vì bản text rút gọn, hỗ trợ họ/tên riêng và hạng phòng kèm số lượng. Khi nâng cấp parser, đọc lại tối đa 20 thư gần nhất một lần để bổ sung lịch sử/popup đang chờ, không báo trùng; vẫn giữ mốc UID và thư mới còn thiếu khi mất mạng.
- Nhận diện voucher Agoda song ngữ Anh/Việt: `Customer First Name / Tên Khách Hàng`, `Customer Last Name / Họ Khách Hàng`, bảng `Room Type / Loại Phòng` và `No. of Rooms / Số phòng`. Ghép đủ họ tên, giữ số lượng kể cả `x1` khi email ghi 1 phòng. Chỉ sửa dữ liệu booking ngày cũ đã có trong lịch sử, không thêm booking ngày khác hoặc phát lại cảnh báo.
- Nếu tên/phòng của booking đang chờ được bổ sung, cập nhật ngay popup hiện tại, hàng chờ, phần chép Excel và F92; không đóng/mở lại popup hay phát lại âm thanh.
- Booking ngày khác và thư chỉnh sửa/hủy được bỏ qua; không tự lên lịch nhắc booking tương lai từ dữ liệu cũ.
- Phân biệt Expedia Collect và Hotel/Property Collect; ưu tiên đúng khoản tiền khách sạn thực nhận.
- Nhận diện mẫu Expedia `New Booking`: đầy đủ tên khách, `Room Type Name` (không lấy `Room Type Code`), số phòng từ bảng mã xác nhận hoặc `Room Nights / số đêm ở`. Giữ `x1` cho một phòng; không đoán số lượng khi dữ liệu thiếu hoặc không khớp.
- Hiển thị popup, phát âm thanh, lưu lịch sử và chép 9 cột sang Excel.
- Phát MP3 hoặc WAV như bản cũ; tệp âm thanh/thiết bị lỗi không đóng popup booking. Giờ yên lặng 00:00–08:00 giữ cảnh báo chờ và hiện sau 08:00 nếu bật.
- Chép Excel đúng mẫu 1.5.5: STT trống | tên khách | số ngày đến | số ngày đi | số đêm | tiền dạng số | trống | trống | nguồn + hạng phòng + số lượng phòng đọc được. Dán bắt đầu từ cột A; không chèn mã booking hoặc tiêu đề email. Popup và lịch sử dùng chung format này.
- Giao diện desktop tối–vàng cao cấp; nhấp đúp, Ctrl+C hoặc chuột phải để sao chép một/nhiều booking sang Excel.
- Hỗ trợ màn hình/loa F92 qua USB serial.
- F92 hiển thị lịch tháng và đồng hồ khi chờ, cập nhật mỗi phút; giữ màn hình booking cho đến khi xác nhận.
- Nhật ký ghi mã booking/ngày check-in và UID email chưa đọc được để kiểm tra các mẫu email mới.
- OTA dùng HTTPS, SHA-256 và chữ ký Ed25519 độc lập; updater tự rollback khi thay thế thất bại.
- OTA chỉ đóng ứng dụng khi bộ cập nhật đã kiểm tra xong tệp và quyền ghi; chờ tiến trình cũ kết thúc, thử lại khi Windows còn giữ tệp EXE, mở lại ứng dụng trong môi trường PyInstaller độc lập và chờ giao diện xác nhận khởi động. Lỗi cập nhật hiện hộp thoại và lưu tại `%LOCALAPPDATA%\AgodaTodayNotifier\update-error.log`.
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
- `booking_notifier/state.py`: lưu cảnh báo hôm nay, dấu chống trùng, mốc UID và thư còn thiếu; giữ lịch sử khi nâng cấp.
- `booking_notifier/mail_monitor.py`: kết nối IMAP, khởi tạo từ 20 thư mới nhất rồi chỉ đọc UID mới/thư còn thiếu; báo booking hôm nay ngay khi đọc được.
- `booking_notifier/ota_update.py`: xác minh chữ ký manifest, checksum và rollback.
- `booking_notifier/f92_device.py`: giao tiếp thiết bị F92.
- `tests/`: regression test cho các lỗi đã phát hiện ở v1.5.5.

## Bảo mật phát hành

Workflow release cần secret `OTA_SIGNING_PRIVATE_KEY`. Nếu có chứng thư code-signing tin cậy, cấu hình thêm `WINDOWS_CERTIFICATE_PFX_BASE64` và `WINDOWS_CERTIFICATE_PASSWORD` để ký Authenticode. Không commit private key hoặc file `.pfx` vào repository.

# Booking Check-in Hôm nay

Ứng dụng Windows đọc thư xác nhận mới nhất từ Agoda, Expedia Partner Central và Traveloka, báo ngay nếu booking check-in hôm nay, theo cơ chế bản 1.5.5.

## Tính năng

- Đọc email Agoda, Expedia và Traveloka qua IMAP với TLS có xác minh chứng chỉ.
- Lần đầu chỉ đọc 20 thư gần nhất; các lần sau chỉ đọc thư mới bằng mốc UID lưu trên máy. Không tải toàn bộ hộp thư hoặc tìm lại 500 thư cũ.
- Khi tắt máy/mất mạng, lần kết nối sau đọc đủ thư mới chưa xử lý, kể cả hơn 20 thư. Lưu danh sách thư cần đọc trước khi tải; mất kết nối/thoát giữa chừng không làm mất phần còn thiếu. Luôn đọc thư mới trước và báo ngay.
- Agoda, Expedia và Traveloka dùng chung cơ chế: email xác nhận mới → đọc ngày check-in → nếu là hôm nay, lưu cảnh báo và hiện popup; booking ngày khác và chỉnh sửa/hủy bỏ qua.
- Ghép tên khách/hạng phòng xuống nhiều dòng; ưu tiên phần HTML đầy đủ thay vì bản text rút gọn, hỗ trợ họ/tên riêng và hạng phòng kèm số lượng. Khi nâng cấp parser, đọc lại tối đa 20 thư gần nhất một lần để bổ sung lịch sử/popup đang chờ, không báo trùng; vẫn giữ mốc UID và thư mới còn thiếu khi mất mạng.
- Nhận diện voucher Agoda song ngữ Anh/Việt: `Customer First Name / Tên Khách Hàng`, `Customer Last Name / Họ Khách Hàng`, bảng `Room Type / Loại Phòng` và `No. of Rooms / Số phòng`. Ghép đủ họ tên, giữ số lượng kể cả `x1` khi email ghi 1 phòng. Chỉ sửa dữ liệu booking ngày cũ đã có trong lịch sử, không thêm booking ngày khác hoặc phát lại cảnh báo.
- Nếu tên/phòng của booking đang chờ được bổ sung, cập nhật ngay popup hiện tại, hàng chờ, phần chép Excel và F92; không đóng/mở lại popup hay phát lại âm thanh.
- Tự phục hồi booking hôm nay đã lưu thiếu tên hoặc hạng phòng: tìm đúng email theo mã booking, kể cả ngoài 20 thư gần nhất. Chỉ bổ sung booking đã biết, không quét 500 nội dung thư, không tạo popup trùng hoặc thay mốc UID. Tối đa 20 mã mỗi lượt và 3 email khớp mỗi mã; kiểm tra nguồn, mã, ngày đến và loại xác nhận trước khi sửa. Lỗi/không tìm thấy thì thử lại sau 15 phút; **Cài đặt → Quét email ngay** thử lại ngay. Email gốc cần còn trong Inbox; nếu không đọc được mẫu mới, Nhật ký ghi mã cần kiểm tra, không tự đoán tên/phòng.
- Booking ngày khác và thư chỉnh sửa/hủy được bỏ qua; không tự lên lịch nhắc booking tương lai từ dữ liệu cũ.
- v1.7.17 thêm Traveloka từ email `CONFIRMED - Traveloka Itinerary ID`: chỉ nhận đúng miền gửi Traveloka, lấy ngày nhận/trả phòng trong nội dung (không suy từ mã itinerary), ghép Customer First/Last Name, hạng phòng/số lượng từ Room Information, khoản khách sạn nhận từ `Total you will receive` (không lấy nhầm subtotal/giá khách trả). Nguồn và ghi chú Excel là `Traveloka + hạng phòng + số lượng`. Nâng parser chỉ đọc lại tối đa 20 thư gần nhất một lần để nhận Traveloka trước cập nhật; giữ cursor/thư đang chờ và không báo lại Agoda/Expedia đã đóng.
- v1.7.16 chặn popup trùng khi thông báo từ luồng IMAP đến muộn sau lúc popup phục hồi/kiểm tra định kỳ đã được đóng. Kiểm tra dấu xác nhận đã lưu trước khi xếp hàng và ngay trước khi mở popup; đóng lặp không ghi thêm dòng lịch sử. Lọc bản pending trùng hoặc đã đóng khi khởi động lại, không xóa lịch sử cũ. Chống trùng theo nguồn + mã booking + ngày đến, không theo tên khách: email Expedia gửi lại với Message-ID/UID khác vẫn không báo lại, booking khác của cùng khách vẫn báo. Giữ cấu hình, mốc UID, thư đang chờ và cơ chế chỉ check-in hôm nay.
- v1.7.15 sửa lỗi `BookingEvent.__init__() missing ... booking_id`: dòng lịch sử/popup cũ thiếu `source`/`booking_id`, mã JSON dạng số hoặc mã trống không làm ngắt lượt quét. Agoda thiếu mã nhưng xác nhận mới check-in hôm nay vẫn báo theo cơ chế 1.5.5, dùng khóa chống trùng theo nội dung nhận diện sẵn có. Nhận diện dòng lịch sử không cần giải mã ngày cũ, nên dòng cũ ghi ngày khác định dạng không chặn email mới. Không xóa lịch sử/cấu hình hay reset UID; thư đang chờ đọc được thử lại sau cập nhật, chỉ báo khách đến hôm nay, không báo lại booking đã xác nhận.
- Phân biệt Expedia Collect và Hotel/Property Collect; ưu tiên đúng khoản tiền khách sạn thực nhận.
- Nhận diện mẫu Expedia `New Booking`: đầy đủ tên khách, `Room Type Name` (không lấy `Room Type Code`), số phòng từ bảng mã xác nhận hoặc `Room Nights / số đêm ở`. Giữ `x1` cho một phòng; không đoán số lượng khi dữ liệu thiếu hoặc không khớp.
- Hiển thị popup, phát âm thanh, lưu lịch sử và chép 9 cột sang Excel.
- Popup cả ba nguồn giữ 2 nút chính lớn: **Sao chép** và **Đóng thông báo**, cao tối thiểu 96 px, chữ 18 pt, rộng bằng nhau. Expedia và Traveloka thêm nút **In phiếu [nguồn] - 1 trang A4** phía trên, không thu nhỏ hai nút chính. Sao chép/in không xác nhận booking hay dừng âm báo; đóng thông báo mới xác nhận và dừng âm thanh. Vẫn giữ chuột phải và Ctrl+C để sao chép.
- Expedia in phiếu tóm tắt nội bộ thay vì cắt trang đầu email: đủ họ tên, điện thoại/email, mã và ngày đặt, ngày đến/đi, hạng phòng/số lượng, người lớn/trẻ em, mã xác nhận khách sạn, yêu cầu, khoản thu/thuế/phụ thu và các trường thẻ có trong email (chủ thẻ, số thẻ, CVV, hết hạn, ngày kích hoạt, địa chỉ). Booking nhiều nhóm phòng giữ khoản thu và thẻ từng nhóm, không cộng nhầm khoản toàn booking; không đoán phân bổ một thẻ cuối email khi email chưa ghi rõ. Không tự tạo thẻ/số lượng còn thiếu. Nếu gặp nhiều thẻ chưa phân bổ được hoặc phiếu không vừa một trang ở chữ tối thiểu 8 pt, báo lỗi để kiểm tra email gốc, không tự cắt mất nội dung.
- Nhấn In trong popup hoặc chuột phải đúng dòng Expedia/Traveloka → **In phiếu [nguồn] - 1 trang A4** → kiểm tra bản xem trước → **In 1 trang A4**, chọn máy in Windows. Chỉ gửi một trang khổ A4 dọc; hủy chọn máy in/đóng bản xem trước không đóng popup booking. Cửa sổ xem trước vẫn hoạt động khi màn hình chính ẩn khay. Agoda không có nút/mục in này.
- v1.7.18 thêm phiếu Traveloka dùng cùng cơ chế Expedia: đầy đủ thông tin khách có trong email, mã itinerary, ngày đặt/đến/đi, hạng và số phòng, khách/giường thêm/bữa sáng, yêu cầu/chính sách, giá theo ngày và khoản thực nhận. Thẻ ảo `Virtual Credit Card`, `Valid Until`, `CVC`, `VCC Amount` được đọc từ thư thanh toán Traveloka khớp đúng mã reservation/ngày đến; không ghép chỉ theo tên khách hoặc Payment ID. Nếu email không có thẻ, ghi rõ thiếu, không tự tạo số thẻ/CVV. Phiếu ghi rõ trạng thái đã thu khi email xác nhận `VCC has been charged`, không coi biên nhận là yêu cầu thu lần nữa. Thư `PAYMENT COMPLETED` không phát sinh thông báo booking mới.
- Không suy ra thẻ đã thu chỉ từ tiêu đề hoặc một phần câu: giữ nguyên các trạng thái chưa thu, đang chờ, thất bại và thu một phần. Nếu cùng số thẻ đầy đủ xuất hiện trong xác nhận và biên nhận đã thu, mọi khối thông tin của thẻ đó có cảnh báo đối chiếu để tránh thu trùng, vẫn giữ riêng CVV/ngày hết hạn/khoản tiền. Không ghép theo bốn số cuối của thẻ đã che; trường số thẻ trống không lấy nhầm thẻ của khối tiếp theo.
- Khi in, đọc email gốc theo đúng mã booking từ Inbox bằng tìm trên máy chủ: Expedia tải tối đa 3 thư khớp; Traveloka tải tối đa 6 thư khớp để ghép xác nhận và thanh toán. Kiểm tra nguồn/ngày đến/mã trước khi tạo phiếu, không ghép thẻ của booking khác hoặc đoán phân bổ khi thư có nhiều reservation chưa rõ. Không ảnh hưởng mốc quét thư, không đánh dấu đã đọc, không tạo booking/popup mới. Cần cấu hình IMAP đã lưu và email gốc còn trong Inbox.
- Parser p12 tách thư thanh toán Traveloka khỏi cảnh báo booking và đánh dấu đã xử lý để không tải lại vô hạn. Các trường thẻ là ranh giới tên khách/hạng phòng, không bị nối vào dữ liệu booking. Khi nâng cấp chỉ đọc lại tối đa 20 thư gần nhất một lần, giữ mốc UID và dấu đã đóng; không mở lại popup cũ.
- Thông tin thẻ/phiếu in chỉ xử lý trong bộ nhớ, không ghi vào lịch sử, log, clipboard Excel hoặc tệp tạm của ứng dụng. Email thật không đưa lên GitHub. **Phiếu có thẻ là tài liệu nội bộ, không giao khách; bảo quản/hủy giấy an toàn. Máy in/hàng đợi in hoặc máy in PDF có thể lưu bản in; người dùng cần bảo vệ/xóa dữ liệu đó.**
- Phát MP3 hoặc WAV như bản cũ; tệp âm thanh/thiết bị lỗi không đóng popup booking. Giờ yên lặng 00:00–08:00 giữ cảnh báo chờ và hiện sau 08:00 nếu bật.
- Âm thanh riêng theo nguồn: **Cài đặt → Cấu hình → Âm thanh theo nguồn booking**, chọn MP3/WAV cho Agoda, Expedia và Traveloka, nghe thử từng nguồn rồi **Lưu & khởi động**. Âm báo dùng file của nguồn booking đang hiện popup, lặp đến khi đóng thông báo; không lấy nhầm file của nguồn khác khi các booking nối tiếp nhau. Nhấn Sao chép không dừng âm báo, ẩn khay không ảnh hưởng phát âm thanh.
- v1.7.17 tích hợp nguyên bản ba file được cung cấp: **Agoda → 1-agoda.mp3**, **Expedia → 2-expedia.mp3**, **Traveloka → 3-traveloka.mp3**. MP3 đi kèm EXE; tự chép/kiểm tra SHA-256 ở `%APPDATA%\AgodaTodayNotifier\sounds`, không phụ thuộc thư mục Downloads hoặc thư mục tạm giải nén EXE. Lần đầu cập nhật này tự đổi ba lựa chọn nguồn sang đúng bộ MP3, giữ email/mật khẩu/cấu hình khác và đường dẫn âm chung cũ; lưu dấu `source_sound_pack` để những lựa chọn riêng sau đó không bị ghi đè khi khởi động/cập nhật.
- Mỗi MP3 có bản PCM WAV đi kèm, giải mã từ chính âm thanh đó. Nếu Windows thiếu bộ phát MP3/MCI, app thử đúng bản PCM cùng nguồn trước âm chung/chuông dự phòng, không chuyển sang âm của nguồn khác. Bộ kiểm thử giải mã cả ba MP3 độc lập không cần loa; kiểm tra MCI thực chỉ bỏ qua khi một WAV chuẩn cũng không khởi tạo được hạ tầng MCI trên máy kiểm thử.
- Thứ tự dự phòng: file riêng → MP3 tích hợp đúng nguồn → bản ghi âm PCM cùng nguồn → âm thanh chung cũ → chuông WAV riêng nguồn → chuông Windows khi thiết bị lỗi. Không chọn âm của nguồn khác. Tệp tích hợp bị hỏng được chép lại nguyên tử từ gói EXE; chuông WAV tổng hợp chỉ là dự phòng sau khi bản ghi âm thật không phát được.
- Nghe thử dùng lựa chọn đang nhập, không tự lưu, tự dừng sau 4 giây; nút Dừng nghe thử/đóng Cài đặt không dừng booking đang chờ. Không cho nghe thử khi có popup booking; nếu booking đến lúc đang nghe thử, âm báo booking được ưu tiên ngay. Hủy bộ hẹn giờ cũ khi chuyển âm thanh, không chồng nhiều vòng chuông. Đây là file phát trên loa máy tính; âm báo tích hợp F92 vẫn giữ cấu hình 1–4 riêng.
- Chép Excel đúng mẫu 1.5.5: STT trống | tên khách | số ngày đến | số ngày đi | số đêm | tiền dạng số | trống | trống | nguồn + hạng phòng + số lượng phòng đọc được. Dán bắt đầu từ cột A; không chèn mã booking hoặc tiêu đề email. Popup và lịch sử dùng chung format này.
- Giao diện desktop tối–vàng cao cấp; nhấp đúp, Ctrl+C hoặc chuột phải để sao chép một/nhiều booking sang Excel.
- Màn hình chính chỉ có bảng **Booking hôm nay** và nút bánh răng **Cài đặt**. Lưu & khởi động, quét thủ công, kiểm tra IMAP/cập nhật, Thoát, cấu hình và nhật ký đều nằm trong cửa sổ Cài đặt. Đóng Cài đặt không dừng theo dõi email hoặc mất nội dung đang nhập.
- Menu chuột phải giữ **Sao chép dòng đã chọn sang Excel**, không có Sao chép tất cả. Dòng Expedia/Traveloka thêm **In phiếu [nguồn] - 1 trang A4**, in đúng dòng vừa nhấn chuột phải, không lấy nhầm dòng khác trong lựa chọn nhiều dòng. Bảng lọc theo ngày hôm nay và tự đổi ngày; sao chép đúng dòng hiển thị sau khi lọc, không xóa lịch sử cũ.
- Bấm X hoặc thu nhỏ cửa sổ chính để ẩn xuống khay hệ thống Windows; ứng dụng vẫn quét email, phát âm thanh và hiện popup Agoda/Expedia/Traveloka riêng, không tự mở lại cửa sổ chính. Popup đang mở không bị đóng hay xác nhận khi ẩn ứng dụng. Bấm biểu tượng khay để mở lại; chuột phải có Mở Booking hôm nay, Cài đặt và Thoát ứng dụng. Nếu khay lỗi, giữ cửa sổ trên Taskbar để vẫn mở lại được.
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
- `booking_notifier/expedia_print.py`: dữ liệu in Expedia tạm trong bộ nhớ, đọc đúng email gốc và bộ dựng A4 Unicode một trang dùng chung.
- `booking_notifier/traveloka_print.py`: dữ liệu in Traveloka tạm trong bộ nhớ, đọc/ghép đúng email xác nhận và thẻ thu tiền; không thay đổi dữ liệu thông báo/lịch sử.
- `booking_notifier/windows_print.py`: chọn máy in Windows, ép A4 dọc/một bản, một trang GDI; hủy lệnh in khi có lỗi.
- `booking_notifier/ota_update.py`: xác minh chữ ký manifest, checksum và rollback.
- `booking_notifier/f92_device.py`: giao tiếp thiết bị F92.
- `tests/`: regression test cho các lỗi đã phát hiện ở v1.5.5.

## Bảo mật phát hành

Workflow release cần secret `OTA_SIGNING_PRIVATE_KEY`. Nếu có chứng thư code-signing tin cậy, cấu hình thêm `WINDOWS_CERTIFICATE_PFX_BASE64` và `WINDOWS_CERTIFICATE_PASSWORD` để ký Authenticode. Không commit private key hoặc file `.pfx` vào repository.

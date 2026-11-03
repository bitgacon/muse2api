# Studio 1.6.9 — sửa giao tin và nhận video

Đối chiếu Studio 1.6.8 `8f4c5f1` với upstream `1cd0168` (02/11/2026).

## Các sửa đã đưa vào

- Chờ React hydration, ô nhập được bật và workspace hết Connecting trước khi gửi. Lỗi kiểm tra readiness trước đây bị bỏ qua; page chưa ready cũng được dọn đúng cách.
- Chat và tạo video báo lỗi khi Still sending / Delivery not confirmed kéo dài, thay vì chờ hết timeout mà không có giải thích. Không tự gửi lại trong vòng polling chat.
- Chờ attachment video thật khi Muse thông báo đã tạo xong; chấp nhận video đến muộn 15/30/60 giây. Ảnh bìa không được tính là video.
- Giữ kiểm tra từ chối tạo media và phát hiện video chưa lập được thread sau một chu kỳ gửi nhưng trang im lặng kéo dài.
- Xác nhận VM RUNNING sau yêu cầu wake; HTTP 200 chỉ xác nhận đã nhận yêu cầu, không phải VM đã chạy.
- Engine, Store và extension chỉ yêu cầu hatch_sess. Cookie workspace như hatch_vml không còn chặn nhập tài khoản. Extension tiếng Việt 1.0.2 được đóng lại trong muse2api-extension.zip.
- Giữ luồng chat Studio có thread/tài khoản, reference, hàng đợi, GUI và xuất media. Đây là backport chọn lọc; không tuyên bố đã nhập toàn bộ upstream (proxy từng tài khoản, quota UI và chính sách nguồn ảnh không nằm trong bản sửa này).

## Cập nhật Windows

1. Chờ tác vụ đang chạy hoàn tất, tắt dịch vụ và GUI cũ.
2. Tải ZIP source nhánh main, giải nén vào thư mục mới. Giữ bản cũ để hoàn tác.
3. Chép .env và data từ bản cũ sang thư mục mới. Không đưa các tệp này lên GitHub.
4. Cài Python 3.11 hoặc 3.12 64-bit và Chrome/Edge nếu chưa có.
5. Chạy 01_CAI_DAT.bat, sau đó 05_MO_GUI.bat hoặc 02_CHAY_MUSE2API.bat. Giữ cổng trong .env, ví dụ 19210.
6. Thay extension bằng bản 1.0.2 trong ZIP đi kèm, hoặc Reload extension unpacked từ thư mục muse2api-extension/muse2api-extension.
7. Mở Muse trên trình duyệt, kiểm tra chat website hoạt động, rồi nhập lại cookie nếu phiên cũ không dùng được. Kiểm tra 1 chat ngắn và 1 video trước khi chạy hàng loạt.

EXE portable 1.6.8 chứa Python engine đã đóng gói. File .py ngoài EXE không thay thế engine đó. Bản portable 1.6.9 đã được build và phát hành tại https://github.com/bitgacon/muse2api/releases/tag/v1.6.9. Dùng ZIP đầy đủ, giải nén vào thư mục mới rồi chuyển .env/data sau khi tắt bản cũ. Không cần cài Python cho portable. BUILD.txt và scripts/build_windows.py mô tả quy trình build.

## Kiểm tra

Kiểm tra offline bằng engine/DOM giả lập: readiness, dọn page thất bại, video đến muộn 31 tình huống, chat chờ video 15/30/60s, giao tin chưa xác nhận, yêu cầu cookie và ZIP extension. Kiểm tra hồi quy API ảnh, chat theo thread/tài khoản, reference/ordering và chuỗi 20 cảnh.

Không có tài khoản Muse thật trong phiên sửa: chưa xác nhận end-to-end với website/tài khoản của người dùng. EXE đã qua kiểm tra GUI/API khởi động và dừng trên Windows GitHub Actions (run 41607647995); báo cáo QA_PORTABLE_1.6.9.json và SHA256SUMS.txt có trong Release. Kiểm tra browser/Muse có tài khoản thật vẫn chưa được thực hiện. Các sửa dựa trên lỗi code tái hiện được và regression upstream; chưa thể khẳng định mọi nguyên nhân trên máy người dùng đã được loại trừ.

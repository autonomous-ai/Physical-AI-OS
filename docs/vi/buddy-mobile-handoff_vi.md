# Prompt triển khai Buddy MQTT cho mobile

Thực hiện trong `/Users/macbookpro/IdeaProjects/ecm-sds-mobile`. Chỉ sửa mobile;
không sửa BFF. Đọc và tuân thủ `CLAUDE.md`/`AGENTS.md` của repo trước khi sửa;
dùng FVM cho Flutter validation và cập nhật docs theo quy định repo. Không tự
commit hoặc push. Giữ nguyên thay đổi không thuộc task.

Mục tiêu: màn hình Autonomous OS biết Mac đã pair/revoke và đang kết nối hay
không qua MQTT trực tiếp. Bắt đầu từ `_BuddyPairRow` trong
`lib/src/features/device/ui/presentation/pages/autonomous_os_detail_page_record_tab.dart`.
Luồng `startBuddyPairing` hiện gọi qua BFF: thay transport cho Buddy bằng MQTT
trực tiếp, giữ nguyên transport của tính năng khác. Repo đã khai báo
`mqtt_client`; dùng thông tin từ `DeviceEntity`: `mqttServer`, `mqttPort`,
`mqttUsr`, `mqttPwd`, `faChannel`, `fdChannel`. Kiểm tra kiểu/nullability và luồng
lấy dữ liệu thực tế trước khi triển khai. Không hardcode broker hoặc topic.

## Contract device đã cung cấp

Publish JSON lên `faChannel`, QoS 1, không retain:

```json
{"cmd":"data","kind":"buddy.status","data":{}}
{"cmd":"data","kind":"buddy.pair.start","data":{}}
{"cmd":"data","kind":"buddy.pair.revoke","data":{}}
```

Nhận trên `fdChannel`; envelope có thể kèm metadata device/version/id/mac/time,
client cần bỏ qua field không biết. Ba phản hồi thành công:

```json
{"type":"data","kind":"buddy.status","status":"success","data":{"paired":true,"connected":false,"instance_id":"service-instance-id","revision":1,"buddy_id":"buddy-id","name":"Leo’s Mac","os_version":"15.0","paired_at":"2026-09-08T10:00:00Z"}}
{"type":"data","kind":"buddy.pair.start","status":"success","data":{"code":"123456","expires_in":60}}
{"type":"data","kind":"buddy.pair.revoke","status":"success","data":{"revoked":true}}
```

Chưa pair:

```json
{"type":"data","kind":"buddy.status","status":"success","data":{"paired":false,"connected":false,"instance_id":"service-instance-id","revision":2}}
```

Lỗi có `status:"failure"` và `error`, không được coi là chưa pair. `data` của
request tùy chọn và được bỏ qua. Không có request ID/correlation ID; serialize
request cùng kind và disable thao tác trùng khi đang pending. Không tự retry
`buddy.pair.start` do mỗi lần cấp mã sẽ thay mã cũ. Không coi PUBACK là phản hồi
ứng dụng hay xác nhận đã pair.

Device tự gửi `buddy.status` cùng format khi khởi động, confirm HTTP thành công,
revoke HTTP/MQTT/Buddy tự revoke, connect/disconnect WebSocket hiện tại.
`paired` là pairing đã lưu, `connected` là kết nối WebSocket của Mac; offline
không có nghĩa đã revoke. Các field Mac bị bỏ khi chưa pair; chuỗi tùy chọn rỗng
cũng bị bỏ. `paired_at` dạng RFC3339. Không có token/code/fingerprint trong status.

`revision` là uint64, bắt đầu 0 và tăng theo thao tác pair/revoke/connect/current-
disconnect thành công; query không tăng. `instance_id` đổi mỗi lần service
restart. Chỉ so revision trong cùng instance, bỏ duplicate/revision thấp hơn;
instance mới reset thứ tự. Status query và event dùng chung reducer. QoS 1 có
thể trùng message; event có thể đến trước response của lệnh. Queue device có giới
hạn, gộp thành snapshot mới nhất; lỗi publish log/bỏ. Event không retain và không
bảo đảm replay mọi thay đổi, nên query là bắt buộc khi đồng bộ lại.

## Triển khai mobile

1. Tạo transport/provider MQTT theo device, client ID riêng duy nhất cho app và
   phiên kết nối, không dùng `device-<id>`. Dùng TLS theo cấu hình broker thật;
   không tắt kiểm tra chứng chỉ. Không log username/password hoặc payload mã pair.
   Kiểm tra thiếu cấu hình và hiển thị lỗi có thể retry. Broker ACL phải cho phép
   publish FA/subscribe FD đúng device; cần kiểm chứng mạng Wi-Fi và cellular.
2. Gắn listener trước, subscribe FD, chờ SUBACK thành công rồi query status.
   Lọc topic, `type`, `kind`, parse JSON an toàn; bỏ message không liên quan hoặc
   malformed. Không suy luận `paired:false` từ timeout/offline.
3. Khi vào màn hình, reconnect hoặc resume, subscribe lại nếu cần và query
   `buddy.status`. Dùng timeout hữu hạn cho connect/SUBACK/response; cung cấp
   retry và trạng thái loading/error. Hủy timer, listener và subscription khi
   dispose/chuyển device; không đóng kết nối đang được consumer khác sử dụng.
4. Pair: gửi start, hiển thị mã dạng chuỗi giữ số 0 đầu và countdown theo
   `expires_in`. Thành công start chỉ là cấp mã. Khi status báo paired, dừng
   countdown, ẩn mã và hiện tên Mac, trạng thái kết nối, nút Revoke. Nếu trước đó
   đã pair và người dùng chủ động cấp mã thay Mac, không nhầm snapshot pairing
   cũ là xác nhận mã mới; theo dõi revision/buddy_id/paired_at làm baseline.
   Hết hạn thì bỏ mã, query lại và cho phép cấp mã mới; không tự cấp vòng lặp.
5. Revoke: gửi revoke; không giả định thứ tự response và status. Sau success,
   query status nếu cần; chỉ dùng snapshot có revision hợp lệ để cập nhật UI,
   tránh response/query cũ ghi đè event mới. Revoke không hủy mã pair đang chờ.
   `connected:false,paired:true` vẫn hiện đã pair với nhãn Mac đang offline.
6. Chỉ cần hoạt động foreground. Không thêm FCM/APNs hoặc hứa thông báo khi app
   bị đóng. API lấy thông tin device vẫn có thể dùng backend hiện có; riêng các
   thao tác Buddy không cần thay đổi hoặc trung chuyển qua BFF.

## Kiểm chứng và bàn giao

Viết test có ý nghĩa cho parser/reducer/provider và UI: status paired/unpaired,
Mac offline vẫn paired, event đến trước ack, duplicate/revision cũ, instance
restart, reconnect/resume query, timeout/failure/malformed/unrelated message,
countdown hết hạn và cleanup khi chuyển device/dispose. Kiểm chứng start → Mac
confirm → status → revoke → unpaired nếu có device/broker thật được cấp quyền.
Chạy analyze/test/build bằng FVM theo `CLAUDE.md`, báo chính xác lệnh và giới hạn.
Không tự SSH/restart/deploy device. ACL và reachability của credentials hiện tại
chưa được kiểm chứng trên device thật trong thay đổi phía OS.

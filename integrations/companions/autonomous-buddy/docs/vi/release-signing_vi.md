# Autonomous Buddy — sign + notarize cho production

> **Sản phẩm hợp nhất (tháng 9/2026):** Chạy lệnh build/ký số từ `integrations/companions/autonomous-buddy/`. `make build`, `app` và `install` đóng gói một app Electron chứa helper Swift, mặc định theo kiến trúc Node hiện tại; dùng `make build BUDDY_ARCH=x64` cho Intel. `make dmg`, `dmg-signed` và `notarize` mặc định dùng `BUDDY_ARCHS="arm64 x64"`, tạo hoặc xử lý tuần tự hai DMG riêng cho Apple Silicon và Intel. Đây không phải universal binary. App output: `desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app`; DMG: `dist/Autonomous-Buddy-<version>-<arch>.dmg`. Recipe release universal chỉ Swift cũ giữ dưới tiền tố `native-*` và vẫn dùng tên `AutonomousBuddy-<version>.dmg`. `make build`, `make install` và `make dmg` không gửi lên Apple; `make dmg` dùng đóng gói local, còn target upload mặc định dùng artifact release đã notarize. Cross-compile chưa chứng minh tương thích runtime Intel; vẫn cần kiểm tra mở app, terminal/helper native và quyền trên máy Mac đích.

## Cua Driver đóng gói cùng Buddy

App hợp nhất đóng gói Cua Driver chính thức **0.28.2** trong `Contents/Helpers/CuaDriver.app`, nên người dùng chỉ cài Buddy. Build tải release upstream cố định và kiểm tra checksum SHA-256 trước khi đưa vào staging; không commit binary driver. Giữ chữ ký nhà phát hành của app con thay vì ký lại bằng identity Buddy. Kiểm tra chữ ký và khả năng hỗ trợ kiến trúc đã chọn trước khi ký app Buddy ngoài cùng. ZIP updater chứa cùng driver với DMG. Các target `native-*` chỉ Swift cũ vẫn là luồng phát triển riêng.

Khi chạy, Buddy chỉ khởi động driver nhúng lúc cần và quản lý vòng đời của nó; người dùng không chạy `cua-driver serve` hay cài app thứ hai. Chế độ nhúng dùng danh tính quyền macOS của Buddy. Kiểm tra Accessibility, Screen Recording, nút **Restart computer use** hiện có sau khi đổi quyền, Pause/hủy và việc dừng process con từ app đã đóng gói trên từng kiến trúc đích. Quyền cấp cho CuaDriver độc lập hoặc build local thành công không chứng minh Buddy đóng gói có quyền, đã notarize hay được Gatekeeper chấp nhận. Kiểm tra bản phân phối cuối cùng theo các bước bên dưới.

Quyền Screen Recording của bản ad-hoc cũ có thể vẫn hiện bật nhưng macOS từ chối chữ ký hiện tại. Nếu TCC báo `Failed to match existing code requirement`, xóa mục Buddy cũ trong Screen & System Audio Recording, thêm lại `/Applications/Autonomous Buddy.app`, rồi khởi động lại Buddy. Installer không reset hoặc sửa quyền riêng tư macOS.

## Upload và metadata OTA theo kiến trúc

Từ thư mục gốc repo, `make upload-autonomous-buddy` tăng patch version chung trong `VERSION_AUTONOMOUS_BUDDY` đúng một lần, build, ký số, notarize và staple cả hai DMG, kiểm tra ticket và Gatekeeper chấp nhận rồi upload từng file lên `${BUCKET_PREFIX}/ota/autonomous-buddy/<arch>/<version>.dmg` (thông thường là `os/ota/autonomous-buddy/...`). Mỗi mục metadata được chọn, `autonomous-buddy.arm64` hoặc `autonomous-buddy.x64`, có `version`, `url`, `sha256` và `updated_at` riêng. Release một kiến trúc giữ nguyên version của kiến trúc còn lại và mọi component khác. Khi chuyển metadata phẳng cũ, publisher giữ mục cũ dưới `arm64` nếu chưa có mục này, vì DMG đã phát hành trước đó chỉ dành cho Apple Silicon. Vì vậy release mới chỉ Intel vẫn giữ link tải Apple Silicon cũ, cùng checksum nếu trước đó có. Sau đó cập nhật các kiến trúc được chọn và xóa các trường version/URL Buddy cấp trên cũ vốn không phân biệt kiến trúc. Bên tải thủ công chọn kiến trúc tương ứng. Release đã ký còn xuất bản ZIP và feed Squirrel cho [updater trong app](app-updates_vi.md); metadata chung vẫn là danh mục tải DMG.

```bash
# Dùng profile notarytool có sẵn trong Keychain (xem setup bên dưới).
export NOTARY_PROFILE=autonomous-notary
# Cả Apple Silicon và Intel; tăng version một lần, ký số và notarize.
make upload-autonomous-buddy
# Chỉ release Intel, giữ nguyên mục metadata Apple Silicon.
BUDDY_ARCHS=x64 make upload-autonomous-buddy
# Thử lại bằng hai DMG đã notarize; không tăng version, build lại hay cần profile.
BUDDY_SKIP_BUILD=1 make upload-autonomous-buddy
# Thử lại release chỉ Intel.
BUDDY_SKIP_BUILD=1 BUDDY_ARCHS=x64 make upload-autonomous-buddy
```

Giữ nguyên các kiến trúc đã chọn và `BUDDY_DMG_TARGET` khi thử lại. Mặc định `BUDDY_DMG_TARGET=dmg-signed` yêu cầu `NOTARY_PROFILE` và kiểm tra credential bằng `notarytool history` trước khi tăng version hoặc build. `BUDDY_SKIP_BUILD=1` dùng lại DMG đã notarize mà không tăng version, build lại, gửi notarize hay cần profile; bước staple app có thể lấy ticket đã có. Trước mọi upload, tất cả DMG được chọn phải tồn tại và đạt kiểm tra version, kiến trúc, chữ ký, ticket và Gatekeeper; ZIP updater cũng phải đạt kiểm tra.

`BUDDY_DMG_TARGET=dmg` là override tường minh cho local/test: kiểm tra version, kiến trúc và chữ ký app, chỉ xuất bản metadata DMG thủ công, giữ nguyên feed updater. Chỉ chấp nhận hai target này; không nhận artifact standalone `native-*`. Override `GCS_PATH` hoặc `BUDDY_URL` chỉ dùng cho một kiến trúc và phải có đuôi `.dmg`; URL phải dùng HTTPS. ZIP và `latest.json` nằm cùng thư mục đích với DMG.

Nếu đọc metadata hiện tại lỗi, script dừng xuất bản metadata thay vì thay feed chung. Cập nhật metadata chung đã ký cần `OTA_SIGNING_PRIVATE_KEY`. Artifact có thể đã upload khi bước xuất bản metadata thất bại.

Trước khi ký, bước đóng gói thêm quyền ghi cho owner của file và thư mục trong bundle, giữ nguyên bit thực thi và không đi theo symlink. Squirrel cần quyền này để cài: privacy manifest của SwiftPM có thể giữ mode `0444`, khiến thay thế app thất bại. Kiểm tra ZIP release từ chối resource chỉ đọc, kể cả khi thử lại với `BUDDY_SKIP_BUILD=1`; cần build, ký và notarize lại app thay vì đổi quyền trong release đã ký.

### ZIP và feed tự cập nhật

Upload bản ký trích xuất đúng app trong từng DMG đã notarize, staple app, rồi kiểm tra chữ ký Developer ID, ticket và Gatekeeper. Cả hai version plist (`CFBundleShortVersionString`, `CFBundleVersion`) và `Contents/Resources/app/package.json` phải khớp `VERSION_AUTONOMOUS_BUDDY`; binary Electron, helper Swift và node-pty phải khớp kiến trúc đã chọn; CuaDriver nhúng phải hỗ trợ kiến trúc đó và giữ chữ ký nhà phát hành đã được kiểm tra. `ditto` tạo `dist/Autonomous-Buddy-<version>-<arch>.zip`, giữ tên app bundle; bản giải nén mới được kiểm tra lại. Để chỉ tạo ZIP này từ DMG đã notarize, chạy `make update-zip BUDDY_ARCH=arm64` trong thư mục Buddy. Không build lại app.

Thứ tự upload là `<version>.dmg` rồi `<version>.zip` của từng kiến trúc, tiếp đến `metadata.json` chung, cuối cùng là `latest.json` của từng kiến trúc. Đường dẫn ZIP mặc định là `${BUCKET_PREFIX}/ota/autonomous-buddy/<arch>/<version>.zip`. Feed Squirrel tĩnh chứa:

```json
{"currentRelease":"<version>","releases":[{"version":"<version>","updateTo":{"version":"<version>","name":"<release name>","url":"<HTTPS ZIP URL>","pub_date":"<timestamp>","sha256":"<ZIP SHA256>","size":12345}}]}
```

Squirrel dùng feed ZIP; DMG tiếp tục dùng cho cài lần đầu và khôi phục thủ công. Buddy 0.0.21 trở xuống cần cài thủ công một lần để có updater. Thay đổi mã nguồn này không xuất bản release hay tăng version.

Doc handover cho dev sẽ làm Apple Developer enrolment. Sau khi setup 1 lần xong, mỗi release chỉ cần:

```bash
cd integrations/companions/autonomous-buddy
export DEV_ID_APP="Developer ID Application: <Your Org> (<TEAMID>)"
export NOTARY_PROFILE=autonomous-notary
make dmg-signed
```

Mỗi output `dist/Autonomous-Buddy-<version>-<arch>.dmg` được sign + notarize + staple — user mount, drag app vào Applications, double-click, macOS mở luôn không có cảnh báo Gatekeeper hay phải right-click → Open.

Packager Electron hợp nhất chọn signing identity trước khi build. `DEV_ID_APP` khác rỗng được ưu tiên (tên certificate hoặc fingerprint SHA-1). Nếu không có, packager đọc identity dùng được bằng `security find-identity -v -p codesigning`, chỉ nhận dòng `Developer ID Application:` hoàn chỉnh, bỏ Apple Development và dòng bị revoked/expired. Một ứng viên duy nhất được chọn tự động. Khi có nhiều ứng viên, ưu tiên team của `/Applications/Autonomous Buddy.app`; nhiều kết quả vẫn mơ hồ hoặc đã biết team đang cài nhưng không có ứng viên khớp sẽ báo lỗi kèm cách đặt `DEV_ID_APP`, không âm thầm đổi team. Không hardcode certificate hoặc cá nhân.

Khi không có identity Developer ID dùng được, packaging giữ fallback ad-hoc và cảnh báo rõ Accessibility/Screen Recording có thể cần cấp lại sau khi cài. Tra cứu identity thất bại sẽ dừng, không âm thầm hạ xuống ad-hoc. Biến rỗng bật tự dò; `DEV_ID_APP=-` chủ động chọn ad-hoc cho packager hợp nhất. Helper nhúng và app ngoài cùng dùng identity đã chọn. Vì vậy build thông thường không còn âm thầm thay bản Developer ID bằng ad-hoc chỉ do shell chưa export `DEV_ID_APP`. Điều này ổn định signing identity, không hứa tự khôi phục quyền TCC đã mất hiệu lực từ lần cài trước.

Recipe Makefile `native-*` cũ giữ quy tắc dò identity và override hiện có. Với release có nhiều certificate, cần export tường minh identity mong muốn để mọi đường packaging/signing dùng cùng certificate.

Thứ `make dmg-signed` thêm so với `make dmg` là notarize, staple và kiểm tra Gatekeeper, cần `NOTARY_PROFILE`. Apple hướng dẫn notarize container phân phối ngoài cùng: gửi từng DMG cuối cùng bao gồm app và binary bên trong. ZIP updater được tạo từ đúng app đó sau khi staple và kiểm tra ở cấp app; không cần gửi ZIP riêng. Xem [Apple: Packaging Mac software for distribution](https://developer.apple.com/documentation/xcode/packaging-mac-software-for-distribution).

## Khác biệt vs build ad-hoc

| | Ad-hoc (máy chưa cài cert) | Production (`make dmg-signed`) |
|---|---|---|
| Signing identity | Không (`-` placeholder) | Developer ID Application cert từ Apple |
| Hardened runtime | Off | **On** (`--options runtime`, Apple bắt buộc) |
| Secure timestamp | Không | **Có** (`--timestamp`) |
| Apple notarize | Không | **Có** (notarytool submit + wait) |
| Stapler ticket | Không | **Có** (`stapler staple`) — verify offline được |
| UX lần đầu mở | Right-click → Open Anyway | Double-click bình thường |
| TCC reset khi rebuild | Mỗi build (cdhash đổi) | Ổn định qua các bản (cert identifier ổn định) |
| User permission grants | Re-grant Accessibility + Screen Recording mỗi release | Grant 1 lần, giữ qua các release |

TCC ổn định là win lớn nhất — không có nó, mỗi lần tester nhận build mới phải bấm lại ~3 dialog permission.

## Setup 1 lần (dev giữ cert làm)

### 1. Đăng ký Apple Developer Program

`https://developer.apple.com/programs/enroll/` — $99/năm. Cá nhân hay tổ chức đều OK; tổ chức tốt hơn vì cert không bị buộc vào 1 Apple ID cụ thể.

Sau khi enrol xong, ghi nhớ **Team ID** (chuỗi 10 ký tự alphanumeric, ví dụ `ABCDE12345`) — xem ở header trang Apple Developer account, cần dùng bên dưới.

### 2. Tạo cert Developer ID Application

Cách thẳng nhất là qua Xcode (download Xcode nếu chưa có):

1. Xcode → **Settings → Accounts** → "+" → đăng nhập với Apple ID đã enrol.
2. Chọn team → **Manage Certificates…**.
3. "+" → **Developer ID Application**. Xcode tự gen CSR + download `.cer` + cài private key vào login Keychain trong 1 bước.

Đường thủ công (không có Xcode) — chỉ khi không dùng được Xcode:

1. Keychain Access → **Certificate Assistant → Request a Certificate From a Certificate Authority…** → lưu CSR.
2. `https://developer.apple.com/account/resources/certificates` → "+" → **Developer ID Application** → upload CSR → download `.cer`.
3. Double-click `.cer` → cài vào login Keychain; private key đã tạo cùng CSR ở bước 1.

Verify cert dùng được:

```bash
security find-identity -v -p codesigning
```

Tìm dòng `Developer ID Application: <Your Org> (<TEAMID>)`. Chuỗi đầy đủ trong dấu nháy là cái dùng làm `DEV_ID_APP` khi gọi `make`.

### 3. Setup credentials cho notarytool

Notarize chạy với **app-specific password** do Apple cấp, không phải password iCloud. Tạo 1 lần:

1. `https://account.apple.com/account/manage` → Sign-In and Security → **App-Specific Passwords** → Generate (đặt nhãn `autonomous-buddy-notary` hoặc tương tự).
2. Copy password (định dạng `xxxx-xxxx-xxxx-xxxx`).
3. Lưu bộ 3 (Apple ID, Team ID, app-specific password) vào Keychain để `notarytool` đọc không phải prompt:

```bash
xcrun notarytool store-credentials autonomous-notary \
  --apple-id "apple-id-cua-ban@example.com" \
  --team-id "ABCDE12345" \
  --password "xxxx-xxxx-xxxx-xxxx"
```

`autonomous-notary` là alias profile — pass nó qua `NOTARY_PROFILE` khi `make`. Đặt tên gì cũng được, miễn nhất quán.

Smoke test:

```bash
xcrun notarytool history --keychain-profile autonomous-notary
```

History rỗng OK — nghĩa là auth chạy đúng.

## Flow mỗi release

```bash
cd integrations/companions/autonomous-buddy

# Lưu vào shell rc 1 lần, hoặc export mỗi session.
export DEV_ID_APP="Developer ID Application: Autonomous Inc (ABCDE12345)"
export NOTARY_PROFILE=autonomous-notary

# VERSION đọc từ VERSION_AUTONOMOUS_BUDDY; target upload tăng đúng một lần.

make dmg-signed
# Chỉ Intel: make dmg-signed BUDDY_ARCHS=x64
```

Với từng kiến trúc được chọn, make target chạy theo thứ tự:

1. Compile Electron main/renderer. Rebuild node-pty cho kiến trúc Electron được chọn trong bản sao dependency ở staging, giữ nguyên dependency dùng phát triển.
2. Cross-compile helper Swift release cho kiến trúc được chọn (`x64` ánh xạ sang Swift `x86_64`; Apple Silicon dùng `arm64`).
3. Đóng gói `desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app`, nhúng helper và resource SwiftPM trong `Contents/Resources/native/`, cùng app CuaDriver đã kiểm tra trong `Contents/Helpers/`.
4. `codesign` app với Developer ID, hardened runtime, secure timestamp. Packaging kiểm tra chữ ký và dùng `lipo` kiểm tra kiến trúc đích của Electron, helper Swift, node-pty và spawn helper của nó, cùng khả năng hỗ trợ kiến trúc và chữ ký nhà phát hành của CuaDriver nhúng.
5. `hdiutil create` DMG (layout drag-to-Applications).
6. `codesign` DMG với Developer ID.
7. `xcrun notarytool submit … --wait` — upload lên Apple, block 1-5 phút chờ verdict.
8. `xcrun stapler staple` — embed ticket notarize vào DMG để Gatekeeper verify offline.
9. `xcrun stapler validate` — kiểm tra ticket đã nhúng trong DMG, sau đó `spctl --assess --type open --context context:primary-signature` kiểm tra Gatekeeper chấp nhận. Chạy riêng các kiểm tra app còn lại bên dưới trước khi phân phối.

Output mặc định là `dist/Autonomous-Buddy-<version>-arm64.dmg` và `dist/Autonomous-Buddy-<version>-x64.dmg`. Phân phối file khớp với máy Mac của người dùng. `make dmg BUDDY_ARCHS=x64` chỉ build Intel; `make notarize BUDDY_ARCHS=x64` chỉ notarize DMG Intel đã có.

## Verify build trước khi ship

```bash
# 1. Chữ ký app hợp lệ.
codesign --verify --deep --strict --verbose=2 "desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app"

# 2. Gatekeeper accept app.
spctl --assess --type execute --verbose=4 "desktop/artifacts/package/Autonomous Buddy-darwin-<arch>/Autonomous Buddy.app"
#   expected: "accepted source=Developer ID notarized"

# 3. DMG có ticket được staple.
xcrun stapler validate "dist/Autonomous-Buddy-<version>-<arch>.dmg"
#   expected: "The validate action worked!"

# 4. Gatekeeper dry-run thật trên DMG.
spctl --assess --type open --context context:primary-signature --verbose=4 "dist/Autonomous-Buddy-<version>-<arch>.dmg"
#   expected: "accepted source=Notarized Developer ID"
```

Chạy cả bốn kiểm tra cho từng kiến trúc trước khi phân phối. Cần kiểm tra thêm mở app và tính năng native trên Mac Intel và Mac Apple Silicon; cross-compile và kiểm tra chữ ký không thay thế kiểm tra runtime này.

## Lỗi hay gặp

**`errSecInternalComponent` khi codesign.** Private key thiếu hoặc bị lock. Mở Keychain Access → login → tìm "Developer ID Application" → confirm có private key sibling. Nếu chỉ có cert, bạn import `.cer` ở máy khác máy gen CSR — làm lại bước 2 trên máy này.

**`Hardened Runtime is not enabled` trong notarytool log.** Thiếu flag `--options runtime`. Makefile target đã set; nếu bạn chạy `codesign` thủ công, re-sign với `--options runtime`.

**`The signature does not include a secure timestamp`.** Thiếu flag `--timestamp`. Tương tự — Makefile có sẵn; codesign thủ công cần thêm.

**Notarize status `Invalid` log có `disallowed-entitlement`.** Bạn dùng entitlement Apple không cho phép với Developer ID distribution. Buddy hiện không set entitlement nên không nên gặp. Nếu gặp, lấy full log:

```bash
xcrun notarytool log <submission-id> --keychain-profile autonomous-notary
```

…và check entitlement nào bị reject.

**Notarize status `Accepted` nhưng Gatekeeper vẫn cảnh báo trên máy user.** DMG chưa staple. Hoặc chạy lại `make notarize` trên DMG hiện có (re-staple), hoặc build lại với `make dmg-signed`.

**User báo "app is damaged".** Thường là quarantine xattr đã set mà staple ticket thiếu/invalid. Bảo user chạy `xattr -d com.apple.quarantine /Applications/AutonomousBuddy.app` 1 lần; fix triệt để là ship DMG đã staple.

## Khi nào cần re-notarize

Phải notarize lại mọi lần binary .app hoặc DMG thay đổi — tức là mỗi release. Notarize bind với chính xác bits đó; không thể "chuyển" ticket từ build này sang build khác. Makefile xử lý tự động qua full chain.

Staple offline-capable nên user cài lần đầu khi offline vẫn được trải nghiệm không cảnh báo miễn DMG có ticket nhúng sẵn.

## Phần doc này CỐ Ý KHÔNG cover

- **Phát hành qua Mac App Store.** Cert khác (`Apple Distribution`), App Sandbox bắt buộc, flow submission riêng qua App Store Connect. Out of scope.
- **Cập nhật app.** App Electron hợp nhất dùng Squirrel.Mac với feed ZIP đã ký; xem [cập nhật app](app-updates_vi.md). App native-only cũ chưa có updater.
- **CI signing.** Làm được (GitHub Actions với cert + notarytool keychain profile encrypt làm secrets), nhưng handoff hiện tại giả định 1 dev sign local. Setup CI khi build cadence justify.

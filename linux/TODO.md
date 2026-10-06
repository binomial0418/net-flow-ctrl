# TODO

## 時間快到／時間到的提示

> **已實作（2026-10-06）**，使用方式見 [README.md](README.md#時間提醒通知)。以下保留當時的調查與實測紀錄。尚未實測：音效、播放中送通知是否會卡住。

**目標**：在使用者的螢幕上提示「時間快到了」與「時間到了」。主要裝置是 Google TV（電視內建與外接電視盒），手機較少。

### 做法：電視裝通知 App，由 VM 直接推播

在 Google TV 安裝「Notifications for Android TV」這類 App（Home Assistant 的 Android TV 通知整合用的就是它），並允許「顯示在其他應用程式上層」。App 會在電視上開一個區網連接埠，收到訊息就在畫面角落顯示浮動通知，**不中斷正在播放的影片**。

電視都在 VM 的網段（192.168.50.x），VM 直接送 HTTP 即可，不經 Home Assistant。電視時間到被斷外網後，VM → 電視是區網連線，一樣送得到。

### 預定行為

| 時機 | 訊息範例 |
|---|---|
| 剩 10 分鐘 | 「客廳電視今天還可以用 10 分鐘」 |
| 剩 1 分鐘（可選） | 再提醒一次 |
| 時間到 | 「今天的使用時間已用完」／「已超出可用時段」 |
| 只限 YouTube 的裝置時間到 | 「YouTube 已暫停，其他 App 可以繼續使用」 |

- 每台裝置可個別開關通知、設定提醒分鐘數。
- 「剩餘時間」的計算：
  - 時數上限依**實際使用**計時，剩 10 分鐘＝再使用 10 分鐘，閒置時不倒數。
  - 時段限制依時鐘，例如 21:00 結束就在 20:50 提醒。
  - 兩者取先到者；有本日延長時以延長到的時間為準。
- 每個提醒在同一個邏輯日只送一次，重開機不重送（記錄在狀態檔）。

### 已驗證：TvOverlay（2026-10-06）

在客廳電視（192.168.50.194）安裝 **TvOverlay**（Play 商店，`com.tabdeveloper.tvoverlay`），從 VM 送出測試通知，**畫面上可以正常顯示**：

- 電視上監聽 **5001** 埠，`POST /notify`，JSON 欄位：`title`、`message`、`source`、`duration`（秒），另有 `image`、`video`、位置等（[GitHub](https://github.com/gugutab/TvOverlay)、[欄位定義](https://github.com/gugutab/TvOverlay/blob/main/json/notification.json)）。
- 回應 `{"success":true,"message":"Notification received"}`。

Notifications for Android TV 在台灣區 Play 商店找不到；Android TV Notifier 主要用來從手機轉送通知，沒有公開 API，不採用。

**尚待確認**：音效（文件沒寫，可試 `video` 欄位帶有聲短片）、中文顯示、播放影片時送通知是否會中斷播放。

**字體太小的解法（已驗證可行）**：TvOverlay 的 API 與內建版面（預設、極簡、只顯示圖示）都不能調字體大小。改由 VM 用 Pillow 把提醒文字以大字畫成 PNG（例如 960×300、主文字 110 px 粗體），以 Base64 放進 `image` 欄位送出，電視上顯示清楚。
- VM 需要套件 `python3-pil`、`fonts-noto-cjk`（已手動安裝，實作時要加進 `deploy/install.sh`）。
- 字型：`/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc`，`index=2` 是繁體中文。

**通知上方的小字無法完全拿掉（2026-10-06 實測）**：
- 不帶標題、內容，只送圖片：上方仍有一行「REST API」。
- `source` 設成零寬字元：仍顯示「REST API」；設成「上網提醒」：顯示「上網提醒 REST API」。「REST API」是 TvOverlay 固定加上的（通知經由 API 送入），從外部無法移除。
- 版面「Minimalist」：只有圖片的通知完全不顯示。版面「Icon Only」：圖片要放在 `largeIcon` 才顯示，但「REST API」仍在。
- 目前維持「Default」版面＋原本的格式（標題、內容、圖片）。
- 之後可再嘗試：TvOverlay 付費版自訂版面（官方稱可自行設計版面），或改用 PiPup（側載 APK）。

**TvOverlay 預設會在右上角常駐一個時鐘**：用 `POST :5001/set/overlay`、`{"clockOverlayVisibility": 0}` 關掉（已在客廳電視關閉）。實作時，每台電視啟用提醒時自動送一次，不用手動設定。通知預設出現在右上角（`hotCorner: top_end`）。

### 候選 App 與音效（調查時的紀錄）

| App | 音效 | 安裝 | 說明 |
|---|---|---|---|
| Notifications for Android TV | 查不到音效設定，要實測是否有系統提示音 | Play 商店 | Home Assistant 文件只列出位置、顏色、透明度、顯示時間等視覺設定（[HA 文件](https://www.home-assistant.io/integrations/nfandroidtv/)） |
| PiPup | 可以：通知可放影片，預設靜音，可指定播放聲音 | 側載 APK | 開源，在電視上開 7979 埠，`POST /notify`（JSON）（[GitHub](https://www.github.com/rogro82/PiPup)） |
| PiPup 改版 | 可以，含語音朗讀（TTS） | 側載 APK | 另有按鈕、螢幕控制（[HA 社群](https://community.home-assistant.io/t/pipup-fork-home-assistant-integration-popups-tts-buttons-screen-control-on-android-tv-fire-tv/1022171)） |

需要聲音的話傾向 PiPup：
- **提示音**：通知內放一段幾秒、有聲音的短影片（例如「叮咚」），並指定播放聲音。
- **語音朗讀**：用 PiPup 改版直接念出「客廳電視還剩 10 分鐘」。

**風險**：資料提到，某些裝置上通知播放聲音會讓**正在播的節目卡住**，這也是 PiPup 預設靜音的原因，必須實測。

### 先實測再實作

1. 在一台 Google TV 上安裝 Notifications for Android TV 和 PiPup（可以的話加上改版），兩者都開好「顯示在其他應用程式上層」權限。
2. 新 AP 與 VM 啟用、電視改連新網路後，從 VM 送測試通知：
   - 兩個 App 的畫面通知都看得到嗎？
   - Notifications for Android TV 有沒有提示音？
   - PiPup 帶聲音的短影片、改版的語音朗讀，能不能正常播放？
   - **播放 YouTube／其他影片時送出帶聲音的通知，播放會不會卡住？**
3. 依結果選定 App 與是否使用聲音，再實作自動提醒。

**未確認**：
- Notifications for Android TV 在該 Google TV 型號、台灣區 Play 商店能否下載。
- PiPup 側載安裝在該型號上是否可行。
- 較新版 Google TV 對「顯示在其他應用程式上層」權限限制較多，可能需要額外設定。

### 其他可考慮的搭配

- **時間到時用 ADB 暫停播放、回主畫面**：讓時間到更有感，需在電視開啟開發人員選項與網路偵錯。
- **手機**：通知家長的手機（例如透過 Home Assistant 推播），而不是孩子的裝置。
- **HomeBoard 顯示倒數**：剩不到 10 分鐘時，裝置卡片變色。

### 評估後不採用

- **投放（Cast）訊息畫面**：會中斷播放；時間到斷網後，接收端無法從網路載入。
- **網路登入頁（captive portal）**：Google TV 支援很差，頂多顯示「已連線，但無網際網路」。

## 調整「YouTube 辨識可能失效」警示，避免誤報

**問題**：警示條件之一是「最近 5 分鐘查詢過 YouTube App 的網址至少 2 次」，用來判斷 YouTube App 正在使用。但 ESP32 版實測時，電視在播 **Hami Video** 期間，背景仍查詢了 `youtubei.googleapis.com` 2 次。Google TV 上的 YouTube App 即使沒開也會在背景連線，所以看其他影音 App（流量大、又幾乎沒有 YouTube 影片流量）時，可能被誤判為「YouTube 辨識失效」。

PR #1 把 `www.youtube.com`、`m.youtube.com` 也算進「App 使用中」，誤報機率可能再高一些（PR 註明尚未在 Google TV 上確認）。

**作法**：VM 啟用、電視改連新網路後：

1. 在 `/etc/netflow/netflow.json` 開啟 `"log_queries": true`，重啟服務。
2. 分別記錄一段時間（例如各 10 分鐘），用 `journalctl -u netflow` 觀察：
   - 實際觀看 YouTube 時，App 相關網址的查詢頻率。
   - 觀看 Hami Video 等其他 App、YouTube 在背景時的查詢頻率。
   - 停在 Google TV 首頁時的查詢頻率。
3. 依兩者差異調整條件，例如：
   - 提高 `health_min_app_lookups`（5 分鐘內的最少查詢次數）。
   - 從 `youtube_app_domains` 移除背景也會查詢的網址。
   - 必要時改用其他更能代表「前景使用」的訊號。
4. 調整完關閉 `log_queries`（記錄量很大）。

## 偵測正在看的 YouTube 頻道（進行中）

**原理**：網路流量全部加密，看不出頻道。改用 ADB 讀電視的媒體工作階段（`adb shell dumpsys media_session`）：YouTube App 會回報影片標題與頻道名稱（作者）。電視在 VM 網段內，VM 可直接連線。可延伸：設定頁顯示「正在看」、觀看歷史、封鎖特定頻道（偵測到就暫停或回主畫面）。

**2026-10-06 進度**：
- 客廳電視（192.168.50.194）已開啟開發人員選項與無線偵錯。
- VM 上已安裝 Google 官方 platform-tools：`/opt/platform-tools/adb`（版本 37.0.1）。Debian 套件的 `adb` 也裝了，但不要用它。
- `adb pair` **兩次都成功**（配對埠 35023、46799，每次開配對視窗都會換）。
- **`adb connect` 失敗**：連 192.168.50.194:35189（使用者一開始提供的連線埠）時 TLS 交握失敗，錯誤 `Handshake failed in SSL_accept/SSL_connect [invalid library (0)]`。Debian 版和官方版的 adb 都一樣，表示是電視拒絕了這把金鑰。
- 掃到電視開著的埠：5001（TvOverlay）、6466/6467（Android TV 遙控）、8008/8009/8443（Cast）、9000、10001、35189、35833、41875，以及當時的配對埠。

**明天要確認**（在電視的「無線偵錯」畫面）：
1. 畫面上方目前的「IP 位址和通訊埠」（主畫面的連線埠，不是配對視窗的）。
2. 「已配對的裝置」清單裡有沒有 `duckegg@netFlowControlDebian`。
3. 無線偵錯是否被關閉又重開過（會換埠，也可能讓配對失效）。

依結果重新配對或改連正確的埠，連上後先跑一次 `dumpsys media_session`，確認看得到頻道名稱。

# TODO

## 時間快到／時間到的提示

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

### 候選 App 與音效

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

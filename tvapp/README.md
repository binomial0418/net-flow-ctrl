# NetFlow TV

裝在 Google TV 上的小 App：把電視「正在播放」的內容（App、標題、作者——YouTube 是**頻道名稱**、播放狀態）回報給 net-flow-ctrl 路由器 VM，設定頁會顯示「▶ 標題 — 頻道」，VM 日誌也會記錄。

## 原理

Android 的 `MediaSessionManager` 能讀到各 App 回報的媒體工作階段，但前提是 App 擁有「通知存取權」，所以核心是一個 `NotificationListenerService`（[NowPlayingService.kt](android/app/src/main/kotlin/tw/netflow/netflow_tv/NowPlayingService.kt)）。它不處理通知，只監聽媒體工作階段：內容一變就（延遲 1 秒合併後）`POST http://192.168.50.1/api/nowplaying`，另每 60 秒回報一次。授權後由系統常駐執行，不需要開著 App 畫面。

Flutter 畫面（[lib/main.dart](lib/main.dart)）只是狀態頁：權限是否開啟、上次回報結果、目前的媒體，可用遙控器操作。

**限制**：只有會回報媒體工作階段的 App 才看得到。YouTube、Spotify 可以；Hami Video 不提供。

## 建置與安裝

```bash
cd tvapp
flutter build apk --release
scp build/app/outputs/flutter-apk/app-release.apk netflow-vm:/tmp/netflow-tv.apk

# 在 VM 上（電視需開啟無線偵錯，連線埠看無線偵錯主畫面）
A="/opt/platform-tools/adb -s 192.168.50.194:<埠>"
$A connect 192.168.50.194:<埠>
$A install -r /tmp/netflow-tv.apk
$A shell cmd notification allow_listener tw.netflow.netflow_tv/tw.netflow.netflow_tv.NowPlayingService
```

裝好、授權後，無線偵錯就可以關掉；更新 App 時再打開。

已驗證：Chromecast with Google TV（Android 14）。

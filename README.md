# net-flow-ctrl

個別管控家中裝置（主要是電視盒）的上網時段、每日使用時數與 YouTube。

| 資料夾 | 版本 | 說明 |
|---|---|---|
| [esp32/](esp32/) | ESP32 版 | 單晶片雙模路由器，功能完整；轉發頻寬約 10–20 Mbps，不足以穩定播放 4K |
| [linux/](linux/) | Linux 版（主力） | PVE 上的 Debian VM 加獨立 AP，2026-10-06 上線 |
| [tvapp/](tvapp/) | Google TV App | 回報電視正在播放的內容（YouTube 頻道名稱等）給 Linux 版 |

兩個版本的功能與設定頁相同。Linux 版上線後，ESP32 版作為備援，不可與 Linux 版同時開機（兩者都使用 `netflow.local`）。

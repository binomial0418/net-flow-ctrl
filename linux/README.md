# net-flow-ctrl — Linux 版

把 ESP32 版的功能移植到 Linux 路由器（PVE 上的 Debian 13 VM），解決 ESP32 轉發頻寬不足、無法順暢播放 4K 的問題。

ESP32 版（[../esp32/](../esp32/)）維持不變，作為備援。兩版的規則判定、計時方式與設定頁相同。

**2026-10-06 起正式上線**：電視改連新 AP，經 VM 上網，4K 播放順暢；ESP32 已停用。

## 使用

| 你的手機／電腦連在 | 設定頁 |
|---|---|
| 家用 WiFi（RT2600ac） | **http://netflow.local** 或 http://10.0.4.188 |
| 電視那台 AP 的 WiFi | http://192.168.50.1 |

不需帳號密碼。HomeBoard 用 `netflow.local` 連線，與 ESP32 版的 API 相容，不用改。

**ESP32 版不可再同時開機**：它也會使用 `netflow.local` 這個名字，兩台互搶時 HomeBoard 可能連錯對象。要切回 ESP32 備援時，先在 VM 上停用 avahi（`sudo systemctl disable --now avahi-daemon`）；這樣電視網段的 iPhone 也會找不到 HomePod（見下方）。

## 架構

```
電視 ──WiFi──> AP（AP 模式）──> PVE enp2s0 / vmbr1
                                    │
                              路由器 VM（Debian 13）
                              ├─ dnsmasq：DHCP（只發 IP，不做 DNS）
                              ├─ netflow.service（Python）
                              │    ├─ DNS 代理：YouTube 辨識、封鎖、學習影片 IP
                              │    ├─ 每秒：偵測裝置、計時、規則判定、更新 nftables
                              │    ├─ 時間提醒：推送到電視上的 TvOverlay
                              │    └─ 設定頁與 API（:80）
                              ├─ avahi：在家用網路以 netflow.local 回應；兩邊網段互轉 mDNS
                              └─ nftables：NAT（只對網際網路）、依 MAC 封鎖、每台裝置流量計數、擋加密 DNS
                                    │
                              PVE enp3s0 / vmbr0 ──> RT2600ac
```

電視端網段 `192.168.50.0/24`，VM 為 `192.168.50.1`。家用網段 `10.0.4.0/24`（`nftables.conf` 的 `HOME_NET`），VM 為 `10.0.4.188`。

## iPhone 與 HomePod

iPhone 也可以連電視那台 AP，一起管控 YouTube，同時照常使用家用網路上的 HomePod（AirPlay、「家庭」App）。這需要兩件事：

1. **mDNS 互轉**：HomePod 靠 mDNS（Bonjour）被找到，而 mDNS 不會跨網段。VM 上的 avahi 開啟 reflector，在兩個網段之間轉送（`install.sh` 會設定）。
2. **家用網段走路由、不做 NAT**：AirPlay 播放時，HomePod 會主動回連 iPhone（對時、事件通道）。所以 VM 對家用網段不做 NAT，並且**只允許 HomePod**（`nftables.conf` 的 `homepods` 集合：10.0.4.38、10.0.4.87、10.0.4.222）主動連進電視網段。家用網段的其他設備仍然連不進來，電視上的 TvOverlay、Cast、無線偵錯等服務不會暴露；連到網際網路仍然做 NAT，網際網路仍然無法連進來。

   **HomePod 的 IP 要在 RT2600ac 綁定固定**（DHCP 保留），IP 變了就要更新 `homepods` 集合。

**RT2600ac 必須加一條靜態路由**（SRM「網路中心」的靜態路由設定）：

| 目的地網路 | 子網路遮罩 | 閘道 |
|---|---|---|
| 192.168.50.0 | 255.255.255.0 | 10.0.4.188 |

**先加好這條路由，再更新 VM**。否則家用網段的設備（HomePod、NAS）回應電視網段時找不到路，連 NAS 也會中斷。加好後在家用網路的電腦上 `ping 192.168.50.1` 應該要通。

其他注意事項：

- iPhone 的「WiFi → 這個網路 → 私密 WiFi 位址」請設為**固定**（不要選「輪替」），否則 MAC 會變，管控系統會把它當成新裝置。
- 時間到被封鎖時，iPhone 連 HomePod 也一併中斷（和連 NAS 一樣），只剩設定頁可用。
- 用 AirPlay 播音樂到 HomePod 的流量會計入 iPhone 的使用時間（和看 NAS 影片一樣）。
- 不做 NAT 之後，NAS 看到的來源是電視網段的 IP（192.168.50.x），不再是 VM 的 10.0.4.188。NAS 若有用 IP 限制 NFS 分享，或開了 Synology 防火牆、自動封鎖，要允許這個網段。
- 去程（VM → NAS、HomePod）直接送達，回程經過 RT2600ac 的靜態路由（非對稱路由）。若 RT2600ac 的防火牆丟棄這類封包，電視看 NAS 會中斷——加好路由、更新 VM 後要實測。
- avahi 的 mDNS 互轉偶爾會讓 Apple 裝置看到自己的名字而自動改名（例如「iPhone (2)」），這是 avahi reflector 的已知現象。附帶效果：家用網路上的手機也能找到並投放到電視。

## 正在播放與觀看記錄

電視裝了 [NetFlow TV App](../tvapp/) 後，會把正在播放的內容（App、標題、作者——YouTube 是**頻道名稱**、播放狀態）回報到 `POST /api/nowplaying`：

- 設定頁在裝置名稱下顯示「▶ 標題 — 頻道」；VM 日誌（`journalctl -u netflow`）記錄每次換片。
- **觀看記錄**：每秒檢查一次，裝置的最新回報（90 秒內）是「播放中」就記 1 秒，依邏輯日（05:00 換日）、**小時**（實際時鐘 0–23 點，2026-10-08 起）、裝置、App、頻道、影片累計。暫停不算。存在 `/var/lib/netflow/history.db`（SQLite），每分鐘寫入一次，保留 90 天（`history_keep_days`）。
- 設定頁的「觀看記錄」卡片可選期間（今天／7／30／90 天）與裝置，顯示 24 小時時段分布圖、各頻道時間，點開看影片清單。API：`GET /api/history?days=7&mac=AA:BB:…`。
- **YouTube Shorts 沒有標題與頻道**：YouTube 電視版播 Shorts 時只回報「播放中」，標題、頻道都是空的（畫面文字也讀不到）。這類時間記在「Shorts／無標題」底下（廣告可能也算在內）；其他 App 播放中但沒標題的，記在「（無標題）」。
- 不提供媒體資訊的 App（例如 Hami Video）看不到；主程式重啟後最多約一分鐘（等下一次回報）不會記到。

### 封鎖 YouTube Shorts

裝置設定勾選「封鎖 YouTube Shorts」後，由電視上的 NetFlow TV App 直接暫停 Shorts，VM 透過 TvOverlay 顯示「短影音已封鎖／不可以看抖音/短影音喲」（每台每分鐘最多一次）。

- **判斷方式**：YouTube／YouTube Kids「播放中但沒有標題」持續 2.5 秒。一般影片開始播放約 1 秒內就會有標題，Shorts 一直沒有。其他 App 完全不檢查。
- **設定傳遞**：App 每次回報時，VM 在回應裡附上 `{"policy": {"blockShorts": …}}`，App 記下來後在電視端自行判斷、暫停。改設定後最晚 60 秒（下一次回報）生效。
- **尚未實測**：影片前的**廣告**若也沒有標題會被暫停；**YouTube Kids** 若一律不提供標題，所有 Kids 影片都會被暫停。遇到時調整判斷或把 Kids 移出檢查清單。

## 與 ESP32 版的差異

| 項目 | ESP32 版 | Linux 版 |
|---|---|---|
| 封包處理 | lwIP netif hook | nftables（規則見 [deploy/nftables.conf](deploy/nftables.conf)） |
| DNS 辨識 | 偷看經過的 DNS 封包 | **所有 DNS 查詢都強制轉給自己的 DNS 代理**，寫死 8.8.8.8 的裝置也一樣 |
| 擋加密 DNS / 封鎖 | 自組 RST、ICMP 封包 | nftables `reject` |
| 裝置名稱 | MAC 後六碼 | 優先用裝置的 DHCP 主機名稱 |
| 設定儲存 | NVS | `/var/lib/netflow/state.json`（寫入暫存檔後原子性替換，斷電不會寫壞） |
| 對外連線 | WiFi STA | 網路線（ens18），沒有 WiFi 掃描、NTP 設定（系統自動校時） |
| 當機恢復 | 看門狗、定時重開 | systemd 自動重啟；nftables 規則在程式重啟期間仍然有效 |
| 時間提醒 | 無 | 快到期、時間到時在電視上跳出大字通知（見下方） |

## 時間提醒通知

在電視上安裝 **TvOverlay**（Play 商店），裝置設定裡勾選「時間提醒通知」並設定提前幾分鐘（預設 10 分鐘）。

- **快到期**：剩餘時間低於設定值時，顯示「還剩 X 分鐘」。剩餘時間取時數上限、時段、本日延長中最先到的那個。時數上限依實際使用計時，閒置時不倒數。
- **時間到**：被時數上限或時段切斷時，顯示「時間到了」。只限 YouTube 的裝置顯示「YouTube 時間到了／其他 App 可以繼續使用」。
- **延長後會再提醒**：提醒跟著每一次「到期」走，不是一天一次。按了本日延長、調高上限後，再快到期、再到期都會再提醒；延長得很短也會立刻提醒剩幾分鐘。
- VM 重開機不會補送已經過了的提醒。電視關機或沒裝 TvOverlay 時，送不到就略過，不影響管控。
- 通知內容由 VM 畫成大字圖片送出，只送圖片；TvOverlay 會固定在上方加一行「REST API」小字，無法移除。第一次送通知時，會自動關掉 TvOverlay 內建的常駐時鐘，並把版面設成「Default」。
- 設定頁的「送測試通知」可以確認電視收得到。

## YouTube 辨識的維護

**IP 變動不影響**：程式沒有寫死任何 YouTube 的 IP，影片伺服器的位址都是從 DNS 回應即時學到的。

### 辨識方式

1. 所有 DNS 查詢都會被導到 VM 上的 DNS 代理。
2. 被「封鎖 YouTube」的裝置查 YouTube 網域時，直接回 NXDOMAIN。
3. 其他查詢轉給上游。上游的回應如果是影片網域（`googlevideo.com`），就把回應裡的 IP 記進 nftables 的 `ytvideo` 集合。
4. 封包經過時，依目的 IP 判斷是否為 YouTube 影片：計入 YouTube 時數，或在封鎖時切斷。

細節：

- **依裝置記錄**：`ytvideo` 存的是「裝置 IP . 影片伺服器 IP」，只有自己查過的裝置會被判定為 YouTube。同一個 IP（例如 ISP 機房內的 Google 快取節點）也會服務 Play 商店、系統更新的下載，這樣其他裝置下載更新時不會被誤算或誤擋。
- **有流量就續期**：每筆記錄在**沒有流量 1 小時**後過期（`nftables.conf` 的 `timeout 1h`）。影片播放期間的封包會不斷續期，長時間播放不會中途失效。
- **跟著 CNAME 走**：查詢的名稱即使不在清單內，只要經 CNAME 指到影片網域，一樣會學到 IP；被封鎖 YouTube 的裝置也會拿到 NXDOMAIN。
- **查不懂的查詢不放行**：被封鎖 YouTube 的裝置送出無法解析的查詢（例如一個封包裡有多個問題），直接回 FORMERR，不轉給上游。
- **只處理 IPv4**：電視網段沒有發 IPv6（dnsmasq 沒開 RA）。萬一日後有 IPv6，`from_lan` 會一律拒絕 IPv6 轉發，裝置會改走 IPv4，不會繞過時數與封鎖。

### 擋加密 DNS

辨識依賴看得到 DNS 查詢，所以設定頁的「擋加密 DNS」開啟時（預設開啟），有兩層：

- **依連接埠與 IP**：擋 DoT（853 埠），以及常見公共 DNS 的 IP 上的 443 埠（`nftables.conf` 的 `dohips`）。
- **依網域**：DNS 代理對 DoH 服務的網域回 NXDOMAIN（`dns.google`、`cloudflare-dns.com`、`dns.quad9.net` 等，清單見 `enc_dns_domains`）。裝置要先查到 DoH 伺服器的位址才能使用，所以 IP 不在 `dohips` 裡的新服務也擋得到。其中 `use-application-dns.net` 是 Firefox 的偵測網域，回 NXDOMAIN 會讓 Firefox 不啟用 DoH。

### 設定檔

網域清單與警示門檻都可以在 VM 的 `/etc/netflow/netflow.json` 覆寫，改完重啟服務即可（`sudo systemctl restart netflow`），不用改程式。清單寫了就**取代**內建清單，所以要列出完整內容。網域大小寫、結尾的點不影響比對：

```json
{
  "youtube_domains": ["youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "..."],
  "video_domains": ["googlevideo.com"],
  "youtube_app_domains": ["youtubei.googleapis.com", "www.youtube.com", "m.youtube.com"],
  "enc_dns_domains": ["dns.google", "cloudflare-dns.com", "..."]
}
```

| 設定 | 用途 |
|---|---|
| `youtube_domains` | 「封鎖 YouTube」時回 NXDOMAIN 的網域 |
| `video_domains` | 影片伺服器：學習其 IP，用來計算 YouTube 時數、切斷播放中的影片 |
| `youtube_app_domains` | 判斷 YouTube 正在使用的網域（辨識失效警示）。手機 App 查 `youtubei.googleapis.com`，電視 App 與瀏覽器查 `www.youtube.com` / `m.youtube.com` |
| `enc_dns_domains` | 「擋加密 DNS」開啟時回 NXDOMAIN 的 DoH／DoT 網域 |
| `log_queries` | `true` 時把每筆 DNS 查詢（裝置 IP、網域）寫進系統日誌，用來確認裝置實際查了哪些網域。預設 `false`，平常不要開 |
| `health_*` | 辨識失效警示的門檻，見下節 |

內建預設值見 [netflow/dnsmsg.py](netflow/dnsmsg.py) 與 [netflow/app.py](netflow/app.py) 的 `Conf`。

舊版的 `video_timeout_s` 已移除（設定檔裡留著也不影響）。影片 IP 的過期時間改由 `nftables.conf` 中 `ytvideo` 的 `timeout` 決定。

從舊版升級時，`ytvideo` 集合的格式改了，`install.sh` 會重新載入 nftables 規則，已學到的影片 IP 會清空一次。正在播放的影片要等裝置下次查 DNS（通常幾分鐘內）才會重新被辨識。

### 辨識失效警示

如果裝置找到繞過 DNS 代理的方式（例如新的加密 DNS 伺服器），時數和封鎖會無聲失效。所以系統會偵測這個特徵：

- 最近 5 分鐘內查過 YouTube 的網域（`youtube_app_domains`）至少 2 次；
- 總流量至少 20 MB；
- 但被辨識為 YouTube 影片的流量不到 5%。

持續 2 分鐘就在設定頁顯示「⚠️ YouTube 辨識可能失效」，也會寫入系統日誌（`journalctl -u netflow`）；恢復正常 5 分鐘後警示自動消失。被封鎖 YouTube 的裝置不會觸發。API 也會回報這個狀態（`/api/status` 的 `ytDetectWarn`、`/api/devices` 每台裝置的 `ytDetectWarn`）。

門檻可在設定檔調整：

| 設定 | 預設 | 意義 |
|---|---|---|
| `health_window_sec` | 300 | 觀察的時間範圍（秒） |
| `health_min_app_lookups` | 2 | 範圍內至少幾次 YouTube 查詢 |
| `health_min_mb` | 20 | 範圍內總流量至少幾 MB |
| `health_max_yt_share` | 0.05 | YouTube 影片流量低於這個比例才算可疑 |
| `health_raise_sec` | 120 | 可疑狀態持續幾秒才警示 |
| `health_clear_sec` | 300 | 恢復正常幾秒後解除警示 |

**待實機確認**：電視版 YouTube 實際查詢的網域尚未在 Google TV 上驗證。建議啟用後暫時開 `log_queries`，在電視上播 YouTube，用 `journalctl -u netflow -f` 確認有出現 `youtube_app_domains` 裡的網域；若看到 Google TV 主畫面推薦內容讓播放其他串流時誤報，可提高 `health_min_app_lookups`。

## 檔案

| 路徑 | 職責 |
|---|---|
| `netflow/model.py` | 資料結構（裝置規則、全域設定、封鎖原因） |
| `netflow/rules.py` | 規則判定、邏輯日、本日延長、60 秒流量視窗（移植自 `nfc_state.cpp`） |
| `netflow/app.py` | 每秒主迴圈與 API 邏輯 |
| `netflow/nft.py` | 更新 nftables 集合與鏈、讀取流量計數 |
| `netflow/dnsproxy.py`、`dnsmsg.py` | DNS 代理與 DNS 封包解析 |
| `netflow/clients.py` | 從 DHCP 租約與鄰居表找出線上裝置 |
| `netflow/portal.py`、`page.html` | 設定頁與 JSON API（與 ESP32 版相同，另加 `/api/notify-test`） |
| `netflow/notifier.py` | 把時間提醒畫成大字圖片，推送到電視上的 TvOverlay |
| `netflow/store.py` | 狀態存檔 |
| `deploy/` | nftables 規則、dnsmasq 設定、systemd 服務、安裝腳本 |
| `tests/` | 單元測試、命名空間整合測試 |
| `tools/preview.py` | 在本機用範例資料預覽設定頁 |

## 測試

```bash
# 單元測試（Mac 或 VM，Python 3.9 以上）
python3 -m unittest discover -s tests

# 整合測試（VM 上，需 root）：在隔離的網路命名空間內模擬「電視 ↔ 路由器 ↔ 網際網路」，
# 跑真正的 nftables 規則、DNS 代理與主程式，不影響 VM 本身的網路
sudo bash tests/integration_netns.sh
```

## 部署

```bash
# 在 Mac 上，從 linux/ 目錄複製到 VM
COPYFILE_DISABLE=1 tar --no-xattrs --exclude=__pycache__ -czf - . | \
  ssh netflow-vm 'sudo rm -rf ~/netflow-src && mkdir ~/netflow-src && tar -xzf - -C ~/netflow-src'

# 在 VM 上安裝或更新（不啟用電視端網路）
ssh netflow-vm 'cd ~/netflow-src && sudo sh deploy/install.sh'

# AP 接上 enp2s0 後，首次啟用電視端網路
ssh netflow-vm 'cd ~/netflow-src && sudo sh deploy/install.sh --activate'
```

**啟用前**務必確認 PVE 的 enp2s0 已經不接家用網路，否則 dnsmasq 會對家裡的設備發 IP。

## 進度

| 階段 | 內容 | 狀態 |
|---|---|---|
| 1 | 網路打通：NAT、DHCP | 完成，2026-10-06 上線，4K 實測順暢 |
| 2 | 基本管控：nftables 規則、常駐程式、設定頁 | 完成，實機運作中 |
| 3 | YouTube：DNS 代理、影片 IP 集合、擋加密 DNS | 完成，實機辨識率 100% |
| 4 | 時間提醒、netflow.local（HomeBoard 免改） | 完成，實機運作中 |

後續規劃見 [TODO.md](TODO.md)。

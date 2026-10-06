# net-flow-ctrl — Linux 版

把 ESP32 版的功能移植到 Linux 路由器（PVE 上的 Debian 13 VM），解決 ESP32 轉發頻寬不足、無法順暢播放 4K 的問題。

ESP32 版（[../esp32/](../esp32/)）維持不變，作為備援。兩版的規則判定、計時方式與設定頁相同。

## 架構

```
電視 ──WiFi──> AP（AP 模式）──> PVE enp2s0 / vmbr1
                                    │
                              路由器 VM（Debian 13）
                              ├─ dnsmasq：DHCP（只發 IP，不做 DNS）
                              ├─ netflow.service（Python）
                              │    ├─ DNS 代理：YouTube 辨識、封鎖、學習影片 IP
                              │    ├─ 每秒：偵測裝置、計時、規則判定、更新 nftables
                              │    └─ 設定頁與 API（:80）
                              └─ nftables：NAT、依 MAC 封鎖、每台裝置流量計數、擋加密 DNS
                                    │
                              PVE enp3s0 / vmbr0 ──> RT2600ac
```

電視端網段 `192.168.50.0/24`，VM 為 `192.168.50.1`。

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

## YouTube 辨識的維護

**IP 變動不影響**：程式沒有寫死任何 YouTube 的 IP，影片伺服器的位址都是從 DNS 回應即時學到的。

**網域清單可以從設定檔修改**：YouTube 若改用新網域，在 VM 的 `/etc/netflow/netflow.json` 覆寫對應清單後重啟服務即可（`sudo systemctl restart netflow`），不用改程式。寫了就**取代**內建清單，所以要列出完整內容：

```json
{
  "youtube_domains": ["youtube.com", "youtu.be", "googlevideo.com", "ytimg.com", "..."],
  "video_domains": ["googlevideo.com"],
  "youtube_app_domains": ["youtubei.googleapis.com"]
}
```

| 清單 | 用途 |
|---|---|
| `youtube_domains` | 「封鎖 YouTube」時回 NXDOMAIN 的網域 |
| `video_domains` | 影片伺服器：學習其 IP，用來計算 YouTube 時數、切斷播放中的影片 |
| `youtube_app_domains` | YouTube App 的 API，用來判斷 App 正在使用（辨識失效警示） |

內建預設值見 [netflow/dnsmsg.py](netflow/dnsmsg.py)。

**辨識失效警示**：辨識依賴看得到 DNS 查詢。如果裝置找到繞過的方式（例如新的加密 DNS 伺服器），時數和封鎖會無聲失效。所以系統會偵測這個特徵：最近 5 分鐘內有查詢 YouTube App 的 API、總流量至少 20 MB，但被辨識為 YouTube 影片的流量不到 5%。持續 2 分鐘就在設定頁顯示「⚠️ YouTube 辨識可能失效」，也會寫入系統日誌（`journalctl -u netflow`）；恢復正常 5 分鐘後警示自動消失。被封鎖 YouTube 的裝置不會觸發。API 也會回報這個狀態（`/api/status` 的 `ytDetectWarn`、`/api/devices` 每台裝置的 `ytDetectWarn`）。

## 檔案

| 路徑 | 職責 |
|---|---|
| `netflow/model.py` | 資料結構（裝置規則、全域設定、封鎖原因） |
| `netflow/rules.py` | 規則判定、邏輯日、本日延長、60 秒流量視窗（移植自 `nfc_state.cpp`） |
| `netflow/app.py` | 每秒主迴圈與 API 邏輯 |
| `netflow/nft.py` | 更新 nftables 集合與鏈、讀取流量計數 |
| `netflow/dnsproxy.py`、`dnsmsg.py` | DNS 代理與 DNS 封包解析 |
| `netflow/clients.py` | 從 DHCP 租約與鄰居表找出線上裝置 |
| `netflow/portal.py`、`page.html` | 設定頁與 JSON API（與 ESP32 版相同） |
| `netflow/store.py` | 狀態存檔 |
| `deploy/` | nftables 規則、dnsmasq 設定、systemd 服務、安裝腳本 |
| `tests/` | 單元測試、命名空間整合測試 |

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
| 1 | 網路打通：NAT、DHCP | 設定完成，等 AP 接上後啟用 |
| 2 | 基本管控：nftables 規則、常駐程式、設定頁 | 完成，整合測試通過 |
| 3 | YouTube：DNS 代理、影片 IP 集合、擋加密 DNS | 完成，整合測試通過 |
| 4 | 收尾：實機驗證、文件 | 進行中 |

後續規劃見 [TODO.md](TODO.md)。

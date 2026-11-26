# Safe Code 桌面捷徑圖示

酒紅底、金色兩行文字，上排 SAFE、下排 CODE，參考用戶提供的標題配色。

- `safe-code-shortcut.svg`：文字已轉為矢量輪廓的母版，不依賴安裝字體。
- `safe-code-shortcut.png`：1024 × 1024 透明圓角預覽。
- `safe-code-shortcut.ico`：Windows 圖示，含 16、24、32、48、64、128、256 像素七種尺寸。

只在 Inno Setup 的桌面捷徑條目指定 `IconFilename`。程式沒有設定 `ApplicationIcon`，開始選單、卸載條目、工作列和視窗介面未指定這個圖示。圖示作為獨立資源隨安裝包儲存到 `Icons` 目錄。

矢量文字輪廓由 `tools/build_shortcut_icon.py` 使用本機 Georgia Bold Italic 生成，不分發字體檔案。PNG 由 Chromium 渲染 SVG，ICO 由 Pillow 編碼。

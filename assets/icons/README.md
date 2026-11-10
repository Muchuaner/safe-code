# Safe Code 桌面快捷方式图标

酒红底、金色两行文字，上排 SAFE、下排 CODE，参考用户提供的标题配色。

- `safe-code-shortcut.svg`：文字已转为矢量轮廓的母版，不依赖安装字体。
- `safe-code-shortcut.png`：1024 × 1024 透明圆角预览。
- `safe-code-shortcut.ico`：Windows 图标，含 16、24、32、48、64、128、256 像素七种尺寸。

只在 Inno Setup 的桌面快捷方式条目指定 `IconFilename`。程序没有设置 `ApplicationIcon`，开始菜单、卸载条目、任务栏和窗口界面未指定这个图标。图标作为独立资源随安装包保存到 `Icons` 目录。

矢量文字轮廓由 `tools/build_shortcut_icon.py` 使用本机 Georgia Bold Italic 生成，不分发字体文件。PNG 由 Chromium 渲染 SVG，ICO 由 Pillow 编码。

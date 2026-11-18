# Social Auto Upload 二维码登录适配来源

- 上游：https://github.com/dreammis/social-auto-upload
- 固定提交：`0012d2c355f88f683cc38dde2a2db209e14091bc`
- 许可证：MIT，Copyright (c) 2023 dreammis；完整文本见同目录 `LICENSE`。
- 复用范围：`uploader/douyin_uploader/main.py` 的二维码选择器和扫码标签切换；`uploader/xiaohongshu_uploader/main.py` 的扫码面板切换和二维码区域定位；`uploader/tencent_uploader/main.py` 的新版微信 `qrconnect` iframe 支持。
- 本仓适配：`app/publish_browser_engine.py` 使用标准 Playwright 的隔离浏览器上下文和图片截图；`client/src-tauri/src/publish_login.js` 使用官方 WebView2 页面已加载的图片及严格来源校验的 iframe 消息。
- 2026-09-15 实测补充：视频号同时存在隐藏的 `img.qrcode` 与可见的 `img.js_qrcode_img`；选择可见图片，跳过隐藏旧模板。登录页面提交后立即观察二维码，避免可选资源拖延 DOM 加载事件。
- 快手复用同一提交的 `uploader/ks_uploader/main.py` 中二维码面板、`KSVideo.upload` 和 `set_thumbnail` 的可见控件。适配位于 `app/publish_browser_engine.py` 与 `app/publishers/kuaishou_adapter.py`，使用标准 Playwright、全新的云端扫码会话和单次即时发布。
- 快手身份和提交结果来自官方创作者页面自身的正常响应。发布成功同时需要明确的提交回执与作品管理页跳转。未知结果进入人工核对。登录态沿用本仓的 Fernet 加密和用户会话围栏。保留上游 MIT 授权，无需安装上游包。
- 桌面和网页分发同时携带 `client/public/licenses/social-auto-upload-MIT.txt`，由既有前端资源打包，不引入 Tauri 外部资源。

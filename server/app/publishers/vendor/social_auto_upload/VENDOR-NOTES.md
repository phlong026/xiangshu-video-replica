# Social Auto Upload 登录与快手视频发布适配来源

- 上游：https://github.com/dreammis/social-auto-upload
- 固定提交：`0012d2c355f88f683cc38dde2a2db209e14091bc`
- 许可证：MIT，Copyright (c) 2023 dreammis；完整文本见同目录 `LICENSE`。
- 二维码复用范围：`uploader/douyin_uploader/main.py` 的二维码选择器和扫码标签切换；`uploader/xiaohongshu_uploader/main.py` 的扫码面板切换和二维码区域定位；`uploader/tencent_uploader/main.py` 的新版微信 `qrconnect` iframe 支持；`uploader/ks_uploader/main.py` 的快手扫码切换、二维码、确认与过期状态。
- 快手视频复用范围：同一固定提交的 `KSVideo.upload`、`set_thumbnail` 和可见发布确认弹窗流程，移植到 `app/publishers/kuaishou_adapter.py`。标题与描述合并为一个发布正文，话题最多三个；定时由本仓任务队列调度，到点执行立即发布。
- 本仓使用标准 Playwright 和隔离浏览器上下文；桌面二维码使用官方 WebView2 页面的已加载图片。账号归属、身份确认、超时取消、Fernet 加密与会话围栏沿用本仓实现。登录态通过内存 `storage_state` 传递，不写上游明文账号文件或调试页面。
- 快手扫码域限定为 `passport.kuaishou.com` 的官方登录路径；本人身份仅从 `cp.kuaishou.com` 的实际 `POST /rest/v2/creator/pc/authority/account/current` 回执确认。使用平台真实 `userId`、`userName`、`userAvatar`，并在投递前核对账号 ID。
- 快手只发起一次发布，至多处理一次可见确认弹窗；提交后的未知结果进入待核对状态，不采用上游的重复点击发布循环。成功沿用作品管理页的严格跳转依据，已观察到的失败提交回执不得被跳转覆盖；没有可靠作品 ID 或链接时保留空值。
- 未引入上游 Patchright、签名实现或反检测补丁。标准 Playwright 的真实平台兼容性仍需受控实机验收；本地假浏览器测试不能代替真实扫码与上传验收。
- 2026-09-15 视频号实测：同时存在隐藏的 `img.qrcode` 与可见的 `img.js_qrcode_img`；选择可见图片，跳过隐藏旧模板。登录页面提交后立即观察二维码，避免可选资源拖延 DOM 加载事件。
- 桌面和网页分发同时携带 `client/public/licenses/social-auto-upload-MIT.txt`，由前端资源打包复制到 `licenses/`，不引入 Tauri 外部资源。

快手官方公开前端证据（2026-11-17，只读核对）：

- https://p66-plat.wskwai.com/kos/nlav11104/static/ks-cp/js/7565.ccc7dd62.js ：本人身份端点、用户字段与视频提交端点。
- https://p66-plat.wskwai.com/kos/nlav11104/static/ks-cp/js/chunk-common.1fc9987c.js ：登录回调与平台成功／未登录回执约定。
- https://w2.kskwai.com/kos/nlav12555/js/app.88aff6a9.js ：`main#login-form`、二维码图片、切换、确认与过期控件。

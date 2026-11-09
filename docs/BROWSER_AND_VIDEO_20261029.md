# 大肥鱼浏览器与视频能力（2026-10-29）

本轮按本人要求实施通用公网浏览器，所有实际好友/已加入群可用。浏览器按“聊天scope + 真实发起QQ”隔离；同群不同发起者也不共享Cookie、标签或文件。原DeepSeek官方滚动deepseek-flash/high、记忆V2、好友/群核验、短聊与打字节奏、原账目保持。正式上线状态以evidence/browser-deployment.json为准。

## 当前可用能力

|类别|实际接口和行为|
|---|---|
|检索|原web_search/web_fetch保留，公开事实查询优先使用；动态页才开浏览器|
|网页|打开公开URL、读取动态DOM、可见元素ref、查找、长正文按offset/limit分页、iframe、前进/后退/刷新、滚动|
|交互|基于当前页面ref点击、输入、搜索Enter、下拉选项、复选框、悬停、拖动；旧ref在页面快照更新后失效|
|标签|新建、列表、切换、关闭；每个隔离会话最多6个标签|
|图片与文件|网页截图、导出PDF、受限下载、将当前生成的图片/文件发到当前QQ会话；文件路径和收件人不能由模型指定|
|书签|每个scope+发起者独立保存、列出和移除公开网页书签；重置浏览器不删除书签|
|视频|发现视频、播放/暂停、跳转秒数、调速、静音/取消静音；滚动后优先当前可见视频，可继续刷下一条|
|视频理解|实际截图抽帧、时间/尺寸/播放状态、站点可用textTracks字幕；交同一DeepSeek视觉模型理解|
|管理|本人当前会话浏览器状态、截图和重置；停止生成会取消对应浏览器任务，取消不影响别人的环境|

浏览器在后台运行，不占用你的个人Chrome窗口。直接在QQ自然提问，例如：

- “打开这个GitHub项目，看看README和安装方法：<链接>”
- “帮我在这个网页里搜索XXX，点开第二个相关结果。”
- “打开这个视频，看看前几秒在讲什么：<公开链接>”
- “往下刷下一条，暂停在第10秒，再解释这个画面。”
- “把当前网页截图发给我。”或“导出成PDF发来。”
- “收藏当前网页，下次再看。”

免费管理命令（不调用模型）：`大肥鱼 浏览器状态`、`大肥鱼 浏览器截图`、`大肥鱼 重置浏览器`。这些命令只操作发送者在当前群/私聊的浏览器，不能关闭别人的会话。普通聊天不会固定多一次浏览器调用。网页/视频截图通过视觉输入交同一high模型，不另买浏览器模型或云浏览服务。

## 视频的真实边界

视频observe默认在约6秒内抽3张实际画面，可调至最多15秒采样区间，非完整连续视频模型。字幕只读取站点已提供的textTracks；有些站把字幕放在别处、没有字幕或需要登录。当前没有音轨转写/ASR，不能声称听懂未转写的声音；DRM、付费、登录、地区限制、验证码和站点风控可能阻止播放，遇到限制如实说明，不绕过。

目前能够“刷视频”是打开、播放、滚动/点下一条，再根据实际画面/字幕回应；不会在无人请求时永久刷视频或自动消耗模型额度。短视频站是否可匿名播放取决于站点本身，不将“通用能力接入”写成“任意网站保证可用”。

## 安装、来源和版本

- 独立Python运行时：tools/browser/python，Playwright固定1.64.1，支持依赖greenlet3.5.6/pyee14.0.0/typing-extensions4.16.0；config/browser-requirements.lock保存完整上游哈希。
- 项目已有Node：tools/node-v24.22.1-win-x64/node.exe，通过PLAYWRIGHT_NODEJS_PATH显式复用。
- 浏览器程序：从本机已安装且Google签名有效的Chrome156.0.8078.48复制到tools/browser/chrome；只复制Application程序文件，未复制个人User Data、Cookie、历史和书签。
- 原完整wheel下载速度过慢；项目安装器tools/install_browser_runtime.py从官方files.pythonhosted.org分段获取除重复node.exe之外的wheel成员，逐成员核对上游RECORD的SHA256/大小。实际安装193个成员。没有下载完整wheel，不冒称整包SHA256验证通过；这一差异明确记在config/browser-runtime-manifest.json。
- 所有新增运行文件、浏览器临时目录、下载及证据在本项目内；没有改全局PATH、个人Chrome配置或Codex MCP配置。现有Kimi WebBridge健康检查为未运行，未启动全局桥或接管个人浏览器。
- 安装前源码/主库快照：evidence/browser-savepoint.txt。改动来源与版本细节：evidence/browser-sources.json、config/browser-runtime-manifest.json。

## 实际架构与运行范围

QQ可信事件 → ConversationService工具循环 → BrowserBroker → 项目独立Python worker → Playwright/Chrome。

工具只有固定browser参数协议，没有任意JavaScript、shell、文件路径、Cookie导出或个人浏览器接管能力。worker仅继承Windows运行所需的基础环境和项目内目录，不接收DeepSeek Key。浏览器用单独匿名Context，Cookie和站点存储只在该Context内；空闲约15分钟关闭，重启后端会关闭全部Context。最多6个同时保留的Context，资源满时清理非活动最旧环境；这属于内存背压，不是模型/费用配额。

浏览器网络通过随机本机端口的受限代理，连接前校验所有DNS地址均为公网，再钉住验证后的IP建立连接；拒绝本机/内网/云元数据地址、file/javascript/data顶层导航、外部应用协议。WebSocket、ServiceWorker和非阅读型网站写入请求默认阻止；支持YouTube公开阅读类player/browse/next/search接口。不会为了打开网站关闭证书校验。

账号登录、密码/验证码输入、支付、购买、发布、评论、上传、授权和法律条款确认没有自动开放，需要本人另行处理。表单仅自动提交明确搜索框；网页和字幕不能授权动作，也不能修改QQ管理身份/收件人。当前不包含网页上传本地文件、网站账号写操作或通用电脑应用控制。

生成的截图/PDF和下载文件存runtime/browser/artifacts/<不可逆会话摘要>/，默认24小时到期，QQ回复只给文件编号与固定当前收件人。单文件32MiB，执行文件类型不下载、不执行，不自动解压ZIP。页面操作默认超时、连续浏览任务约6分钟，超时可在下一条消息继续；这些是执行/资源边界，不调整原聊天金额/次数配置。

浏览器“只读资料+交互导航”不是虚拟机或对所有网站语义行为的形式证明；没有向聊天模型开放底层浏览器API。若未来要加登录态/网站发布，应先设计单独用户接管和动作审批，不直接移除当前限制。

## 测试证据

- tests/test_browser.py：公网URL与DNS混合私网答案拒绝、无任意JS/路径、scope+actor隔离、模型工具循环和图像输入、免费管理命令。
- tests/test_browser_engine.py：真实Chrome+真实代理+合成站点，导航/输入/搜索/标签、iframe、旧ref失效、Cookie/文件跨会话拒绝、播放/暂停/跳转/字幕/3帧、截图/PDF/下载、密码/支付/外部协议阻断和取消隔离。
- tests/test_open_contacts.py新增浏览器输出固定好友收件人、文件SHA256与路径归属验证。
- evidence/browser-public-probe.json：使用生产公网策略实际读取GitHub项目页面，播放MDN CC0公开MP4（960×540，播放时间逐帧推进，MP4/WebM支持），同时验证localhost被阻断。没有测试用本机端口例外。
- evidence/browser-full-tests.txt、browser-lint.txt、browser-architecture.txt为最终回归。开发DeepSeek调用0、真实QQ测试消息0；正常用户聊天和原记忆学习仍按原账目运行。
- 截图evidence/browser-synthetic.jpg和browser-video-sample.jpg仅为合成验收，不假冒真人聊天或私人网页。真实好友/群对自然语言浏览器的体验待日常反馈。

## 运维与回退

02启动聊天后端会挂载浏览器能力，浏览器worker在后台预热，各隔离Context在首次请求时创建。03停止会关闭本项目浏览器和任务，QQ专用实例独立保留。状态在runtime/status.json的browser字段；故障不自动替换模型，不改费用账本。

仅停用浏览器可把config/settings.json的browser_enabled设为false后有序重启本项目后端；原Search/Fetch、聊天、记忆和表情继续，主库不回滚。若恢复源码使用browser-savepoint指向快照，数据恢复仍按最新删除注册表/账本/去重闸门，不直接覆盖旧库。项目内浏览器不自动更新；更换版本先做引擎回归和公开页面验证。

## 参考项目与官方资料

1. Microsoft Playwright MCP：https://github.com/microsoft/playwright-mcp 。参考结构化可访问页面快照、稳定ref与独立会话设计，不安装其全部MCP权限或修改Codex配置。
2. Browser Use：https://github.com/browser-use/browser-use 。参考观察—操作—再观察的工具循环与视频/网页agent边界；未启用其云服务、反风控或验证码处理。
3. Playwright Context隔离：https://playwright.dev/python/docs/browser-contexts 。使用独立Cookie/localStorage/SessionStorage环境。
4. Playwright网络与代理：https://playwright.dev/python/docs/network 。使用Context请求拦截与本机受限代理。
5. Playwright浏览器与媒体说明：https://playwright.dev/python/docs/browsers 。使用复制的品牌Chrome支持本机现有媒体编解码器。
6. 公开播放验收：https://interactive-examples.mdn.mozilla.net/pages/tabbed/video.html 。仅公开示例内容，不需要账号。

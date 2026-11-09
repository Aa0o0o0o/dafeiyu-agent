# 语境表情与永久能力档案（2026-10-29）

## 为什么收藏成功却很少主动发

现场核对23张表情已在大肥鱼自己的QQ账号同步，但全为泛标签。旧版每轮只给模型16个摘要，而且标记meaning_verified=false；模型不知道图意，正确做法本应避免随意套图。本次补上实际看候选、缓存图意、按上下文召回和完整发送顺序。

## 当前实现

- QQ收藏仍同步到1111111111本人账号；本地是经哈希核验的发送缓存，全授权会话可用。普通照片不自动当表情收藏，不额外采集私人聊天到共用标签。
- 聊天同一Flash/high模型可调用qq_expression.search/inspect/describe。按当前话题、近几轮聊天及群内近期文字召回；首次需要时查看1至3张原图/动图关键帧，视觉回复成功后才能保存图意。不存在另一个逐句付费分类器或后台开机批量扫描；发生工具续传仍按原共享账目计费。
- 图片描述、图上文字、情绪和用途标签写入runtime/stickers/index.sqlite3的sticker_meanings。含义为模型视觉解释，可能有误；主人“大肥鱼 表情标签 编号 标签”更正后优先于模型。仅存图自身，禁止写入本轮人名、个人资料或偏好。用户个人长期记忆继续各scope隔离。
- 检索结果可无匹配，不机械配图。已缓存图意重复使用，不每次看图；当前候选不够可主动搜索整库。模型根据语境决定发图时机，不另提高群聊触发概率。
- 支持原QQ收藏图片/GIF/WebP，原始发送字节保留；收到完整来源的mface可保存QQ商城表情重发字段，仍以本地文件哈希绑定，模型只看到内部编号；旧商城缓存无完整来源时发送原收藏图，不伪造商城凭证。
- QQ自带/动画表情通过真实face消息段发送，ID来自当前安装版NapCat的已校验表情表；骰子/猜拳用原生消息，不伪造结果。文字、收藏、QQ表情可按顺序穿插，也支持纯表情回复，统一打字延迟、取消和发送回执。
- 所有出站目标固定当前已核验好友/群；模型不能提供收件人、任意文件路径、商城key或任意OneBot动作。发送结果未知不自动重发。

QQ会员、商城购买/下载授权、云收藏删除和消息贴表情回应不在本次实现内，不冒称所有QQ客户端交互都已接通。

## 永久能力知识

config/capabilities.json是大肥鱼的自身能力档案，src/capabilities.py每轮按实际模块/配置注入system的runtime_capabilities。包含聊天、视觉、引用、记忆/情绪、联网、浏览器/视频、QQ资料、表情、群内提及的启用状态，另列未实现功能。清空记忆、遗忘衰减、上下文裁剪、重启都不会移除档案；关闭的功能不能说成当前能用。运行故障要按工具实际结果说明。

命令“大肥鱼 能力”（群内需正常唤起）免费返回启用功能，不调用模型。“大肥鱼 发表情 开心”进入正常模型语境选图；不带参数/指定合法S编号仍是原免费直接发图命令。

## 后续开发必须同步

每次增加/变更功能，依次更新实现与启用条件、config/capabilities.json、使用提示、对应测试、PLAN/CHANGELOG/PROGRESS/需求对照与运行说明，再部署。src/tool_catalog.py是模型工具唯一注册清单；启动与回归检查其与能力档案一一覆盖，漏登记不能悄悄上线。新增非工具功能也必须按本段维护，不能只改提示词说已完成。

能力知识是项目事实，不混入用户画像表，不受普通聊天、网页或可删除个人记忆改写。原AGENTS.md完整保留；自动审批拒绝了修改该文件的动作，维护规则按本文件及能力档案执行，未修改原授权/安全边界。

## 验证与费用边界

新增tests/test_qq_expressions.py覆盖看图前/失败时不写语义、真正视觉续传后缓存、下轮只用一次聊天调用、主人更正优先、作用域/哈希检查、GIF不改原文件、原生发送目标/回执/无重试、能力不随清空记忆消失及未来工具漏登记阻断。实际QQ手机渲染、自然选图贴切程度需用户日常反馈，不以合成模型替代真人体验。

测试证据：evidence/expressions-new-tests.txt、expressions-full-tests.txt、expressions-lint.txt、expressions-architecture.txt；最终部署状态expressions-deployment.json。开发没有付费DeepSeek调用或主动向真人QQ发测试消息。回退按evidence/expressions-savepoint.json还原源码；新增缓存表可保留，不回滚聊天数据、账目或删除注册表。例行数据备份仍遵守7天保留。

## 参考资料

- [MaiBot文字表情选择器](https://github.com/TogawaSakiko-desuwa/Maibot_Emoji_Select_With_Text)：描述和语境候选选择，无匹配不乱发。
- [AstrBot meme magpie](https://github.com/Whereis-Alice/astrbot_plugin_meme_magpie)：收集、视觉描述、语境检索分离。
- [NapCat官方消息格式](https://napneko.github.io/develop/msg)：原生表情、收藏图片和商城表情格式。
- 以项目已安装NapCat4.18.41的packages/napcat-onebot/api/msg.ts与types/message.ts核对实际实现，SHA256见evidence/expressions-sources.json。

仅参考设计并独立实现；没有下载/激活陌生插件，没有改个人QQ/全局浏览器或新装分类模型，未引入新的第三方依赖。

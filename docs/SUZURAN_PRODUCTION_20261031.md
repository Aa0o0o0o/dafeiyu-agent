# 铃兰 GSV 正式上线记录（2026-10-31）

用户反馈“效果还行，先上线，后面有需求再改”。未指定A/B/C编号，按现有生产引擎与此前铃兰偏好，选择A/GSV路线先上线，避免引擎迁移。此为可继续调整的当前版本，不表示声音完美或不可再改。

## 正式行为
- 主音色dafeiyu_suzuran_cn_v1，revision=suzuran-gsv-production-20261031；GPT-SoVITS v5 Turbo原环境、原4步/短句cut0参数。
- 铃兰CN010柔和日常基准，轻快映射同演员CN030，好奇映射CN001；认真/安慰保留CN010，用当前轻度讲话计划微调语速。没有独立任意情绪向量能力，不把参考切换冒称自由表演。
- 新默认与allowed_profiles同步；旧scope的拉普兰德选择不覆盖新音色，个人禁用/字幕偏好、会话模式保持。自动语音概率0.45、短聊上限100字符保持；语音不重复附相同文字，复杂问题完整文字和失败回退保持。
- config/capabilities.json永久能力版本2026-10-31-suzuran-production，大肥鱼每轮能获知当前真实功能。

## 验证
41项语音/讲话计划/能力回归及8项GSV适配通过，Ruff通过。新增回归验证旧音色偏好被过滤而个人禁用/字幕不被重置。
重载只操作已核验的本项目后端/TTS；实际正式HTTP柔和/轻快/好奇三段48kHz PCM通过，用时约3.53/3.62/2.82秒。QQ连接、1111111111身份复核、自动语音和TTS就绪；没有主动发QQ测试或新增付费模型测试。
证据：evidence/suzuran-production-tests.txt、suzuran-production-gsv-tests.txt、suzuran-production-http.json、suzuran-production-deployment.json。

## 清理与回退
正式验证成功后，执行之前约定的条件清理：按绝对路径、祖先非链接/非junction、内容散列逐个核验并删除32个文件，合计13712570字节，约13.08MiB。含旧GSV/拉普兰德/铃兰/Index试听、未采用的澄闪试验参考及两份候选清单/专用试听入口；空目录仅为空才删除，无递归删除。
删除清单与散列：evidence/suzuran-production-cleanup.json。已归档必要候选来源元数据到evidence/suzuran-retired-trial-provenance.json；此前试听路径不再可播放属于已授权清理，不是生产缺文件。
保留三段正式铃兰参考、必要回归/脱敏证据、原生产参考和已有环境供回退；不删除QQ/聊天数据、凭据或有效备份。本轮未新增模型/环境。
上线前配置/服务源文件在backups/suzuran-production-20261031；需回退时恢复其中voices/registry.json、config/settings.json、config/capabilities.json及tts_service/gsv_service.py，再仅重载本项目TTS/后端。没有迁移或清空记忆数据库。

下一步：正常使用，按后续实际反馈调整，无需再等待上线批准。

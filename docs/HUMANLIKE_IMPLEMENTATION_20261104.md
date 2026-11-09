# 大肥鱼连续人格与行为改进

2026-11-04。用户要求实施拟人化调研方案。状态：源码已正式加载；完整离线回归运行591项、跳过8项，其余通过。项目后端、QQ连接及登录、语音和Memory V2就绪；真人盲评和长期自然交流效果尚待观察。本文记录真实实现与验收范围；完整研究背景见 [DAFEIYU_HUMANLIKE_ROADMAP_20261104.md](DAFEIYU_HUMANLIKE_ROADMAP_20261104.md)。

## 本轮实现

- **对话状态**：`src/conversation_state.py` 从当前消息和既有会话上下文生成临时、低置信度的交流/事件/媒体提示；不新增一轮模型调用，也不持久化心理标签。显式“不用分析”提示优先，模型仍需根据上下文判断。私聊追问可用之前的本人消息辅助检索；群聊只取同一真实发言者的近期消息。
- **相关经历**：`src/memory_v2/astrbot_bridge.py` 在短追问时以同一说话者的近期文本扩展本地检索。`conversation.py` 可临时关联当前已召回、同主体且有共同词项的记忆ID；关联不创建新事实，沿用V2的主体、时效、撤回和发送前版本检查。私人对话与自主公共经历仍按原scope隔离。
- **情绪与语音**：`memory_state.py` 使用保守事件线索，避免明显否定、引述和第三方叙述被当作当前说话人的情绪；受限状态继续衰减。`voice/delivery.py` 可参考当前对话线索选择已有讲话风格，`voice/coordinator.py` 尊重文字优先和现有用户偏好。可用语气仍以生产语音引擎真实登记的参考为准。
- **持续兴趣**：`autonomy_goal_threads` 将公开兴趣的初始主题与后续问题分开。旧目标升级时以当时的现有话题建立 `migrated_current` 基线，不冒称找到了原始问题。主人scope内按话题可召回最近三条以外、具有真实工具观察的旧探索；完整回执留在原记录，发送给模型的额外投影受长度限制。
- **工具反馈和暂时看法**：`autonomy_run_feedback` 区分实际工具尝试、观察、失败/休息和模型对目标完成情况的暂时判断；执行成功不自动代表任务完成。通过比较真实回执，系统可识别“搜索后抓取结果中实际出现的来源”这一方法；一次为候选，两次不同搜索观察到相同流程才标记为在两次查询中出现，仍不代表网页结论被证实。`autonomy_viewpoints` 允许已有自主反思回合提出可修正观点，必须关联同scope、同目标、已成功且有观察回执的运行。新看法保留前一版本链；模型判断仍可能出错，不等于独立事实核验。联网回执标注工具是否实际返回成功，敏感内容仍按既有规则过滤。失败不会自动重试。
- **主动分享**：原主人outbox保留实际送达、取消和不重发规则；自主上下文可参考最近三条确有送达回执的主人消息。额外去除仅标点差异的重复，其他近义重复交由模型判断，不静默屏蔽可能包含新信息的观点。
- **评测资料**：`fixtures/humanlike_scenarios_20261104.json` 是60个不含真实聊天的合成场景；`tests/test_humanlike.py` 覆盖会话主体、否定/引用、撤回、重启、真实回执、观点版本、超时及分享去重等路径。`tools/humanlike_eval.py` 可把已有的两套合成案例回答随机成对盲评、再根据评价计数；脚本不调用模型或QQ。

没有变更现有按当前消息关键词切换角色分区的行为。保留默认每天最多6次后台探索、每次3模型/8工具/6分钟、原QQ与会话范围、固定主人主动分享范围、取消和回执。未引入新模型、框架、外部账号或自行修改源码的能力。

## 离线盲评的使用

准备两个 UTF-8 JSON 文件，格式为 `{"D01":"旧版本回答", "D02":"旧版本回答"}` 和相同ID集合的新版回答，内容只能使用合成场景。下面示例仅读写本地文件；`--blind` 和 `--key` 必须使用不存在的不同路径。

```powershell
.\.venv\Scripts\python.exe tools\humanlike_eval.py prepare --baseline .tmp\old.json --candidate .tmp\new.json --blind .tmp\blind.json --key .tmp\blind-key.json
```

盲评者阅读 `blind.json`，对每个案例记录 `{"id":"D01","preference":"a"}`，也可选择 `b` 或 `tie`，将记录组成JSON数组。结束后执行：

```powershell
.\.venv\Scripts\python.exe tools\humanlike_eval.py score --ratings .tmp\ratings.json --key .tmp\blind-key.json
```

结果分别报告新版胜、旧版胜、平局和实际评分数量。该工具没有生成答案，也没有代替人工判断事实、隐私、自然度或统计显著性。实际模型对比需要另行运行并记录用量；本轮未发真实QQ测试消息，也未额外调用付费模型。

## 升级、边界与回退

三个新表采用 `CREATE TABLE IF NOT EXISTS` 增量创建，不覆盖原始消息、回执或旧记忆。现有自主清空与记忆代次变化会清理本scope的新表数据，已送达回执沿原outbox规则处理。恢复数据库时必须沿用Memory V2最新删除注册表，不能用旧快照覆盖已有撤回。

原源码备份在 `backups/humanlike-20261104-source`。回退时先有序停止本项目聊天后端，恢复其中相应代码、提示词和能力文件；为避免退回旧版后留下没有删除联动的新增派生数据，还需在数据库备份后清理本轮新增的 `autonomy_viewpoints`、`autonomy_run_feedback` 和 `autonomy_goal_threads` 三张表，再启动旧版。保留原 `autonomy_runs`、`autonomy_goals`、聊天记忆、账目及QQ数据。数据库回退前先核对运行状态和最新撤回记录，切勿恢复未经检查的旧数据库。

上线前一致性数据库备份为 `backups/humanlike-20261104-source/pre-deploy.sqlite3`；上线时保留原QQ/语音服务并只重载聊天后端。临时影子迁移与正式数据库 `quick_check` 均通过，证据见 `evidence/humanlike-shadow-migration-20261104.json`、`evidence/humanlike-deployment-20261104.json`。最终完整测试见 `evidence/humanlike-full-tests-release-20261104.txt`。本次修改文件Ruff通过；项目其他未修改文件仍有61项既有Ruff问题，详见 `evidence/humanlike-static-checks-20261104.txt`。

**效果边界**：对话状态依赖有限的本地规则和模型自身理解；话题关联是词项线索，不是语义理解的保证；看法的来源回执只能证明观察发生，不能证明观点正确；重复检测不能识别所有语义改写；自然聊天、真实盲评以及长期使用效果尚待用户观察。

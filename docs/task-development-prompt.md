# 可复制到新对话的任务能力开发 prompt

请直接在本项目实现“开拓者自主做任务”的能力，不要只给方案。这个对话只负责任务能力，并与现有生存策略兼容。

项目绝对路径：`D:\2026-云核软件大赛\Competition-main\2026-CloudCore-Software-Competition`。
先检查 Git 状态，以当前工作区最新生存代码为基础；保留已有未提交修改和测试，不要用远端旧代码覆盖。平台入口已修正为 `main3.py`，不要改回 `main.py`。

先仔细阅读以下材料：

1. `D:\2026-云核软件大赛\Competition-main\Competition-main\docs\任务书.md`
2. `D:\2026-云核软件大赛\Competition-main\Competition-main\docs\接口文档.md`
3. `D:\2026-云核软件大赛\Competition-main\Competition-main\docs\request.txt`
4. `D:\2026-云核软件大赛\Competition-main\Competition-main\docs\response.txt`
5. `D:\2026-云核软件大赛\Competition-main\Competition-main\Demo\CoreGeek`，主要参考 HTTP 交互。
6. `D:\2026-云核软件大赛\Competition-main\replay.json` 中 `teamName=Agentic麻辣烫`、`teamId=4284` 的行为。
7. 项目内 `docs/survival-optimization.md`、`agent/strategy.py`、`agent/logistics.py`、`agent/server.py` 及已有测试。

冠军回放已观察到：开拓者分别在第 11/14、18/21、112/115、119/123、149/152、156/159 回合接取/提交自进化任务，这 6 次提交的 passRate 都是 1.0；第 942 回合有 summonTreasure 行为。请从回放进一步提取任务类型、描述、站位、可见答案、时机和返回结果，区分直接观察与推断。回放不保证包含完整 LLM/沙盒交互链，不能凭空补全，也不要把单局答案或坐标硬编码成通用策略。

开发目标与优先顺序：

1. 先实现自进化任务的完整闭环：选择本队可接取且收益/路程合适的 playerTasks，寻路到周围一格，发送 acceptTask，读取 phaseTask，调用顶层 prompt / executeCmd，处理后续回合的 llmResp / lastCmdResult，生成 taskAnswer 并 submitAnswer，依据反馈确认成功、部分成功、超时或失败，再进入下一任务。
2. 为任务实现跨回合状态机、重复请求幂等、有限重试和可复用解题经验；不能每个回合重复领取、重复调用 LLM 或忽略上一回合命令结果。状态应在新局、换边、回合回退时正确隔离；处理角色死亡、复活、任务取消和耗尽。
3. 做任务期间必须留在对应任务点周围一格；遵守 isValid、coldDownRounds、timeoutRounds。用预估解题时间、到达时间和真实返岗距离判断是否来得及。不能为了买升级券或返回炮位，悄悄走离任务点导致已接任务丢失；紧急防守确需中断时明确记录原因。
4. 与生存模块共享角色分配、动作占用和金币预算。炮手在夜间仍要轮流操作三台火箭台，维修工和采矿收入也要保留。第一阶段优先白天做任务；若要像冠军那样夜间做任务，必须先确认威胁已消除或另一名角色已实际到位接管，不能只因为火箭台冷却就撤走炮手。同回合一名角色只能做一件事，也只能操控一台武器。
5. LLM 走比赛接口的 prompt，不新增个人 API key 或外网依赖。按文档区分日调用额度与任务执行期间的豁免；executeCmd 仅在任务期间使用，通过后续请求的 lastCmdResult 读取结果，处理退出码、TIMEOUT、JUDGER_ERROR 和 TRUNCATED。HTTP 决策要在 5 秒内返回，不在本地同步等待平台执行。
6. 自进化闭环通过后，再实现世界新闻/民间传闻的跨日记录、宝藏线索推理和预算允许的物品采购、summonTreasure。严格匹配物品清单、开启时机和结果码，不能无证据反复献祭消耗物品；宝藏采购不能花掉基地升级及维修预留金币。

接口必须严格兼容：JSON ID key 和 controllerId 是字符串，targetPos 是坐标对象数组；不要把回放的数字 roleType、backpacks、targetName 当成实时请求响应字段。response.txt 是动作目录，含重复 key 和局部语法问题，不可原样当作 JSON 模板。现有生存响应中的 prompt/executeCmd 默认空字符串，任务调度需通过统一输出层有条件填写。

验证要覆盖两侧、成功/部分成功/失败/超时、连续任务、冷却、LLM 限额、沙盒异常、死亡复活、重复请求和临近日落的任务/防守交接。保留已有生存回归测试；增加可重复的多回合任务模拟输入，验证真正走完“领取→求解→提交→确认”而非只发出 acceptTask。提供修改说明、回放证据、运行方法、测试结果及未验证的限制。没有真实服务端时不能把模拟成功说成平台成功。

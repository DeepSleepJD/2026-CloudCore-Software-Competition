# 自主任务能力

## 自进化失败修复（2026-09-18）

针对北京任务日志中的文件定位耗时、认证头过时、错误对象被当成记录以及6条命令额度耗尽，当前实现作如下调整：

- 接取并收到phaseTask后，程序通过平台executeCmd合并定位、读取当前任务和同目录API文档，并部署标准HTTP查询工具。当前任务文档独立保存在bootstrap上下文中，不随最近8条交互记录轮换丢失；文件读取及输出均有上限，截断显式标记。
- LLM除executeCmd/taskAnswer外，可返回httpRequest（url、apiKey、header）。客户端仅组装命令，平台沙盒工具编码中文查询、检查HTTP和业务错误；仅在服务端明确要求Bearer认证时，用同一个任务key重试一次。分页脚本可通过runpy加载同一工具，必须核实记录类型和分页完整性。
- 移除固定8次LLM、6条命令限制。按任务截止回合保留提交和反馈时间：发起LLM至少剩3回合，执行命令至少剩4回合。日志与prompt均提供按当前回合估算的remainingCalls/remainingCommands；它们是时间容量，不是平台配额。不同答案提交仍限3次。无时间继续求解记insufficient_rounds。
- exitCode=0仍检查结构化API错误；错误响应不再提示“命令正常完成”。发现的任务目录、成功请求的接口路径/认证头、明确认证错误提示保存在同局discoveries中，即使任务失败也保留；不存查询参数、key、响应记录或token。完成任务后的experience仍单独保存，当前任务输出优先。

`tests/test_task_sandbox.py`使用真实临时任务文件、Python子进程和本地HTTP服务，覆盖过时认证头、HTTP 401/HTTP 200业务错误、中文编码、嵌套记录分页、15回合提交闭环、失败发现保留、上下文轮换、跨局清理及截止回合保护。LLM决策与比赛裁判仍为确定性模拟；不代表真实平台任务通过率。

验证结果：`python -m unittest discover -q`共67项通过；`python tools/validate_tasks.py --rounds 260`双方各260回合通过，每方5项模拟任务完成，最大单次决策约0.042秒。真实本地HTTP/分页用例从接取到反馈确认用8回合（限制15回合）。尚未在官方平台或真实LLM上复赛验证。

## 对战诊断日志（2026-09-18）

默认 INFO 日志现在记录完整任务交互，沿用 `main3.py` 的标准错误输出，无需增加启动参数。保留原有 `task_result=` 汇总，新增 `task_trace=` JSON 事件：

- `task_selected` / `accept_request`：任务元数据、领取回合及平台 `timeoutRounds`。
- `round_input`：当前阶段、完整 `phaseTask`、`llmResp`、`lastCmdResult`、平台错误、角色位置/血量及动作反馈；在超时、死亡等终态判断前记录，避免丢掉最后一次命令结果。
- `llm_request` / `command_request` / `submit_request`：完整提示词、实际发出的命令和答案，以及重试反馈。
- `command_rejected`：分别记录命令类型错误、长度超限、次数耗尽，补充原有汇总原因。
- `context_trim`：发给模型的上下文被截短或移除旧记录的情况。
- `finish`：终止原因，区分平台任务超时与本地截止回合判断。

事件包含队伍、任务点、开始/领取回合、阶段、上次发送回合、截止回合及剩余 LLM/命令/提交预算。`round_input.commandTimedOut` 表示沙箱返回 `[TIMEOUT]`；`finish.serverTaskTimeout` 表示平台返回错误码 1。协议不提供实际命令耗时，日志不能据此给出执行秒数。

单个事件超过 4000 字符时，使用 `task_trace_chunk=` 分片保存完整 JSON，避免单行过长。按同一次事件的 `part` 顺序拼接已 JSON 解码的 `data` 字段，再解析拼接后的 JSON；不要只保留一片。日志内容不使用模型上下文的 12000 字符截取，因此可以排查上下文丢失前的原始输出。平台自身的 `[TRUNCATED]` 仍表示源输出不完整。

对战后请保留从 `task_selected` 到 `finish` 的所有日志行（包括分片），最好提供完整客户端日志。本次仅增加诊断，不改变提示词、限次、截止判断或解题策略。

后续调整：夜间清场后的任务窗口、历史耗时估算及当前防御投资顺序见 [火力与任务收益优化](investment-optimization.md)。下文“仅白天做任务”和 50 项测试结果记录的是任务模块初版。

在当前工作区生存优化上增量实现，保留 `main3.py` 入口、HTTP 协议、现有测试及未提交内容。开发开始时生存优化尚未提交，期间本地 HEAD 已推进到 `43126df`；本轮未执行 checkout、reset、stash 或 commit。生存对照使用该最新提交，通过 Git 只读加载，不覆盖工作区。

## 实现与运行

`agent/tasks.py` 实现选择任务点、寻路、acceptTask、phaseTask、prompt、executeCmd、llmResp/lastCmdResult、submitAnswer 和终态确认。支持任务点2的两格区域；只选择本队 playerTasks，检查 isValid、coldDownRounds 和 timeoutRounds。没有 timeoutRounds 的旧样例不启动任务，不猜测平台超时配置。

领取以后开拓者被调度器占用，即使本轮只是等待也不会用于购物、送券或其他移动。任务期间不依赖 isValid 为真，因为领取后任务点可能已不可领取。路径计划预估解题12回合（受任务超时截断），加入去程和真实地图返岗距离及余量；若解题较慢、通路变化、受威胁或日落临近，明确记录中断原因后返岗。返岗距离包含墙体；长程估计允许队友稍后让路，实际移动仍避让所有角色和本轮预约位置。

状态机为 travel → accept → bootstrap命令 → llm ↔ cmd → submit → 确认/修正。LLM和命令次数由剩余回合约束，最多3次不同答案提交；结果缺失等2回合再请求恢复，不重复原答案。错误码1处理为超时，2交回求解器修正，5停止当前任务调用。沙盒退出码非0、TIMEOUT、JUDGER_ERROR、TRUNCATED和结构化API错误都传给求解器，不能当成完整成功输出。命令来自当前任务的bootstrap、结构化HTTP请求或LLM响应，只通过响应字段送给比赛平台，选手HTTP进程不执行、不等待命令，也不调用外网或个人API。

给平台LLM的输出约定：单一JSON对象，三选一：

```json
{"httpRequest":{"url":"http://localhost:端口/文档接口?查询参数","apiKey":"任务文档提供的key","header":"X-API-Key"}}
```

```json
{"executeCmd":"读取任务文件或调用任务沙盒API的有界命令"}
```

```json
{"taskAnswer":{"任务要求的字段":"从当次沙盒结果计算的值"},"experience":"可复用的解题步骤，不包含当次答案"}
```

taskAnswer若为对象则序列化为协议要求的字符串。历史命令与结果有长度限制；被推定完成任务的方法会按任务类型保存在本局记忆中，后续仍需查询当前任务数据，不能复用token或城市答案。没有为冠军的文件名、答案、坐标写运行时查表。

同队、同阵营、同基地、同回合返回缓存的响应副本，不重复推进状态。换队、换边、回合回退会重置记忆；若请求额外提供matchId也参与隔离。角色死亡会结束本地任务，复活后可选新任务。任务取消、未知phase、回合跳跃和迟到结果不会被当成当前答案。记忆仅存进程内：文档规定进程退出不再拉起，当前没有磁盘恢复机制。没有比赛ID且两场首个请求身份、回合完全一样时，协议本身不足以区分新局与重试。

`agent/strategy.py` 统一输出与角色占用。工人继续建造、维修、采矿与配送，使用原有金币预算。夜间仍由一名角色轮流操作三台火箭台；存活的原炮手继续当班，开拓者在远处复活不会抢走岗位。只有当前快照已经站到共同炮位的角色才可替换原炮手。本轮任务与宝藏行走均限白天；没有实现夜间派出任务者的交接流程。

## 结果确认的边界

实时接口没有 `passRate` 或独立任务完成结果结构；回放的 `team.task` 不能混入实时协议。因此日志区分：

- `completed_inferred`：紧接一次合法提交后phaseTask消失，没有错误，角色存活且仍邻接任务点，并早于超时边界。是依据协议的完成推断，不是读取到通过率1.0。
- `partial_or_wrong_ended`：结束同时返回错误码2。此码同时表示全错或部分正确，无法凭现有字段区分数值通过率；记录passRate=null。若任务仍在则继续修正，平台负责保留最高通过率。
- `timeout`、`ended_unconfirmed`、`death`、`accept_rejected`、`accept_unconfirmed`、`phase_replaced`、`position_lost`、`retry_exhausted` 等单独记录，不冒充成功。
- `defense_return_deadline`、`emergency_retreat` 明确说明主动中断原因。

不能用金币或总积分上涨单独证明答题成功，因为同回合还可能出售矿石或击杀机器人。

## 宝藏

`agent/treasure.py` 保存跨日官方新闻与民间传闻。非任务期间每个游戏日最多3次平台LLM调用，同一份线索只推理一次；任务执行期间的调用不计入该日预算。LLM返回的坐标、精确物品清单和绝对回合窗口都必须有可核对的传闻引用，置信度须为high。引用存在只能证明来源文本存在，不能形式化证明LLM推理正确；此项仍需真实平台验证。

仅使用当局商店存在的任务用品，排除升级券、药品、维修包、武器、召唤令和矿石。采购前核算整个缺口、背包容量、商店绕路、任务点去程与返程，并在工人与配送支出之后检查剩余预算。预留当前战略升级、维修补货、未建火箭台及30金币应急资金。到达后等待开启窗口，献祭清单不多不少；结果1/4终止后续尝试，2/3/0或丢失结果都不会再次献祭同一个方案。新证据使旧方案失效；已尝试指纹禁止重复。保持保守策略可能错过远处、只能夜晚开启或线索不完整的宝藏。

## 冠军回放：直接观察与推断

来源：`D:\2026-云核软件大赛\Competition-main\replay.json`，teamId=4284、teamName=Agentic麻辣烫。完整可见答案和动作在 `task-replay-evidence.json`，由提取工具生成，仅用于证据分析。

|领取/提交回合|类型/描述引用文件|开拓者站位|提交内容形态|回放结果|
|---|---|---|---|---|
|11/14|自进化类1 / task_1_beijing.md|(24,13)|city、oldest_era、total_count、types、world_heritage_count|passRate=1.0|
|18/21|自进化类2 / task_1_alpha.md|(25,16)|token|passRate=1.0|
|112/115|自进化类2 / task_2_beta.md|(27,16)|token|passRate=1.0|
|119/123|自进化类1 / task_2_nanjing.md|(24,13)|城市统计字段|passRate=1.0|
|149/152|自进化类2 / task_3_gamma.md|(25,18)|token|passRate=1.0|
|156/159|自进化类1 / task_3_chengdu.md|(23,15)|城市统计字段|passRate=1.0|

可见描述都是要求读取对应文件；回放没有完整LLM/终端交互链，因此不能声称冠军用了某条命令或某个SOP。六次任务均用3或4回合完成，提示文件/API探索方法值得复用，这是推断。

任务点2的回放task.pos=(27,17)，但(25,16)/(25,18)也成功领取，因为地图同类型任务点还包括(26,17)。这说明不能只按单一taskPosition检查站位。

112/115/119/123为夜晚，全图快照仍有35个存活机器人，但它们全部归在对方4289的roles中；冠军4284的roles中为0。该观察支持“本方清场后外出”的解释，不能据此推断所有夜晚均安全，更不能用炮台冷却替代威胁判断或实际交接。

942回合直接观察：开拓者在(4,2)，向(3,3)执行合法summonTreasure，结果码1。前一回合背包为AcientTablet、FlameBreath、StarSand，之后为空。这支持该局献祭组合的判断，但回放动作本身未暴露item数组；不把这一组物品/坐标当通用解。

## 可复现验证

在项目目录执行：

```powershell
python main3.py 8080
python -B -m unittest discover -v
python -B tools/validate_tasks.py --rounds 260 --output docs/task-validation.json
python -B tools/analyze_task_replay.py ../replay.json --output docs/task-replay-evidence.json
python -B tools/compare_survival.py --baseline 43126df --rounds 260 --output docs/task-survival-comparison.json
python -B tools/analyze_replay.py ../replay.json --validate
```

本轮50项测试通过：保留原21项测试，新增20项任务测试和9项宝藏/交接测试。包括真实本机HTTP传输下的多回合协议链，但LLM和任务反馈仍由测试桩产生。覆盖两侧、成功推断、错误/部分正确、超时、连续任务、冷却/耗尽、额度、沙盒异常/截断、角色死亡复活、重复回合、换边回退、日落返岗和炮手恢复交接。

两侧各260回合合成任务推演，各完成4次任务提交并收到模拟结束反馈，最大单次决策约0.035秒（机器负载可能改变）。这验证状态机和动作协议，不证明真实LLM能解出官方题目。详细事件和终态见 `task-validation.json`。

复跑期间曾出现一次本机HTTP客户端5秒超时与WinError 10053连接中止，同次工具调用也发生长时间停顿；原因未能确定，不能据此保证所有环境下的5秒时限。未放宽测试超时或修改HTTP行为；单独复跑HTTP多回合测试及9项宝藏测试共10项，1.319秒全部通过。

与43126df的无任务生存对照，双方260回合资源推演的升级时机、动作数量、剩余金币完全一致；固定276金币场景都在288/306回合升级基地至2/3级；固定每回合80墙伤场景均维修6次、没有墙洞、末尾墙血1520。原回放1115个可验证快照也通过动作合法性检查。这些均不是官方机器人AI对战，不表示真实存活天数或胜率。

**尚未连接真实比赛服务端、真实任务沙盒或平台LLM；没有本实现的真实平台通过率、收益或宝藏成功结果。** 官方题目数据、LLM输出格式遵循程度、接口任务结束时序和真实压力下的返岗仍需上线观察。保守日间调度和限次可能放弃较长任务；这是为保留生存能力采取的取舍。

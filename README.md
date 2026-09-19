# 2026-CloudCore-Software-Competition

云核软件大赛《未来战争》首版生存策略，开发分支 `feature/jd`。
Python **3.10+**，仅使用标准库，不需要 pip 安装，也不调用外部服务。

## 启动

平台按任务书约定调用：

```bash
bash run.sh 8080
```

Windows 本地启动：

```powershell
python main3.py 8080
```

监听 `0.0.0.0:<port>`，接受任意路径的 HTTP POST。请求必须为接口文档的 JSON Request；
响应固定包含 `roleCommandMap`、`prompt`、`executeCmd`。任务求解和宝藏推理按需填写后两项，无操作时为空字符串。
部署时将 `run.sh`、`main3.py`、`agent/` 放在同一目录；运行不依赖本仓库外的 Demo、docs 或 replay。
平台日志显示其实际启动入口为 `/home/docker/CoreGeek/main3.py`，上传后须确保该路径存在。

### 自进化任务与部署核对（2026-09-19）

固定提示词位于 `agent/task_prompt.py`，动态任务上下文由 `agent/tasks.py` 的 `ask()` 追加。
当前操作为 `httpRequest`、`runSop`、`executeCmd`、`taskAnswer`，只能选择一种；模型提供的证据不会覆盖程序观察结果。

文化遗产查询首次从真实 HTTP 响应学习参数名、认证头、记录路径及 offset/limit 分页结构。
`runSop` 根据当前任务文件核对城市，在沙盒执行 `agent/task_sop.py` 中的固定分页统计程序；
客户端重新核对每页、唯一ID、总数、记录字段并计算答案，成功后直接提交，不再经过一轮模型改写。
同类后续任务复用方法，但重新读取本题文件和全部数据；不会保存或复用旧城市答案/API key。
环境修复任务支持原样提交沙盒 JSON token，或校验含实际检查器 `TOKEN=...` 输出的结构化结果后直接提交。

上传前在项目根目录运行：

```powershell
python -X utf8 -B -m agent.build_info
```

启动日志 `agent_build=` 会输出 `buildId`、实际入口路径、模块根目录和源文件 SHA-256；任务日志也带相同 `buildId`。
上传完整 `agent/`、`main3.py` 和 `run.sh`，重启后核对平台日志的 `buildId` 与本地一致。
指纹由文件内容生成，不依赖平台保留 `.git`。仅修改本地文件不能确认平台版本已更新。

验证说明见 `docs/task-fix-20260919.md`。固定SOP目前支持已观察到的文化遗产 records/pagination 结构；
未知结构、未知年代或最早年代出现歧义时会停止提交并提供错误，不能以猜测结果通过校验。

## 当前策略

- 从基地实际坐标判断左右侧，不依赖 teamA/teamB 字符串或固定角色 ID。
- 前方两格建 12 段半圈围墙；后方建 3 台火箭台，保留共同操作格和后方通道。
- 75 初始金币优先用于三台火箭台。工人采石建墙，矿点枯竭后重新寻路；之后按实时收购价和路程选择矿石，批量出售。
- 按到账资金升级：主炮 3 级 → 其余两炮 2 级 → 两段前墙 2 级 → 第二炮 3 级 → 再两段前墙 2 级 → 第三炮 3 级 → 补强前墙。基地不再按天数强推满级，濒危时优先用升级恢复血量；保留两次维修的现金，已有维修包抵扣预留。
- 工人择近运送升级券，在同一次商店行程中批量购买当前及后续可用的券，已有券计入计划，紧急送货不追加购物。没有任务且返岗时间充足时开拓者也可协助；进攻期间炮手不承担送货。
- 夜间一个角色轮流操控三台火箭台，每回合至多一台。按实际冷却和攻击范围发射，考虑中心伤害、溅射、剩余血量和基地威胁；升级后输出对应数量的落点。
- 工人继续采矿、交易；一人预备 2～6 个修复包并兼顾夜间维修。健康维修工可从基地内侧邻墙格施救；有敌情且包未用完时守岗，缺一段墙也继续维修，相邻急修优先于买券。机器人过近或自身低血量时避险。
- 围墙根据血量和近期掉血速度决定维修优先级；白天补建损毁建筑，低血量工人可购买药剂。闲置工人会让出基地入口，避免升级券送不进去。
- 路径避开建筑、矿点、中立单位、敌方可见单位和队友；预留本回合移动/建造目标，避免己方争抢。采集失败后暂避该矿点。
- 自主选择己方自进化任务点，执行领取、平台 LLM/沙盒求解、提交和反馈确认；按历史完成耗时估算接单窗口，按真实路径提前返岗。夜晚生成窗口过后，确认己方机器人清空且基地附近安全时可继续做任务；己方威胁重现立即中断返防。
- 跨日保存新闻和传闻；仅在坐标、物品及时间都有引用证据时执行宝藏方案，保留防御预算，献祭失败不重复消耗同一方案。

## 验证

```powershell
python -B -m unittest discover -v
python -B tools/rollout.py --rounds 1300 --seed 42
python -B tools/analyze_replay.py ../replay.json --validate
python -B tools/compare_survival.py --baseline a7b2b00 --rounds 1300
python -B tools/validate_tasks.py --rounds 260 --output docs/task-validation.json
python -B tools/analyze_task_replay.py ../replay.json --output docs/task-replay-evidence.json
python -B tools/analyze_opponents.py <回放1> <回放2> <回放3> --validate --output docs/opponent-replay-evidence.json
python -B tools/compare_investment.py --baseline-dir <修改前快照的agent目录> --output docs/investment-comparison.json
```

单元测试无需服务端；官方 Request 样例测试在外部样例文件不存在时跳过。
回放分析工具需要显式传入回放文件。`tools/rollout.py` 模拟资源、建造、交易、升级、冷却，
使用静止的合成机器人检查攻击合法性，**不模拟真实机器人 AI、建筑受伤与生存积分**。
回放状态检查也不会将我们的动作写回原对局，因此不能据此声称真实比赛已存活 1300 回合。

规则依据、冠军回放观察及已知限制见 [开发依据](docs/strategy-notes.md)，
实际检查结果见 [验证记录](docs/validation.md)。

第二轮生存优化的依据和取舍见 [生存优化说明](docs/survival-optimization.md)。
新对话开发任务能力可使用 [任务开发 prompt](docs/task-development-prompt.md)。
本轮实现、回放证据、合成测试结果和真实平台未验证项见 [任务能力说明](docs/task-development.md)。

三位对手的共同策略、资金重新分配和本轮验证见 [火力与任务收益优化](docs/investment-optimization.md)。该策略取代第二轮说明中的基地日程优先规则。

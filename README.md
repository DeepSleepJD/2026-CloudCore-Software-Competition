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
响应固定包含 `roleCommandMap`、`prompt`、`executeCmd`。后两项当前为空字符串。
部署时将 `run.sh`、`main3.py`、`agent/` 放在同一目录；运行不依赖本仓库外的 Demo、docs 或 replay。
平台日志显示其实际启动入口为 `/home/docker/CoreGeek/main3.py`，上传后须确保该路径存在。

## 当前策略

- 从基地实际坐标判断左右侧，不依赖 teamA/teamB 字符串或固定角色 ID。
- 前方两格建 12 段半圈围墙；后方建 3 台火箭台，保留共同操作格和后方通道。
- 75 初始金币优先用于三台火箭台。工人采石建墙，矿点枯竭后重新寻路；之后按实时收购价和路程选择矿石，批量出售。
- 基地升级有时间优先级：通常第 2～3 天优先升 2 级、第 3～4 天优先升 3 级，资金不足则等待收入；与主炮升级交错进行，不再排在全部武器满级之后。
- 开拓者和工人择近运送战略升级券，固定当前运送人并保留预算；夜间炮手不离岗，工人仍可购买、送回基地升级券。返岗时间按实际寻路距离判断。
- 夜间一个角色轮流操控三台火箭台，每回合至多一台。按实际冷却和攻击范围发射，考虑中心伤害、溅射、剩余血量和基地威胁；升级后输出对应数量的落点。
- 工人继续采矿、交易；一人预备 2～6 个修复包并兼顾夜间维修。健康维修工可从基地内侧邻墙格施救，机器人过近或自身低血量时避险；备货量随天数提高。
- 围墙根据血量和近期掉血速度决定维修优先级；白天补建损毁建筑，低血量工人可购买药剂。闲置工人会让出基地入口，避免升级券送不进去。
- 路径避开建筑、矿点、中立单位、敌方可见单位和队友；预留本回合移动/建造目标，避免己方争抢。采集失败后暂避该矿点。
- 本次不实现领取任务、LLM、自进化任务及宝藏策略。

## 验证

```powershell
python -B -m unittest discover -v
python -B tools/rollout.py --rounds 1300 --seed 42
python -B tools/analyze_replay.py ../replay.json --validate
python -B tools/compare_survival.py --baseline a7b2b00 --rounds 1300
```

单元测试无需服务端；官方 Request 样例测试在外部样例文件不存在时跳过。
回放分析工具需要显式传入回放文件。`tools/rollout.py` 模拟资源、建造、交易、升级、冷却，
使用静止的合成机器人检查攻击合法性，**不模拟真实机器人 AI、建筑受伤与生存积分**。
回放状态检查也不会将我们的动作写回原对局，因此不能据此声称真实比赛已存活 1300 回合。

规则依据、冠军回放观察及已知限制见 [开发依据](docs/strategy-notes.md)，
实际检查结果见 [验证记录](docs/validation.md)。

第二轮生存优化的依据和取舍见 [生存优化说明](docs/survival-optimization.md)。
新对话开发任务能力可使用 [任务开发 prompt](docs/task-development-prompt.md)。

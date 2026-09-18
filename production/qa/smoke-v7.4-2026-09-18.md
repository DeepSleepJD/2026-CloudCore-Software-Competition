# v7.4 通用任务流程验证

## 改动范围

分页证据累计、URL 端点识别、当前参考文档发现、同局接口经验、修复与验收合并及脚本换行兼容。不包含特定城市答案、修复值或接口凭据；模型仍按当前任务生成计划。

Python unittest 和既有 Linux/Windows × Python 3.10/3.12 CI 矩阵用于自动验证；无需人工逐次上传进行流程冒烟测试。完整日志分析见 analysis/full-task-log-diagnosis-v7.4.md。

## 验证记录

- 新增 test_task_iteration.py 10 个测试方法，内含多个参数和失败边界子案例。针对性测试通过；Windows 跳过 1 项 POSIX sh 验收测试，由 Linux CI 执行。
- 北京真实分页数据重放：第 35 回合允许提交。三组变更参数的查询、三份修复规范构造的任务均在领取后第 4 回合提交。
- 17/17 变异检查检出，新增故意不累计分页、拒绝分离参数后的已知 URL、丢弃成功接口经验，对应测试失败。
- 本地完整测试当时为 163 项，47.364 秒，通过，跳过 1 项 POSIX 专用测试；随后增加真实南京/成都任务书发现测试，并强化三组修复验收断言，10 项定向测试再次通过（Windows 跳过 POSIX 项）。最终完整集为 164 项，见远端验证。
- git diff --check 通过；17 个包内源码/说明文件与工作区逐字节一致（统一 LF）。

模型回复为可控测试输入；HTTP 服务和修复/验收子进程真实执行。测试不代表正式模型对未知任务的正确率。

## 远端验证：PASS

提交 8bc8122，Linux/Windows × Python 3.10/3.12 四组全部成功。Linux 完整执行 164 项；Windows 执行 163 项，跳过 1 项只适用于 POSIX 的 CRLF 验收。每组 17 项变异检查和部署构建均通过，两组 Linux 的真实 run.sh 入口和 v7.4-jd 健康检查通过。[执行记录](https://github.com/DeepSleepJD/2026-CloudCore-Software-Competition/actions/runs/35354738653)。

## 提交包

源码提交：0293999f70bf7d9295dab7881049528c68ee41ee。版本 v7.4-jd。官方 CoreGeek/ 根目录，run.sh 权限 0755，LF 换行，包含 agent/task_evidence.py；不含测试资料、原始日志或本地缓存。

artifacts/CoreGeek.tar.gz 与本地 CoreGeek-v7.4.tar.gz 相同，40996 字节；SHA256：`539ac49b1e33b7ba78bd2d17989706b12c7cefa02889fe927a5aef28109c8a3e`。

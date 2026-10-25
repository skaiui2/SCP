# SCP 慢启动 pacing 研究

本分支研究 g 怎样影响慢启动阶段的 cwnd 和数据包分散，执行真实 SCP C 核心，
使用完整反馈 RTT 为 100 ms、无丢包的虚拟网络。全程保持慢启动。
当前核心、公共网络和传输逻辑与基线 `da52bfe3690752a393f17d26f0ec60e7966db0b8` 一致；实验配置与只读观测位于 `test/research/`。

- [结果、画图脚本及复现步骤](../../reproduction/README.md)
- [代码与执行路径审查入口](../../test/research/README.md)
- [22 个 g 及实验设置](../../test/research/scp_fixed_experiment.json)
- [历史已通过的 Actions 运行 39697564304](https://github.com/skaiui2/SCP/actions/runs/39697564304)
- [主分支 pacing 论文](https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf)及 [LaTeX 源码](https://github.com/skaiui2/SCP/tree/main/papers/pacing)

[2026-10-22 结果解读](../../reproduction/README.md#current-results-and-the-python-difference)
记录早期 Python 逐 gap 截断的累计影响，并区分完整包组与固定 RTT 时间箱。

每次传输 1 GiB = 1,073,741,824 字节，从 1 MSS（1430 字节）开始。
两个方向各传播 50 ms，不加入序列化和带宽排队。22 个固定 g 各重复三次，
另外运行三次原生策略。原生策略使用 Q16 的 `2*ln(2)`（90852）。共享核心的 AIMD CA 与 PROB 使用固定 `3*ln(3)/2`（107998）；本分支仍只运行 SS 复现材料。

调度器保留下一可用发送时刻：每批从 `max(next_slot, now)` 开始，先给包分配
时间戳，再按该包的 gap 推进 next_slot。虚拟网络逐次推进 1 ms，把实际发出的
包交给对端 SCP 处理；ACK 由对端核心产生。重建后的 Python 模型逐次推进 1 us，
保留预约小数，并按整数微秒派发。保存的 2026-10-22 对照表使用早期逐 gap 截断模型；
当前代码与旧观察分开标注。gain 表示、执行网格、整 MSS 约定
仍与原生实现存在差异，详见复现指南。

本分支只保留自身的精简结果。逐包原始证据可在有效期内从 Actions 下载，
过期后按复现指南重跑。两篇正式论文和通用项目说明统一在
[主分支](https://github.com/skaiui2/SCP/tree/main)维护。

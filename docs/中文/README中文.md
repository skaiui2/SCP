# SCP（Simple Stream Control Protocol）

[English README](../../README.md)

SCP 是一个构建在 UDP 或其他不可靠分组传输之上的轻量级可靠字节流协议。它面向传输协议研究、用户态网络、嵌入式系统，以及需要明确观察和修改传输行为的场景。

SCP 不依赖内核 TCP 栈，自行实现序号、ACK/SACK、重传、流量控制、连接管理、RTT/RTO 估计和拥塞控制。目前提供两种可切换的拥塞控制器：

- **SCP-PROB**：实验性的非线性控制器，将丢包和 RTT 证据转换为连续的拥塞似然信号。
- **AIMD**：采用传统加性增大、乘性减小的拥塞控制器，也用于对照实验。

当前版本还加入了发送端 pacing 层：拥塞窗口允许发送的数据会根据 `cwnd` 和 RTT 分配发送时间，再由 pacing 定时器释放。

> **项目状态：** SCP 仍在开发中，API 和线格式可能变化。传输协议核心与可选拥塞控制器分别评估；PROB 的具体实验结果见下文。

概率驱动控制器的理论见：[SCP-PROB：概率驱动的非线性拥塞控制](../../SCP-PROB_Probability-Driven_Nonlinear_Congestion_Control.pdf)。

Pacing 研究见：[Pacing and Congestion-Window Growth: An Exact Packet-Level Analysis](../../Pacing_and_Congestion_Window_Growth.pdf)。论文分析明确规定的逐 ACK 追加调度器，不等同于当前 SCP 实现。[Pacing_and_Congestion_Window_Growth.tex](../../papers/pacing/Pacing_and_Congestion_Window_Growth.tex) 是唯一论文源码，直接上传 Overleaf 并使用 pdfLaTeX 编译即可；证明、参考文献和公式绘制的图均包含在其中，不依赖实验包或外部图片。

## 特性

- 在不可靠分组传输之上提供可靠字节流
- 序号、累计 ACK 和 SACK 信息
- 乱序缓存与重传
- 接收端通告窗口
- RTT、RTT 方差和重传超时估计
- CONNECT/FIN 连接生命周期
- 可切换的拥塞控制回调
- 可切换的 PROB 和 AIMD 拥塞控制器
- 基于 `cwnd / RTT` 的发送端 pacing
- 使用红黑树管理定时器和乱序接收缓存
- 保持可阅读、可移植的小型 C 实现

## 仓库结构

```text
scp.c / scp.h                  协议核心
lib/                           侵入式容器和基础数据结构
test/nodeA / test/nodeB        Linux 集成测试
setup_scp_netns.sh             可复现的 network namespace 拓扑
test/analyze_scp.py            日志分析
docs/                          使用文档
```

协议核心通过用户提供的发送回调输出数据：

```c
struct scp_transport_class {
    int (*send)(void *user, const void *buf, size_t len);
    int (*recv)(void *user, void *buf, size_t maxlen);
    int (*close)(void *user);
    void *user;
};
```

因此核心不依赖具体的 UDP 封装或网络设备。

## 连接状态机

```mermaid
stateDiagram-v2
    [*] --> CLOSED

    CLOSED --> SYN_SENT: 发送 CONNECT
    CLOSED --> SYN_RECV: 收到 CONNECT / 发送 CONNECT_ACK

    SYN_SENT --> ESTABLISHED: 收到 CONNECT_ACK / 发送 ACK
    SYN_RECV --> ESTABLISHED: 收到 ACK

    ESTABLISHED --> ESTABLISHED: DATA / ACK / SACK
    ESTABLISHED --> FIN_WAIT: 主动关闭 / 发送 FIN+ACK
    ESTABLISHED --> LAST_ACK: 收到 FIN+ACK / 发送 ACK+FIN

    FIN_WAIT --> CLOSED: 收到 FIN+ACK / 发送 ACK
    LAST_ACK --> CLOSED: 收到 ACK

    FIN_WAIT --> FIN_WAIT: FIN 超时 / 重传
    LAST_ACK --> LAST_ACK: FIN 超时 / 重传
```

## 拥塞控制与 pacing

### SCP-PROB

SCP-PROB 将平滑丢包估计与 RTT 测量结合，构造残差拥塞信号，通过校准的 sigmoid 和滞回机制决定非线性控制律增加还是减少窗口。

其设计目标是在有丢包或变化的网络环境中传输数据。在下文记录的 0% 随机丢包配置中，两条 PROB 流分配不均，PROB 与 AIMD 竞争时获得的份额很小。这些观测针对所测 PROB 控制器及配置，不能据此判断 AIMD 控制器或 SCP 的可靠传输机制失效。

### AIMD

AIMD 是可独立选择的拥塞控制器，采用传统的加性增大和乘性减小。它与 SCP-PROB 使用相同的拥塞控制接口，但有自己的窗口更新规则，同时用于下文的对照实验。

### Pacing

pacing 不替代拥塞窗口：

1. `cwnd` 和对端窗口决定允许进入网络的数据量；
2. 发送端根据当前 RTT 和拥塞窗口计算逐包间隔；
3. 按递增方式为数据包分配计划发送时间；
4. 1 ms pacing 定时器发送所有已经到期的数据包。

当计算间隔小于 1 ms，或者用户态进程唤醒较晚时，同一个 tick 可以释放多个包。pacing 集成测试测量的是真实 transport `send` 回调时刻，不是数据经过 qdisc 后的物理链路离开时刻。

### Pacing 讨论记录

分析关注的是 pacing 如何改变 ACK 时序和窗口增长，以及 pacing 系数如何影响这一过程。相关 ICCRG 邮件记录了研究中涉及的问题和实现细节：

- [慢启动时序与比较模型](https://mailarchive.ietf.org/arch/msg/iccrg/K0LZfeCUDnp3Kj91dMx-0iPGef8/)，涉及[固定批次与逐 ACK 调度的区别](https://mailarchive.ietf.org/arch/msg/iccrg/GSzlNebpiu6Gp1UTRZJISBRiIi8/)，以及[窗口限制与 pacing 速率限制的关系](https://mailarchive.ietf.org/arch/msg/iccrg/6K-oKt8LfaaH1XQKzp62wfElujc/)。
- [Linux 为什么在慢启动中采用 2 倍 pacing 速率](https://mailarchive.ietf.org/arch/msg/iccrg/ms7Nww2LvUBuDaUmOD71osmp8bI/)：[Neal Cardwell 的回复](https://mailarchive.ietf.org/arch/msg/iccrg/ERsHqvYQLs7cNCxOPw13U5Jl9sg/)提供了 paced/unpaced CUBIC 时序图和 BBR Startup gain 推导；[后续问题](https://mailarchive.ietf.org/arch/msg/iccrg/a59n7DRXZr4q6it_djAZh23wuas/)继续讨论连续模型算出 `2 ln 2` 所依赖的假设，以及它与 Linux 系数 2 的关系。

## 编译

当前测试程序面向 Linux。

```bash
git clone https://github.com/skaiui2/SCP.git
cd SCP

mkdir -p test/nodeA/build test/nodeB/build

cd test/nodeA/build
cmake ..
make -j$(nproc)

cd ../../nodeB/build
cmake ..
make -j$(nproc)
```

需要采集控制器状态并绘图时，把 `scp.h` 中的 `SCP_DUMP_SS` 从 `0` 改成 `1`，然后重新编译两端。不需要时间序列时改回 `0`。JSON 日志可能干扰性能，因此实验记录必须注明是否开启。

新增测试源文件后，需要先重新执行 `cmake ..` 再运行 `make`，因为当前 CMake 会在配置阶段扫描源文件。

## 回归测试

统一测试入口包含五项测试：

| 测试名 | 目的 |
|---|---|
| `prob-100m` | 使用 SCP-PROB 单向发送 100 MiB 文件 |
| `aimd-100m` | 使用 AIMD 单向发送 100 MiB 文件 |
| `pacing` | 传输并校验 8 MiB，同时记录真实发送回调时间 |
| `prob-prob` | 测量两条持续有数据的 PROB 流之间的公平性 |
| `prob-aimd` | 测量持续有数据的 PROB 与 AIMD 的共存情况 |

100 MiB 测试严格保留历史单向文件测试：

```text
nodeA/build/testA.bin  -->  nodeB/build/outB.bin
```

测试程序不会生成 `testA.bin`。运行前必须准备一个严格等于 100 MiB 的输入文件：

```bash
cd test/nodeA/build
truncate -s 100M testA.bin
```

### 建立网络拓扑

仓库提供三 namespace 拓扑：

```text
scp-c（客户端） <--> scp-r（路由器/netem） <--> scp-s（服务端）
```

下面模拟双向各 5 Mbit/s、单向 50 ms 延迟、0% 随机丢包和 1000 包队列：

```bash
cd SCP
sudo ./setup_scp_netns.sh setup 5mbit 50ms 0% 1000
sudo ./setup_scp_netns.sh ping
```

### 回归冒烟测试

`all` 用于检查所有 case 能否顺序跑通。它会混合多个独立实验和重新开始的时钟，因此输出**不能**用于绘制性能图。

先运行 nodeB：

```bash
cd test/nodeB/build
sudo ip netns exec scp-s ./nodeB all 10
```

再运行 nodeA：

```bash
cd test/nodeA/build
sudo ip netns exec scp-c ./nodeA 10.0.2.1 all 10
```

最后一个参数是每项公平性实验的测量时间，单位为秒。

### 性能与绘图实验

每个实验必须单独运行，并分别保存 A/B 日志。下面以 `prob-prob` 为例，先运行接收端：

```bash
cd SCP/test/nodeB/build
sudo ip netns exec scp-s ./nodeB prob-prob 300 \
    | tee nodeB-prob-prob.log
```

再运行发送端：

```bash
cd SCP/test/nodeA/build
sudo ip netns exec scp-c ./nodeA 10.0.2.1 prob-prob 300 \
    | tee nodeA-prob-prob.log
```

其他测试使用对应文件名，例如 `nodeA-prob-100m.log`、`nodeA-aimd-100m.log` 和 `nodeA-prob-aimd.log`。不能把无关 case 追加到同一个性能日志。

### 校验文件完整性

```bash
cd SCP
stat -c '%n %s' \
    test/nodeA/build/testA.bin \
    test/nodeB/build/outB.bin

md5sum \
    test/nodeA/build/testA.bin \
    test/nodeB/build/outB.bin
```

两个文件都必须为 104857600 字节，并且哈希值必须相同。由于输入文件由用户提供，因此 README 不再写死某个哈希值。

## 当前验证快照

以下结果来自 **2026-08-19** 的一次真实运行，只是透明记录，不是多轮基准测试，也不代表普遍性能结论。

环境：

```text
双向速率：        每方向 5 Mbit/s
单向延迟：        50 ms
预期 RTT：        约 100 ms
队列上限：        1000 包
文件大小：        100 MiB
公平性测量时间：  300 秒
```

### 0% 随机丢包

| 测试 | 观测结果 |
|---|---|
| PROB 单流 | 接收端有效吞吐 4.686 Mbit/s |
| AIMD 单流 | 接收端有效吞吐 4.746 Mbit/s |
| pacing | 校验 8 MiB；发送回调平均间隔 2.200 ms；同一毫秒最多观测到 4 个 DATA 包 |
| PROB 对 PROB | 总有效吞吐 4.735 Mbit/s；Jain 指数 0.641；流量比 6.95:1 |
| PROB 对 AIMD | 总有效吞吐 3.421 Mbit/s；Jain 指数 0.504；PROB 仅获得接收字节的 0.36% |

100 MiB 文件通过了端到端哈希校验。

### 10% 随机丢包

| 测试 | 观测结果 |
|---|---|
| PROB 对 PROB | 总有效吞吐 2.044 Mbit/s；Jain 指数 0.999887；流量比 1.022:1 |

在这次快照中，两条 PROB 流在 10% 随机丢包下分配接近均衡，在 0% 随机丢包下分配不均；有丢包时的总有效吞吐较低。PROB 的分配行为及其与 AIMD 的共存仍需进一步研究。表格中的控制器吞吐、公平性测量，与传输层的文件完整性检查分别记录。

测试输出中的 `"status":"success"` 记录测试执行成功，并通过相应的协议或完整性检查；吞吐和公平性作为独立指标报告。

## 日志分析

开启 `SCP_DUMP_SS=1` 后，SCP 会输出 `type:"ss"` 状态记录。每条记录包含 `stream_fd`，因此可以分别绘制公平性实验中的两条流。

为发送端两条流分别生成完整诊断图：

```bash
cd SCP
python3 test/analyze_scp.py test/nodeA/build/nodeA-prob-prob.log \
    --stream-fd 1 --out test/nodeA/build/plots-prob-prob-flow1

python3 test/analyze_scp.py test/nodeA/build/nodeA-prob-prob.log \
    --stream-fd 2 --out test/nodeA/build/plots-prob-prob-flow2
```

生成论文用紧凑图片：

```bash
python3 test/fig.py test/nodeA/build/nodeA-prob-prob.log \
    --stream-fd 1 --out test/nodeA/build/paper-prob-prob-flow1

python3 test/fig.py test/nodeA/build/nodeA-prob-prob.log \
    --stream-fd 2 --out test/nodeA/build/paper-prob-prob-flow2
```

单流日志可以省略 `--stream-fd`。禁止把 `all` 日志直接交给绘图脚本，因为它包含互不相关的 case 和多次归零的时钟。

拥塞窗口和性能曲线会随拓扑、算法版本和随机种子变化，因此 README 不再把旧图片当作当前证据。报告实验时应同时保存命令、源码版本、构建模式、netem 参数和原始日志。

## 适用场景与限制

SCP 适合：

- 拥塞控制和可靠传输研究；
- 在自定义不可靠介质之上的用户态或嵌入式传输；
- 受控的内部通信和文件传输实验；
- 学习和观察传输协议机制。

当前限制包括：

- 所测 PROB 配置在 0% 随机丢包下的流量分配问题仍待解决，包括与 AIMD 竞争时的表现；
- 协议核心没有生产级安全层、认证或加密；
- 不兼容 TCP；
- 不保证线格式和 API 稳定；
- 尚未经过大规模互操作、对抗性测试或生产部署审计。

## AI 辅助声明

作者 **Skai Uijing** 对本项目的人与 AI 分工作如下说明：

- **协议代码：**`scp.c` 和 `scp.h` 基本由作者编写，AI 辅助审核和检查错误，也参与了 pacing 机制的辅助工作。
- **测试与绘图：**测试代码基本由 AI 生成，包括大文件传输等测试，作者检查其合法性与适用性；画图脚本也由 AI 生成。
- **SCP-PROB 论文与控制设计：**概率驱动设计、残差算法以及本项目中的 AIMD 控制环设计由作者完成，这不表示作者发明了 AIMD 本身。AI 提供论文翻译、编写帮助，并辅助检查了部分数学论证，例如贝叶斯推导与公平性分析；这些辅助检查不等于相关结论已获证明。
- **Pacing 研究：**作者提出了三明治比较模型，以及 pacing 系数很大时趋近于 burst、过小时会拖慢窗口增长的思路。作者进一步提出研究是否存在一个系数，既不拖慢窗口增长，也不让数据包发送越来越集中。这是分析的出发点，不是对所有 pacer 都成立的普遍保证。
- **后续建模与推导：**采用微分方程分析、把模型表示为速率等方法，以及上述作者思路之外的其他扩展分析，由 GPT 6 ASTRA 提出。由于生成内容较多，作者尚未逐项独立验证这些分析与推导。请读者仔细判断其假设、证明和适用范围，包括 pacing 论文中的相关内容；AI 的检查不能代替作者验证或独立审查。

本 README 使用 AI 辅助编辑。报告的实验测量来自运行日志和文件完整性检查，而不是 AI 生成的断言。代码审查、实验证据与数学结论的验证是不同的工作，不能相互替代。

## 许可证

SCP 使用 [MIT License](../../LICENSE)。

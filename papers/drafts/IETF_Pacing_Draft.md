# Pacing and Congestion-Control Dynamics
# Pacing 与拥塞控制动态

Skai Uijing
Intended status / 预期类别：Informational
English text with paragraph-aligned Chinese translation / 英文正文及逐段中文译文

Working draft / 讨论草稿，2026-10-25；current next-slot study, not an adopted IETF document / 当前next-slot研究，尚非IETF采纳文件。

## Abstract / 摘要

**English**

This work began with a slowdown observed after introducing pacing into SCP. Following the last packet, its returning acknowledgment, and the new packets it permits connects window growth with packet spreading. The development follows continuous window growth, discrete packet timing, sandwich bounds, and experiments. SCP uses fixed phase gains: 2 ln 2 in slow start and (3/2) ln 3 in congestion avoidance. The latter is an engineering choice supported by the birth-front integral and gain sweeps. Independent Python results and actual SCP measurements in a fixed-delay virtual network are labelled separately. The discussion also relates these mechanisms to Reno, CUBIC and BBR, without claiming measurements of those other stacks.

**中文**

研究起于给 SCP 引入 pacing 后观察到的传输变慢。沿最后一个包、它返回的确认及释放的新包追踪，为窗口增长与包分散建立联系。研究依次经过连续窗口增长、离散包时序、三明治夹逼与实验。SCP 采用固定阶段系数：慢启动为 2 ln 2，拥塞避免为 (3/2) ln 3；后者由出生点积分与扫参共同支持。独立 Python 结果与实际 SCP 核心在固定时延虚拟网络中的测量分别标注。本文还讨论它们与 Reno、CUBIC、BBR 的联系，但不声称测量了其他协议栈。

## 1. Introduction / 引言

**English**

A congestion window limits outstanding data. Pacing distributes eligible transmissions over time. Acknowledgments connect these two functions: a packet must leave the sender and cross the path before its acknowledgment can release window space or contribute to further growth. The congestion controller therefore operates on feedback whose timing depends in part on the sender's own scheduling decisions.

**中文**

拥塞窗口限制尚未确认的数据量。Pacing 将获准发送的数据分布在时间上。确认将这两项功能联系起来：数据包必须先离开发送端并经过网络路径，其确认才能释放窗口空间或推动进一步增长。因此，拥塞控制器所使用的反馈，其时序在一定程度上取决于发送端自身的调度决策。

**English**

This interaction is particularly visible during startup. Rapid window growth continuously creates new sending opportunities, while the pacer determines how soon they become transmissions. Spreading those transmissions changes the feedback that drives later growth. Conversely, the growing window changes the amount of data to be spread and, for a window-based pacer, the rate used to spread it.

**中文**

这种相互作用在启动阶段尤为明显。窗口快速增长，不断创造新的发送机会，而 pacer 决定这些机会多快能够变成实际发送。分散发送改变了推动后续增长的反馈；反过来，增长中的窗口又改变了需要分散的数据量，并且对窗口驱动的 pacer 而言，还改变了用于分散这些数据的速率。

**English**

Startup delay and transfer completion time are established concerns in TCP pacing [TCP-PACING]. Transport specifications describe how pacing rates are derived and applied [RFC9002] [PACING]. The SCP implementation question motivated the author's construction: trace a packet to its returning ACK, then follow the last new packet permitted by that ACK. The following sections retain this path from a small example to group spans, and explain why congestion avoidance requires a different accounting of the new sending credit. Independent model calculations and actual SCP measurements are identified separately throughout.

**中文**

启动延迟和传输完成时间是 TCP pacing 研究已有的关注点 [TCP-PACING]。传输规范说明了 pacing 速率如何计算和应用 [RFC9002] [PACING]。SCP 的实现问题促使作者提出这个构造：跟踪一个包到它返回的 ACK，再考察该 ACK 允许的最后一个新包。下文保留从小特例走到包组跨度的路径，并解释 CA 为什么需要重新计算新发送额度。独立模型计算与真实 SCP 测量始终分别标明。

## 2. Feedback and Transmission Timing / 反馈与发送时序

**English**

An arriving ACK can release space in the congestion window, update the congestion controller, and cause more data to be scheduled. Actual transmission may follow immediately or wait behind other reservations. The next feedback delay begins when the packet is transmitted. This sequence repeats as the connection grows.

**中文**

到达的 ACK 可以释放拥塞窗口空间、更新拥塞控制器，并使更多数据进入发送计划。实际发送可能立即发生，也可能需要等待已有预约。下一次反馈时延从数据包实际发送时开始计算。连接增长过程中，这个过程不断重复。

**English**

The examples in Sections 3 and 4 use a fixed send-to-ACK delay R, continuously available data, full-sized packets, one ACK per packet, and no loss. During slow start, each ACK increases the window by one packet on arrival. Windows observed at RTT boundaries therefore include all ACKs processed through those times. Between boundaries, ACK processing and packet scheduling continue normally.

**中文**

第 3 节和第 4 节的例子采用固定的发送到确认时延 R、持续可用的数据、完整大小的数据包、每包一个 ACK 和无丢包条件。慢启动中，每个 ACK 在到达时使窗口增加一个包。因此，RTT 边界处观察到的窗口包含截至该时刻已处理的全部 ACK。两个边界之间，ACK 处理和包调度照常进行。

| Term / 术语 | Meaning / 含义 |
| --- | --- |
| cwnd | Congestion window. / 拥塞窗口。 |
| MSS | Payload size of a full-sized data packet. / 完整数据包的负载大小。 |
| Flight | Transmitted data awaiting acknowledgment in the loss-free analysis. / 无丢包分析中，已发送且等待确认的数据。 |
| Reservation | A future transmission admitted by the sender and assigned a sending time. / 发送端已接纳并分配发送时刻、尚未实际发送的数据。 |
| R | Time from actual DATA transmission to processing its returning ACK. / 从 DATA 实际发送到处理其返回 ACK 的时间。 |
| Gain g | Multiplier applied to a specified reference rate. The window-based examples use cwnd divided by the RTT estimate. / 施加于特定基准速率的乘数。窗口驱动例子的基准为 cwnd 除以 RTT 估计值。 |

## 3. Congestion-Window Growth During Startup / 启动阶段的拥塞窗口增长

### 3.1. From One ACK to the Next / 从一次确认到下一次确认

**English**

With the specified slow-start update, one ACK removes a packet from flight and adds a packet to cwnd. Together these changes make room for two new packets. Their acknowledgments can enlarge the window further, but the timing of that increase depends on when the two packets actually depart.

**中文**

采用上述慢启动更新规则时，一个 ACK 从 flight 中移除一个包，同时向 cwnd 增加一个包。这两项变化共同腾出两个新包的空间。它们的确认可以继续扩大窗口，而这次增长何时发生，取决于两个包实际离开发送端的时间。

**English**

For example, send one packet at time zero. Its ACK arrives at R and increases cwnd to two packets. If the next two packets depart at R and 1.5R, the window reaches three packets at 2R and four at 2.5R. Sending both at R would make the window reach four at 2R. Each sender updates cwnd immediately on every ACK; their different departure times account for the different growth in time.

**中文**

例如，在时间零点发送一个包。它的 ACK 在 R 到达，使 cwnd 增加到两个包。如果接下来的两个包在 R 和 1.5R 发出，窗口就在 2R 达到三个包，在 2.5R 达到四个包。若两个包都在 R 发出，窗口便会在 2R 达到四个包。两个发送端都在每个 ACK 到达时立即更新 cwnd；不同的实际发送时间造成了不同的时间增长过程。

### 3.2. Comparison Models / 比较模型

**English**

With fixed feedback delay, the packets transmitted in one RTT interval are acknowledged in the following interval. Counting those transmissions therefore determines the next window increment. This accounting provides a way to compare scheduling rules while preserving the same per-ACK growth rule.

**中文**

反馈时延固定时，一个 RTT 区间内发送的包，会在随后的区间内得到确认。因此，统计实际发送量就能确定下一段时间的窗口增量。这种计数关系提供了比较调度规则的方法，同时保留相同的逐 ACK 增长规则。

**English**

Two schedules starting with one packet give useful reference sequences. A sender that immediately uses every new window position reaches windows of 1, 2, 4, 8, and so on at successive RTT boundaries. A sender that fixes a batch at each boundary and spaces its packets uniformly over the following RTT, sending the first packet at the boundary, reaches 1, 2, 3, 5, 8, and so on. In the second schedule, ACKs still update cwnd immediately; their increases affect the next batch. The Fibonacci sequence follows from this choice of when additional window space enters the sending plan [GROWTH].

**中文**

从一个包开始的两种调度给出了有用的参考序列。立即使用每一份新增窗口空间的发送端，在连续 RTT 边界达到 1、2、4、8 等窗口。每个边界确定一批包、将它们均匀分布在随后一个 RTT 内，并在边界发送首包的发送端，则达到 1、2、3、5、8 等窗口。第二种调度仍在 ACK 到达时立即更新 cwnd，其增加量影响下一批计划。斐波那契序列来自这种将新增窗口空间纳入发送计划的时机 [GROWTH]。

**English**

An ACK-driven append scheduler uses new window space as each ACK arrives. It appends the two newly admitted packets to the pacing queue, computes their spacing from the updated window and gain, and leaves earlier timestamps unchanged. For the scheduler that also preserves a final spacing after the queue empties, gains of at least one give window growth bounded by the Fibonacci and immediate-send sequences [GROWTH]. The bound concerns this scheduling rule; the append scheduler's own window can grow faster than the Fibonacci sequence.

**中文**

ACK 驱动的追加调度器在每个 ACK 到达时使用新增窗口空间，将新接纳的两个包追加到 pacing 队尾，根据更新后的窗口和增益计算它们的间隔，并保持先前的时间戳不变。对于在队列排空后还保留最后一段间隔的调度器，至少为一的增益使窗口增长介于斐波那契序列和立即发送序列之间 [GROWTH]。这个界对应上述调度规则；追加调度器自身的窗口可以比斐波那契序列增长得更快。

### 3.3. Growth Rate and Window Trajectory / 增长速率与窗口轨迹

**English**

A low pacing rate can leave reservations continuously backlogged, extending the time needed for a generation of ACKs to produce the next generation. Increasing the rate can shorten that time. In the append model, a gain of 2 ln 2, approximately 1.386294, is the boundary at which the asymptotic window multiplier reaches two per RTT. Above the boundary, the multiplier remains two while departures move earlier within each round [GROWTH].

**中文**

较低的 pacing 速率可能使预约持续积压，延长一代 ACK 产生下一代 ACK 所需的时间。提高速率可以缩短这段时间。在追加模型中，增益 2 ln 2（约为 1.386294）是渐近窗口倍率达到每 RTT 两倍的边界。超过这一边界后，倍率保持为两倍，而实际发送在每轮内部提前 [GROWTH]。

**English**

The growth multiplier describes only part of the trajectory. An early delay can leave one window smaller than another even after both begin doubling. Their relative size then remains unchanged at corresponding boundaries. ACKs can also occupy different parts of the RTT, producing different internal window trajectories between identical boundary samples. Window size, growth multiplier, and time to reach a given amount of acknowledged data consequently answer different questions.

**中文**

增长倍率只描述轨迹的一部分。早期延迟可能使一个窗口较小，即使之后两个窗口都开始翻倍。在对应边界，它们的相对大小便保持不变。ACK 也可能分布在 RTT 的不同位置，使相同边界采样之间出现不同的内部窗口轨迹。因此，窗口大小、增长倍率和确认给定数据量所需的时间，分别回答不同的问题。

## 4. Packet Distribution / 数据包分布

### 4.1. Transmission Span / 发送跨度

**English**

Packet spacing is interpreted together with the amount of data being sent. As a window grows, the sender can transmit more packets at shorter intervals while still spreading them across an entire RTT. A decrease in adjacent-packet spacing alone therefore leaves the overall concentration undetermined.

**中文**

理解包间隔时，需要同时考虑正在发送的数据量。窗口增长后，发送端可以以更短的间隔发送更多数据包，同时仍将它们分散在整个 RTT 中。因此，仅凭相邻包间隔缩短，还无法确定整体发送的集中程度。

**English**

Time zero is the first DATA transmission. For the pacing question, a descendant group contains the packets released by the preceding group's ACKs, and L is its last actual DATA send minus its first. Groups may move relative to fixed RTT boundaries. We also sample cwnd and fixed-bin span in [nR, (n+1)R); that span uses a different packet set and is labelled separately. One departure gives a span of zero; an empty bin has no span. Smaller bins describe density and gaps within a group.

**中文**

首个 DATA 实际发送为时间零点。研究 pacing 时，后代包组由前一组 ACK 释放的新包构成，L 是同一组末次与首次 DATA 实际发送之间的时间差；包组可以相对固定 RTT 边界移动。我们也在 [nR, (n+1)R) 采样 cwnd 和固定箱跨度；后者包含的数据包不同，单独标注。只有一次发送时跨度为零，空箱不定义跨度。更小时间箱揭示组内密度与空隙。

### 4.2. Sending Periods and Idle Periods / 发送时段与空闲时段

**English**

A sender can maintain regular local spacing and still concentrate its traffic into short sending periods. After using the available window, it waits for further ACKs. Raising its pacing rate can shorten the sending periods and lengthen the intervening waits. The resulting traffic remains paced within each period, while more of the RTT is idle.

**中文**

发送端可以保持局部有规律的包间隔，同时将流量集中在较短的发送时段内。可用窗口用完后，它等待进一步的 ACK。提高 pacing 速率可以缩短发送时段，延长中间的等待。每段内部的流量仍然经过 pacing，而 RTT 中有更多时间处于空闲状态。

**English**

In the append model, consecutive packet groups recur one RTT apart at and above the doubling boundary. Their first-to-last span approaches an RTT at the boundary and a smaller fraction of an RTT at higher gains. Increasing gain without bound makes each group's packet times converge to the immediate-send schedule. Finite groups approach these limits gradually [GROWTH].

**中文**

在追加模型中，翻倍边界及以上的连续包组每隔一个 RTT 出现一次。在边界处，它们的首末包跨度逐渐接近一个 RTT；在更高增益下，跨度逐渐接近 RTT 中更小的一部分。不断增大增益，各组的包时刻便趋近立即发送计划。有限包组逐步接近这些极限 [GROWTH]。

**English**

These analytical groups are defined by packet indices. Below the boundary, a group can extend across more than one RTT. A span measured in a fixed RTT interval and a span calculated for an indexed group refer to different sets of packets. Comparisons retain the same grouping and boundary convention.

**中文**

这些分析用包组按数据包编号定义。低于边界时，一组可能跨越超过一个 RTT。固定 RTT 区间中的实测跨度与编号包组的计算跨度，对应不同的数据包集合。比较时需要保持分组方式和边界约定一致。

## 5. Startup in Reno, CUBIC, and BBR / Reno、CUBIC 与 BBR 的启动

### 5.1. Reno / Reno

**English**

Reno slow start increases cwnd from newly acknowledged data [RFC5681]. With one ACK per full-sized packet, the reference update in Section 2 gives the two sending positions described in Section 3.1. A window-based pacer derives its rate from cwnd and an RTT estimate. Its scheduling of those positions then determines the subsequent ACK timing. Delayed or aggregated ACKs require the actual acknowledgment and byte-counting rules to be included in that calculation.

**中文**

Reno 慢启动根据新确认的数据增加 cwnd [RFC5681]。每个完整包对应一个 ACK 时，第 2 节中的参考更新产生第 3.1 节所述的两个发送位置。窗口驱动的 pacer 根据 cwnd 和 RTT 估计值确定速率，对这些位置的调度进一步决定后续 ACK 时序。采用延迟确认或聚合确认时，需要将实际确认规则和字节计数规则纳入计算。

### 5.2. CUBIC / CUBIC

**English**

CUBIC uses a slow-start algorithm before entering its CUBIC window-growth phase. RFC 9438 recommends HyStart++, which starts with rapid growth and uses delay observations to enter a conservative phase before congestion avoidance [RFC9438] [RFC9406]. While the same per-packet window update and scheduling assumptions hold, the feedback example in Section 3 also applies to a CUBIC connection. The conservative phase and the exit decision introduce their own growth and timing rules.

**中文**

CUBIC 在进入自身的窗口增长阶段之前使用慢启动算法。RFC 9438 推荐 HyStart++：它先快速增长，再依据时延观测进入保守阶段，随后进入拥塞避免 [RFC9438] [RFC9406]。在逐包增窗规则和调度假设相同的阶段，第 3 节的反馈例子也适用于 CUBIC 连接。保守阶段和退出决策则引入各自的增长与时序规则。

### 5.3. BBR Startup / BBR Startup

**English**

BBR Startup raises the sending rate to discover available bandwidth. Its pacer uses a delivery-rate estimate, and arriving ACKs update the path model and control parameters [BBR]. This creates a related feedback process: scheduled transmissions generate the measurements used to set later transmissions. The analysis must follow the delivery-rate estimator as well as the window constraint.

**中文**

BBR Startup 提高发送速率以发现可用带宽。它的 pacer 使用交付速率估计，到达的 ACK 更新路径模型和控制参数 [BBR]。这形成了一个相关的反馈过程：被调度的发送产生测量结果，再用于确定后续发送。分析需要同时跟踪交付速率估计器和窗口约束。

**English**

BBR's Startup pacing-gain analysis derives 4 ln 2, about 2.77, to support rate doubling [BBR-GAIN]. The window-based append model reaches its doubling boundary at 2 ln 2. Each value belongs to its reference rate and feedback model. A comparison of their startup behavior therefore follows the evolution of the rate estimate, cwnd, and actual packet departures.

**中文**

BBR 的 Startup pacing gain 分析，从维持速率翻倍得到 4 ln 2，约为 2.77 [BBR-GAIN]。窗口驱动的追加模型在 2 ln 2 达到翻倍边界。每个数值都对应各自的基准速率和反馈模型。因此，比较它们的启动行为，需要跟踪速率估计、cwnd 和实际发包的演化。

## 6. Implementation Measurements / 实现测量

### 6.1. Current Scheduling and Conditions / 当前调度与条件

**English**

The recorded next-slot campaigns execute the actual SCP sender, receiver and ACK paths over a fixed-delay virtual network [EXPERIMENT] [CA-EXPERIMENT]. Each phase has 22 fixed gains with three repetitions, plus three native-policy controls. Every native transfer carries exactly 1 GiB, starts at one MSS (1430 bytes), and has 50 ms propagation per direction, no loss and no serialization. Complete DATA-send to ACK-processing feedback is verified as 100 ms. SS and CA are separate campaigns; CA starts with ssthresh equal to one MSS. Their fixed-gain scans retain their source revisions. Earlier CA controller-policy entries used the adaptive continuous reference; the current controller uses fixed (3/2) ln 3.

**中文**

此前记录的两批 next-slot 实验运行真实 SCP 的发送、接收与 ACK 路径 [EXPERIMENT] [CA-EXPERIMENT]。每阶段22个固定gain各重复三次，另加三次原生策略对照。每次原生传输精确1 GiB，初始窗口一MSS（1430字节），双向各50 ms，无丢包、无序列化。逐包核对实际发送到ACK处理的完整反馈为100 ms。SS与CA分开运行，CA初始ssthresh也为一MSS。固定gain扫描保留各自源码版本；此前CA控制器策略项使用连续动态参考式，当前控制器使用固定(3/2)ln3。

**English**

The scheduler retains the next available time across empty queues. A batch starts at max(next_slot, now); each packet receives that timestamp before its own gap advances the slot. This takes the next-slot principle from Linux TCP [LINUX], without claiming to reproduce its jitter compensation, initial-segment exemption, offloads or qdisc behavior. Reservations use Q32.32 milliseconds, gains use Q16, and a fixed 1 ms clock checks due packets using the reservation's integer part. The network advances every tick; it does not jump between events. Retired empty-queue-reset results are not used for the current claims.

**中文**

调度器在队列清空后仍保留下一可用时刻。每批从max(next_slot, now)开始，先给包分配时刻，再按该包gap推进slot。它参考Linux TCP的next-slot骨架 [LINUX]，不声称复刻jitter补偿、前期分段豁免、offload或qdisc。预约为Q32.32毫秒、gain为Q16，固定1 ms时钟按预约整数部分检查到期。网络逐tick前进，不跳事件时间。当前主张不引用已弃用空队列重置版本的数值。

### 6.2. Slow-Start Results / 慢启动结果

**English**

For eight sampled gains from 1 through 2 ln 2 minus 0.05, the measured RTT-18/RTT-17 window ratio differs from exp(g_Q/2) by less than 0.003%. Gain 1 gives 1.648756. At the critical target, the effective Q16 gain is 1.38629150390625, slightly below mathematical 2 ln 2. The early boundary windows are 1, 2, 3, 5, 9, 17, 33, and 65 MSS. A finite trace and a quantized gain do not prove an infinite-time critical value [GROWTH].

**中文**

从1到2 ln2减0.05的八个采样gain，实测W18/W17相对exp(g_Q/2)的差小于0.003%；g=1为1.648756。临界目标的Q16有效值为1.38629150390625，略低于数学2 ln2。早期边界窗口为1、2、3、5、9、17、33和65 MSS。有限记录和量化gain不能证明无限时间临界值 [GROWTH]。

| Target gain / 目标gain | Fixed-bin L at RTT 18 / 固定箱跨度(ms) | Final DATA ACK / 最后DATA ACK(ms) |
|---|---:|---:|
| 2 ln 2 | 99 | 2051 |
| 2 | 69 | 2035 |
| 8 | 17 | 2008 |

**English**

Completion improves by about 2.10% from the critical target to gain 8, while the largest 1 ms burst share at RTT 18 rises from 1.3813% to 7.7396%. Fixed-bin spans cannot establish absence of crossing: a complete indexed cohort at gain 1 spans 138 ms in the native run despite 99 ms fixed-bin spans. The current reproduction tables retain the two group definitions separately [REPRODUCE].

**中文**

从临界目标到g=8，完成时间改善约2.10%，RTT18最拥挤1 ms的包数份额却由1.3813%增至7.7396%。固定箱不能证明不跨界：g=1某个完整编号组的原生跨度为138 ms，固定箱仍为99 ms。当前复现表分别保存两种分组 [REPRODUCE]。

### 6.3. Model and Integer Differences / 模型与整数差异

**English**

The rebuilt independent Python sender, receiver and network advance in 1 us ticks. The sender retains fractional next-slot reservations and dispatches their integer microsecond parts; the network separately delivers DATA and ACKs after 50 ms each. It uses real-valued CA, unquantized gains and full-MSS packets. SCP instead uses integer-byte CA, Q16 gain and 1 ms dispatch. The old Python script floored each gap, erasing spacing at large windows: its gain-1 SS completion was 2521.217 ms. After retaining fractions, completion is 2682.611 ms, against native 2682 ms. A separate ACK-index evaluator checks the rebuilt model's output. Its mathematical reference is not protocol execution.

**中文**

重建后的独立Python发送方、接收方和网络按1 us逐tick前进。发送方保留next-slot预约小数，按整数微秒派发；网络分别把DATA与ACK延迟50 ms。它使用实数CA、未量化gain和完整MSS包；SCP则使用整数字节CA、Q16 gain和1 ms派发。旧Python逐gap截断，使大窗口间隔消失，g=1的SS完成时间为2521.217 ms；保留小数后为2682.611 ms，原生为2682 ms。另一个ACK序号求值器检查新模型输出，它提供数学参考，不是协议栈执行。

**English**

In CA, every observed native ACK matches C += max(1, floor(MSS * acked / C)). At 750868 full-packet ACKs, native C is 1476947 bytes versus 1752403.2934 bytes in the real recurrence, a 15.719% deficit. Integer thresholds also explain the sawtooth in gain-dependent window differences: the faster flow reaches the next lower increment before the reference flow. This must be separated from pacing's effect on ACK timing [CA-EXPERIMENT].

**中文**

CA中，每次原生ACK均匹配C += max(1, floor(MSS * acked / C))。750868次完整包ACK后，原生C为1476947字节，实数递推为1752403.2934字节，差15.719%。整数门槛也解释不同gain窗口差图的锯齿：较快的流先进入更低的每ACK增窗档位。它必须与pacing改变ACK时序的作用分开 [CA-EXPERIMENT]。

## 7. The Network Path / 网络路径的作用

**English**

A bottleneck serializes arriving packets. A burst entering the bottleneck can leave it as a packet train, and the resulting ACKs can spread later transmissions. Thus, packet spreading can arise at the sender and along the path. As startup fills the bottleneck, the path's service rate increasingly shapes the feedback received by the congestion controller.

**中文**

瓶颈逐个串行发送到达的数据包。进入瓶颈的突发可以在离开时成为包列，随后产生的 ACK 又可以分散后续发送。因此，发送端和网络路径都可以产生数据包分散。随着启动过程逐渐填满瓶颈，路径服务速率越来越多地塑造拥塞控制器收到的反馈。

**English**

Paced and unpaced Linux CUBIC connections can reach nearly the same transmission and acknowledgment progress by the end of slow start [CUBIC-TRACES]. This behavior is consistent with bottleneck serialization gradually spreading an initially unpaced flow: once the bottleneck remains busy, its service rate governs departures and the returning ACK stream. Similar end-to-end progress can therefore coexist with different sender departure patterns and queue occupancy along the path.

**中文**

采用和未采用 pacing 的 Linux CUBIC 连接，可以在慢启动结束时达到非常接近的发送与确认进度 [CUBIC-TRACES]。这一行为与瓶颈串行化逐步分散原本未采用 pacing 的流量相符：瓶颈持续忙碌后，其服务速率决定离开瓶颈的数据包时序及返回的 ACK 流。因此，相近的端到端进度可以与不同的发送端发包分布、不同的路径队列占用同时存在。

**English**

On a capacity-limited path, the receiver sees a packet distribution shaped by both sender pacing and bottleneck service. Sender departure times alone cannot determine that distribution or the resulting queueing delay and loss. Measurements on both sides of the bottleneck relate changes in spreading to path behavior and transfer completion time. RTT variation, ACK grouping, and competing traffic further alter the feedback through their effects on data and acknowledgment arrival times.

**中文**

在容量受限的路径上，接收端看到的数据包分布由发送端 pacing 和瓶颈服务共同塑造。仅凭发送端的发包时刻，还无法确定这一分布及其产生的排队时延和丢包。瓶颈两侧的测量可以将分散程度的变化与路径行为、传输完成时间联系起来。RTT 变化、ACK 分组和竞争流量，还会通过影响数据与确认的到达时间进一步改变反馈。

## 8. Congestion Avoidance: From Window Growth to a Fixed Gain / 拥塞避免：从窗口增长到固定系数

### 8.1. Continuous Reference / 连续参考

**English**

After startup, each controller applies its own window or rate update [RFC5681] [RFC9438] [BBR]. For the real-valued Reno-style reference, normalize time by R and window by MSS. One ACK adds 1/w. If q(tau) is the actual sending rate, fixed feedback gives w w' = q(tau-1). Integrating for one RTT relates the current flight to the future window: f(tau) = [w(tau+1)^2 - w(tau)^2]/2. A continuously full window therefore obeys w_(n+1) = sqrt(w_n^2 + 2w_n), with large-window expansion w_n = n - (1/2) ln n + O(1).

**中文**

启动之后，各控制器采用自身的窗口或速率更新 [RFC5681] [RFC9438] [BBR]。在实数 Reno 式参考中，时间按 R、窗口按 MSS 归一化，每个 ACK 增加 1/w。若 q(tau) 为实际发送率，固定反馈给出 w w' = q(tau-1)。积分一个 RTT，将当前 flight 与未来窗口联系起来：f(tau) = [w(tau+1)^2 - w(tau)^2]/2。持续满窗口时得到 w_(n+1) = sqrt(w_n^2 + 2w_n)，大窗口下 w_n = n - (1/2) ln n + O(1)。

**English**

Two useful intermediate results come from this continuous picture. A single group's service budget tends to 1/g RTT, while maintaining continuous full flight gives q = (w+1)w' and the first-order reference g(w) = 1 + 1/(2w). These describe different budgets. Neither alone accounts for a whole packet waiting for its parent's ACK or inheriting an earlier reservation. The adaptive formula is retained as a modelling step, rather than used as the current packet-level policy.

**中文**

这幅连续图景给出两个有用的中间结果：单轮服务预算趋于 1/g 个 RTT；持续满 flight 则由 q = (w+1)w' 得到一阶参考式 g(w) = 1 + 1/(2w)。它们计算的是不同预算，单独使用都没有处理整包必须等待父包 ACK、并继承旧预约时序的情况。动态公式作为建模过程保留，当前包级策略使用固定系数。

### 8.2. Discrete Birth Front and the Sandwich / 离散出生点与三明治

**English**

An ordinary CA ACK releases a replacement packet; crossing the next integer window boundary also releases an extra full packet. Replacement packets can preserve their parents' group phases, even though the newly computed gaps are smaller. The author's construction follows the rank r_p of that extra packet's birth, rather than adding all gaps of just one RTT. With u_(k+1) = u_k + 1/u_k, the ACK count needed to reach window p is K_p = p^2/2 + O(ln p). Across successive birth groups beginning at q, this gives r_p - 1 = (3/2)(p-q) + O(ln q). The birth point advances about 3/2 positions per group; the tail grows by about one. Consequently it reaches the tail as the group size approaches 3q.

**中文**

普通 CA ACK 释放一个替换包；窗口越过下一个整数门槛时，还释放一个额外完整包。新 gap 虽然更短，替换包仍可能保留父包在组内的相位。作者的构造考察额外包出生的排名 r_p，而不是只加一轮 RTT 的所有 gap。由 u_(k+1) = u_k + 1/u_k，达到窗口 p 的 ACK 数满足 K_p = p^2/2 + O(ln p)。从 q 开始的连续出生组因此满足 r_p - 1 = (3/2)(p-q) + O(ln q)。出生点每组约前进 3/2 个位置，组尾约增加一个位置，所以当组大小接近 3q 时追上尾部。

**English**

Sandwich each new spacing between its neighbouring window values and sum along this moving birth point. The leading work is (3/(2g)) integral_q^(3q) dp/p = (3 ln 3)/(2g). This explains the CA gain scale C = (3/2) ln 3, approximately 1.647918433, and motivates selecting C to spread a group across an RTT. The calculation identifies a leading contribution, not an identity replacing the scheduler's max operation. Actual group span, retained tail slot and extra start delay are checked in execution [CA-ANALYSIS].

**中文**

将新增间隔夹在相邻窗口对应的 gap 之间，再沿移动出生点累加，主项为 (3/(2g)) integral_q^(3q) dp/p = (3 ln 3)/(2g)。这解释了 CA 的系数尺度 C = (3/2) ln 3，约为 1.647918433，并促使我们选择 C，将同组包尽量铺开到一个 RTT。该计算确定的是主项，不是可以替换调度器 max 操作的等式。实际包组跨度、保留尾槽和额外组首延迟继续由运行核对 [CA-ANALYSIS]。

### 8.3. Engineering Policy and Measurements / 工程策略与测量

**English**

The current SCP policy uses Q16 90852 for 2 ln 2 in AIMD slow start, and Q16 107998 for C in AIMD CA. PROB uses the CA value throughout. The effective CA gain is 107998/65536 = 1.647918701171875. This replaces the earlier adaptive 1 + MSS/(2*cwnd) controller policy; historical fixed-gain runs keep their original labels.

**中文**

当前 SCP 策略在 AIMD 慢启动中使用 2 ln 2 对应的 Q16 值 90852，在 AIMD CA 中使用 C 对应的 Q16 值 107998。PROB 全程使用 CA 值。CA 的实际系数为 107998/65536 = 1.647918701171875。它替代控制器此前的动态 1 + MSS/(2*cwnd) 策略；历史固定 gain 运行保留原标签。

**English**

A 22-gain, three-repeat Python clock-model sweep concentrates samples around C, using a 1 GiB request, 1 us ticks, full MSS packets, fixed 100 ms feedback and no loss. It compares complete middle groups 300-600 and late groups 900-1200 within each transfer. Near C, late spans are close to one RTT; larger gains shorten them. Smaller gains can also show near-RTT spans while accumulating group-start delay. Thus the engineering comparison records L = last send minus first send, Omega = trailing slot minus first send, and the next start delay max(0, Omega-R) together. Terminal groups are excluded. The compact source table and figure are maintained with the manuscript [CA-SWEEP]; actual SCP validation uses the separate research branch and its source-labelled Actions records [CA-EXPERIMENT].

**中文**

一轮 22 个 gain、每个三次的 Python 时钟模型扫描在 C 附近加密，条件为请求传输 1 GiB、1 us 时钟、全 MSS 包、固定完整反馈 100 ms、无丢包。每次传输分别考察中段第 300-600 组、后段第 900-1200 组的完整包组。C 附近的后段跨度接近一个 RTT，继续增大 gain 后跨度缩短；更低 gain 也可能呈现近 RTT 的跨度，同时累计组首延迟。因此工程比较同时记录 L = 末包发送减首包发送、Omega = 尾槽减首包发送，以及下一组额外延迟 max(0, Omega-R)，排除终止包组。小型数据表与图片随论文维护 [CA-SWEEP]；实际 SCP 验证由独立研究分支及注明源码版本的 Actions 记录提供 [CA-EXPERIMENT]。

**English**

Three new native SCP controls at fixed Q16 C each complete an exact 1 GiB transfer with matching checksums, 100 ms feedback for every packet, and zero retransmissions or drops. They complete in 1339.38 RTTs. In both selected ranges, native L is 88 ms; late occupied span has median 88.732585 ms, with zero accumulated start delay. The corresponding real-window Python run has late L = 99.569 ms and zero start delay. Thus the chosen constant gives different group spreading in the two implementations. Native integer-byte CA increments, millisecond dispatch and Q16 gain remain part of interpreting this result; adopting the engineering policy does not relabel the native span as 100 ms [CA-CONTROL].

**中文**

固定 Q16 C 的三次新原生 SCP 对照都完成精确 1 GiB 传输，校验和一致，每包完整反馈均为 100 ms，没有重传或丢包，完成时间均为 1339.38 个 RTT。两个选区的原生 L 都为 88 ms；后段尾槽占用中位数为 88.732585 ms，累计组首延迟为零。对应实数窗口 Python 运行的后段 L 为 99.569 ms，组首延迟也为零。因此，同一个工程常数在两层实现中产生了不同的包组分散。原生整数字节 CA 增窗、毫秒派发和 Q16 系数仍是解读结果的一部分；采用该策略不意味着将原生跨度改标为 100 ms [CA-CONTROL]。

## 9. Security Considerations / 安全考虑

**English**

Concentrating transmissions can increase short-term pressure on shared queues. Implementations continue to enforce congestion and receiver windows and to respond to congestion signals while pacing. ACK timing affects the feedback processes described here; acknowledgment validation and byte accounting therefore remain part of congestion-control correctness [RFC5681].

**中文**

集中发送可能增加共享队列的短时压力。实现进行 pacing 时，继续执行拥塞窗口与接收窗口限制，并响应拥塞信号。ACK 时序影响本文所述的反馈过程，因此确认校验和字节计数仍是保证拥塞控制正确性的一部分 [RFC5681]。

## 10. IANA Considerations / IANA 考虑

**English**

No IANA action is requested.

**中文**

无需 IANA 执行操作。

## 11. Informative References / 资料性参考文献

- **[RFC5681]** Allman, M., Paxson, V., and E. Blanton, *TCP Congestion Control*, RFC 5681, September 2009. <https://www.rfc-editor.org/rfc/rfc5681.html#section-3.1>
- **[RFC9438]** Xu, L., Ha, S., Rhee, I., Goel, V., and L. Eggert, Ed., *CUBIC for Fast and Long-Distance Networks*, RFC 9438, August 2023. <https://www.rfc-editor.org/rfc/rfc9438.html>
- **[RFC9406]** Balasubramanian, P., Huang, Y., and M. Olson, *HyStart++: Modified Slow Start for TCP*, RFC 9406, May 2023. <https://www.rfc-editor.org/rfc/rfc9406.html>
- **[RFC9002]** Iyengar, J. and I. Swett, *QUIC Loss Detection and Congestion Control*, RFC 9002, May 2021, Section 7.7. <https://www.rfc-editor.org/rfc/rfc9002.html#section-7.7>
- **[PACING]** Welzl, M., Eddy, W., Goel, V., and M. Tüxen, *Pacing in Transport Protocols*, draft-irtf-iccrg-pacing-04, September 2026, Work in Progress. <https://datatracker.ietf.org/doc/html/draft-irtf-iccrg-pacing-04>
- **[TCP-PACING]** Aggarwal, A., Savage, S., and T. Anderson, *Understanding the Performance of TCP Pacing*, IEEE INFOCOM 2000, pp. 1157–1165. <https://cseweb.ucsd.edu/~savage/papers/Infocom2000pacing.pdf>
- **[BBR]** Cardwell, N., Swett, I., and J. Beshay, *BBR Congestion Control*, draft-ietf-ccwg-bbr-06, July 2026, Work in Progress, Sections 4.1 and 5.3. <https://www.ietf.org/archive/id/draft-ietf-ccwg-bbr-06.html>
- **[BBR-GAIN]** Cardwell, N., Cheng, Y., Hassas Yeganeh, S., and V. Jacobson, *BBR Startup Pacing Gain: a Derivation*, June 2018. <https://github.com/google/bbr/blob/master/Documentation/startup/gain/analysis/bbr_startup_gain.pdf>
- **[CUBIC-TRACES]** Cardwell, N., *Linux TCP Startup Transmission and Acknowledgment Traces*. <https://github.com/google/bbr/blob/master/Documentation/startup/gain/analysis/bbr_startup_gain_graph.png>.
- **[GROWTH]** Uijing, S., *Pacing Gain and Congestion-Window Growth: Packet Models and SCP Experiments*, 2026. <https://github.com/skaiui2/SCP/blob/main/Pacing_and_Congestion_Window_Growth.pdf>
- **[EXPERIMENT]** Uijing, S., *Fixed-Feedback Pacing Measurements*, 2026. <https://github.com/skaiui2/SCP/actions/runs/39697564304>.
- **[REPRODUCE]** Uijing, S., *Pacing Experiments: Reproduction Instructions and Reference Results*, 2026. <https://github.com/skaiui2/SCP/tree/research/slowstart-nextslot-q16-20261022/reproduction>
- **[CA-ANALYSIS]** Uijing, S., *Pacing Gain and Congestion-Window Growth: Packet Models and SCP Experiments*, 2026, continuous CA, discrete birth-front timing and the sandwich argument. <https://github.com/skaiui2/SCP/blob/main/papers/pacing/Pacing_and_Congestion_Window_Growth.tex>

- **[CA-EXPERIMENT]** Uijing, S., *Next-Slot CA Measurements and Reproduction*, 2026. <https://github.com/skaiui2/SCP/tree/research/ca-nextslot-q16-20261022/reproduction>. Historical rebuild: <https://github.com/skaiui2/SCP/actions/runs/40084981319>. Current source-labelled runs: <https://github.com/skaiui2/SCP/actions/workflows/pacing-research.yml?query=branch%3Aresearch%2Fca-nextslot-q16-20261022>.
- **[CA-SWEEP]** Uijing, S., *CA Critical-Neighbourhood Python Sweep*, 2026. <https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-critical-sweep.csv>, <https://github.com/skaiui2/SCP/blob/main/papers/pacing/figures/ca-critical-sweep.pdf>.
- **[CA-CONTROL]** Uijing, S., *Fixed-CA-Gain Native Control*, 2026. <https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-native-constant.csv>, <https://github.com/skaiui2/SCP/blob/main/papers/pacing/data/ca-native-transfers.csv>.
- **[LINUX]** Linux v6.12, commit `adc218676eef25575469234709c2d87185ca223a`, `net/ipv4/tcp_output.c`. <https://github.com/torvalds/linux/blob/adc218676eef25575469234709c2d87185ca223a/net/ipv4/tcp_output.c>.

## Author's Address / 作者联系信息

Skai Uijing
Email: skaiuijing@gmail.com

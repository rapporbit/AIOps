# Ragas RAG 评测报告

样本数: **10**

## 平均得分

| 指标 | 平均分 | 含义 |
|---|---|---|
| faithfulness | 0.758 | 答案是否忠于检索上下文 (无编造) |
| answer_relevancy | 0.836 | 答案是否切题 |
| context_precision | 0.950 | 检索排序是否准 (相关在前) |
| context_recall | 0.980 | 检索是否覆盖 ground_truth (无遗漏) |

## 逐条结果

### Q1: Redis 连接池耗尽导致 5xx 升高,怎么排查?

- **ground_truth**: 查看 connected_clients 与 maxclients 比例; 找住连接不释放的客户端 (CLIENT LIST 看 idle); 排查应用侧连接泄漏 (未关闭 / 长事务); 临时扩 maxclients 缓解。
- **生成回答**: 排查分三步：  
1. 先查 Redis 服务端连接数，用 `redis-cli INFO clients` 对比 `connected_clients` 与 `maxclients`，看是否接近上限。  
2. 用 `redis-cli CLIENT LIST` 找连接不释放的客户端，重点关注 `idle` 很高的连接。  
3. 检查应用侧连接池配置，确认 borrow 后是否 finally close，并查看 active、idle、waiters 等指标，同时关联 Redis 慢日志和 CPU 排查长事务或阻塞命令。
- **得分**: faithfulness=0.769 | answer_relevancy=0.826 | context_precision=0.833 | context_recall=1.000

### Q2: MySQL 慢查询拖垮接口,常见原因和处置?

- **ground_truth**: 看 slow_query_log + EXPLAIN 找无索引或全表扫; 看 SHOW PROCESSLIST 是否有锁等待; innodb_buffer_pool_size 是否过小; 索引缺失加索引,锁等待杀长事务。
- **生成回答**: 常见原因：缺索引导致全表扫描、锁等待或长事务阻塞、SQL 写法引发临时表/文件排序、资源不足（如 Buffer Pool 太小）。  
处置：紧急时 KILL 异常长事务或回滚发布；长期加合适联合索引、拆分大事务、调整 `innodb_buffer_pool_size`，必要时限流或读写分离。
- **得分**: faithfulness=1.000 | answer_relevancy=0.804 | context_precision=1.000 | context_recall=1.000

### Q3: Kubernetes Pod 反复 CrashLoopBackOff 怎么定位?

- **ground_truth**: kubectl describe pod 看 Last State 与 Reason; kubectl logs --previous 看上一次崩溃日志; 常见: OOMKilled 调 limits.memory、配置错误改 env/configmap、就绪探针失败查健康检查。
- **生成回答**: 定位 Pod 反复 CrashLoopBackOff，主要分三步：

1.  **查退出原因**：执行 `kubectl describe pod <pod> -n <ns>`，重点看 `Last State`、`Reason` 和 `Exit Code`，判断是 OOMKilled、配置错误还是探针失败。
2.  **看崩溃日志**：执行 `kubectl logs <pod> -n <ns> --previous`，获取容器上一次退出前的标准输出，直接定位应用层报错。
3.  **确认资源与依赖**：用 `kubectl top pod <pod>` 检查内存/CPU 是否达到 limit，同时排查 ConfigMap、Secret 或下游依赖（如数据库）是否可用。
- **得分**: faithfulness=0.714 | answer_relevancy=0.963 | context_precision=0.833 | context_recall=1.000

### Q4: Linux 主机 CPU 飙到 100%, 怎么找罪魁?

- **ground_truth**: top 看哪个进程占 CPU; pidstat -t 看是哪个线程; perf top 或 py-spy/jstack 看热点函数; 区分用户态 us 高 (代码) 还是内核态 sy 高 (系统调用密集)。
- **生成回答**: 先用 `top` 或 `htop` 看哪个进程 CPU 占用最高，并区分是 `%us`、`%sy` 还是 `%wa` 高。  
如果是用户态高，用 `pidstat -u -t -p <pid> 1` 找线程级热点；Java 进程配合 `jstack`，原生进程用 `perf top`。  
如果是内核态高，重点查系统调用、网络包或锁竞争。  
如果是 IO wait 高，则转查磁盘或网络 IO，用 `iostat` 和 `pidstat -d` 定位。
- **得分**: faithfulness=0.889 | answer_relevancy=0.739 | context_precision=1.000 | context_recall=1.000

### Q5: 磁盘空间 95% 告警,如何快速释放空间且找根因?

- **ground_truth**: df -h 定位哪个挂载点满; du -sh /* | sort -h 找大目录; 常见: 日志未轮转、容器 overlay 残留、core dump 堆积; 先清旧日志再排查写入源,长期上 logrotate。
- **生成回答**: 根据上下文，快速释放空间并找根因的步骤如下：

1. **定位满的挂载点**：执行 `df -h` 找到使用率 95% 的分区。
2. **找大文件/目录**：用 `du -xh <挂载点> | sort -h | tail -20` 和 `find <挂载点> -xdev -type f -size +1G` 定位大文件。
3. **检查已删除文件**：运行 `lsof | grep deleted` 查看被进程占用的已删除文件，重启对应进程释放空间。
4. **清理与预防**：优先清理旧日志、临时文件、core dump，并对日志配置 logrotate 避免直接删除活跃日志。容器环境还需检查 `docker system df`。
- **得分**: faithfulness=0.375 | answer_relevancy=0.863 | context_precision=1.000 | context_recall=1.000

### Q6: Nginx 大量返回 502 Bad Gateway,可能原因?

- **ground_truth**: 上游服务挂了或超时 (curl 直接打上游确认); proxy_read_timeout/proxy_connect_timeout 太短; 上游连接被 keepalive 提前关闭; worker_connections 不够导致 upstream socket 耗尽。
- **生成回答**: 可能原因：
1. 上游服务不可用（宕机、重启、过载或端口耗尽），导致 Nginx 无法连接。
2. 超时配置过短（`proxy_connect_timeout`、`proxy_read_timeout`），上游响应慢时连接被提前断开。
3. 连接池不足（`worker_connections` 或 upstream keepalive 不够），无法建立新连接。
4. 上游主动重置连接（`connection reset by peer`），常见于上游进程异常退出。
- **得分**: faithfulness=1.000 | answer_relevancy=0.926 | context_precision=1.000 | context_recall=1.000

### Q7: JVM 应用频繁 Full GC,如何排查?

- **ground_truth**: jstat -gcutil 看 GC 频率与停顿; jmap -histo 看大对象; 常见: 堆太小、内存泄漏 (静态集合无界增长)、Metaspace 泄漏 (类加载未释放); 加堆 + 修内存泄漏 + dump 分析。
- **生成回答**: 根据上下文，排查步骤如下：

1. 使用 `jstat -gcutil <pid> 1000 10` 查看 GC 频率和各区使用率。
2. 用 `jmap -histo:live <pid> | head -40` 查看对象分布，定位占用内存最多的类。
3. 若仍无法定位，导出 heap dump：`jmap -dump:live,format=b,file=/tmp/heap.hprof <pid>`。
4. 同时检查 Metaspace、Direct Buffer、线程数和类加载数量，排查 Metaspace 泄漏或大对象问题。
- **得分**: faithfulness=0.500 | answer_relevancy=0.685 | context_precision=1.000 | context_recall=1.000

### Q8: Kafka consumer lag 持续增长,怎么处理?

- **ground_truth**: 消费速度跟不上生产: 看 consumer group lag (kafka-consumer-groups); 增加 consumer 实例或分区数; 看 consumer 是否卡在某条慢消息 (poison message); 检查下游写入是否成为瓶颈。
- **生成回答**: 根据上下文，处理步骤如下：

1. **定位瓶颈**：先用 `kafka-consumer-groups` 命令查看 lag 分布，对比生产与消费速率，确认是整体吞吐不足还是个别分区卡住。
2. **扩容或优化**：若吞吐不足，增加 consumer 实例，必要时增加 topic 分区数；若下游慢，优化 DB/HTTP 写入、批量提交并增加超时与重试隔离。
3. **处理毒消息**：检查 consumer 日志，将 poison message 跳过或转入死信队列，避免卡住整个分区。
4. **检查 offset 提交**：确认 auto commit 或手动 commit 是否正常，并关注 rebalance 日志。
- **得分**: faithfulness=0.929 | answer_relevancy=0.801 | context_precision=1.000 | context_recall=1.000

### Q9: 调用链 P99 延迟突增,如何定位是哪一跳?

- **ground_truth**: 拉一段 trace 看 span 耗时分布; 排序找最长 span 对应的服务; 检查该服务的依赖 (DB/Redis/下游服务); 同时看其指标 (RT/错误率/连接池); 灰度回滚或限流。
- **生成回答**: 根据上下文，定位调用链 P99 延迟突增的具体跳数，可以按以下步骤操作：

1.  **拉取样本**：先获取异常时间窗口的 trace 样本，并按 trace 总耗时排序。
2.  **定位最长 Span**：展开耗时最长的 trace，按 span duration 找出耗时最长的那一跳。
3.  **下钻分析**：针对该最长 span 对应的服务，检查其自身 RT、错误率、CPU、GC，并查看其依赖的 DB、Redis、HTTP 下游的 span，同时确认是否有近期发布、配置变更或流量突增。
- **得分**: faithfulness=1.000 | answer_relevancy=0.895 | context_precision=0.833 | context_recall=0.800

### Q10: etcd 集群响应慢、k8s API 卡顿,排查方向?

- **ground_truth**: etcdctl endpoint status 看 leader / db size; 磁盘 fsync 延迟过高 (etcd 对磁盘极敏感); db 是否接近 quota (2GB 默认); 网络抖动导致心跳超时;defrag + 提升存储 IOPS。
- **生成回答**: 排查方向如下：

1. 检查 etcd 集群状态和健康度，确认 leader 是否稳定、DB 大小是否接近配额。
2. 检查磁盘 fsync 延迟，慢盘是常见根因，需确保使用高 IOPS SSD 且避免混部。
3. 检查网络抖动和丢包，防止 leader 频繁切换。
4. 若 DB 膨胀，执行 compact 和 defrag 清理碎片。
- **得分**: faithfulness=0.400 | answer_relevancy=0.861 | context_precision=1.000 | context_recall=1.000

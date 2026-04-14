# 舆情分析系统 V4 优化计划

> 整合三轮外部AI评审共26条建议，工程级优化方案。
> 创建时间：2026-03-30
> 状态：**✅ 已完成**

## 三轮建议整合与修正清单

### 第一轮（宏观方向）— 全部采纳
1. LLM API闭环（手搓轻量客户端，不引入LangChain）
2. correct CLI命令实现结构化反馈
3. 热度基准线(Baseline) + 爆发指数前置计算
4. 多步Prompt Chain（感知→判断→输出）
5. 智能评论截断（高赞优先+楼中楼优先）
6. 双轨输出(Markdown+JSON) + daily_insights表

### 第二轮（工程盲区）— 全部采纳
7. Token预算管理器（按比例分配，超预算截断+摘要）
8. VIEW性能优化（嵌套子查询→JOIN聚合）
9. N+1查询合并（41次→3次批量SQL）
10. 时间衰减热度（半衰期7天）
11. Chain中间JSON Schema定义+校验
12. correct命令结构化（--field/--original/--corrected）
13. raw_json外置引用（存文件路径非内联TEXT）
14. LLM客户端错误处理（JSON修复/429限流/超时检测）
15. 评论like_count展示（高赞标记）
16. 用户description输出（自我介绍）
17. 时段聚合（盘前/盘中/盘后）
18. 内容去重

### 第三轮（实现陷阱修正）— 关键修正项
19. **同步requests替代异步httpx**：现有代码全同步，用同步requests避免async复杂度
20. **放弃participant_count冗余字段**：VIEW的JOIN聚合已解决性能
21. **衰减热度与基准线分离**：排序用衰减，比较用原始
22. **Chain中断恢复**：降级输出+缓存+--resume-from
23. **只对转发帖去重**：200字窗口误判率高
24. **可执行Schema格式**：自定义轻量格式+大小写容忍
25. **反馈存SQLite表**：analysis_feedback表替代JSON文件遍历
26. **分步temperature**：step1=0.15, step2=0.35, step3=0.45

## 用户约束

- 不引入LangChain，手搓轻量API调用模块
- 不修改现有爬取/导出/通知逻辑
- 现有prompt/report模式完全兼容
- HTTP客户端用同步requests

## 5阶段 × 9任务

### 阶段1：数据基础层（P0）
- **1.1** VIEW性能优化：discussion_threads VIEW改JOIN聚合
- **1.2** N+1查询合并：get_top_threads从41次SQL→3次
- **1.3** TokenBudget管理器：新增token_budget.py
- **1.4** 智能评论截断+画像增强：高赞优先+description输出+可疑标记

### 阶段2：算法增强层（P1）
- **2.1** 时间衰减热度+基准线（分离设计）
- **2.2** 爆发指数+时段聚合+转发去重

### 阶段3：Prompt Chain层（P2）
- **3.1** 可执行JSON Schema（schemas.py）
- **3.2** 三步Chain模板（step1/step2/step3）
- **3.3** 模板引擎改造 + 现有模板增强

### 阶段4：API闭环层（P2）
- **4.1** 同步LLM客户端 + 分步temperature
- **4.2** analyze --output auto + Chain中断恢复
- **4.3** correct命令（结构化版）

### 阶段5：产品沉淀层（P3）
- **5.1** daily_insights + analysis_feedback表
- **5.2** 双轨输出解析入库

## 执行进展

- [x] 阶段1.1: VIEW性能优化
- [x] 阶段1.2: N+1查询合并
- [x] 阶段1.3: TokenBudget管理器
- [x] 阶段1.4: 智能评论截断+画像增强
- [x] 阶段2.1: 时间衰减热度+基准线分离
- [x] 阶段2.2: 爆发指数+时段聚合+转发去重
- [x] 阶段3: Prompt Chain
- [x] 阶段4: API闭环
- [x] 阶段5: 产品飞轮

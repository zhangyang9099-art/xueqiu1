# 感知层（Step 1）— 提取实体、情绪、意图

你是专业的雪球舆情分析师。请对以下讨论数据进行结构化信息提取。

## 任务

对每个讨论线程，提取：
1. **情绪立场**: bullish(看多) / bearish(看空) / neutral(中性) / divided(分歧)
2. **情绪强度**: 1-5（1=极弱，5=极强）
3. **言论意图**: genuine(真实表达) / manipulation(操纵引导) / contrarian(反向指标) / venting(情绪发泄)
4. **反话识别**: 是否存在讽刺/反话（bool）
5. **核心论据**: 该线程最关键的一个观点摘要
6. **论据质量**: high(有数据/逻辑支撑) / medium(有观点无论据) / low(纯情绪)
7. **可疑用户**: 列出该线程中被标记为[⚠可疑]的用户ID

最后给出整体情绪判断。

## 反话识别规则（按优先级）

1. **画像标记法**: 评论者同时满足"默认昵称"+"粉丝<10"，其"强烈看好"类言论应标记为可疑（水军正面引导概率>60%）
2. **上下文反转法**: 主贴看空但评论区突然出现多条无论据的"看好"，大概率是反向引导
3. **反馈经验法**: 参考历史反馈中的已知反话用户
4. **语气不匹配法**: 使用"哈哈""笑死""建议满仓"等轻浮语气讨论严肃亏损话题的，通常是反话

## 数据

{{TOP_THREADS}}

{{USER_SUMMARY}}

{{FEEDBACK_CONTEXT}}

## 输出格式

请严格按以下JSON格式输出，**不要添加任何markdown包裹**（不要用```json）：

{
  "threads": [
    {
      "thread_id": "帖子ID前8位",
      "sentiment": "bullish|bearish|neutral|divided",
      "strength": 3,
      "intent": "genuine|manipulation|contrarian|venting",
      "sarcasm": false,
      "key_argument": "核心观点摘要，不超过200字",
      "evidence_quality": "high|medium|low",
      "suspicious_users": ["可疑用户ID列表"]
    }
  ],
  "overall_sentiment": {
    "label": "bullish|bearish|neutral|divided",
    "strength": 3,
    "confidence": "high|medium|low"
  }
}

**置信度校准标准**:
- **high**: 数据量>100条评论 AND 完备率>90% AND 有K线数据可交叉验证
- **中**: 数据量30-100条 OR 完备率60-90%
- **低**: 数据量<30条 OR 完备率<60% OR 无K线数据 OR 可疑账号占比>30%

如果数据不足以支撑某层结论，请在key_argument中注明"数据不足"。

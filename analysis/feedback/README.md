# 反馈日志

此目录存放分析反馈日志，用于迭代优化分析质量。

## 触发时机

当用户在对话中纠正分析结论时，由 AI Agent 将反馈记录到此目录。

## 文件格式

文件名：`YYYY-MM-DD_{股票代码}_{主题}.json`

```json
{
  "date": "2026-03-30",
  "symbol": "SH600519",
  "thread_id": "帖子ID",
  "original_judgment": "看多，情绪强度3",
  "user_correction": "这是反话，实际是看空",
  "lesson": "该用户有多次反向指标记录，应降低其言论权重",
  "layer": "sentiment"
}
```

## 字段说明

| 字段 | 说明 |
| --- | --- |
| date | 反馈日期 |
| symbol | 股票代码 |
| thread_id | 相关帖子ID（可选） |
| original_judgment | AI原始判断 |
| user_correction | 用户纠正内容 |
| lesson | 经验总结（下次分析时应注意什么） |
| layer | 相关分析层级 |

## 工作机制

1. 每次 `python main.py analyze` 执行时，Prompt 模板引擎会自动加载此目录下最近 90 天的反馈日志
2. 反馈内容作为"历史经验"注入到 Prompt 中，供 LLM 参考以提高分析准确性
3. 随着反馈积累，分析质量持续提升

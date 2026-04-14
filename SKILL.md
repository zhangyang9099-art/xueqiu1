# 雪球股票讨论区爬虫 Skill

## 触发条件
当用户提到以下关键词时触发本 Skill:
- "雪球"、"股票讨论"、"股票评论"、"舆情"
- "爬取"、"抓取" + 股票名称
- "帮我看看 XXX 的讨论"
- "跟踪用户 XXX"
- "导出数据"

## 能力描述
本 Skill 可以:
1. 爬取指定股票在雪球网的讨论区帖子和评论
2. 跟踪指定雪球用户的全部公开发言
3. 以讨论线程（DiscussionThread）为单元导出数据
4. 支持增量爬取（只爬新内容）和评论回填

## 使用方法

### 前置条件
项目路径: ~/Desktop/xueqiu-scraper/
需要先激活虚拟环境: cd ~/Desktop/xueqiu-scraper && source venv/bin/activate

### 可用命令

```bash
# Token 管理（登录）
python main.py login                    # 弹出浏览器让用户登录，自动获取token
python main.py test-cookie              # 测试 token 是否有效

# 股票管理
python main.py add-stock 茅台           # 自然语言添加股票
python main.py add-stock SH600519 贵州茅台
python main.py remove-stock SH600519

# 用户管理
python main.py add-user 但斌            # 搜索并添加跟踪用户
python main.py add-user 1234567890 某大V
python main.py remove-user 1234567890

# 爬取
python main.py run                      # 执行完整爬取（所有股票+用户）
python main.py backfill-comments        # 回填缺失评论

# 导出
python main.py export                           # 全部格式
python main.py export --format json --days 7    # JSON快照，最近7天
python main.py export --format md               # Markdown文档
python main.py export --format csv              # 层级CSV

# 状态
python main.py status                   # 查看数据统计和爬取日志
```

### 调用 skill_api.py（程序化调用）
```python
from skill_api import XueqiuSkillAPI

api = XueqiuSkillAPI()
api.ensure_token()                      # 检查token，无效则提示登录
api.add_stock("茅台")                   # 添加股票
api.run_scrape()                        # 执行爬取
result = api.export_json(days=7)        # 导出JSON
api.close()
```

## 数据模型
核心分析单元: DiscussionThread（讨论线程）= 帖子 + 全部评论
详见 PROJECT_BIBLE_xueqiu_scraper.py 第二部分（本体论模型）

## 输出格式
- JSON 快照: data/snapshots/{SYMBOL}_{DATE}.json（面向AI分析）
- 层级 CSV: data/export/{SYMBOL}_{TIMESTAMP}.csv（Excel浏览）
- Markdown: data/export/{SYMBOL}_{DATE}.md（人工阅读）

## 注意事项
- xq_a_token 会过期，过期后需要 `python main.py login` 重新登录
- 首次爬取某股票耗时较长（30-60分钟）
- 请求间隔 10-30 秒，避免被封
- 每次运行自动回填近 7 天帖子的新评论

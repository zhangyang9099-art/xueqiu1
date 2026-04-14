# 雪球舆情采集系统

这是一个面向雪球数据采集与后续 AI 分析的工程化项目，不是单一脚本。当前主线能力是：

- 股票讨论区历史补全与增量更新
- 股票帖子评论缺口回填与完整性审计
- 用户/KOL 时间线抓取、历史补全、每日增量同步
- 运行进展、预计剩余时间、数据库覆盖范围查询
- 面向长期运维的持久化浏览器会话、人工验证门禁、断点续跑

## 当前设计原则

- 历史模式和评论回填分离
  - 股票 `history` 主要负责推进帖子最老边界
  - 评论完整性由 `backfill-comments` / `sync-comments` 负责
- 用户模式分为两条线
  - `history` / `sync-users --ensure-history`：从数据库最老边界继续往前补
  - `sync-users`：按最新边界做增量，并补旧发言下后来新增的评论
- 所有长任务都尽量支持：
  - `cursor`
  - 小分段 chunk
  - 页级 checkpoint
  - 中断后继续

## 核心目录

```text
xueqiu-scraper/
├── main.py                    # CLI 入口
├── config.yaml                # 主配置
├── requirements.txt           # 依赖
├── core/                      # 浏览器/客户端/限流/异常
├── scrapers/                  # 股票、评论、用户时间线抓取
├── storage/                   # SQLite 落库与统计
├── utils/                     # 查询、日志、清洗、运行摘要
├── analysis/                  # 分析链路与提示词
├── export/                    # 导出器
├── tests/                     # 回归测试
└── data/                      # 数据库、日志、运行摘要、浏览器 profile
```

## 关键命令

### 股票

```bash
python main.py scrape --stocks 振华科技 --mode history --pages 100
python main.py scrape --stocks 振华科技 --mode update
python main.py backfill-comments --symbol SZ000733 --days 0
python main.py sync-comments
python main.py audit-completeness --symbol SZ000733
```

### 用户 / KOL

```bash
python main.py scrape --users 罗洄头 --mode history --pages 100
python main.py scrape --users 罗洄头 --mode update
python main.py sync-users --users 罗洄头
python main.py sync-users --ensure-history --users 雪月霜 罗洄头
python main.py user-scrape-status --user-ids 1505944393 2632831661
```

### 进展与状态

```bash
python main.py progress
python main.py status
python utils/query_progress.py db-comments
python utils/query_progress.py comment-backfill-status
python utils/query_progress.py history-queue-status
python utils/query_progress.py user-scrape-status --user-ids 1505944393 2632831661
python utils/query_progress.py watchlist-pages
```

## 当前数据库模型

主要表：

- `watched_stocks`
- `tracked_users`
- `posts`
- `comments`
- `comment_memberships`
- `user_statuses`
- `scrape_logs`

几个关键语义：

- `posts`：股票讨论区帖子，或用户时间线涉及到的父帖
- `comments`：帖子下的评论实体
- `comment_memberships`：评论与帖子/线程的归属关系
- `canonical_post_id`：评论的规范归属帖子
- `user_statuses`：用户/KOL 自己的时间线发言

## 反爬与运行方式

当前稳定策略不是“破解验证码”，而是：

- 持久化浏览器 profile
- 人工登录/验证门禁
- 真实浏览器上下文
- 慢速稳态节流
- 遇到异常时轻恢复、浏览器回收、断点续跑

几个已知现实边界：

- 股票讨论区前端通常只有大约 100 页可见历史
- 用户时间线接口往往比股票讨论区可翻得更深
- 用户时间线接口比股票时间线接口更依赖真实登录态
- 某些帖子评论接口会非常慢，往往是整轮运行最主要瓶颈

## 当前查询口径

- “用户发言” = `user_statuses` 里的记录数
- “评论” = `comments` 里的评论实体数
- `reply_count` = 雪球页面声明的互动数，不等于已经落库的评论数
- 股票模式的“缺口评论/缺口帖子”是基于 `reply_count - comments_scraped`
- 用户模式目前的完整性更依赖：
  - 时间范围
  - `history_complete`
  - `history_cursor_page`
  - `last_sync_time`
  - 用户发言对应帖子上的评论缺口统计

## 面向外部 AI 审查时应先看的文件

建议阅读顺序：

1. `README.md`
2. `REVIEW_GUIDE.md`
3. `PROJECT_BIBLE_xueqiu_scraper.py`
4. `main.py`
5. `scrapers/stock_comments.py`
6. `scrapers/user_tracker.py`
7. `core/client.py`
8. `storage/database.py`
9. `utils/query_progress.py`
10. `tests/`

## 当前最值得外部 AI 重点审查的区域

- 用户/KOL 模式的浏览器会话管理与 profile 冲突
- 用户模式的评论统计/缺口统计口径
- `sync-users --ensure-history` 的“单用户触底后再切下一个”编排语义
- 评论接口慢请求下的恢复和超时预算
- 查询命令与真实运行状态的一致性

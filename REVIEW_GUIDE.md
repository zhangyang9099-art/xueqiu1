# 外部 AI 审查说明

这份项目已经整理成适合外部 AI 审查的结构化代码包。你如果把整个审查目录上传给另一个模型，建议它按下面顺序阅读和审查。

## 审查目标

建议重点看 4 件事：

1. 用户/KOL 模式是否真正满足本体论采集要求
2. 浏览器持久化 profile 与人工登录门禁是否存在结构性问题
3. 股票历史补全、评论回填、用户增量同步三条链路是否职责清晰
4. 查询工具、ETA 估算、数据库统计口径是否和真实运行行为一致

## 建议阅读顺序

1. `README.md`
2. `PROJECT_BIBLE_xueqiu_scraper.py`
3. `main.py`
4. `config.yaml`
5. `scrapers/stock_comments.py`
6. `scrapers/user_tracker.py`
7. `core/client.py`
8. `storage/database.py`
9. `utils/query_progress.py`
10. `tests/`

## 你需要知道的当前真实状态

### 股票模式

- 股票 `history`：
  - 从数据库最老边界继续向前
  - 默认不内联抓评论
- 股票评论：
  - `backfill-comments` 补缺口
  - `sync-comments` 用于每日更新已有评论股票到今天
- 已经做过评论归属唯一化：
  - `canonical_post_id`
  - `comment_memberships`

### 用户/KOL 模式

- 当前支持：
  - `scrape --users ... --mode history`
  - `scrape --users ... --mode update`
  - `sync-users`
  - `sync-users --ensure-history`
- 目标语义是：
  - 历史补全时，从最新一路翻到更早时间，直到触底
  - 每日增量时，不只补新发言，也补旧发言下后来新增的评论
- 当前已知最难的问题不是内容解析，而是：
  - 用户接口登录态要求更高
  - 浏览器 profile 容易和外部 Chrome 冲突
  - 评论接口慢请求会显著拖慢整体任务

## 外部 AI 最值得验证的问题

### 1. 用户模式是否真正符合本体论规范

当前目标是：

- 用户自己发帖：
  - 保存帖子本身
  - 保存帖子下所有评论和回复
- 用户评论自己的帖子：
  - 应被同一线程覆盖
- 用户评论别人的帖子：
  - 应抓取父帖
  - 应补齐父帖完整评论树

请重点检查：
- `scrapers/user_tracker.py`
- `scrapers/stock_comments.py`
- `storage/database.py`

### 2. 用户模式的进程编排是否正确

现在最值得审查的是：

- `sync-users --ensure-history`
  - 是否真的会持续补历史直到触底
  - 还是只跑一轮 100 页就切用户
- 单用户失败后：
  - 是否应该自动重试
  - 还是应暂停整个队列

### 3. 浏览器会话与 profile 冲突

当前经常遇到：

- `launch_persistent_context` 失败
- 外部 Chrome 和 Playwright 同时占用同一 profile
- 用户明明已经登录，但用户接口仍然要登录

请重点检查：
- `core/client.py`
- `main.py`
- `config.yaml`

### 4. 查询和统计口径

当前需要重点核对：

- `utils/query_progress.py`
- `main.py progress`
- 用户模式下“评论数/缺口数”是否与实际落库一致
- ETA 是否基于真实进度，而不是静态猜测

## 不需要外部 AI 花时间的内容

这些内容可以快速浏览，不必当作主要审查对象：

- `.idea/`
- `data/`
- 历史 `.bak` 备份文件
- 旧的 `phase*_setup.py` 一次性迁移脚本
- `claude日志/`

## 交付给外部 AI 时的建议提问

如果你想让另一个 AI 审查得更聚焦，可以直接给它下面这些问题：

1. 用户/KOL 模式是否真的满足本体论采集目标？
2. 用户模式为什么比股票历史模式更容易出问题，结构性根因是什么？
3. 当前浏览器 profile / 登录门禁设计是否合理？
4. 现在的查询命令和数据库口径是否一致？
5. 项目里哪些恢复逻辑、超时处理、队列编排最值得重构？

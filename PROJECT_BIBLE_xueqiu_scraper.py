#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║        雪球舆情智能投研系统 — 项目全景文档 v8                        ║
║        最后更新: 2026-03-23 10:55                                  ║
║                                                                      ║
║  本文件是项目当前可执行状态的完整说明书。把它发给任何 AI 助手，        ║
║  对方应能直接理解项目结构、当前能力、已修复问题、运维方式和遗留风险。  ║
║                                                                      ║
║  Phase 1-5 已完成 ✅ | 2026-03-16 ~ 2026-03-23 运维重构已落地 ✅     ║
║  Phase 6-9 仍是规划目标，尚未正式启动                                ║
║                                                                      ║
║  获取最新代码:                                                        ║
║  cd ~/Desktop/xueqiu-scraper && find . -name "*.py" \\               ║
║    -not -path "./venv/*" -not -path "*/__pycache__/*" | sort |       ║
║    while read f; do echo ""; echo "###$f###"; cat "$f"; done         ║
║    && echo "### config.yaml ###" && cat config.yaml                  ║
╚══════════════════════════════════════════════════════════════════════╝
"""


# ====================================================================
# 第一部分：项目概述与愿景
# ====================================================================

PROJECT_OVERVIEW = """
【项目名称】雪球舆情智能投研系统 (xueqiu-scraper → sentiment-alpha)
【项目路径】~/Desktop/xueqiu-scraper/
【虚拟环境】~/Desktop/xueqiu-scraper/venv/
【激活命令】cd ~/Desktop/xueqiu-scraper && source venv/bin/activate

【项目愿景】
  从“手工刷雪球”升级成“自动采集、自动补数、自动导出、可供后续 AI 分析”的
  半自动投研基础设施。

【当前系统定位】
  这不是单纯的抓帖脚本，而是一套“可恢复、可续跑、可审计”的雪球数据采集系统，
  当前重点已经从“能抓到”转成“稳定历史补全 + 评论完备性修复 + 自动运维”。

【当前四条主线】
  1. 历史模式：从数据库当前最老边界继续向更早历史推进
  2. 增量模式：从最近时间向后更新新帖、新评论、旧帖新增评论
  3. 评论回填：独立于历史翻页，对缺口帖子做后置评论补数
  4. 完整性审计：对评论缺口、孤儿评论、跨帖归属做数据库级检查

【核心技术栈】
  - Playwright (Chromium / chrome channel)
  - SQLite (WAL 模式)
  - 持久化浏览器 profile + 外部人工验证门禁
  - 历史游标 + 页级 checkpoint + 断点续跑
  - 树形评论 / 本体论建模 / 归属唯一化
  - 运行摘要 run_reports + 隔夜批跑日志 overnight_runs

【CLI / 自动化设计原则】
  - 所有目标通过命令行传入，不改代码
  - 支持自然语言股票名 / 用户名解析
  - 运行前可展示数据库已有时间窗口和本轮清单
  - 面向 OpenClaw / Skill / 长批次自动化调用
"""


# ====================================================================
# 第二部分：开发历程
# ====================================================================

DEVELOPMENT_HISTORY = """
━━━ Phase 1 ✅ 数据质量 + 存储升级（2026-03-15 完成）━━━
  - 时间字段标准化
  - 数据库迁移脚本
  - 评论回填基础命令

━━━ Phase 2 ✅ 导出重构（2026-03-15 完成）━━━
  - JSON / CSV / Markdown 导出统一
  - 讨论线程树形快照

━━━ Phase 3 ✅ 智能参数输入（2026-03-15 完成）━━━
  - 股票名解析
  - 用户名解析
  - CLI 输入标准化

━━━ Phase 4 ✅ Skill 封装（2026-03-15 完成）━━━
  - skill_api.py
  - SKILL.md

━━━ Phase 5 ✅ 自动化运维基础（2026-03-15 完成）━━━
  - 数据库 v3 升级
  - trending_scraper
  - scheduler / health_monitor
  - scrape 命令统一入口

━━━ 2026-03-16 ~ 2026-03-23 连续运维重构 ✅ ━━━
  本阶段不是新 Phase，而是在 Phase 5 基础上做了大量线上可用性重构：

  1. 历史模式语义重写
     - 不再从今天一路往回重扫
     - 改成从数据库最老帖子边界继续往更早历史推进
     - 引入 history_cursor_page / history_cursor_oldest_time / stagnant_runs

  2. 会话模型重写
     - 历史批次复用单一持久化 profile
     - 手工验证改为 external Chrome 接管同一 profile
     - 验证门禁前置，不再按股票/分段反复重建登录态

  3. 历史中断恢复重写
     - `unexpected_html` 与 `session_expired` 分流
     - 历史主 transport 改为 `page`
     - 仅在必要时降级到 `isolated_page`
     - 单页失败隔离，支持 deferred / partial / blocked

  4. 评论本体修复
     - 新增 `canonical_post_id`
     - 新增 `comment_memberships`
     - 彻底修掉“一条评论挂多个帖子”的结构性重复
     - 评论归属唯一化，跨帖挂载大幅清理

  5. 历史与评论链路彻底分离
     - `history_inline_comments=false`
     - 历史模式只推进帖子边界
     - 评论补全走 `backfill-comments`

  6. 运行摘要与批跑能力
     - 新增 `utils/run_reporter.py`
     - 每次 scrape / backfill-comments 自动写 `latest_run.md/json`
     - 新增 `utils/watchlist_queue_runner.py`
     - 支持自选股隔夜批跑
     - 支持 `--rerun-failed-manifest` 只重跑失败批次

  7. 低风险提效
     - 历史模式默认节流由更保守档提升到稳态提速档
     - history_chunk_pages: 2 → 3
     - 主动 session recycle 阈值放宽
     - 调度层数据库连接复用

  8. 测试补齐
     - manual_verification
     - history_timeline_recovery
     - history_session_reuse
     - comment_extraction
     - comment_parent_reconcile
     - comment_backfill_runtime / scope
"""


# ====================================================================
# 第三部分：最近新增变化（必须让新 AI 先读）
# ====================================================================

RECENT_CHANGES = """
如果接手本项目，最容易搞错的是下面这些“新语义”：

1. 历史模式默认不抓评论
   - 这是设计，不是 bug
   - 当前配置: `history_inline_comments: false`
   - 所以历史补帖后看到“新增评论 0”是正常现象
   - 评论补全必须再跑 `backfill-comments`

2. 历史模式的成功标准已经变了
   - 不是“从第 1 页开始扫完 N 页”
   - 而是“从数据库最老边界继续向更早时间推进”
   - 是否触底由 `history_complete + history_stagnant_runs` 决定

3. 历史模式比评论回填更容易被风控
   - 历史依赖 `query/v1/symbol/search/status.json`
   - 评论回填主要依赖 `statuses/v3/comments.json` / `statuses/comments.json`
   - 这两条链路稳定性不同，不能混为一谈

4. 评论结构去重已经从“按 comment id 去重”升级成“实体归属唯一化”
   - 仅 comments 表不重复还不够
   - 现在还要求 comment_memberships 不出现多帖挂载
   - 当前数据库里 `comment_multi_post = 0`

5. 验证策略已经明确
   - 不做自动破解滑块
   - 采用“降低触发率 + 持久化会话 + 人工验证门禁 + 断点续跑”

6. 隔夜批跑不要再开 history watchdog
   - 正常强制休息会超过 180 秒
   - 历史批跑队列里 watchdog 已禁用
   - 改为需要时人工中断 / 按 manifest 重跑失败批次
"""


# ====================================================================
# 第四部分：战略路线图（Phase 6-9）
# ====================================================================

STRATEGIC_ROADMAP = """
━━━ Phase 6 📋 LLM 分析引擎 ━━━
  - 批量情绪打分
  - 高价值帖子 / 评论摘要
  - 情绪时间线与价格联动分析

━━━ Phase 7 📋 水军识别 + 用户可信度 ━━━
  - 协调行为检测
  - 用户画像质量评分
  - 可疑传播链

━━━ Phase 8 📋 KOL 跟踪增强 ━━━
  - 用户观点时间线
  - 个股/行业观点映射
  - 高价值 KOL 标签体系

━━━ Phase 9 📋 回测闭环 + 自我迭代 ━━━
  - 舆情因子回测
  - 板块轮动信号
  - MCP / Skill 化能力封装
"""


# ====================================================================
# 第五部分：服务目标体系
# ====================================================================

SERVICE_OBJECTIVES = """
━━━ S1: 市场情绪判断 ━━━
  输入: 帖子、评论、互动指标、时间上下文
  输出: 个股情绪、舆情拐点、阶段性叙事变化

━━━ S2: 水军/庄家动作分析 ━━━
  输入: 用户画像、跨账号同向行为、股价配合
  输出: 可疑协调行为、异常传播链、操纵风险事件

━━━ S3: 个股投资机会挖掘 ━━━
  输入: KOL、深讨论线程、高互动帖子
  输出: 逻辑链、争议点、散户预期变化

━━━ S4: 板块轮动趋势预判 ━━━
  输入: 标的舆情热度迁移、热门话题、板块关联
  输出: 月度轮动方向与板块热度变化
"""


# ====================================================================
# 第六部分：本体论模型 v3
# ====================================================================

ONTOLOGY_MODEL = """
设计原则: 平台无关 | 深层层级 | 归属唯一 | 时间语义 | 可恢复

E1 Platform
  - xueqiu / 未来可扩展到其他社区

E2 Stock
  - symbol, name, sector, market

E3 DiscussionThread
  - 一个帖子及其评论树
  - 帖子是线程根，评论通过 memberships 和 canonical ownership 归属

E4 Comment
  - parent_comment_id
  - depth
  - reply_to_user
  - canonical_post_id
  - parent_post_id
  - parent_scope

E5 User
  - screen_name, profile, followers, verified_type

E6 TimeContext
  - created_at / created_at_str / market_phase

E7 TrendingTopic
  - 热门事件、热度排名、关联标的

E8 Engagement
  - like / fav / view / retweet / reply_count

关键变化:
  - 评论不再只靠 `post_id` 单字段表达归属
  - 现在用 `canonical_post_id + comment_memberships` 同时表达
    “唯一归属”与“展示入口”
"""


# ====================================================================
# 第七部分：技术架构
# ====================================================================

TECHNICAL_ARCHITECTURE = """
━━━ 会话与验证层 ━━━
  - Playwright + persistent session profile
  - session_profile_dir = data/manual_chrome_profile
  - manual_verification_mode = external
  - manual_verification_gate_enabled = true
  - 历史模式前置验证门禁，整批任务复用同一已验证会话

━━━ 历史时间线链路 ━━━
  - 主接口: /query/v1/symbol/search/status.json
  - 默认 transport: page
  - fallback transport: isolated_page
  - 页级 checkpoint + history_cursor_page
  - 单页失败隔离: success / partial / deferred / blocked

━━━ 评论补全链路 ━━━
  - 主接口: /statuses/v3/comments.json
  - 兜底接口: /statuses/comments.json
  - 独立命令运行，不内联到历史翻页主链路
  - 单帖预算控制，支持楼中楼 / 子回复链展开

━━━ 故障分类 ━━━
  - explicit_waf
  - captcha_required
  - http_forbidden
  - transport_timeout
  - page_dead
  - unexpected_html
  - session_expired

  注意:
  - `unexpected_html` 不再直接等于 `session_expired`
  - `session_expired` 只在明确证据下触发

━━━ 存储层 ━━━
  - SQLite WAL
  - posts / comments / comment_memberships / watched_stocks / tracked_users
  - 讨论线程视图 discussion_threads
  - run_reports 摘要文件

━━━ 运维层 ━━━
  - 单次运行自动写 `data/run_reports/latest_run.md/json`
  - 自选股隔夜批跑 `utils/watchlist_queue_runner.py`
  - 可按 manifest 仅重跑失败批次
"""


# ====================================================================
# 第八部分：文件结构
# ====================================================================

FILE_STRUCTURE = """
~/Desktop/xueqiu-scraper/
├── config.yaml
├── main.py
├── skill_api.py
├── SKILL.md
├── PROJECT_BIBLE_xueqiu_scraper.py
├── core/
│   ├── client.py                  # 会话、验证、请求分流、恢复
│   ├── cookie_manager.py
│   ├── auto_login.py
│   ├── rate_limiter.py            # 含历史自适应节流能力（默认关闭）
│   ├── browser_pool.py
│   ├── scheduler.py
│   ├── health_monitor.py
│   └── exceptions.py
├── scrapers/
│   ├── api_endpoints.py
│   ├── stock_comments.py          # 股票帖子/评论主逻辑
│   ├── scrape_cmd.py              # scrape 命令编排、历史续跑、deferred
│   ├── trending_scraper.py
│   └── user_tracker.py
├── storage/
│   └── database.py                # SQLite + 迁移 + 归属修复
├── utils/
│   ├── stock_resolver.py
│   ├── user_resolver.py
│   ├── time_utils.py
│   ├── run_reporter.py            # 运行摘要
│   ├── watchlist_queue_runner.py  # 隔夜批跑 / 失败批次重跑
│   ├── logger.py
│   ├── html_cleaner.py
│   └── notifier.py
├── export/
│   ├── json_exporter.py
│   ├── csv_exporter.py
│   └── markdown_exporter.py
├── tests/
│   ├── test_manual_verification.py
│   ├── test_manual_verification_external.py
│   ├── test_history_timeline_recovery.py
│   ├── test_history_session_reuse.py
│   ├── test_comment_extraction.py
│   ├── test_comment_parent_reconcile.py
│   ├── test_comment_backfill_scope.py
│   └── test_comment_backfill_runtime.py
└── data/
    ├── xueqiu.db
    ├── logs/
    ├── run_reports/
    ├── overnight_runs/
    ├── snapshots/
    └── manual_chrome_profile/
"""


# ====================================================================
# 第九部分：CLI 命令手册
# ====================================================================

CLI_COMMANDS = """
前置:
  cd ~/Desktop/xueqiu-scraper && source venv/bin/activate

━━ 登录 / 会话 ━━
  python main.py login
  python main.py test-cookie

━━ 股票 / 用户管理 ━━
  python main.py add-stock 振华科技
  python main.py add-stock 000733
  python main.py add-user 罗洄头
  python main.py remove-stock SZ000733
  python main.py remove-user 1234567890
  python main.py status

━━ 爬取核心 ━━
  # 增量
  python main.py scrape --stocks 振华科技 西藏矿业
  python main.py scrape --users 罗洄头
  python main.py scrape --all

  # 历史
  python main.py scrape --stocks 振华科技 --mode history --pages 100
  python main.py scrape --stocks 海航控股 移远通信 --mode history --pages 100 --workers 1

  # 自动化场景
  python main.py scrape --stocks 振华科技 --mode history --pages 100 --yes --no-preflight

━━ 评论补全 / 审计 ━━
  python main.py backfill-comments --days 0
  python main.py backfill-comments --symbol SH600519 --days 0
  python main.py audit-completeness --symbol SH600519

━━ 话题 / 健康 / 摘要 ━━
  python main.py scrape-trending
  python main.py health
  python main.py daily-digest
"""


# ====================================================================
# 第十部分：scrape / backfill 运行语义
# ====================================================================

SCRAPE_COMMAND_SPEC = """
━━━ scrape 运行语义 ━━━

update:
  - 从最近边界向后更新
  - 采新帖
  - 回填旧帖新增评论
  - 适合日常运行

history/backfill:
  - 从数据库当前最老帖子为边界，继续向更早历史推进
  - 默认不抓评论（history_inline_comments=false）
  - 页级 checkpoint，失败后从游标续跑
  - 适合补数据库旧数据

backfill-comments:
  - 只针对评论缺口 / 孤儿评论 / 评论结构缺口
  - 不负责推进帖子时间线
  - 可独立长跑

━━━ 运行前行为 ━━━
  - 股票名 / 用户名自动解析
  - 默认展示数据库已有时间窗口
  - 默认展示本轮股票 / KOL 清单
  - `--yes` 跳过确认
  - `--no-preflight` 跳过运行前窗口展示与清单编辑

━━━ 自动化建议 ━━━
  - 历史模式建议单线程
  - 大批量任务：先历史，后评论回填
  - 不要同时开两个真实写入进程（历史 + 评论回填）
"""


# ====================================================================
# 第十一部分：已验证 API 接口
# ====================================================================

VERIFIED_API = """
必须从 xueqiu.com 同域发出（浏览器上下文 fetch）。

1. 帖子时间线
   https://xueqiu.com/query/v1/symbol/search/status.json

2. 评论列表（旧）
   https://xueqiu.com/statuses/comments.json

3. 评论列表（v3，优先）
   https://xueqiu.com/statuses/v3/comments.json

4. 用户发言
   https://xueqiu.com/v4/statuses/user_timeline.json

5. 用户搜索
   https://xueqiu.com/statuses/search.json

6. 热门话题
   https://xueqiu.com/hot_event/list.json

7. 股票搜索
   https://xueqiu.com/stock/search.json

不可用或不建议:
  - requests / curl_cffi 直打核心接口
  - stock.xueqiu.com/* 跨域
  - 未经过主页 / referer 暖场的裸打 JSON
"""


# ====================================================================
# 第十二部分：数据库表结构 v3+
# ====================================================================

DATABASE_SCHEMA = """
SQLite (WAL), 路径: data/xueqiu.db

watched_stocks:
  symbol, name, sector,
  last_scrape_time, oldest_post_time,
  history_complete, history_stagnant_runs,
  history_cursor_page, history_cursor_oldest_time, history_cursor_updated_at,
  is_active

tracked_users:
  user_id, screen_name,
  last_check_time, oldest_status_time,
  history_complete, history_stagnant_runs,
  is_active, note, credibility_score

posts:
  id, platform_id, symbol, user_id, user_name,
  title, text_html, text_plain, description,
  created_at, created_at_str, market_phase,
  thread_root_post_id,
  reply_count, like_count, retweet_count, fav_count, view_count,
  comments_scraped, max_comment_depth, scraped_at

comments:
  id, post_id, platform_id, user_id, user_name,
  text_html, text_plain, created_at, created_at_str, market_phase,
  like_count,
  parent_comment_id, reply_to_user_id, reply_to_user_name, depth,
  status_id, root_status_id, retweet_status_id, comment_reply_count,
  canonical_post_id, parent_post_id, parent_scope, scraped_at

comment_memberships:
  post_id, comment_id, relation_scope, linked_at
  作用:
    - comments 保存评论实体
    - comment_memberships 保存评论在某帖子中的展示归属

user_profiles:
  用户画像

user_statuses:
  KOL / 用户发言

trending_topics:
  热门事件榜单

discussion_threads 视图:
  线程级汇总，已按 comment_memberships 统计 actual_comments / participants

当前结构性结论:
  - comment_multi_post = 0
  - comments_missing_canonical = 0
"""


# ====================================================================
# 第十三部分：当前配置与数据状态（2026-03-23 快照）
# ====================================================================

CURRENT_STATUS = """
【配置快照】
  update 模式:
    - 2~5s 请求间隔
    - 95 次请求后休息 90~120s

  history 模式:
    - 7~11s 请求间隔
    - 50 次请求后休息 150~240s
    - history_chunk_pages = 3
    - history_timeline_transport = page
    - fallback = isolated_page
    - timeline timeout = 12000ms
    - retries = 1
    - history_reuse_single_client = true
    - history_inline_comments = false
    - history_browser_recycle_pages = 8
    - history_browser_recycle_requests = 28
    - history_adaptive_pacing = false（能力已实现，默认关闭）

  comment backfill:
    - 8~14s 请求间隔
    - 50 次请求后休息 150~240s
    - 单帖预算 75s
    - comment_v3_enabled = true

  manual verification:
    - enabled = true
    - mode = external
    - gate_enabled = true
    - profile = data/manual_chrome_profile

【数据库快照】
  - posts: 133748
  - comments: 24993
  - watched_stocks: 198
  - tracked_users: 1
  - 已触底股票: 20
  - 活跃股票: 197

【当前系统结论】
  - 历史模式已经可以稳定长跑 pages 100
  - 评论回填链路整体比历史模式更稳
  - 评论多帖重复挂载问题已修复
  - 大规模自选股历史批跑已跑过多轮，支持失败批次重跑
"""


# ====================================================================
# 第十四部分：运维与批跑
# ====================================================================

OPERATIONS = """
━━━ 运行摘要 ━━━
  每次 scrape / backfill-comments 结束后自动写:
  - data/run_reports/latest_run.md
  - data/run_reports/latest_run.json
  - 时间戳归档文件

━━━ 隔夜批跑 ━━━
  入口:
    python utils/watchlist_queue_runner.py --start-index 21 --pages 100 --workers 1

  新能力:
    python utils/watchlist_queue_runner.py \\
      --rerun-failed-manifest data/overnight_runs/queue_manifest.json \\
      --pages 100 --workers 1

  说明:
    - history 队列内 watchdog 已禁用
    - 正常强制休息不再被误判成卡死
    - 每批一个独立日志
    - manifest 记录批次返回码、股票列表、日志路径

━━━ 当前运维习惯 ━━━
  1. 先跑历史批次
  2. 再对已补完历史的股票跑评论回填
  3. 再做完整性审计
"""


# ====================================================================
# 第十五部分：导出格式与运行产物
# ====================================================================

EXPORT_FORMATS = """
JSON:
  data/snapshots/{SYMBOL}_{DATE}.json
  - 树形评论
  - 含 children / depth / reply_to_user

CSV:
  - 帖子 / 评论交错导出
  - 可供表格分析

Markdown:
  - 按线程或按运行结果汇总

运行产物:
  - data/logs/scraper.log
  - data/run_reports/*.md / *.json
  - data/overnight_runs/batch_*.log
  - data/overnight_runs/queue_manifest.json
"""


# ====================================================================
# 第十六部分：已知问题与后续优化方向
# ====================================================================

KNOWN_ISSUES = """
━━━ 当前已知问题 ━━━
1. 官网过滤
   - 某些帖子页面可见评论数 > 接口可返回评论数
   - 这类缺口不是简单多跑一次就能清零

2. 历史模式仍比评论回填更脆弱
   - 因为依赖 status.json
   - 遇到风控页 / HTML 响应的概率更高

3. 个别非股票标的解析容易歧义
   - 例如 XAU 被错误匹配成 GOLDMONEY INC
   - COMEX 黄金 / 白银、指数类标的要单独处理

4. 历史触底判断仍是启发式
   - 基于 stagnant_runs / 游标 / 最老边界
   - 不是官方“已无更多页”的绝对证明

5. 评论完备性仍受雪球接口上限约束
   - 尤其是深层楼中楼和官网过滤型帖子

━━━ 后续优化方向 ━━━
6. 标的类型识别
   - 把股票、港股、美股、ETF、期货、贵金属、指数明确分层

7. 评论回填队列化
   - 给 comment backfill 做独立 manifest / 批跑器

8. 触底证明增强
   - 增加“官网页码核验”或更强的边界探测

9. 舆情分析层
   - 仍未正式启动
   - 当前系统主要是高质量采集与整理
"""


# ====================================================================
# 第十七部分：开源调研摘要
# ====================================================================

OPEN_SOURCE_RESEARCH = """
【Scrapling】
  - 断点续爬、失败分级、长任务运维思路
  - 已吸收其“状态机/恢复”思想，但未直接接入

【xueqiu_mcp / pysnowball】
  - 为未来行情 / MCP 封装提供参考

【FinBERT2 / cnsenti】
  - 为 Phase 6 情绪打分准备

【AIMM / Hide-and-Shill】
  - 为 Phase 7 水军/操纵识别提供方法论
"""


# ====================================================================
# 第十八部分：给 AI 助手的指引
# ====================================================================

AI_INSTRUCTIONS = """
━━━ 必读规则 ━━━
1. 用户不是程序员，回答要能直接落到终端命令和结果上
2. 默认环境是 macOS + zsh
3. 不要改回 requests / curl_cffi 主链路
4. 所有关键接口必须在 xueqiu.com 同域浏览器上下文中请求
5. 历史模式和评论回填是两条不同链路，不要混着讲
6. 历史模式当前默认不抓评论，不要把“评论 0”误判成 bug
7. 任何涉及代码编辑都优先用 apply_patch
8. 项目中已有大量运行日志与摘要，先查现状再下判断

━━━ 先查什么 ━━━
1. python main.py status
2. python main.py health
3. 查看 data/run_reports/latest_run.md
4. 查看 data/overnight_runs/*.log（如果是批跑问题）

━━━ 关键事实 ━━━
1. 历史模式:
   - 从数据库最老边界继续向前
   - 依赖 history_cursor_page
   - 风险点在 status.json

2. 评论回填:
   - 更稳定
   - 用于补评论缺口、孤儿父链、结构缺口

3. 评论归属:
   - canonical_post_id + comment_memberships 才是当前正确模型
   - 不要再退回“comment 只靠 post_id”那种旧逻辑

4. 运行摘要:
   - 统一看 data/run_reports/latest_run.md / json

5. 批跑:
   - 看 utils/watchlist_queue_runner.py
   - 失败批次可用 rerun-failed-manifest

━━━ 默认行动原则 ━━━
  1. 先查现状
  2. 再定位是历史链路还是评论链路
  3. 优先修恢复语义，不要打临时补丁
  4. 大批跑之前先确认当前数据库边界和是否已触底
  5. 汇报时优先用中文股票名
"""


# ====================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  雪球舆情智能投研系统 项目文档 v8 — 18个章节")
    print("  Phase 1-5 ✅ | 2026-03-16~03-23 运维重构 ✅")
    print("  当前重点: 稳定历史补全 + 评论回填 + 自动运维")
    print("=" * 60)
    print()
    for i, name in enumerate([
        "项目概述与愿景",
        "开发历程",
        "最近新增变化",
        "战略路线图(Phase 6-9)",
        "服务目标体系",
        "本体论模型 v3",
        "技术架构",
        "文件结构",
        "CLI 命令手册",
        "scrape / backfill 运行语义",
        "已验证 API",
        "数据库表结构 v3+",
        "当前配置与数据状态",
        "运维与批跑",
        "导出格式与运行产物",
        "已知问题与后续优化",
        "开源调研摘要",
        "AI 助手指引",
    ], 1):
        print(f"  {i:2d}. {name}")

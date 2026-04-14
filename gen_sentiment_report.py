#!/usr/bin/env python3
"""
雪球舆情 + K线 综合投研报告生成器
从数据库拉取所有有评论的A股，结合K线数据生成HTML报告
"""
import sqlite3
import json
import os
from datetime import datetime, timezone, timedelta
from collections import defaultdict

DB_PATH = 'data/xueqiu.db'
OUTPUT_DIR = 'data/analysis-reports'
os.makedirs(OUTPUT_DIR, exist_ok=True)
OUTPUT_FILE = os.path.join(OUTPUT_DIR, f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_sentiment_kline_report.html')

conn = sqlite3.connect(DB_PATH)

# ============================================================
# 1. 舆情数据采集
# ============================================================

now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
seven_days_ago = now_ms - 86400000 * 7
thirty_days_ago = now_ms - 86400000 * 30

# --- 1a. 最近7天评论活跃度 ---
comment_stats = conn.execute('''
    SELECT p.symbol,
           COUNT(DISTINCT c.id) as comment_count,
           COUNT(DISTINCT c.user_id) as unique_users,
           SUM(c.like_count) as total_likes,
           MAX(c.created_at) as latest_comment
    FROM comments c
    JOIN comment_memberships cm ON c.id = cm.comment_id
    JOIN posts p ON cm.post_id = p.id
    WHERE (p.symbol LIKE 'SH%' OR p.symbol LIKE 'SZ%')
      AND c.created_at >= ?
      AND length(COALESCE(c.text_plain, '')) > 5
    GROUP BY p.symbol
    ORDER BY comment_count DESC
''', (seven_days_ago,)).fetchall()

# --- 1b. 最近7天评论详情（热门评论TOP30）---
hot_comments = conn.execute('''
    SELECT p.symbol, c.text_plain, c.created_at, c.user_name, c.like_count, c.depth
    FROM comments c
    JOIN comment_memberships cm ON c.id = cm.comment_id
    JOIN posts p ON cm.post_id = p.id
    WHERE (p.symbol LIKE 'SH%' OR p.symbol LIKE 'SZ%')
      AND c.created_at >= ?
      AND length(COALESCE(c.text_plain, '')) > 10
    ORDER BY c.like_count DESC
    LIMIT 30
''', (seven_days_ago,)).fetchall()

# --- 1c. 最近30天热门帖子 TOP20 ---
hot_posts = conn.execute('''
    SELECT p.symbol, p.title, p.text_plain, p.created_at, p.like_count,
           p.retweet_count, p.reply_count, p.user_name,
           COALESCE(cnt.c, 0) as comment_count
    FROM posts p
    LEFT JOIN (
        SELECT post_id, COUNT(*) as c FROM comment_memberships GROUP BY post_id
    ) cnt ON cnt.post_id = p.id
    WHERE (p.symbol LIKE 'SH%' OR p.symbol LIKE 'SZ%')
      AND p.created_at >= ?
      AND (p.like_count > 5 OR COALESCE(cnt.c, 0) > 20)
    ORDER BY (p.like_count + COALESCE(cnt.c,0) * 2 + COALESCE(p.retweet_count,0) * 3) DESC
    LIMIT 20
''', (thirty_days_ago,)).fetchall()

# --- 1d. 总量统计 ---
total_posts = conn.execute('''
    SELECT COUNT(*) FROM posts 
    WHERE (symbol LIKE 'SH%' OR symbol LIKE 'SZ%')
''').fetchone()[0]

total_comments = conn.execute('''
    SELECT COUNT(*) FROM comments c
    JOIN comment_memberships cm ON c.id = cm.comment_id
    JOIN posts p ON cm.post_id = p.id
    WHERE (p.symbol LIKE 'SH%' OR p.symbol LIKE 'SZ%')
''').fetchone()[0]

stock_count = conn.execute('''
    SELECT COUNT(DISTINCT p.symbol) FROM posts p
    WHERE (p.symbol LIKE 'SH%' OR p.symbol LIKE 'SZ%')
''').fetchone()[0]

# ============================================================
# 2. K线数据采集
# ============================================================

# 获取所有有评论且有K线的股票的最近20日K线
kline_data = {}
active_symbols = [r[0] for r in comment_stats[:10]]  # TOP10活跃股

for sym in active_symbols:
    rows = conn.execute('''
        SELECT trade_date, open, high, low, close, pct_chg, vol, amount
        FROM kline_daily 
        WHERE symbol = ?
        ORDER BY trade_date DESC 
        LIMIT 20
    ''', (sym,)).fetchall()
    if rows:
        kline_data[sym] = list(reversed(rows))  # 按日期升序

# 涨跌幅统计（5日）
performance = {}
for sym, klines in kline_data.items():
    if len(klines) >= 5:
        latest = klines[-1][4]  # close
        five_ago = klines[-5][4]
        performance[sym] = ((latest / five_ago) - 1) * 100 if five_ago > 0 else 0

# 成交额变化
volume_change = {}
for sym, klines in kline_data.items():
    if len(klines) >= 5:
        recent_vol = sum(k[7] for k in klines[-5:]) / 5
        if len(klines) >= 10:
            prev_vol = sum(k[7] for k in klines[-10:-5]) / 5
            volume_change[sym] = ((recent_vol / prev_vol) - 1) * 100 if prev_vol > 0 else 0
        else:
            volume_change[sym] = 0

# ============================================================
# 3. 舆情-价格联动分析
# ============================================================

# 最近30天帖子数按股票统计
post_volume_30d = conn.execute('''
    SELECT p.symbol, COUNT(*) as cnt
    FROM posts p
    WHERE (p.symbol LIKE 'SH%' OR p.symbol LIKE 'SZ%')
      AND p.created_at >= ?
    GROUP BY p.symbol
    ORDER BY cnt DESC
''', (thirty_days_ago,)).fetchall()

# ============================================================
# 4. 生成HTML报告
# ============================================================

# 股票名称映射
stock_names = {}
for r in conn.execute('SELECT symbol, name FROM watched_stocks').fetchall():
    if r[1]:
        stock_names[r[0]] = r[1]

def get_name(sym):
    """获取股票名称，确保始终有可读名称"""
    name = stock_names.get(sym)
    if name:
        return name
    # 兜底：返回代码去掉前缀
    return sym[2:]

def fmt_date(ts):
    return datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%m-%d %H:%M')

def fmt_pct(val):
    color = '#e74c3c' if val > 0 else '#2ecc71' if val < 0 else '#95a5a6'
    sign = '+' if val > 0 else ''
    return f'<span style="color:{color}">{sign}{val:.2f}%</span>'

def calc_sentiment(texts):
    """简单情绪统计（基于关键词辅助）"""
    if not texts:
        return 'neutral', 0
    bullish_words = ['涨','牛','买','加仓','持有','看好','突破','新高','机会','利好','反转','反弹','修复','翻倍','赚钱','底部','抄底','长线','价值','低估','增长']
    bearish_words = ['跌','熊','卖','减仓','套','亏','割肉','止损','风险','泡沫','高位','破位','下跌','崩','恐慌','割','套牢','回调','跌停','缩量']
    
    bull = 0
    bear = 0
    for t in texts:
        for w in bullish_words:
            bull += t.count(w)
        for w in bearish_words:
            bear += t.count(w)
    
    total = bull + bear
    if total == 0:
        return 'neutral', 0
    
    ratio = (bull - bear) / total
    if ratio > 0.2:
        return 'bullish', round(ratio * 100)
    elif ratio < -0.2:
        return 'bearish', round(abs(ratio) * 100)
    else:
        return 'neutral', 0

# 按股票聚合评论做情绪判断
stock_sentiments = {}
for sym in active_symbols:
    comments = conn.execute('''
        SELECT c.text_plain FROM comments c
        JOIN comment_memberships cm ON c.id = cm.comment_id
        JOIN posts p ON cm.post_id = p.id
        WHERE p.symbol = ? AND c.created_at >= ?
          AND length(COALESCE(c.text_plain, '')) > 10
    ''', (sym, seven_days_ago)).fetchall()
    texts = [r[0] or '' for r in comments]
    sent, strength = calc_sentiment(texts)
    stock_sentiments[sym] = {'sentiment': sent, 'strength': strength, 'count': len(texts)}

# 按板块聚合
from analysis.sector_analysis import load_sector_mapping
sector_map = load_sector_mapping()
sector_heat = defaultdict(lambda: {'posts': 0, 'comments': 0, 'stocks': []})
for sym, cnt in post_volume_30d:
    for sector, stocks in sector_map.items():
        if sym in stocks:
            sector_heat[sector]['posts'] += cnt
            sector_heat[sector]['comments'] += sum(r[1] for r in comment_stats if r[0] == sym)
            sector_heat[sector]['stocks'].append(sym)

# ===== ECharts配置 =====
echarts_cdn = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"

# --- K线图数据 ---
kline_series = []
for sym in active_symbols[:6]:
    if sym not in kline_data:
        continue
    data = kline_data[sym]
    name = get_name(sym)
    kline_series.append({
        'name': name,
        'dates': [str(d[0]) for d in data],
        'closes': [d[4] for d in data],
        'pct_chgs': [d[5] for d in data],
    })

# --- 舆情热度数据 ---
heat_data = []
for r in comment_stats[:15]:
    sym = r[0]
    kline_5d = performance.get(sym)
    heat_data.append({
        'symbol': sym,
        'name': get_name(sym),
        'comments': r[1],
        'users': r[2],
        'likes': r[3] or 0,
        'heat': (r[3] or 0) + r[1] * 2,
        'price_chg': kline_5d,
    })

# --- 板块热度数据 ---
sector_list = sorted(sector_heat.items(), key=lambda x: x[1]['posts'], reverse=True)[:8]

# --- 热门帖子数据 ---
posts_list = []
for r in hot_posts[:15]:
    dt = datetime.fromtimestamp(r[3]/1000, tz=timezone.utc).strftime('%m-%d')
    title = (r[1] or r[2] or '')[:80].replace('\n', ' ').replace("'", "\\'")
    heat = (r[4] or 0) + r[8] * 2 + (r[5] or 0) * 3 + (r[6] or 0)
    posts_list.append({
        'symbol': r[0], 'name': get_name(r[0]),
        'date': dt, 'user': r[7] or '?',
        'likes': r[4] or 0, 'comments': r[8], 'heat': round(heat),
        'title': title,
    })

# --- 热门评论数据 ---
comments_list = []
for r in hot_comments[:20]:
    dt = fmt_date(r[2])
    text = (r[1] or '')[:150].replace('\n', ' ').replace('<', '&lt;').replace('>', '&gt;')
    comments_list.append({
        'symbol': r[0], 'name': get_name(r[0]),
        'time': dt, 'user': r[3] or '?',
        'likes': r[4] or 0, 'depth': r[5] or 0,
        'text': text,
    })

# ===== 生成HTML =====
html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>雪球舆情投研报告 | {datetime.now().strftime("%Y-%m-%d")}</title>
<script src="{echarts_cdn}"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ 
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", sans-serif;
    background: #0a0a0f; color: #e0e0e0; line-height: 1.6;
  }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 20px; }}
  
  /* 头部 */
  .header {{ 
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
    border-radius: 16px; padding: 32px; margin-bottom: 24px;
    border: 1px solid #2a2a4a;
  }}
  .header h1 {{ font-size: 28px; font-weight: 700; color: #fff; margin-bottom: 8px; }}
  .header .meta {{ color: #888; font-size: 14px; }}
  .header .kpi-row {{ display: flex; gap: 32px; margin-top: 20px; flex-wrap: wrap; }}
  .kpi {{ text-align: center; }}
  .kpi .num {{ font-size: 32px; font-weight: 700; }}
  .kpi .label {{ font-size: 12px; color: #888; margin-top: 4px; }}
  .kpi.red .num {{ color: #e74c3c; }}
  .kpi.green .num {{ color: #2ecc71; }}
  .kpi.blue .num {{ color: #3498db; }}
  .kpi.yellow .num {{ color: #f39c12; }}
  
  /* 卡片 */
  .card {{
    background: #12121a; border-radius: 12px; padding: 24px;
    margin-bottom: 24px; border: 1px solid #1e1e2e;
  }}
  .card h2 {{ 
    font-size: 18px; font-weight: 600; color: #fff; margin-bottom: 16px;
    padding-bottom: 12px; border-bottom: 1px solid #2a2a3a;
    display: flex; align-items: center; gap: 8px;
  }}
  .card h2 .icon {{ font-size: 20px; }}
  
  /* 表格 */
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ 
    text-align: left; padding: 10px 12px; color: #888; font-weight: 500;
    border-bottom: 1px solid #2a2a3a; font-size: 12px; text-transform: uppercase;
  }}
  td {{ padding: 10px 12px; border-bottom: 1px solid #1a1a2a; }}
  tr:hover {{ background: #1a1a2e; }}
  .sentiment-tag {{
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; font-weight: 600;
  }}
  .sentiment-bullish {{ background: rgba(231,76,60,0.2); color: #e74c3c; }}
  .sentiment-bearish {{ background: rgba(46,204,113,0.2); color: #2ecc71; }}
  .sentiment-neutral {{ background: rgba(149,165,166,0.2); color: #95a5a6; }}
  
  /* 图表容器 */
  .chart-container {{ width: 100%; height: 400px; }}
  
  /* 两列布局 */
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }}
  @media (max-width: 768px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  
  /* 评论卡片 */
  .comment-item {{
    background: #1a1a2e; border-radius: 8px; padding: 14px 16px;
    margin-bottom: 10px; border-left: 3px solid #2a2a4a;
  }}
  .comment-item.hot {{ border-left-color: #e74c3c; }}
  .comment-item .meta {{ font-size: 11px; color: #666; margin-bottom: 6px; }}
  .comment-item .user {{ color: #3498db; font-weight: 500; }}
  .comment-item .text {{ font-size: 13px; color: #ccc; line-height: 1.5; }}
  .comment-item .likes {{ color: #e74c3c; font-size: 12px; }}
  
  /* 帖子卡片 */
  .post-item {{
    background: #1a1a2e; border-radius: 8px; padding: 14px 16px;
    margin-bottom: 10px; display: flex; gap: 12px; align-items: flex-start;
  }}
  .post-item .rank {{
    min-width: 28px; height: 28px; border-radius: 6px;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 13px;
  }}
  .post-item:nth-child(1) .rank {{ background: linear-gradient(135deg, #e74c3c, #c0392b); }}
  .post-item:nth-child(2) .rank {{ background: linear-gradient(135deg, #e67e22, #d35400); }}
  .post-item:nth-child(3) .rank {{ background: linear-gradient(135deg, #f1c40f, #f39c12); color: #333; }}
  .post-item:nth-child(n+4) .rank {{ background: #2a2a3a; color: #666; }}
  .post-item .content {{ flex: 1; }}
  .post-item .title {{ font-size: 13px; color: #ddd; margin-bottom: 4px; }}
  .post-item .meta {{ font-size: 11px; color: #666; }}
  .post-item .heat-badge {{
    display: inline-block; background: rgba(231,76,60,0.15);
    color: #e74c3c; padding: 1px 6px; border-radius: 3px; font-size: 11px;
  }}
  
  /* 洞察框 */
  .insight-box {{
    background: linear-gradient(135deg, rgba(52,152,219,0.1), rgba(155,89,182,0.1));
    border: 1px solid rgba(52,152,219,0.3); border-radius: 12px;
    padding: 20px; margin-bottom: 16px;
  }}
  .insight-box h3 {{ color: #3498db; font-size: 15px; margin-bottom: 8px; }}
  .insight-box p {{ color: #bbb; font-size: 13px; }}
  
  .tag {{ 
    display: inline-block; padding: 2px 8px; border-radius: 4px;
    font-size: 11px; margin-right: 4px; 
  }}
  .tag-red {{ background: rgba(231,76,60,0.15); color: #e74c3c; }}
  .tag-green {{ background: rgba(46,204,113,0.15); color: #2ecc71; }}
  .tag-blue {{ background: rgba(52,152,219,0.15); color: #3498db; }}
  .tag-yellow {{ background: rgba(243,156,18,0.15); color: #f39c12; }}
  
  .footer {{ text-align: center; color: #444; font-size: 12px; margin-top: 32px; padding: 16px; }}
</style>
</head>
<body>
<div class="container">

<!-- ====== 头部 ====== -->
<div class="header">
  <h1>📊 雪球舆情投研报告</h1>
  <div class="meta">报告时间：{datetime.now().strftime("%Y-%m-%d %H:%M")} | 数据覆盖：{stock_count}只A股 | 数据源：雪球</div>
  <div class="kpi-row">
    <div class="kpi blue"><div class="num">{total_posts:,}</div><div class="label">累计帖子</div></div>
    <div class="kpi yellow"><div class="num">{total_comments:,}</div><div class="label">累计评论</div></div>
    <div class="kpi red"><div class="num">{len(comment_stats)}</div><div class="label">7日活跃股</div></div>
    <div class="kpi green"><div class="num">{len(kline_data)}</div><div class="label">有K线</div></div>
  </div>
</div>

<!-- ====== 核心洞察 ====== -->
<div class="card">
  <h2><span class="icon">🔍</span> 核心洞察</h2>
  <div class="grid-2">
'''

# 生成洞察
insights = []

# 洞察1: 最活跃的股票
if comment_stats:
    top_sym = comment_stats[0][0]
    top_name = get_name(top_sym)
    top_cnt = comment_stats[0][1]
    top_perf = performance.get(top_sym)
    perf_str = fmt_pct(top_perf) if top_perf is not None else '无K线'
    insights.append({
        'icon': '🔥', 'color': 'blue',
        'title': f'{top_name} 舆情最活跃',
        'text': f'近7天{top_cnt}条评论，5日涨跌{perf_str}。社区讨论热度最高。'
    })

# 洞察2: 涨幅最大但评论少（潜在机会）
for sym, chg in sorted(performance.items(), key=lambda x: -x[1]):
    comment_cnt = next((r[1] for r in comment_stats if r[0] == sym), 0)
    if chg > 3 and comment_cnt < 5:
        insights.append({
            'icon': '💎', 'color': 'yellow',
            'title': f'{get_name(sym)} 静默上涨',
            'text': f'5日涨幅{fmt_pct(chg)}，但近7天仅{comment_cnt}条评论。冷清股吧中的强势股，需关注。'
        })
        break

# 洞察3: 下跌但社区看多（分歧信号）
for sym in active_symbols:
    sent = stock_sentiments.get(sym, {}).get('sentiment', 'neutral')
    chg = performance.get(sym)
    if sent == 'bullish' and chg is not None and chg < -2:
        insights.append({
            'icon': '⚡', 'color': 'red',
            'title': f'{get_name(sym)} 舆情-价格分歧',
            'text': f'社区偏看多，但5日下跌{fmt_pct(chg)}。可能存在抄底机会或陷阱，需深入分析。'
        })
        break

# 洞察4: 放量股
for sym, vc in sorted(volume_change.items(), key=lambda x: -x[1]):
    if vc > 30:
        chg = performance.get(sym, 0)
        insights.append({
            'icon': '📈', 'color': 'green',
            'title': f'{get_name(sym)} 成交额异动',
            'text': f'近5日成交额较前5日放大{vc:.0f}%，5日涨跌{fmt_pct(chg)}。放量需关注方向选择。'
        })
        break

# 洞察5: 板块轮动
if sector_list:
    top_sector = sector_list[0]
    insights.append({
        'icon': '🔄', 'color': 'blue',
        'title': f'{top_sector[0]} 板块最活跃',
        'text': f'近30天{len(top_sector[1]["stocks"])}只股票有帖子，共{top_sector[1]["posts"]}条。资金关注度最高。'
    })

for ins in insights[:4]:
    html += f'''
    <div class="insight-box" style="border-color: rgba(52,152,219,0.3);">
      <h3>{ins["icon"]} {ins["title"]}</h3>
      <p>{ins["text"]}</p>
    </div>'''

html += '''
  </div>
</div>
'''

# ====== 舆情热度排行 ======
html += '''
<div class="card">
  <h2><span class="icon">🔥</span> 近7日舆情热度 TOP15</h2>
  <div id="heat-chart" class="chart-container"></div>
</div>
'''

# ====== K线走势 ======
html += '''
<div class="card">
  <h2><span class="icon">📉</span> 活跃股近期走势（涨跌幅）</h2>
  <div id="kline-chart" class="chart-container"></div>
</div>
'''

# ====== 舆情-价格联动表 ======
html += '''
<div class="card">
  <h2><span class="icon">🔗</span> 舆情-价格联动分析</h2>
  <table>
    <tr>
      <th>股票</th><th>7日评论</th><th>独立用户</th>
      <th>总点赞</th><th>舆情热度</th><th>情绪判断</th><th>5日涨跌</th><th>信号</th>
    </tr>'''

for r in comment_stats[:15]:
    sym = r[0]
    name = get_name(sym)
    heat = (r[3] or 0) + r[1] * 2
    
    sent = stock_sentiments.get(sym, {}).get('sentiment', 'neutral')
    strength = stock_sentiments.get(sym, {}).get('strength', 0)
    
    chg = performance.get(sym)
    chg_str = fmt_pct(chg) if chg is not None else '<span style="color:#666">--</span>'
    
    # 信号判断
    signal = ''
    signal_class = 'tag-blue'
    if sent == 'bullish' and chg and chg < -2:
        signal = '抄底关注'; signal_class = 'tag-yellow'
    elif sent == 'bearish' and chg and chg > 3:
        signal = '见顶警示'; signal_class = 'tag-red'
    elif sent == 'bullish' and chg and chg > 3:
        signal = '趋势共振↑'; signal_class = 'tag-red'
    elif sent == 'bearish' and chg and chg < -3:
        signal = '趋势共振↓'; signal_class = 'tag-green'
    elif r[1] < 3 and chg and chg > 2:
        signal = '冷清上涨'; signal_class = 'tag-yellow'
    elif heat > 50:
        signal = '高热度'; signal_class = 'tag-blue'
    
    sent_class = f'sentiment-{sent}'
    sent_label = {'bullish': '看多', 'bearish': '看空', 'neutral': '中性'}.get(sent, '中性')
    
    html += f'''
    <tr>
      <td><b>{name}</b> <span style="color:#555;font-size:11px">{sym}</span></td>
      <td>{r[1]}</td>
      <td>{r[2]}</td>
      <td>{r[3] or 0}</td>
      <td>{heat}</td>
      <td><span class="sentiment-tag {sent_class}">{sent_label} ({strength}%)</span></td>
      <td><b>{chg_str}</b></td>
      <td><span class="tag {signal_class}">{signal}</span></td>
    </tr>'''

html += '</table></div>'

# ====== 板块热度 ======
if sector_list:
    html += '''
<div class="card">
  <h2><span class="icon">🏗️</span> 板块热度分布（近30天帖子数）</h2>
  <div id="sector-chart" class="chart-container"></div>
</div>'''

# ====== 热门帖子 ======
if posts_list:
    html += '''
<div class="card">
  <h2><span class="icon">📌</span> 近30天热门帖子 TOP15</h2>'''
    for i, p in enumerate(posts_list):
        html += f'''
    <div class="post-item">
      <div class="rank">{i+1}</div>
      <div class="content">
        <div class="title">{p["title"]}</div>
        <div class="meta">
          <span class="tag tag-blue">{p["name"]}</span>
          @{p["user"]} · {p["date"]} · ❤{p["likes"]} 💬{p["comments"]}
          <span class="heat-badge">热度 {p["heat"]}</span>
        </div>
      </div>
    </div>'''
    html += '</div>'

# ====== 热门评论 ======
if comments_list:
    html += '''
<div class="card">
  <h2><span class="icon">💬</span> 近7天高赞评论 TOP20</h2>'''
    for c in comments_list:
        hot_class = ' hot' if c['likes'] >= 3 else ''
        html += f'''
    <div class="comment-item{hot_class}">
      <div class="meta">
        <span class="tag tag-blue">{c["name"]}</span>
        <span class="user">@{c["user"]}</span> · {c["time"]}
        <span class="likes">❤ {c["likes"]}</span>
      </div>
      <div class="text">{c["text"]}</div>
    </div>'''
    html += '</div>'

html += '''
<div class="footer">
  雪球舆情智能投研系统 | 数据仅供参考，不构成投资建议 | Generated by AI Agent
</div>
</div>

<script>
// ===== 舆情热度横向柱状图 =====
(function() {{
  var chart = echarts.init(document.getElementById('heat-chart'));
  var data = {json.dumps(heat_data, ensure_ascii=False)};
  data.sort((a, b) => a.heat - b.heat);
  var names = data.map(d => d.name);
  
  chart.setOption({{
    backgroundColor: 'transparent',
    grid: {{ left: 90, right: 60, top: 10, bottom: 20 }},
    xAxis: {{ type: 'value', axisLine: {{ show: false }}, axisTick: {{ show: false }},
             splitLine: {{ lineStyle: {{ color: '#1e1e2e' }} }},
             axisLabel: {{ color: '#666' }} }},
    yAxis: {{ type: 'category', 
             data: names,
             axisLine: {{ show: false }}, axisTick: {{ show: false }},
             axisLabel: {{ color: '#ccc', fontSize: 11 }} }},
    series: [{{
      type: 'bar', data: data.map(d => d.heat),
      itemStyle: {{
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
          {{ offset: 0, color: '#2d5aa0' }},
          {{ offset: 1, color: '#e74c3c' }}
        ]),
        borderRadius: [0, 4, 4, 0]
      }},
      label: {{ show: true, position: 'right', color: '#ccc', fontSize: 11 }}
    }}],
    tooltip: {{
      trigger: 'axis',
      formatter: function(params) {{
        var d = data[params[0].dataIndex];
        return '<b>' + d.name + '</b><br/>' +
               '评论: ' + d.comments + '<br/>' +
               '用户: ' + d.users + '<br/>' +
               '点赞: ' + d.likes + '<br/>' +
               '5日涨跌: ' + (d.price_chg !== null ? d.price_chg.toFixed(2) + '%' : '--');
      }}
    }}
  }});
  window.addEventListener('resize', () => chart.resize());
}})();

// ===== K线涨跌幅折线图 =====
(function() {{
  var chart = echarts.init(document.getElementById('kline-chart'));
  var series = {json.dumps(kline_series, ensure_ascii=False)};
  
  var option = {{
    backgroundColor: 'transparent',
    legend: {{ 
      data: series.map(s => s.name),
      textStyle: {{ color: '#888', fontSize: 11 }},
      top: 0
    }},
    grid: {{ left: 60, right: 30, top: 40, bottom: 30 }},
    xAxis: {{ 
      type: 'category',
      data: series[0].dates,
      axisLine: {{ lineStyle: {{ color: '#2a2a3a' }} }},
      axisLabel: {{ color: '#666', fontSize: 10 }}
    }},
    yAxis: {{ 
      type: 'value',
      name: '涨跌幅(%)',
      nameTextStyle: {{ color: '#666' }},
      axisLine: {{ show: false }},
      splitLine: {{ lineStyle: {{ color: '#1e1e2e' }} }},
      axisLabel: {{ color: '#666', fontSize: 10 }}
    }},
    series: series.map((s, i) => ({{
      name: s.name,
      type: 'line',
      data: s.pct_chgs,
      smooth: true,
      lineStyle: {{ width: 2 }},
      symbol: 'circle',
      symbolSize: 4,
      itemStyle: {{ color: ['#e74c3c','#3498db','#2ecc71','#f39c12','#9b59b6','#e67e22'][i % 6] }}
    }})),
    tooltip: {{ trigger: 'axis' }},
    dataZoom: [{{ type: 'inside' }}]
  }};
  chart.setOption(option);
  window.addEventListener('resize', () => chart.resize());
}})();

// ===== 板块热度图 =====
(function() {{
  var el = document.getElementById('sector-chart');
  if (!el) return;
  var chart = echarts.init(el);
  var sectors = {json.dumps([{'name': s[0], 'posts': s[1]['posts'], 'comments': s[1]['comments'], 'stocks': s[1]['stocks']} for s in sector_list], ensure_ascii=False)};
  sectors.sort((a, b) => b.posts - a.posts);
  
  chart.setOption({{
    backgroundColor: 'transparent',
    grid: {{ left: 100, right: 60, top: 10, bottom: 20 }},
    xAxis: {{ type: 'value', axisLine: {{ show: false }}, splitLine: {{ lineStyle: {{ color: '#1e1e2e' }} }},
             axisLabel: {{ color: '#666' }} }},
    yAxis: {{ type: 'category', data: sectors.map(s => s.name),
             axisLine: {{ show: false }}, axisLabel: {{ color: '#ccc', fontSize: 11 }} }},
    series: [{{
      type: 'bar', data: sectors.map(s => s.posts),
      itemStyle: {{
        color: new echarts.graphic.LinearGradient(0, 0, 1, 0, [
          {{ offset: 0, color: '#2d5aa0' }},
          {{ offset: 1, color: '#f39c12' }}
        ]),
        borderRadius: [0, 4, 4, 0]
      }},
      label: {{ show: true, position: 'right', color: '#ccc', fontSize: 11,
               formatter: function(p) {{ return p.value + '帖 | ' + sectors[p.dataIndex].stocks.length + '股'; }} }}
    }}],
    tooltip: {{
      trigger: 'axis',
      formatter: function(params) {{
        var s = sectors[params[0].dataIndex];
        return '<b>' + s.name + '</b><br/>帖子: ' + s.posts + '<br/>评论: ' + s.comments + '<br/>股票: ' + s.stocks.join(', ');
      }}
    }}
  }});
  window.addEventListener('resize', () => chart.resize());
}})();
</script>
</body>
</html>'''

conn.close()

with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    f.write(html)

print(f'报告已生成: {OUTPUT_FILE}')
print(f'大小: {os.path.getsize(OUTPUT_FILE) / 1024:.1f} KB')

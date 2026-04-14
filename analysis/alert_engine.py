#!/usr/bin/env python3
"""
异常检测引擎 — 基于基准线对比发现异常信号

9 种检测类型：
  1. volume_spike      — 讨论量暴增（帖子/评论量远超基准线）
  2. volume_drop       — 讨论量骤降（活跃股票突然冷清）
  3. sentiment_extreme  — 情绪极端（看多/看空比例异常高）
  4. sentiment_shift    — 情绪突变（与基准线情绪方向翻转）
  5. new_account_influx — 新账号涌入（大量低粉丝账号突然出现）
  6. manipulation_risk  — 操纵风险（LLM 标注的模板化/协调评论占比高）
  7. kol_activity       — KOL 动态（高粉丝用户突然发声）
  8. narrative_drift    — 叙事漂移（讨论主题突然变化）
  9. volume_price_divergence — 量价背离（讨论热度与价格走势相反）

每种检测返回标准化的 Alert 对象，写入 alerts 表。

用法：
  内部模块，由 daily-scan 调用
"""

import os
import sqlite3
from datetime import datetime
from typing import List

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Alert:
    """标准化告警对象"""

    def __init__(self, alert_type: str, symbol: str, severity: str,
                 title: str, detail: str = "", data_json: str = None,
                 suggestion: str = ""):
        self.alert_type = alert_type
        self.symbol = symbol
        self.severity = severity  # high / medium / low
        self.title = title
        self.detail = detail
        self.data_json = data_json
        self.suggestion = suggestion

    def to_dict(self) -> dict:
        return {
            "alert_type": self.alert_type,
            "symbol": self.symbol,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "data_json": self.data_json,
            "suggestion": self.suggestion,
        }


def save_alerts(conn: sqlite3.Connection, alerts: List[Alert]):
    """批量保存告警到 alerts 表"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for a in alerts:
        conn.execute("""
            INSERT INTO alerts (alert_type, symbol, severity, title,
                               detail, data_json, suggestion, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            a.alert_type, a.symbol, a.severity, a.title,
            a.detail, a.data_json, a.suggestion, now_str,
        ))
    conn.commit()


# ============================================================
# 9 种检测器
# ============================================================

def detect_volume_spike(symbol: str, today: dict, baseline: dict) -> List[Alert]:
    """1. 讨论量暴增：当日帖子/评论量超过基准线的 3 倍"""
    alerts = []

    for metric, label in [("posts", "帖子"), ("comments", "评论")]:
        baseline_val = baseline.get(f"avg_daily_{metric}", 0)
        today_val = today.get(metric, 0)

        if baseline_val > 0 and today_val >= baseline_val * 3:
            ratio = round(today_val / baseline_val, 1)
            severity = "high" if ratio >= 5 else "medium"
            alerts.append(Alert(
                alert_type="volume_spike",
                symbol=symbol,
                severity=severity,
                title=f"{label}量暴增 {ratio}x",
                detail=f"今日{label}量 {today_val}，基准线 {baseline_val:.0f}/天，偏离 {ratio}x",
                data_json=f'{{"today": {today_val}, "baseline": {baseline_val:.0f}, "ratio": {ratio}}}',
                suggestion="检查是否有重大消息或事件驱动，警惕拉高出货",
            ))

    return alerts


def detect_volume_drop(symbol: str, today: dict, baseline: dict) -> List[Alert]:
    """2. 讨论量骤降：当日帖子量为 0 但基准线 >5"""
    alerts = []

    avg_posts = baseline.get("avg_daily_posts", 0)
    today_posts = today.get("posts", 0)

    if avg_posts >= 5 and today_posts == 0:
        alerts.append(Alert(
            alert_type="volume_drop",
            symbol=symbol,
            severity="low",
            title="论坛冷清（今日零帖子）",
            detail=f"基准线 {avg_posts:.0f} 帖/天，今日 0 帖",
            suggestion="可能是数据未更新，或市场关注度转移",
        ))

    return alerts


def detect_sentiment_extreme(symbol: str, today: dict) -> List[Alert]:
    """3. 情绪极端：单边情绪占比 >80%"""
    alerts = []

    annotated = today.get("annotated_count", 0)
    if annotated < 5:
        return alerts  # 样本太少，不判断

    pct_bull = today.get("pct_bullish", 0)
    pct_bear = today.get("pct_bearish", 0)

    if pct_bull >= 80:
        alerts.append(Alert(
            alert_type="sentiment_extreme",
            symbol=symbol,
            severity="medium",
            title=f"极度看多（{pct_bull}%）",
            detail=f"看多{pct_bull}% 看空{pct_bear}%（{annotated}条已标注）",
            suggestion="极端看多可能是顶部信号，注意风险",
        ))
    elif pct_bear >= 80:
        alerts.append(Alert(
            alert_type="sentiment_extreme",
            symbol=symbol,
            severity="medium",
            title=f"极度看空（{pct_bear}%）",
            detail=f"看空{pct_bear}% 看多{pct_bull}%（{annotated}条已标注）",
            suggestion="极端看空可能是底部信号，但需确认基本面",
        ))

    return alerts


def detect_sentiment_shift(symbol: str, today: dict, baseline: dict) -> List[Alert]:
    """4. 情绪突变：当日主导情绪与基准线方向相反"""
    alerts = []

    annotated = today.get("annotated_count", 0)
    if annotated < 10:
        return alerts

    baseline_sent = baseline.get("avg_sentiment", 0)  # 正=看多，负=看空
    today_pct_bull = today.get("pct_bullish", 0)
    today_pct_bear = today.get("pct_bearish", 0)

    # 基准线看多(>0.1)但今日看空(<-0.1)
    if baseline_sent > 0.1 and today_pct_bear - today_pct_bull > 30:
        alerts.append(Alert(
            alert_type="sentiment_shift",
            symbol=symbol,
            severity="high",
            title="情绪突然翻空",
            detail=f"基准线偏多({baseline_sent:.2f})，今日看空占{today_pct_bear}%",
            suggestion="重大情绪转变，检查是否有利空消息",
        ))
    # 基准线看空(<-0.1)但今日看多(>0.1)
    elif baseline_sent < -0.1 and today_pct_bull - today_pct_bear > 30:
        alerts.append(Alert(
            alert_type="sentiment_shift",
            symbol=symbol,
            severity="high",
            title="情绪突然翻多",
            detail=f"基准线偏空({baseline_sent:.2f})，今日看多占{today_pct_bull}%",
            suggestion="情绪突然转向，检查是否有利好催化",
        ))

    return alerts


def detect_new_account_influx(symbol: str, conn: sqlite3.Connection) -> List[Alert]:
    """5. 新账号涌入：当日评论中低粉丝账号占比 >40%"""
    alerts = []

    today = datetime.now().strftime("%Y-%m-%d")

    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN COALESCE(up.followers_count, 0) < 5
                      OR COALESCE(up.status_count, 0) < 10
                 THEN 1 ELSE 0 END) AS new_accounts,
            SUM(CASE WHEN COALESCE(up.is_default_name, 0) = 1
                      OR COALESCE(up.is_default_avatar, 0) = 1
                 THEN 1 ELSE 0 END) AS suspicious_accounts
        FROM comments c
        JOIN comment_memberships m ON m.comment_id = c.id
        JOIN posts p ON p.id = m.post_id
        LEFT JOIN user_profiles up ON up.user_id = c.user_id
        WHERE p.symbol = ? AND c.created_at_str = ?
    """, (symbol, today)).fetchone()

    r = dict(row)
    total = r["total"] or 0
    if total < 5:
        return alerts

    new_ratio = (r["new_accounts"] or 0) / total
    suspicious_ratio = (r["suspicious_accounts"] or 0) / total

    if new_ratio >= 0.4:
        severity = "high" if new_ratio >= 0.6 else "medium"
        alerts.append(Alert(
            alert_type="new_account_influx",
            symbol=symbol,
            severity=severity,
            title=f"新账号涌入（{new_ratio:.0%}）",
            detail=f"今日{total}条评论，新账号{r['new_accounts']}条({new_ratio:.0%})，"
                   f"可疑账号(默认名/头像){r['suspicious_accounts']}条({suspicious_ratio:.0%})",
            suggestion="疑似水军活动，关注评论内容是否模板化",
        ))

    return alerts


def detect_manipulation_risk(symbol: str, conn: sqlite3.Connection) -> List[Alert]:
    """6. 操纵风险：LLM 标注的 template/coordinated 占比 >20%"""
    alerts = []

    today = datetime.now().strftime("%Y-%m-%d")

    row = conn.execute("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN manipulation_flag = 'template' THEN 1 ELSE 0 END) AS template_count,
            SUM(CASE WHEN manipulation_flag = 'coordinated' THEN 1 ELSE 0 END) AS coordinated_count
        FROM llm_annotations
        WHERE symbol = ? AND annotated_at >= ?
    """, (symbol, today)).fetchone()

    r = dict(row)
    total = r["total"] or 0
    if total < 5:
        return alerts

    risk_count = (r["template_count"] or 0) + (r["coordinated_count"] or 0)
    risk_ratio = risk_count / total

    if risk_ratio >= 0.2:
        severity = "high" if risk_ratio >= 0.4 else "medium"
        alerts.append(Alert(
            alert_type="manipulation_risk",
            symbol=symbol,
            severity=severity,
            title=f"操纵风险偏高（{risk_ratio:.0%}）",
            detail=f"今日{total}条标注，模板化{r['template_count']}条，"
                   f"协调行为{r['coordinated_count']}条",
            suggestion="检查相关评论内容，确认是否存在水军刷评",
        ))

    return alerts


def detect_kol_activity(symbol: str, conn: sqlite3.Connection) -> List[Alert]:
    """7. KOL 动态：粉丝 >10000 的用户今日发帖"""
    alerts = []

    today = datetime.now().strftime("%Y-%m-%d")

    rows = conn.execute("""
        SELECT p.user_name, p.text_plain, p.like_count,
               up.followers_count, p.created_at_str
        FROM posts p
        LEFT JOIN user_profiles up ON up.user_id = p.user_id
        WHERE p.symbol = ?
          AND p.created_at_str = ?
          AND COALESCE(up.followers_count, 0) >= 10000
        ORDER BY p.like_count DESC
    """, (symbol, today)).fetchall()

    for row in rows:
        r = dict(row)
        content_preview = (r["text_plain"] or "")[:100]
        alerts.append(Alert(
            alert_type="kol_activity",
            symbol=symbol,
            severity="low",
            title=f"KOL 发声: {r['user_name']}（{r['followers_count']}粉丝）",
            detail=f"内容: {content_preview}... | +{r['like_count']}赞",
            suggestion="关注 KOL 观点方向和后续市场反应",
        ))

    return alerts


def detect_narrative_drift(symbol: str, conn: sqlite3.Connection, baseline: dict) -> List[Alert]:
    """8. 叙事漂移：今日叙事主题与近期的显著不同"""
    alerts = []

    today = datetime.now().strftime("%Y-%m-%d")

    # 今日主题
    today_row = conn.execute("""
        SELECT narrative_themes FROM llm_batch_summaries
        WHERE symbol = ? AND summary_date = ?
    """, (symbol, today)).fetchone()

    # 近期主题（过去7天）
    recent_row = conn.execute("""
        SELECT narrative_themes FROM llm_batch_summaries
        WHERE symbol = ? AND summary_date >= date('now', '-7 days')
          AND summary_date < ?
        ORDER BY summary_date DESC LIMIT 5
    """, (symbol, today)).fetchall()

    if not today_row or not recent_row:
        return alerts

    import json
    try:
        today_themes = set(json.loads(today_row["narrative_themes"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        return alerts

    recent_themes = set()
    for r in recent_row:
        try:
            recent_themes.update(json.loads(dict(r)["narrative_themes"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

    if not today_themes or not recent_themes:
        return alerts

    # 新出现的主题（今日有但近期没有的）
    new_themes = today_themes - recent_themes
    # 消失的主题
    gone_themes = recent_themes - today_themes

    if new_themes and len(new_themes) >= 2:
        alerts.append(Alert(
            alert_type="narrative_drift",
            symbol=symbol,
            severity="medium",
            title=f"叙事主题漂移",
            detail=f"新主题: {', '.join(list(new_themes)[:5])} | "
                   f"消失主题: {', '.join(list(gone_themes)[:5]) if gone_themes else '无'}",
            suggestion="讨论焦点转移，可能反映新的市场逻辑",
        ))

    return alerts


def detect_volume_price_divergence(symbol: str, today: dict, baseline: dict) -> List[Alert]:
    """9. 量价背离：讨论热度暴增但无价格数据，或热度暴降但价格稳定"""
    alerts = []

    # 简化版：只检测讨论量异常但缺少K线数据
    heat = today.get("heat_score", 0)
    avg_heat = baseline.get("avg_heat_score", 0)

    if avg_heat > 0 and heat >= avg_heat * 3:
        # 讨论量暴增，提醒检查价格走势
        kline_dir = os.path.join(PROJECT_ROOT, "data", "kline", symbol)
        has_kline = os.path.exists(os.path.join(kline_dir, "daily.parquet"))

        if not has_kline:
            alerts.append(Alert(
                alert_type="volume_price_divergence",
                symbol=symbol,
                severity="low",
                title="讨论热度暴增但无K线数据",
                detail=f"今日热度 {heat}，基准线 {avg_heat:.0f}，无K线数据可对比",
                suggestion="建议下载K线数据以完善量价分析",
            ))

    return alerts


# ============================================================
# 统一调度
# ============================================================

def run_all_detections(conn: sqlite3.Connection, symbol: str,
                       today: dict, baseline: dict) -> List[Alert]:
    """运行所有 9 种检测，返回告警列表"""
    all_alerts = []

    # 不需要额外数据库查询的检测
    all_alerts.extend(detect_volume_spike(symbol, today, baseline))
    all_alerts.extend(detect_volume_drop(symbol, today, baseline))
    all_alerts.extend(detect_sentiment_extreme(symbol, today))
    all_alerts.extend(detect_sentiment_shift(symbol, today, baseline))
    all_alerts.extend(detect_volume_price_divergence(symbol, today, baseline))

    # 需要额外数据库查询的检测
    all_alerts.extend(detect_new_account_influx(symbol, conn))
    all_alerts.extend(detect_manipulation_risk(symbol, conn))
    all_alerts.extend(detect_kol_activity(symbol, conn))
    all_alerts.extend(detect_narrative_drift(symbol, conn, baseline))

    return all_alerts

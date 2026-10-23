"""每账号滚动窗口积分预算：取生效值 + 统计窗口内已用积分。

上游按「账号 × 5 小时窗口」的使用量限流（错误码 6000–6008），窗口起点是该号
窗口内的第一笔请求 —— 逐号不同、随用随滚，所以「按钟点清零」的定点闸门永远
对不齐。这里改用滚动窗口的积分和做本地预算：选号前算一遍，达预算就跳过该号。

取值的优先级刻意与 `wb_paths` **相反**（那边是环境变量 > 后台保存值）：

    后台保存值 > 环境变量 > 关闭

因为这是一个**运行时调参**的旋钮：运维用环境变量给一个初始值（容器重建后不丢），
但后台要能随时改。若沿用「环境变量优先」，一旦在 stack 里写了值，界面就成了只读，
用户提的「方便手动调整」就不成立。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from admin.config import settings

_logger = logging.getLogger(__name__)

#: `system_settings` 里的键名（值是 JSON：{"budget": 210, "hours": 5}）。
KEY = "pool_window_budget"

#: 窗口长度的兜底值（小时）。
DEFAULT_HOURS = 5.0

#: 后台可设置的上限/下限，同时被路由层与 UI 使用。
MIN_BUDGET = 0.0
MAX_BUDGET = 100000.0
MIN_HOURS = 0.5
MAX_HOURS = 24.0


def load_saved(db) -> dict | None:
    """读取后台保存值；没有或损坏时返回 None（绝不抛）。"""
    from admin.models import SystemSetting

    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == KEY).first()
        if not row or not row.value:
            return None
        obj = json.loads(row.value)
        if not isinstance(obj, dict):
            return None
        budget = obj.get("budget")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            return None
        hours = obj.get("hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
            hours = DEFAULT_HOURS
        return {"budget": float(budget), "hours": float(hours)}
    except Exception as exc:  # pragma: no cover - 只在库里数据坏了时触发
        _logger.warning("读取窗口预算失败，按未设置处理：%s", exc)
        return None


def save(db, budget: float, hours: float) -> None:
    """写入后台保存值（调用方负责校验范围）。"""
    from admin.models import SystemSetting

    value = json.dumps({"budget": float(budget), "hours": float(hours)}, ensure_ascii=False)
    row = db.query(SystemSetting).filter(SystemSetting.key == KEY).first()
    if row is None:
        db.add(SystemSetting(key=KEY, value=value))
    else:
        row.value = value
    db.commit()


def clear(db) -> None:
    """删掉后台保存值，回落到环境变量（界面上的「恢复默认」）。"""
    from admin.models import SystemSetting

    db.query(SystemSetting).filter(SystemSetting.key == KEY).delete()
    db.commit()


def effective(db) -> tuple[float, float, str]:
    """返回 (预算, 窗口小时, 来源)。来源取值：saved | env | off。"""
    saved = load_saved(db)
    if saved is not None:
        return saved["budget"], saved["hours"], "saved"
    budget = float(getattr(settings, "WINDOW_CREDIT_BUDGET", 0.0) or 0.0)
    hours = float(getattr(settings, "WINDOW_CREDIT_HOURS", DEFAULT_HOURS) or DEFAULT_HOURS)
    if budget > 0:
        return budget, hours, "env"
    return 0.0, hours, "off"


def spend(db, ids: list[int], now: datetime, hours: float) -> dict[int, float]:
    """最近 `hours` 小时内每个账号已消耗的积分（无记录 = 0）。

    数据源是 usage_logs 本身（`ix_usage_logs_account_created` 覆盖），所以窗口
    边界天然跟随上游的滚动语义，不需要知道窗口起点在哪。
    """
    from sqlalchemy import func

    from admin.models import UsageLog

    if not ids:
        return {}
    since = now - timedelta(hours=hours)
    rows = (db.query(UsageLog.account_id, func.sum(UsageLog.credits))
            .filter(UsageLog.account_id.in_(ids), UsageLog.created_at >= since)
            .group_by(UsageLog.account_id).all())
    return {aid: float(total or 0) for aid, total in rows}

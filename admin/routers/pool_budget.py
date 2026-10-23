"""号池窗口预算：后台读写接口（对应「设置 → 号池窗口预算」卡片）。

只做配置与观测，不发任何上游请求：读的是 `system_settings` 与 `usage_logs`，
所以面板可以随便轮询。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin import credit_budget
from admin.db import get_db
from admin.models import Account
from admin.security import require_admin

router = APIRouter(prefix="/api/pool/window-budget", tags=["pool"])


class BudgetIn(BaseModel):
    budget: float = Field(ge=credit_budget.MIN_BUDGET, le=credit_budget.MAX_BUDGET)
    hours: float = Field(ge=credit_budget.MIN_HOURS, le=credit_budget.MAX_HOURS)


def _payload(db: Session) -> dict:
    budget, hours, source = credit_budget.effective(db)
    now = datetime.utcnow()
    accounts = (db.query(Account).filter(Account.status == "active")
                .order_by(Account.id).all())
    spent = credit_budget.spend(db, [a.id for a in accounts], now, hours)
    items = []
    for acc in accounts:
        used = round(spent.get(acc.id, 0.0), 2)
        items.append({
            "id": acc.id,
            "name": acc.name or acc.uid or f"#{acc.id}",
            "used": used,
            "budget": round(budget, 2) if budget > 0 else None,
            "percent": round(min(100.0, used / budget * 100.0), 1) if budget > 0 else None,
            "over": bool(budget > 0 and used >= budget),
        })
    return {
        "budget": round(budget, 2),
        "hours": round(hours, 2),
        "source": source,
        "enabled": budget > 0,
        "env": {
            "budget": float(getattr(credit_budget.settings, "WINDOW_CREDIT_BUDGET", 0.0) or 0.0),
            "hours": float(getattr(credit_budget.settings, "WINDOW_CREDIT_HOURS", credit_budget.DEFAULT_HOURS)),
        },
        "limits": {
            "min_budget": credit_budget.MIN_BUDGET,
            "max_budget": credit_budget.MAX_BUDGET,
            "min_hours": credit_budget.MIN_HOURS,
            "max_hours": credit_budget.MAX_HOURS,
        },
        "accounts": items,
    }


@router.get("")
def read_budget(_: Annotated[bool, Depends(require_admin)],
                db: Annotated[Session, Depends(get_db)]):
    return _payload(db)


@router.put("")
def write_budget(body: BudgetIn, _: Annotated[bool, Depends(require_admin)],
                 db: Annotated[Session, Depends(get_db)]):
    if not (credit_budget.MIN_BUDGET <= body.budget <= credit_budget.MAX_BUDGET):
        raise HTTPException(400, "预算超出允许范围")
    if not (credit_budget.MIN_HOURS <= body.hours <= credit_budget.MAX_HOURS):
        raise HTTPException(400, "窗口长度超出允许范围（0.5–24 小时）")
    credit_budget.save(db, body.budget, body.hours)
    return _payload(db)


@router.delete("")
def reset_budget(_: Annotated[bool, Depends(require_admin)],
                 db: Annotated[Session, Depends(get_db)]):
    """恢复默认：删掉后台保存值，回落到环境变量（没有就是关闭）。"""
    credit_budget.clear(db)
    return _payload(db)

"""安全：管理后台 JWT 登录 + API Key 校验与配额判断。"""
import base64
import hashlib
import hmac
import os
from datetime import datetime, timedelta

import jwt
from fastapi import Header, HTTPException
from sqlalchemy import func, or_

from admin.config import settings
from admin.db import SessionLocal
from admin.models import ApiKey


# ---------------------------------------------------------------------------
# 管理后台登录
# ---------------------------------------------------------------------------

def create_admin_token() -> str:
    exp = datetime.utcnow() + timedelta(hours=settings.JWT_EXPIRE_HOURS)
    return jwt.encode({"sub": settings.ADMIN_USERNAME, "exp": exp},
                      settings.JWT_SECRET, algorithm="HS256")


def verify_admin_token(token: str | None):
    if not token:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
        local_runtime = os.getenv("WORKBUDDY_DESKTOP") == "1" or os.getenv("WORKBUDDY_ANDROID") == "1"
        if "exp" not in payload and not (local_runtime and payload.get("local_shell") is True):
            raise HTTPException(status_code=401, detail="无效令牌")
        if payload.get("sub") != settings.ADMIN_USERNAME:
            raise HTTPException(status_code=401, detail="无效令牌")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="无效令牌")


def require_admin(x_admin_token: str = Header(default=None, alias="X-Admin-Token")):
    """FastAPI 依赖：校验管理后台令牌。"""
    verify_admin_token(x_admin_token)
    return True


# ---------------------------------------------------------------------------
# 管理后台密码（PBKDF2 哈希存储，杜绝明文泄漏）
# ---------------------------------------------------------------------------

_PBKDF2_ITER = 100_000


def hash_password(pw: str) -> str:
    """返回可持久化的哈希串：pbkdf2$<iter>$<salt_b64>$<dk_b64>。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITER)
    return "pbkdf2$" + str(_PBKDF2_ITER) + "$" + \
        base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    """校验密码；兼容旧明文存储（首次读取时由 server 统一迁移为哈希）。"""
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        return hmac.compare_digest(pw, stored)  # 旧明文兜底
    try:
        _, iter_s, salt_b64, hash_b64 = stored.split("$", 3)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, int(iter_s))
        return hmac.compare_digest(cand, expected)
    except Exception:
        return False


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def get_key_row(db, api_key: str) -> ApiKey | None:
    return db.query(ApiKey).filter(ApiKey.key_hash == hash_key(api_key)).first()


def check_quota(key: ApiKey) -> None:
    """超额则直接拒绝，提示『积分已耗尽』。"""
    if key.status != "active":
        raise HTTPException(status_code=403, detail={"error": {"message": "API Key 已停用", "type": "key_disabled"}})
    if not key.unlimited and key.credit_used >= key.credit_limit:
        raise HTTPException(
            status_code=402,
            detail={"error": {"message": "当前 API Key 的额度已用完，请调整此 Key 的限额或联系管理员", "type": "quota_exceeded", "code": "key_quota_exceeded", "source": "gateway"}},
        )


_QUOTA_EXCEEDED_DETAIL = {"error": {"message": "当前 API Key 的额度已用完，请调整此 Key 的限额或联系管理员", "type": "quota_exceeded", "code": "key_quota_exceeded", "source": "gateway"}}


def reserve_quota(db, key: ApiKey, estimate: float) -> bool:
    """原子预占额度（审计 H4）：并发请求不能共同越过硬上限。

    原 check_quota 是「请求前读取、完成后累加」，两个并发请求都读到旧值
    就能双双入场。这里改用单条条件 UPDATE 完成「检查+预占」，在 SQLite 与
    MySQL 上都是原子的：estimate 超出剩余额度的请求 rowcount==0，直接拒绝。
    预占在请求收尾统一释放（_release_quota），最终 credit_used 仍等于真实积分。
    """
    if key.unlimited or estimate <= 0:
        return True
    est = int(estimate) if estimate >= 1 else estimate
    n = db.query(ApiKey).filter(
        ApiKey.id == key.id,
        or_(ApiKey.unlimited == 1,
            func.coalesce(ApiKey.credit_used, 0) + est <= ApiKey.credit_limit),
    ).update({ApiKey.credit_used: func.coalesce(ApiKey.credit_used, 0) + est},
             synchronize_session=False)
    db.commit()
    return n > 0


class GatewayRejected(Exception):
    """A local rejection, never an upstream account failure or retry signal."""
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail.get('error', {}).get('message', '网关拒绝请求'))

    def payload(self):
        return {'error': {**self.detail['error'], 'source': 'gateway', 'http_status': self.status_code}}


def recheck_key(db, key_id):
    key = db.get(ApiKey, int(key_id), populate_existing=True)
    if key is None:
        raise GatewayRejected(401, {'error': {'message': 'API Key 已删除或无效',
                                            'type': 'invalid_api_key', 'source': 'gateway'}})
    try:
        check_quota(key)
    except HTTPException as exc:
        raise GatewayRejected(exc.status_code, {'error': {**exc.detail['error'], 'source': 'gateway'}}) from None
    return key


def check_key_current(key_id):
    # Use a short, fresh transaction even when the caller is waiting in a long
    # request (MySQL repeatable-read must not retain an earlier key status).
    from admin.db import SessionLocal as factory
    with factory() as db:
        return recheck_key(db, key_id)

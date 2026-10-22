"""Reject registration bursts before synchronous routes consume worker threads."""
import threading

from starlette.responses import JSONResponse


_JOIN_PATHS = {
    '/api/join/start',
    '/api/join/send',
    '/api/join/verify',
    '/api/join/official/start',
    '/api/join/official/poll',
}


class JoinAdmissionMiddleware:
    def __init__(self, app, max_active=4):
        self.app = app
        self.max_active = max(1, int(max_active))
        self._lock = threading.Lock()
        self._active = 0

    async def __call__(self, scope, receive, send):
        if (scope.get('type') != 'http' or scope.get('method', '').upper() != 'POST'
                or scope.get('path') not in _JOIN_PATHS):
            return await self.app(scope, receive, send)

        with self._lock:
            busy = self._active >= self.max_active
            if not busy:
                self._active += 1
        if busy:
            body = ({'done': False, 'busy': True} if scope.get('path') == '/api/join/official/poll'
                    else {'detail': {'message': '注册请求较多，请稍后重试',
                                     'step': 'busy', 'reason': 'join_capacity'}})
            response = JSONResponse(
                body,
                status_code=200 if scope.get('path') == '/api/join/official/poll' else 503,
                headers={'Retry-After': '3'})
            return await response(scope, receive, send)
        try:
            return await self.app(scope, receive, send)
        finally:
            with self._lock:
                self._active = max(0, self._active - 1)

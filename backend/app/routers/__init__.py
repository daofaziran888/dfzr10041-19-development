"""业务路由汇总：质量补采专用路由在前，通用资源动态路由在后。

resource 里的 /api/{module} 是动态路径，quality 的固定路径必须先注册，
否则 /api/quality/... 会被动态路由抢先匹配。
"""
from __future__ import annotations

from app.routers import quality, resource

ROUTERS = [quality, resource]

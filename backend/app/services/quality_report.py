"""质量报告领域服务：质报汇总后接续调用补采/续采闭环。

动作流转保持通用骨架，领域细节（失败明细、签审、派单）由
app.core.quality 与 /api/quality 路由承载。
"""
from __future__ import annotations

from app.services.base import BaseModuleService


class QualityReportService(BaseModuleService):
    MODULE = "quality_report"
    ENTITY = "质量评估"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["报告编号", "日志来源", "完整率", "解析成功率", "字段覆盖率", "时效延迟", "评估周期", "报告状态"]
    REQUIRED_FIELDS = ["报告编号", "日志来源", "完整率"]
    STATUS_ORDER = ["待生成", "已生成", "需复核", "已发布"]
    # 生成报告后即可在质报入口接续「发起补采」（进入 /api/quality 闭环）。
    ACTION_RULES = {"生成报告": "已生成", "发起补采": "需复核", "发布报告": "已发布"}
    NEGATIVE_ACTIONS = []

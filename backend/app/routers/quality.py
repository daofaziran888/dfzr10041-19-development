"""质量补采路由：解析失败明细、签审、派单领用、规则试算与断点恢复。

与通用资源路由并列注册，路径全部落在 /api/quality 下，
承接 quality_report 汇总入口后的续采/补采动作。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.quality import (
    ACCESS_RULE_V1, ACCESS_RULE_V2, QualityConflict, QualityValidation,
    RuleSpec, build_demo_case, registry,
)

router = APIRouter(prefix="/api/quality", tags=["质量补采"])


class ReviewPayload(BaseModel):
    finding_ids: list[str] = Field(default_factory=list)
    reviewer: str = "值班员"
    reason: str = ""


class RetrialPayload(BaseModel):
    rule_version: int = 2
    note: str = ""


class DispatchPayload(BaseModel):
    job_id: str
    review_id: str | None = None


class AssignmentIdPayload(BaseModel):
    assignment_id: str


class CustomCasePayload(BaseModel):
    case_id: str
    source_id: str = "LOG_-0001"
    source_name: str = "WebAccessLog"
    file_path: str = "/var/log/nginx/access.log"
    raw_lines: list[str]


@router.get("/cases")
def list_cases() -> dict[str, Any]:
    """质报汇总入口：列出补办案情并接续续采指引。"""
    cases = registry.list_cases()
    return {"ok": True, "items": cases, "total": len(cases)}


@router.post("/cases")
def create_case(payload: CustomCasePayload) -> dict[str, Any]:
    """按任意来源文件创建补办案情（初始 v1 规则试算）。"""
    try:
        case = registry.create_case(
            case_id=payload.case_id,
            source_id=payload.source_id,
            source_name=payload.source_name,
            file_path=payload.file_path,
            raw_lines=payload.raw_lines,
            rule=ACCESS_RULE_V1,
        )
    except QualityValidation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "补办案情已建立", "case": case.summary()}


@router.get("/cases/{case_id}")
def get_case(case_id: str) -> dict[str, Any]:
    """案情全量视图：各版本失败明细（可展开来源/物理位置）、签审、派单、交接。"""
    try:
        return {"ok": True, "case": registry.get_case(case_id).to_dict()}
    except QualityValidation as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/cases/{case_id}/failures")
def get_failures(case_id: str, version: int | None = None) -> dict[str, Any]:
    """解析失败明细：按来源、文件、物理行和失败原因展开。"""
    try:
        case = registry.get_case(case_id)
    except QualityValidation as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    target = next((v for v in case.versions if v.version == version),
                  case.current_version)
    grouped: dict[str, Any] = {}
    for finding in target.findings:
        source_key = f"{finding['source_name']}｜{finding['file_path']}"
        grouped.setdefault(source_key, []).append(finding)
    return {
        "ok": True,
        "case_id": case_id,
        "version": target.version,
        "rule_version": target.rule.version,
        "coverage": target.coverage,
        "groups": [
            {
                "source_key": key,
                "source_id": items[0]["source_id"],
                "source_name": items[0]["source_name"],
                "file_path": items[0]["file_path"],
                "findings": items,
            }
            for key, items in grouped.items()
        ],
    }


@router.post("/cases/{case_id}/reviews")
def create_review(case_id: str, payload: ReviewPayload) -> dict[str, Any]:
    """补采签审：只受理明确的缺字段组，格式不符/空行拒绝签审。"""
    try:
        review = registry.review_failures(
            case_id, payload.finding_ids, payload.reviewer, payload.reason)
    except QualityValidation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "签审通过，补采作业可凭本单领取待处理行",
            "review": review.to_dict()}


@router.post("/cases/{case_id}/retrial")
def run_retrial(case_id: str, payload: RetrialPayload) -> dict[str, Any]:
    """规则更正后重新试算：生成新版本，不覆盖旧签审与旧派单范围。"""
    if payload.rule_version <= 1:
        raise HTTPException(status_code=400,
                            detail="规则更正只能基于更高的新版本试算")
    new_rule = RuleSpec(
        rule_id=ACCESS_RULE_V2.rule_id,
        version=payload.rule_version,
        pattern=ACCESS_RULE_V2.pattern,
        required_fields=list(ACCESS_RULE_V2.required_fields),
        note=ACCESS_RULE_V2.note,
    )
    try:
        version = registry.retrial(case_id, new_rule, note=payload.note)
    except QualityValidation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "规则更正已生成新版本试算，旧签审范围保持不变",
            "version": version.to_dict()}


@router.post("/cases/{case_id}/dispatch")
def dispatch(case_id: str, payload: DispatchPayload) -> dict[str, Any]:
    """补采作业领用：同一物理位置只可出单一次，冲突返回 409。"""
    try:
        assignment = registry.dispatch(case_id, payload.job_id, payload.review_id)
    except QualityConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QualityValidation as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "message": "补采单已派出，物理行领用成功",
            "assignment": assignment.to_dict()}


@router.post("/cases/{case_id}/receipts")
def record_receipt(case_id: str, payload: AssignmentIdPayload) -> dict[str, Any]:
    """质报端落派单回执。"""
    try:
        assignment = registry.record_receipt(case_id, payload.assignment_id)
    except QualityValidation as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "message": "派单回执已登记",
            "assignment": assignment.to_dict()}


@router.get("/cases/{case_id}/reconcile")
def reconcile(case_id: str, job_id: str | None = None) -> dict[str, Any]:
    """断点恢复核对：返回实际已领取工作，重开后据此避免重派同一行。"""
    try:
        return {"ok": True, "reconcile": registry.reconcile(case_id, job_id)}
    except QualityValidation as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/cases/{case_id}/handoff")
def build_handoff(case_id: str) -> dict[str, Any]:
    """交接记录：更正过的行何时可处理、其余行怎么补采、旧失败明细保留位置。"""
    try:
        handoff = registry.build_handoff(case_id)
    except QualityValidation as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "handoff": handoff}


@router.get("/cases/{case_id}/continue")
def continue_collection(case_id: str) -> dict[str, Any]:
    """质报汇总入口接续调用续采：给出下一步动作与对应物理行。"""
    try:
        return {"ok": True, "continuation": registry.continue_collection(case_id)}
    except QualityValidation as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/snapshot")
def get_snapshot() -> dict[str, Any]:
    """台账快照：用于服务重开后的恢复核对。"""
    return {"ok": True, "snapshot": registry.snapshot()}


@router.post("/demo")
def reset_demo() -> dict[str, Any]:
    """重建演示案情（测试/演示用）。"""
    registry.reset()
    case = build_demo_case(registry)
    return {"ok": True, "message": "演示案情已重置",
            "case": case.summary()}

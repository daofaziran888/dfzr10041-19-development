"""测试：质量补采路由层（HTTP 语义、状态码与请求体校验）。

环境未安装 httpx，无法用 TestClient；路由端点本身是同步普通函数，
这里直接构造 pydantic 请求体调用，验证 400/404/409 的映射。
确定性测试，不依赖 pytest，直接 python tests/test_quality_api.py 运行。
"""
from __future__ import annotations

import os
import sys
import traceback

from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers.quality import (  # noqa: E402
    DispatchPayload, ReviewPayload, reset_demo, list_cases, get_failures,
    get_case, create_review, dispatch, record_receipt, run_retrial,
    RetrialPayload, build_handoff, reconcile, continue_collection,
    AssignmentIdPayload,
)

CASE_ID = "CASE-DEMO-01"


def assert_http(status: int, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except HTTPException as exc:
        assert exc.status_code == status, (
            f"期望 HTTP {status}，实际 {exc.status_code}：{exc.detail}"
        )
        return exc
    raise AssertionError(f"期望抛出 HTTP {status}，但未抛出")


def test_router_full_lifecycle():
    reset_demo()

    # 列表与失败明细
    listed = list_cases()
    assert listed["total"] >= 1
    failures = get_failures(CASE_ID)
    assert failures["version"] == 1
    assert len(failures["groups"]) == 1
    lines = {f["physical_line"]: f for f in failures["groups"][0]["findings"]}
    assert lines[1]["failure_type"] == "line_empty"
    assert lines[2]["failure_type"] == "format_mismatch"
    # 覆盖率分母与字段差异
    cov = failures["coverage"]
    assert cov["denominator_lines"] == 3
    assert cov["per_field"]["method"]["rate"] == 1.0
    assert cov["per_field"]["request_id"]["rate"] == round(1 / 3, 4)

    # 格式不符签审 → 400
    exc = assert_http(400, create_review, CASE_ID,
                      ReviewPayload(finding_ids=["F1-L2"], reviewer="值班员"))
    assert "格式不符" in exc.detail

    # 空清单签审 → 400
    assert_http(400, create_review, CASE_ID,
                ReviewPayload(finding_ids=[], reviewer="值班员"))

    # 正常签审
    review_resp = create_review(
        CASE_ID,
        ReviewPayload(finding_ids=["F1-L3", "F1-L4"], reviewer="值班员",
                      reason="补 request_id"),
    )
    assert review_resp["ok"] is True
    review_id = review_resp["review"]["review_id"]
    assert len(review_resp["review"]["snapshot"]) == 5

    # 重复签审 → 400
    assert_http(400, create_review, CASE_ID,
                ReviewPayload(finding_ids=["F1-L3", "F1-L4"], reviewer="值班员"))

    # JOB-A 派单成功
    asg_resp = dispatch(CASE_ID, DispatchPayload(job_id="JOB-A", review_id=review_id))
    assignment_id = asg_resp["assignment"]["assignment_id"]
    assert asg_resp["assignment"]["receipt_state"] == "pending"

    # JOB-B 抢同一物理行 → 409
    exc = assert_http(409, dispatch, CASE_ID,
                      DispatchPayload(job_id="JOB-B", review_id=review_id))
    assert "只可出单一次" in exc.detail

    # 不存在的签审单 → 400
    assert_http(400, dispatch, CASE_ID,
                DispatchPayload(job_id="JOB-C", review_id="REV-NOPE"))

    # 同作业重复派单幂等
    again = dispatch(CASE_ID, DispatchPayload(job_id="JOB-A", review_id=review_id))
    assert again["assignment"]["assignment_id"] == assignment_id

    # 落回执
    receipt = record_receipt(CASE_ID, AssignmentIdPayload(assignment_id=assignment_id))
    assert receipt["assignment"]["receipt_state"] == "received"

    # 不存在的案情 → 404
    assert_http(404, get_case, "CASE-MISSING")
    assert_http(404, get_failures, "CASE-MISSING")

    # 规则更正试算：新版本不覆盖旧明细
    v2 = run_retrial(CASE_ID, RetrialPayload(rule_version=2, note="规则更正"))
    assert v2["version"]["version"] == 2
    statuses = {f["physical_line"]: f["failure_type"]
                for f in v2["version"]["findings"]}
    assert statuses[3] == "ok" and statuses[4] == "ok"
    # 同版本重复试算 → 400
    assert_http(400, run_retrial, CASE_ID, RetrialPayload(rule_version=2))
    # 倒退版本 → 400
    assert_http(400, run_retrial, CASE_ID, RetrialPayload(rule_version=1))

    # 旧签审与已派范围保持
    detail = get_case(CASE_ID)["case"]
    assert detail["reviews"][0]["rule_version"] == 1
    assert detail["assignments"][0]["rule_version_at_claim"] == 1
    assert len(detail["assignments"][0]["claim_keys"]) == 2

    # 交接与恢复、续采
    handoff = build_handoff(CASE_ID)["handoff"]
    assert len(handoff["lines"]) == 2
    assert all(line["now_parseable"] for line in handoff["lines"])
    rec = reconcile(CASE_ID)["reconcile"]
    assert len(rec["claimed_locations"]) == 2
    cont = continue_collection(CASE_ID)["continuation"]
    assert cont["current_rule_version"] == 2

    # 恢复后另一作业仍不能重派 → 409
    assert_http(409, dispatch, CASE_ID,
                DispatchPayload(job_id="JOB-D", review_id=review_id))


TESTS = {
    "router_full_lifecycle": test_router_full_lifecycle,
}


if __name__ == "__main__":
    passed = failed = 0
    for name, func in TESTS.items():
        try:
            func()
            passed += 1
            print(f"  PASS  test_quality_api: {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  test_quality_api: {name}")
            traceback.print_exc()
    print(f"\ntest_quality_api: {passed} passed, {failed} failed, {len(TESTS)} total")
    sys.exit(1 if failed else 0)

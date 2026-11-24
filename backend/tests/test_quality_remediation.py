"""测试：质量报告解析失败明细 → 签审 → 派单领用 → 断点恢复 → 规则更正/交接。

对应需求场景：
- 五行采集（空行 / 格式不符 / 两条同文本缺请求编号 / 一条正常）逐行定位；
- 覆盖率按必需字段分别核对实际解析行，method 与 request_id 覆盖不同；
- 签审只受理明确的缺字段组，格式不符不得伪装成空字段；
- 并发领用同一物理位置只可出单一次；
- 中断重开后按实际领用台账恢复，不重派、不被新试算覆盖旧签审；
- 规则更正产生新版本，交接记录说清更正行何时可处理、旧明细如何保留。

确定性测试，不依赖 pytest，直接 python tests/test_quality_remediation.py 运行。
"""
from __future__ import annotations

import os
import sys
import threading
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.quality import (  # noqa: E402
    ACCESS_REQUIRED_FIELDS, ACCESS_RULE_V1, ACCESS_RULE_V2, DEMO_RAW_LINES,
    FAILURE_TYPES, QualityConflict, QualityRemediationRegistry,
    QualityValidation, RuleSpec, build_demo_case, compute_field_coverage,
    parse_lines,
)


def fresh_registry() -> QualityRemediationRegistry:
    reg = QualityRemediationRegistry()
    build_demo_case(reg)
    return reg


def assert_raises(exc_type, func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"期望抛出 {exc_type.__name__}，但未抛出")


# ---------- 解析失败明细 ----------

def test_five_line_sample_classified():
    reg = fresh_registry()
    findings = reg.get_case("CASE-DEMO-01").current_version.findings
    assert len(findings) == 5
    by_line = {f["physical_line"]: f for f in findings}
    assert by_line[1]["failure_type"] == "line_empty"
    assert by_line[2]["failure_type"] == "format_mismatch"
    assert by_line[3]["failure_type"] == "field_missing"
    assert by_line[4]["failure_type"] == "field_missing"
    assert by_line[5]["failure_type"] == "ok"


def test_identical_lines_have_distinct_physical_locations():
    """两条文本相同的失败行必须分别定位到不同物理行。"""
    reg = fresh_registry()
    findings = reg.get_case("CASE-DEMO-01").current_version.findings
    missing = [f for f in findings if f["failure_type"] == "field_missing"]
    assert len(missing) == 2
    assert missing[0]["raw_text"] == missing[1]["raw_text"]
    assert {f["physical_line"] for f in missing} == {3, 4}
    assert {f["finding_id"] for f in missing} == {"F1-L3", "F1-L4"}


def test_field_missing_carries_explicit_missing_group():
    reg = fresh_registry()
    by_line = {
        f["physical_line"]: f
        for f in reg.get_case("CASE-DEMO-01").current_version.findings
    }
    assert by_line[3]["missing_fields"] == ["request_id"]
    assert by_line[3]["parsed_fields"]["method"] == "GET"
    assert "request_id" not in by_line[3]["parsed_fields"]


def test_summary_counter_three_categories():
    reg = fresh_registry()
    counter = reg.get_case("CASE-DEMO-01").summary()["failure_counter"]
    assert counter == {"line_empty": 1, "format_mismatch": 1,
                       "field_missing": 2, "ok": 1}


def test_failure_reasons_are_human_readable():
    reg = fresh_registry()
    by_line = {
        f["physical_line"]: f
        for f in reg.get_case("CASE-DEMO-01").current_version.findings
    }
    assert "空行" in by_line[1]["reason"]
    assert "格式不符" in by_line[2]["reason"]
    assert "request_id" in by_line[3]["reason"]
    assert FAILURE_TYPES["line_empty"] == "空行"


# ---------- 逐字段覆盖率 ----------

def test_denominator_excludes_empty_and_mismatch():
    reg = fresh_registry()
    coverage = reg.get_case("CASE-DEMO-01").current_version.coverage
    assert coverage["physical_total"] == 5
    assert coverage["denominator_lines"] == 3


def test_method_and_request_id_rates_differ():
    reg = fresh_registry()
    per_field = reg.get_case("CASE-DEMO-01").current_version.coverage["per_field"]
    # 三行解析行都有 method，但只有一行有 request_id
    assert per_field["method"] == {"filled": 3, "denominator": 3, "rate": 1.0}
    assert per_field["request_id"] == {"filled": 1, "denominator": 3,
                                       "rate": round(1 / 3, 4)}


def test_overall_rate_is_cell_based_not_success_based():
    """不能用“解析成功”替代字段齐全：整体率按字段格计算。"""
    findings = parse_lines(DEMO_RAW_LINES, ACCESS_RULE_V1)
    coverage = compute_field_coverage(findings, ACCESS_REQUIRED_FIELDS)
    # 3 行 × 9 字段 = 27 格，缺 2 个 request_id → 25/27
    assert coverage["overall_rate"] == round(25 / 27, 4)


def test_empty_input_is_safe():
    coverage = compute_field_coverage([], ACCESS_REQUIRED_FIELDS)
    assert coverage["denominator_lines"] == 0
    assert coverage["overall_rate"] == 0.0


# ---------- 签审 ----------

def _review_all_missing(reg, reviewer="值班员"):
    findings = reg.get_case("CASE-DEMO-01").current_version.findings
    ids = [f["finding_id"] for f in findings
           if f["failure_type"] == "field_missing"]
    return reg.review_failures("CASE-DEMO-01", ids, reviewer)


def test_review_only_field_missing_lines():
    reg = fresh_registry()
    findings = reg.get_case("CASE-DEMO-01").current_version.findings
    mismatch = next(f for f in findings if f["failure_type"] == "format_mismatch")
    exc = assert_raises(
        QualityValidation, reg.review_failures,
        "CASE-DEMO-01", [mismatch["finding_id"]], "值班员")
    assert "格式不符" in str(exc)


def test_empty_line_cannot_be_reviewed():
    reg = fresh_registry()
    empty = next(
        f for f in reg.get_case("CASE-DEMO-01").current_version.findings
        if f["failure_type"] == "line_empty"
    )
    assert_raises(QualityValidation, reg.review_failures,
                  "CASE-DEMO-01", [empty["finding_id"]], "值班员")


def test_empty_payload_rejected():
    reg = fresh_registry()
    assert_raises(QualityValidation, reg.review_failures,
                  "CASE-DEMO-01", [], "值班员")


def test_review_freezes_snapshot():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    assert review.rule_version == 1
    assert review.case_version == 1
    # 快照保留签审前实际看到的全部五行失败明细
    assert len(review.snapshot) == 5
    snapshot_types = {f["physical_line"]: f["failure_type"]
                      for f in review.snapshot}
    assert snapshot_types == {1: "line_empty", 2: "format_mismatch",
                              3: "field_missing", 4: "field_missing",
                              5: "ok"}
    assert {loc["physical_line"] for loc in review.locations} == {3, 4}
    assert all(loc["missing_fields"] == ["request_id"]
               for loc in review.locations)


def test_duplicate_review_rejected():
    reg = fresh_registry()
    _review_all_missing(reg)
    findings = reg.get_case("CASE-DEMO-01").current_version.findings
    ids = [f["finding_id"] for f in findings
           if f["failure_type"] == "field_missing"]
    exc = assert_raises(QualityValidation, reg.review_failures,
                        "CASE-DEMO-01", ids, "值班员")
    assert "已签审" in str(exc)


# ---------- 派单领用与并发 ----------

def test_dispatch_claims_each_physical_line_once():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    assignment = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    assert len(assignment.claim_keys) == 2
    assert assignment.rule_version_at_claim == 1


def test_second_job_conflicts_on_same_line():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    exc = assert_raises(QualityConflict, reg.dispatch,
                        "CASE-DEMO-01", "JOB-B", review.review_id)
    assert "JOB-A" in str(exc)
    assert "只可出单一次" in str(exc)


def test_same_job_redispatch_is_idempotent():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    first = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    second = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    assert first.assignment_id == second.assignment_id
    assert len(reg.get_case("CASE-DEMO-01").assignments) == 1


def test_concurrent_dispatch_only_one_wins():
    """并发领用：两张补采单不能同时领到同一物理行。"""
    reg = fresh_registry()
    _review_all_missing(reg)
    barrier = threading.Barrier(4)
    results: dict[str, object] = {}
    lock = threading.Lock()

    def worker(job_id: str):
        barrier.wait()
        try:
            assignment = reg.dispatch("CASE-DEMO-01", job_id)
            with lock:
                results[job_id] = assignment.assignment_id
        except (QualityConflict, QualityValidation) as exc:
            with lock:
                results[job_id] = exc

    threads = [threading.Thread(target=worker, args=(f"JOB-{i}",))
               for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [v for v in results.values() if isinstance(v, str)]
    conflicts = [v for v in results.values()
                 if isinstance(v, (QualityConflict, QualityValidation))]
    assert len(winners) == 1
    assert len(conflicts) == 3
    case = reg.get_case("CASE-DEMO-01")
    assert len(case.assignments) == 1
    assert len(reg.snapshot()["claims"]) == 2


def test_dispatch_without_review_rejected():
    reg = fresh_registry()
    assert_raises(QualityValidation, reg.dispatch, "CASE-DEMO-01", "JOB-A")


def test_receipt_lifecycle():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    assignment = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    assert assignment.receipt_state == "pending"
    updated = reg.record_receipt("CASE-DEMO-01", assignment.assignment_id)
    assert updated.receipt_state == "received"
    assert updated.receipt_at is not None


# ---------- 断点恢复 ----------

def test_restore_keeps_claims_and_blocks_redispatch():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    assignment = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    # 派单已收下、质报端尚未落回执时“中断”
    assert assignment.receipt_state == "pending"

    restarted = QualityRemediationRegistry.restore(reg.snapshot())
    reconcile = restarted.reconcile("CASE-DEMO-01")
    assert len(reconcile["claimed_locations"]) == 2
    assert {loc["physical_line"] for loc in reconcile["claimed_locations"]} == {3, 4}
    # 不能把同一行重派给另一个作业
    assert_raises(QualityConflict, restarted.dispatch,
                  "CASE-DEMO-01", "JOB-B", review.review_id)
    # 原作业仍可幂等拿回原单
    again = restarted.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    assert again.assignment_id == assignment.assignment_id
    assert again.receipt_state == "pending"


def test_retrial_after_restart_does_not_override_old_review():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)

    restarted = QualityRemediationRegistry.restore(reg.snapshot())
    new_version = restarted.retrial("CASE-DEMO-01", ACCESS_RULE_V2)
    assert new_version.version == 2

    case = restarted.get_case("CASE-DEMO-01")
    assert case.versions[0].version == 1
    old_missing = [f for f in case.versions[0].findings
                   if f["failure_type"] == "field_missing"]
    assert {f["physical_line"] for f in old_missing} == {3, 4}
    assert len(case.reviews) == 1
    assert case.reviews[0].finding_ids == review.finding_ids
    assignment = case.assignments[0]
    assert assignment.rule_version_at_claim == 1
    assert len(assignment.claim_keys) == 2


def test_same_rule_version_cannot_overwrite():
    reg = fresh_registry()
    reg.retrial("CASE-DEMO-01", ACCESS_RULE_V2)
    assert_raises(QualityValidation, reg.retrial, "CASE-DEMO-01", ACCESS_RULE_V2)


def test_review_follows_physical_location_across_versions():
    """签审状态跟随物理位置：新版本明细 ID 变了也不能重复签审同一行。"""
    reg = fresh_registry()
    # v1 签审物理行 L3、L4
    review = _review_all_missing(reg)
    assert len(review.locations) == 2
    # 规则更正产生 v2（两行在新口径下已可解析）
    reg.retrial("CASE-DEMO-01", ACCESS_RULE_V2)
    case = reg.get_case("CASE-DEMO-01")
    v2 = case.versions[1]
    # 新明细 ID 不同，但物理位置的签审状态仍保留
    v2_ids = {f["finding_id"] for f in v2.findings}
    assert {"F2-L3", "F2-L4"} <= v2_ids
    by_line = {f["physical_line"]: f for f in v2.findings}
    assert by_line[3]["review_state"] == "approved"
    assert by_line[4]["review_state"] == "approved"
    # 用新版本 ID 再签审同一物理行必须被拒绝
    exc = assert_raises(
        QualityValidation, reg.review_failures,
        "CASE-DEMO-01", ["F2-L3"], "值班员")
    assert "已签审" in str(exc)
    # 续采入口不再把这两行列为待签审
    cont = reg.continue_collection("CASE-DEMO-01")
    assert cont["next_actions"]["awaiting_review"] == []


# ---------- 规则更正与交接 ----------

def test_corrected_rule_parses_former_missing_lines():
    reg = fresh_registry()
    v2 = reg.retrial("CASE-DEMO-01", ACCESS_RULE_V2)
    statuses = {f["physical_line"]: f["failure_type"] for f in v2.findings}
    assert statuses[3] == "ok"
    assert statuses[4] == "ok"
    # 空行与格式不符依旧是失败明细，不会被规则更正抹掉
    assert statuses[1] == "line_empty"
    assert statuses[2] == "format_mismatch"


def test_now_parseable_flag_without_mutating_old_version():
    reg = fresh_registry()
    reg.retrial("CASE-DEMO-01", ACCESS_RULE_V2)
    case = reg.get_case("CASE-DEMO-01")
    old = {f["physical_line"]: f for f in case.versions[0].findings}
    assert old[3]["failure_type"] == "field_missing"
    assert old[3]["now_parseable"] is True
    new = {f["physical_line"]: f for f in case.versions[1].findings}
    assert new[3]["failure_type"] == "ok"


def test_handoff_explains_each_reviewed_line():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    reg.retrial("CASE-DEMO-01", ACCESS_RULE_V2)

    handoff = reg.build_handoff("CASE-DEMO-01")
    assert len(handoff["lines"]) == 2
    for line in handoff["lines"]:
        assert line["now_parseable"] is True
        assert line["job_id"] == "JOB-A"
        assert "续采" in line["instruction"]
        assert line["missing_fields_at_review"] == ["request_id"]
    assert handoff["reviewed_snapshot_kept"][0]["snapshot_size"] == 5
    assert "不得悄悄改写" in handoff["scope_guard"]


def test_handoff_for_line_still_unparseable():
    """构造“规则更正后仍有一行缺字段”的场景，交接需说明其补采方式。"""
    reg = QualityRemediationRegistry()
    reg.create_case(
        case_id="CASE-X", source_id="LOG_-0001", source_name="WebAccessLog",
        file_path="/var/log/nginx/x.log",
        raw_lines=[
            '10.0.0.1 - - [16/Nov/2026:09:00:01 +0800] "GET /a HTTP/1.1" 200 1 "-" "x"',
        ],
        rule=ACCESS_RULE_V1,
    )
    findings = reg.get_case("CASE-X").current_version.findings
    reg.review_failures("CASE-X", [findings[0]["finding_id"]], "值班员")
    reg.dispatch("CASE-X", "JOB-A")
    strict_v2 = RuleSpec(
        rule_id="PARS-ACCESS", version=2,
        pattern=ACCESS_RULE_V1.pattern,
        required_fields=list(ACCESS_REQUIRED_FIELDS),
        note="v2 仍要求 request_id",
    )
    reg.retrial("CASE-X", strict_v2)
    handoff = reg.build_handoff("CASE-X")
    line = handoff["lines"][0]
    assert line["now_parseable"] is False
    assert "缺字段组补采" in line["instruction"]
    assert line["job_id"] == "JOB-A"


def test_dispatched_scope_survives_further_retrials():
    reg = fresh_registry()
    review = _review_all_missing(reg)
    assignment = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    reg.retrial("CASE-DEMO-01", ACCESS_RULE_V2)
    v3 = RuleSpec(
        rule_id="PARS-ACCESS", version=3,
        pattern=ACCESS_RULE_V2.pattern,
        required_fields=list(ACCESS_RULE_V2.required_fields),
        note="v3 再次试算",
    )
    reg.retrial("CASE-DEMO-01", v3)
    case = reg.get_case("CASE-DEMO-01")
    assert case.assignments[0].claim_keys == assignment.claim_keys
    assert case.assignments[0].rule_version_at_claim == 1


# ---------- 质报入口接续续采 ----------

def test_continue_reports_next_actions():
    reg = fresh_registry()
    cont = reg.continue_collection("CASE-DEMO-01")
    assert set(cont["next_actions"]["awaiting_review"]) == {"F1-L3", "F1-L4"}
    assert cont["next_actions"]["ready_to_dispatch_reviews"] == []

    review = _review_all_missing(reg)
    cont2 = reg.continue_collection("CASE-DEMO-01")
    assert review.review_id in cont2["next_actions"]["ready_to_dispatch_reviews"]

    assignment = reg.dispatch("CASE-DEMO-01", "JOB-A", review.review_id)
    cont3 = reg.continue_collection("CASE-DEMO-01")
    assert assignment.assignment_id in cont3["next_actions"]["awaiting_receipt_assignments"]
    assert len(cont3["dispatched_locations"]) == 2


# ---------- 管道层联动 ----------

def test_pipeline_emits_empty_line_entry():
    from app.core.models import ParseResultStatus
    from app.core.pipeline import PRESET_LOG_SOURCES, make_pipeline

    pipeline = make_pipeline()
    source = PRESET_LOG_SOURCES["access-web"]
    entries = pipeline.parse_raw_lines(source, ["", "garbage"])
    assert len(entries) == 2
    assert entries[0].parse_status == ParseResultStatus.LINE_EMPTY
    assert entries[1].parse_status == ParseResultStatus.FORMAT_MISMATCH


def test_parse_rule_reports_missing_optional_group():
    from app.core.models import ParseResultStatus, ParseRule

    rule = ParseRule(
        rule_id="T", source_id="S",
        regex_pattern=r'^(\S+)(?:\s+(\S+))?$',
        field_names=["a", "b"],
    )
    status, fields = rule.parse("only-a")
    assert status == ParseResultStatus.FIELD_MISSING
    assert fields == {"a": "only-a", "b": None}
    status2, fields2 = rule.parse("a b")
    assert status2 == ParseResultStatus.OK
    assert fields2 == {"a": "a", "b": "b"}


TESTS = {
    "five_line_sample_classified": test_five_line_sample_classified,
    "identical_lines_distinct_locations": test_identical_lines_have_distinct_physical_locations,
    "field_missing_explicit_group": test_field_missing_carries_explicit_missing_group,
    "summary_three_categories": test_summary_counter_three_categories,
    "failure_reasons_readable": test_failure_reasons_are_human_readable,
    "coverage_denominator_excludes_bad_lines": test_denominator_excludes_empty_and_mismatch,
    "coverage_method_vs_request_id": test_method_and_request_id_rates_differ,
    "coverage_cell_based": test_overall_rate_is_cell_based_not_success_based,
    "coverage_empty_safe": test_empty_input_is_safe,
    "review_rejects_format_mismatch": test_review_only_field_missing_lines,
    "review_rejects_empty_line": test_empty_line_cannot_be_reviewed,
    "review_rejects_empty_payload": test_empty_payload_rejected,
    "review_freezes_snapshot": test_review_freezes_snapshot,
    "review_duplicate_rejected": test_duplicate_review_rejected,
    "dispatch_claims_lines": test_dispatch_claims_each_physical_line_once,
    "dispatch_conflict": test_second_job_conflicts_on_same_line,
    "dispatch_idempotent": test_same_job_redispatch_is_idempotent,
    "dispatch_concurrent_single_winner": test_concurrent_dispatch_only_one_wins,
    "dispatch_without_review_rejected": test_dispatch_without_review_rejected,
    "receipt_lifecycle": test_receipt_lifecycle,
    "restore_blocks_redispatch": test_restore_keeps_claims_and_blocks_redispatch,
    "retrial_after_restart_keeps_review": test_retrial_after_restart_does_not_override_old_review,
    "same_version_no_overwrite": test_same_rule_version_cannot_overwrite,
    "review_follows_physical_location": test_review_follows_physical_location_across_versions,
    "corrected_rule_parses_missing": test_corrected_rule_parses_former_missing_lines,
    "old_version_immutable": test_now_parseable_flag_without_mutating_old_version,
    "handoff_explains_lines": test_handoff_explains_each_reviewed_line,
    "handoff_still_unparseable": test_handoff_for_line_still_unparseable,
    "dispatched_scope_survives_retrials": test_dispatched_scope_survives_further_retrials,
    "continuation_next_actions": test_continue_reports_next_actions,
    "pipeline_empty_line_entry": test_pipeline_emits_empty_line_entry,
    "parse_rule_missing_group": test_parse_rule_reports_missing_optional_group,
}


if __name__ == "__main__":
    passed = 0
    failed = 0
    for name, func in TESTS.items():
        try:
            func()
            passed += 1
            print(f"  PASS  test_quality_remediation: {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  test_quality_remediation: {name}")
            traceback.print_exc()
    print(f"\ntest_quality_remediation: {passed} passed, {failed} failed, "
          f"{len(TESTS)} total")
    sys.exit(1 if failed else 0)

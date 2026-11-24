"""质量补采领域：解析失败明细 → 补采签审 → 派单领用 → 断点恢复 → 规则试算/交接。

设计要点（对应质报补采闭环的硬性约束）：

1. 失败明细按「来源 + 文件 + 物理行号」定位，逐行区分
   空行 / 格式不符 / 字段缺失，文本相同的两行各自拥有独立物理行。
2. 字段覆盖率按每个必需字段分别核对“实际被正则解析的行”，
   分母不含空行与格式不符行；method 有值而 request_id 缺失时，
   两个字段的覆盖率必须不同，不能用“解析成功”代替字段齐全。
3. 签审只受理字段缺失行，并冻结签审时刻看到的失败明细快照；
   格式不符行不允许伪装成某个空字段进入签审。
4. 派单以签审记录为依据，同一物理位置全局只可出单一次（行级锁），
   并发领用由注册表内的互斥锁保证；已派出的范围不被后续规则试算改写。
5. 规则更正只产生新版本的试算结果，旧版本与旧签审不可变；
   断点恢复以实际领用台账为准，不重派、不覆盖。
"""
from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 访问日志必需字段：覆盖率逐字段核对，缺任一即算字段缺失行。
ACCESS_REQUIRED_FIELDS: list[str] = [
    "client_ip", "time_local", "method", "url", "request_id",
    "status", "body_bytes", "referer", "user_agent",
]

# 失败类型 → 展示口径
FAILURE_TYPES = {
    "line_empty": "空行",
    "format_mismatch": "格式不符",
    "field_missing": "字段缺失",
    "ok": "解析成功",
}


class QualityValidation(Exception):
    """业务校验失败（400）：例如把格式不符行送签审。"""


class QualityConflict(Exception):
    """领用冲突（409）：物理行已被其他补采单领取。"""


@dataclass(frozen=True)
class RuleSpec:
    """某次试算使用的解析规则版本。"""
    rule_id: str
    version: int
    pattern: str
    required_fields: list[str]
    note: str = ""

    def compile(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


# ---------- 场景预置规则 ----------
# v1：request_id 分组可选，缺 request_id 的行正则仍命中 → 字段缺失而非格式不符。
ACCESS_RULE_V1 = RuleSpec(
    rule_id="PARS-ACCESS",
    version=1,
    pattern=(
        r'^(\S+) - \S+ \[([^\]]+)\] "(\S+) (\S+)(?:\s+(\S+))? '
        r'HTTP/[^"]+" (\d{3}) (\d+) "([^"]*)" "([^"]*)"$'
    ),
    required_fields=list(ACCESS_REQUIRED_FIELDS),
    note="v1 初始规则：request_id 为可选捕获组，缺失时记字段缺失",
)

# v2：规则更正——request_id 调整为可选字段（改由字段增强环节补齐），
# 同样的正则在新口径下即可完整解析原先缺 request_id 的行。
ACCESS_RULE_V2 = RuleSpec(
    rule_id="PARS-ACCESS",
    version=2,
    pattern=(
        r'^(\S+) - \S+ \[([^\]]+)\] "(\S+) (\S+)(?:\s+(\S+))? '
        r'HTTP/[^"]+" (\d{3}) (\d+) "([^"]*)" "([^"]*)"$'
    ),
    required_fields=[f for f in ACCESS_REQUIRED_FIELDS if f != "request_id"],
    note="v2 更正：request_id 不再列为必需字段，缺该字段的行可正常解析",
)

# 需求给定的五行采集样本：一空行、一条格式不符、两条同文本缺请求编号、一条正常。
DEMO_RAW_LINES: list[str] = [
    "",
    "###NOT_AN_ACCESS_LOG###",
    '10.0.0.12 - - [16/Nov/2026:09:00:01 +0800] "GET /api/orders HTTP/1.1" 200 512 "-" "curl/7.88"',
    '10.0.0.12 - - [16/Nov/2026:09:00:01 +0800] "GET /api/orders HTTP/1.1" 200 512 "-" "curl/7.88"',
    '10.0.0.13 - - [16/Nov/2026:09:00:02 +0800] "GET /api/orders req-7f3a2c HTTP/1.1" 200 512 "-" "curl/7.88"',
]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _fingerprint(source_id: str, file_path: str, raw_text: str) -> str:
    import hashlib
    blob = f"{source_id}|{file_path}|{raw_text}"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _claim_key(source_id: str, file_path: str, physical_line: int) -> str:
    """物理位置领用键：同一来源文件的同一物理行全局唯一。"""
    return f"{source_id}|{file_path}|L{physical_line}"


def parse_lines(raw_lines: list[str], rule: RuleSpec) -> list[dict[str, Any]]:
    """逐行试算，输出带物理行号的失败/成功明细与逐字段覆盖率原始数据。

    返回的每条记录包含 failure_type、missing_fields、parsed_fields，
    空行与格式不符行不进入任何字段覆盖率的分母。
    """
    compiled = rule.compile()
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_lines):
        physical_line = index + 1
        if not raw.strip():
            findings.append({
                "physical_line": physical_line,
                "raw_text": raw,
                "failure_type": "line_empty",
                "reason": "空行：物理行无有效内容，不参与解析，无需补采",
                "missing_fields": [],
                "parsed_fields": None,
            })
            continue
        match = compiled.match(raw.strip())
        if match is None:
            # 格式不符只能单独成类，禁止伪装成“某个字段为空”。
            findings.append({
                "physical_line": physical_line,
                "raw_text": raw,
                "failure_type": "format_mismatch",
                "reason": "格式不符：文本不满足访问日志解析规则，不能按缺字段签审补采",
                "missing_fields": [],
                "parsed_fields": None,
            })
            continue
        captured = list(match.groups())
        # 捕获组顺序固定为 ACCESS_REQUIRED_FIELDS，规则只调整“必需口径”。
        captured_map = dict(zip(ACCESS_REQUIRED_FIELDS, captured))
        parsed = {
            name: value for name, value in captured_map.items()
            if value is not None
        }
        # 必需字段以规则口径为准；捕获为 None 的必需字段即明确的缺字段组。
        required = rule.required_fields
        missing = [
            name for name in required if captured_map.get(name) is None
        ]
        if missing:
            findings.append({
                "physical_line": physical_line,
                "raw_text": raw,
                "failure_type": "field_missing",
                "reason": f"字段缺失：正则已命中但缺少必需字段 { '、'.join(missing) }",
                "missing_fields": missing,
                "parsed_fields": parsed,
            })
        else:
            findings.append({
                "physical_line": physical_line,
                "raw_text": raw,
                "failure_type": "ok",
                "reason": "解析成功",
                "missing_fields": [],
                "parsed_fields": parsed,
            })
    return findings


def compute_field_coverage(findings: list[dict[str, Any]],
                           required_fields: list[str]) -> dict[str, Any]:
    """逐字段覆盖率：分母 = 实际被正则解析的行（field_missing + ok）。

    空行与格式不符行没有字段语义，不进入分母；因此“有 method 缺 request_id”
    的行只拉低 request_id 的覆盖率，不影响 method。
    """
    parsed_rows = [f for f in findings if f["failure_type"] in ("field_missing", "ok")]
    denominator = len(parsed_rows)
    per_field: dict[str, dict[str, int | float]] = {}
    filled_total = 0
    for name in required_fields:
        filled = sum(
            1 for row in parsed_rows
            if (row.get("parsed_fields") or {}).get(name) not in (None, "")
        )
        filled_total += filled
        per_field[name] = {
            "filled": filled,
            "denominator": denominator,
            "rate": round(filled / denominator, 4) if denominator else 0.0,
        }
    cells = denominator * len(required_fields)
    return {
        "denominator_lines": denominator,
        "physical_total": len(findings),
        "per_field": per_field,
        "overall_rate": round(filled_total / cells, 4) if cells else 0.0,
    }


@dataclass
class CaseVersion:
    """一次规则试算的不可变版本：失败明细与覆盖率在此冻结。"""
    version: int
    rule: RuleSpec
    findings: list[dict[str, Any]]
    coverage: dict[str, Any]
    created_at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "rule_id": self.rule.rule_id,
            "rule_version": self.rule.version,
            "rule_note": self.rule.note,
            "note": self.note,
            "created_at": self.created_at,
            "coverage": self.coverage,
            "findings": self.findings,
        }


@dataclass
class ReviewRecord:
    """补采签审记录：只受理明确的缺字段组，并冻结签审时所见明细。"""
    review_id: str
    case_id: str
    case_version: int
    rule_version: int
    finding_ids: list[str]
    locations: list[dict[str, Any]]
    reviewer: str
    reason: str
    reviewed_at: str
    snapshot: list[dict[str, Any]] = field(default_factory=list)
    state: str = "approved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "review_id": self.review_id,
            "case_id": self.case_id,
            "case_version": self.case_version,
            "rule_version": self.rule_version,
            "finding_ids": list(self.finding_ids),
            "locations": [dict(loc) for loc in self.locations],
            "reviewer": self.reviewer,
            "reason": self.reason,
            "reviewed_at": self.reviewed_at,
            "state": self.state,
            "snapshot": [dict(item) for item in self.snapshot],
        }


@dataclass
class Assignment:
    """补采派单：凭签审记录领取物理行，行级唯一、已派范围不可被试算改写。"""
    assignment_id: str
    case_id: str
    review_id: str
    job_id: str
    claim_keys: list[str]
    locations: list[dict[str, Any]]
    rule_version_at_claim: int
    created_at: str
    receipt_state: str = "pending"  # pending → received（质报端落派单回执）
    receipt_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "case_id": self.case_id,
            "review_id": self.review_id,
            "job_id": self.job_id,
            "claim_keys": list(self.claim_keys),
            "locations": [dict(loc) for loc in self.locations],
            "rule_version_at_claim": self.rule_version_at_claim,
            "created_at": self.created_at,
            "receipt_state": self.receipt_state,
            "receipt_at": self.receipt_at,
        }


class RemediationCase:
    """单个来源文件的一次质量补办案情。"""

    def __init__(self, case_id: str, source_id: str, source_name: str,
                 file_path: str, raw_lines: list[str]):
        self.case_id = case_id
        self.source_id = source_id
        self.source_name = source_name
        self.file_path = file_path
        self.raw_lines = list(raw_lines)
        self.versions: list[CaseVersion] = []
        self.reviews: list[ReviewRecord] = []
        self.assignments: list[Assignment] = []
        # finding_id → 签审状态（仅 field_missing 可被签审）
        self.reviewed_findings: set[str] = set()
        # 物理位置级签审台账：(file_path, physical_line)，跨规则版本持续有效
        self.reviewed_locations: set[tuple[str, int]] = set()
        self.handoffs: list[dict[str, Any]] = []
        self.created_at = _now()

    # ---- 版本与试算 ----
    def add_version(self, rule: RuleSpec, note: str = "") -> CaseVersion:
        raw_findings = parse_lines(self.raw_lines, rule)
        findings: list[dict[str, Any]] = []
        for item in raw_findings:
            finding_id = f"F{rule.version}-L{item['physical_line']}"
            already_reviewed = (
                (self.file_path, item["physical_line"]) in self.reviewed_locations
            )
            findings.append({
                "finding_id": finding_id,
                "case_id": self.case_id,
                "source_id": self.source_id,
                "source_name": self.source_name,
                "file_path": self.file_path,
                "fingerprint": _fingerprint(
                    self.source_id, self.file_path, item["raw_text"]),
                "review_state": "approved" if already_reviewed else "none",
                **item,
            })
        coverage = compute_field_coverage(findings, rule.required_fields)
        version = CaseVersion(
            version=rule.version, rule=rule, findings=findings,
            coverage=coverage, created_at=_now(), note=note,
        )
        self.versions.append(version)
        self._refresh_parseability()
        return version

    def _refresh_parseability(self) -> None:
        """用最新规则口径标注历史缺字段行“现在能否解析”，但不改动任何历史明细。"""
        latest = self.versions[-1]
        parseable_lines = {
            f["physical_line"]
            for f in latest.findings if f["failure_type"] == "ok"
        }
        for version in self.versions:
            for finding in version.findings:
                if finding["failure_type"] == "field_missing":
                    finding["now_parseable"] = (
                        finding["physical_line"] in parseable_lines
                    )
                else:
                    finding["now_parseable"] = False

    @property
    def current_version(self) -> CaseVersion:
        return self.versions[-1]

    def find_finding(self, finding_id: str) -> dict[str, Any] | None:
        for version in self.versions:
            for finding in version.findings:
                if finding["finding_id"] == finding_id:
                    return finding
        return None

    # ---- 序列化 ----
    def summary(self) -> dict[str, Any]:
        current = self.current_version
        counter = {"line_empty": 0, "format_mismatch": 0,
                   "field_missing": 0, "ok": 0}
        for finding in current.findings:
            counter[finding["failure_type"]] += 1
        return {
            "case_id": self.case_id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "file_path": self.file_path,
            "physical_total": len(self.raw_lines),
            "current_rule_version": current.rule.version,
            "failure_counter": counter,
            "coverage": current.coverage,
            "review_count": len(self.reviews),
            "assignment_count": len(self.assignments),
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "raw_lines": list(self.raw_lines),
            "versions": [v.to_dict() for v in self.versions],
            "reviews": [r.to_dict() for r in self.reviews],
            "assignments": [a.to_dict() for a in self.assignments],
            "handoffs": [dict(h) for h in self.handoffs],
        }


class QualityRemediationRegistry:
    """质量补采台账：线程安全地管理案情、签审、派单、回执与恢复。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cases: dict[str, RemediationCase] = {}
        # 物理位置领用台账：claim_key → assignment_id（全局只可出单一次）
        self._claims: dict[str, str] = {}

    # ---------- 案情 ----------
    def create_case(self, case_id: str, source_id: str, source_name: str,
                    file_path: str, raw_lines: list[str],
                    rule: RuleSpec) -> RemediationCase:
        with self._lock:
            if case_id in self._cases:
                raise QualityValidation(f"补办案情 {case_id} 已存在")
            case = RemediationCase(case_id, source_id, source_name,
                                   file_path, raw_lines)
            case.add_version(rule, note="初始试算")
            self._cases[case_id] = case
            return case

    def get_case(self, case_id: str) -> RemediationCase:
        with self._lock:
            case = self._cases.get(case_id)
            if case is None:
                raise QualityValidation(f"补办案情 {case_id} 不存在")
            return case

    def list_cases(self) -> list[dict[str, Any]]:
        with self._lock:
            return [case.summary() for case in self._cases.values()]

    # ---------- 签审 ----------
    def review_failures(self, case_id: str, finding_ids: list[str],
                        reviewer: str, reason: str = "") -> ReviewRecord:
        """对字段缺失行做补采签审；格式不符/空行/重复签审一律拒绝。"""
        if not finding_ids:
            raise QualityValidation("签审必须明确指定待补采的失败明细行")
        with self._lock:
            case = self.get_case(case_id)
            current = case.current_version
            targets: list[dict[str, Any]] = []
            for finding_id in finding_ids:
                finding = case.find_finding(finding_id)
                if finding is None:
                    raise QualityValidation(f"失败明细 {finding_id} 不存在")
                location_key = (finding["file_path"], finding["physical_line"])
                # 先看物理位置是否已签审：即使规则更正后该行变成可解析，
                # 同一物理位置也不能重复出单。
                if location_key in case.reviewed_locations:
                    raise QualityValidation(
                        f"物理行 {finding['physical_line']} 已签审"
                        "（规则更正后也不能重复出单）"
                    )
                if finding["failure_type"] != "field_missing":
                    raise QualityValidation(
                        f"物理行 {finding['physical_line']} 为"
                        f"{FAILURE_TYPES[finding['failure_type']]}，"
                        "不得伪装成缺字段行签审补采"
                    )
                targets.append(finding)

            locations = [{
                "source_id": item["source_id"],
                "source_name": item["source_name"],
                "file_path": item["file_path"],
                "physical_line": item["physical_line"],
                "missing_fields": list(item["missing_fields"]),
                "finding_id": item["finding_id"],
            } for item in targets]
            # 冻结签审时刻看到的失败明细（含全部三类，便于回溯）。
            snapshot = [dict(f) for f in current.findings]
            review = ReviewRecord(
                review_id=f"REV-{case_id}-{uuid.uuid4().hex[:8]}",
                case_id=case_id,
                case_version=current.version,
                rule_version=current.rule.version,
                finding_ids=list(finding_ids),
                locations=locations,
                reviewer=reviewer,
                reason=reason or "值班员签审：按缺字段组领取原行补采",
                reviewed_at=_now(),
                snapshot=snapshot,
            )
            case.reviews.append(review)
            case.reviewed_findings.update(finding_ids)
            case.reviewed_locations.update(
                (item["file_path"], item["physical_line"]) for item in targets
            )
            for finding in current.findings:
                if finding["finding_id"] in finding_ids:
                    finding["review_state"] = "approved"
            return review

    # ---------- 规则试算（新版本，不动旧签审/旧派单） ----------
    def retrial(self, case_id: str, new_rule: RuleSpec,
                note: str = "") -> CaseVersion:
        with self._lock:
            case = self.get_case(case_id)
            if any(v.rule.version == new_rule.version for v in case.versions):
                raise QualityValidation(
                    f"规则版本 v{new_rule.version} 已试算过，不能覆盖旧版本"
                )
            return case.add_version(new_rule, note=note or "规则更正后重新试算")

    # ---------- 派单领用（行级互斥） ----------
    def dispatch(self, case_id: str, job_id: str,
                 review_id: str | None = None) -> Assignment:
        """补采作业凭签审记录领取物理行；同一位置只可出单一次。

        - 同一作业重复领取同一签审 → 幂等返回原派单；
        - 与其他作业的在途派单重叠 → QualityConflict(409)；
        - 派单冻结签审范围，后续规则试算不会改写本单。
        """
        with self._lock:
            case = self.get_case(case_id)
            if review_id is None:
                pending_reviews = [
                    r for r in case.reviews
                    if not any(a.review_id == r.review_id for a in case.assignments)
                ]
                if not pending_reviews:
                    # 没有待领签审：该作业重入则幂等返回原单；
                    # 其他作业到达说明物理行已被别人领走 → 领用冲突。
                    own = next(
                        (a for a in case.assignments if a.job_id == job_id),
                        None,
                    )
                    if own is not None:
                        return own
                    if case.assignments:
                        held = ", ".join(sorted({
                            key for a in case.assignments for key in a.claim_keys
                        }))
                        raise QualityConflict(
                            f"物理行已被其他补采作业领取，同一位置只可出单一次：{held}"
                        )
                    raise QualityValidation("没有可领用的补采签审记录")
                reviews = pending_reviews
            else:
                reviews = [r for r in case.reviews if r.review_id == review_id]
                if not reviews:
                    raise QualityValidation(f"签审记录 {review_id} 不存在")

            # 幂等：同一作业对同一签审的重复调用直接返回原单。
            for review in reviews:
                existing = next(
                    (a for a in case.assignments
                     if a.review_id == review.review_id and a.job_id == job_id),
                    None,
                )
                if existing is not None:
                    return existing

            locations = [loc for review in reviews for loc in review.locations]
            keys = [
                _claim_key(loc["source_id"], loc["file_path"], loc["physical_line"])
                for loc in locations
            ]
            owners = {
                key: self._claims[key] for key in keys if key in self._claims
            }
            if owners:
                held = next(
                    (a for a in case.assignments if a.assignment_id in owners.values()),
                    None,
                )
                holder = held.job_id if held else "其他补采作业"
                raise QualityConflict(
                    f"物理行已被 {holder} 领取，同一位置只可出单一次："
                    + "、".join(sorted(owners))
                )

            assignment = Assignment(
                assignment_id=f"ASG-{case_id}-{uuid.uuid4().hex[:8]}",
                case_id=case_id,
                review_id=reviews[0].review_id if len(reviews) == 1
                else "+".join(r.review_id for r in reviews),
                job_id=job_id,
                claim_keys=keys,
                locations=locations,
                rule_version_at_claim=case.current_version.rule.version,
                created_at=_now(),
            )
            for key in keys:
                self._claims[key] = assignment.assignment_id
            case.assignments.append(assignment)
            return assignment

    def record_receipt(self, case_id: str, assignment_id: str) -> Assignment:
        """质报端落派单回执。"""
        with self._lock:
            case = self.get_case(case_id)
            assignment = next(
                (a for a in case.assignments if a.assignment_id == assignment_id),
                None,
            )
            if assignment is None:
                raise QualityValidation(f"补采单 {assignment_id} 不存在")
            if assignment.receipt_state != "received":
                assignment.receipt_state = "received"
                assignment.receipt_at = _now()
            return assignment

    # ---------- 断点恢复 ----------
    def reconcile(self, case_id: str, job_id: str | None = None) -> dict[str, Any]:
        """核对实际已领取的工作：恢复时据此判断，不重派同一行。"""
        with self._lock:
            case = self.get_case(case_id)
            assignments = [
                a for a in case.assignments if job_id is None or a.job_id == job_id
            ]
            claimed_locations = [
                dict(loc) for a in assignments for loc in a.locations
            ]
            return {
                "case_id": case_id,
                "job_id": job_id,
                "claimed_claim_keys": [
                    key for a in assignments for key in a.claim_keys
                ],
                "claimed_locations": claimed_locations,
                "assignments": [a.to_dict() for a in assignments],
                "global_claim_count": len(self._claims),
            }

    # ---------- 交接记录 ----------
    def build_handoff(self, case_id: str) -> dict[str, Any]:
        """生成交接记录：更正过的行何时可处理、其余行怎么补采、旧明细保留在哪。"""
        with self._lock:
            case = self.get_case(case_id)
            current = case.current_version
            lines: list[dict[str, Any]] = []
            for review in case.reviews:
                for loc in review.locations:
                    finding = case.find_finding(loc["finding_id"])
                    owner = next(
                        (a for a in case.assignments
                         if _claim_key(loc["source_id"], loc["file_path"],
                                       loc["physical_line"]) in a.claim_keys),
                        None,
                    )
                    now_parseable = bool(finding and finding.get("now_parseable"))
                    if owner and now_parseable:
                        instruction = (
                            f"规则已更正至 v{current.rule.version}，本行现可正常解析；"
                            "待该补采单结单后按常规续采重新入库，不重派给其他作业"
                        )
                    elif owner:
                        instruction = (
                            "本行仍不满足新规则，沿用已派出补采单按缺字段组补采，"
                            "其他作业不可再领"
                        )
                    elif now_parseable:
                        instruction = (
                            f"规则已更正至 v{current.rule.version}，本行可处理："
                            "由续采作业按新规则重新解析入库"
                        )
                    else:
                        instruction = "本行仍缺字段：需凭签审记录发起补采单领取原行"
                    lines.append({
                        "finding_id": loc["finding_id"],
                        "physical_line": loc["physical_line"],
                        "file_path": loc["file_path"],
                        "missing_fields_at_review": list(loc["missing_fields"]),
                        "now_parseable": now_parseable,
                        "job_id": owner.job_id if owner else None,
                        "assignment_id": owner.assignment_id if owner else None,
                        "receipt_state": owner.receipt_state if owner else None,
                        "instruction": instruction,
                    })
            handoff = {
                "handoff_id": f"HAND-{case_id}-{uuid.uuid4().hex[:8]}",
                "case_id": case_id,
                "created_at": _now(),
                "current_rule_version": current.rule.version,
                "reviewed_snapshot_kept": [
                    {
                        "review_id": r.review_id,
                        "case_version": r.case_version,
                        "rule_version": r.rule_version,
                        "snapshot_size": len(r.snapshot),
                    } for r in case.reviews
                ],
                "scope_guard": (
                    "已派出补采作业的领用范围以签审为准，"
                    "后续规则试算不得悄悄改写其范围"
                ),
                "lines": lines,
            }
            case.handoffs.append(handoff)
            return handoff

    # ---------- 质报汇总入口：接续调用续采 ----------
    def continue_collection(self, case_id: str) -> dict[str, Any]:
        """质报汇总后的续采指引：返回下一步可执行动作与对应物理行。"""
        with self._lock:
            case = self.get_case(case_id)
            current = case.current_version
            approvable = [
                f["finding_id"] for f in current.findings
                if f["failure_type"] == "field_missing"
                and (f["file_path"], f["physical_line"]) not in case.reviewed_locations
            ]
            dispatched_keys = {
                key for a in case.assignments for key in a.claim_keys
            }
            unclaimed_reviews = [
                r.review_id for r in case.reviews
                if not any(a.review_id == r.review_id for a in case.assignments)
            ]
            pending_receipts = [
                a.assignment_id for a in case.assignments
                if a.receipt_state == "pending"
            ]
            return {
                "case_id": case_id,
                "current_rule_version": current.rule.version,
                "next_actions": {
                    "awaiting_review": approvable,
                    "ready_to_dispatch_reviews": unclaimed_reviews,
                    "awaiting_receipt_assignments": pending_receipts,
                },
                "dispatched_locations": sorted(dispatched_keys),
                "handoff_available": True,
            }

    def reset(self) -> None:
        """清空台账（演示/测试重置用）。"""
        with self._lock:
            self._cases.clear()
            self._claims.clear()

    # ---------- 状态快照（模拟服务重开后的台账恢复） ----------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "claims": dict(self._claims),
                "cases": {cid: case.to_dict() for cid, case in self._cases.items()},
            }

    @classmethod
    def restore(cls, state: dict[str, Any]) -> "QualityRemediationRegistry":
        """从持久化快照重建台账：重开后已领用行依旧被锁定。"""
        registry = cls()
        registry._claims = dict(state.get("claims", {}))
        for cid, payload in state.get("cases", {}).items():
            rule = RuleSpec(
                rule_id=payload["versions"][0]["rule_id"],
                version=payload["current_rule_version"],
                pattern=ACCESS_RULE_V2.pattern,
                required_fields=list(ACCESS_RULE_V2.required_fields),
            )
            case = RemediationCase(
                case_id=cid,
                source_id=payload["source_id"],
                source_name=payload["source_name"],
                file_path=payload["file_path"],
                raw_lines=list(payload["raw_lines"]),
            )
            case.created_at = payload["created_at"]
            for version_payload in payload["versions"]:
                v_rule = RuleSpec(
                    rule_id=version_payload["rule_id"],
                    version=version_payload["rule_version"],
                    pattern=ACCESS_RULE_V1.pattern
                    if version_payload["rule_version"] == 1
                    else ACCESS_RULE_V2.pattern,
                    required_fields=list(
                        ACCESS_REQUIRED_FIELDS
                        if version_payload["rule_version"] == 1
                        else ACCESS_RULE_V2.required_fields
                    ),
                    note=version_payload["rule_note"],
                )
                case.versions.append(CaseVersion(
                    version=version_payload["version"],
                    rule=v_rule,
                    findings=[dict(f) for f in version_payload["findings"]],
                    coverage=version_payload["coverage"],
                    created_at=version_payload["created_at"],
                    note=version_payload.get("note", ""),
                ))
            for review_payload in payload["reviews"]:
                review = ReviewRecord(
                    review_id=review_payload["review_id"],
                    case_id=cid,
                    case_version=review_payload["case_version"],
                    rule_version=review_payload["rule_version"],
                    finding_ids=list(review_payload["finding_ids"]),
                    locations=[dict(x) for x in review_payload["locations"]],
                    reviewer=review_payload["reviewer"],
                    reason=review_payload["reason"],
                    reviewed_at=review_payload["reviewed_at"],
                    snapshot=[dict(x) for x in review_payload.get("snapshot", [])],
                    state=review_payload.get("state", "approved"),
                )
                case.reviews.append(review)
                case.reviewed_findings.update(review.finding_ids)
                case.reviewed_locations.update(
                    (loc["file_path"], loc["physical_line"])
                    for loc in review.locations
                )
            for asg_payload in payload["assignments"]:
                case.assignments.append(Assignment(
                    assignment_id=asg_payload["assignment_id"],
                    case_id=cid,
                    review_id=asg_payload["review_id"],
                    job_id=asg_payload["job_id"],
                    claim_keys=list(asg_payload["claim_keys"]),
                    locations=[dict(x) for x in asg_payload["locations"]],
                    rule_version_at_claim=asg_payload["rule_version_at_claim"],
                    created_at=asg_payload["created_at"],
                    receipt_state=asg_payload["receipt_state"],
                    receipt_at=asg_payload.get("receipt_at"),
                ))
            case.handoffs = [dict(h) for h in payload.get("handoffs", [])]
            registry._cases[cid] = case
        return registry


# ---------- 演示案情工厂 ----------

def build_demo_case(registry: QualityRemediationRegistry,
                    case_id: str = "CASE-DEMO-01") -> RemediationCase:
    """按需求给定的五行样本建立补办案情（v1 规则初始试算）。"""
    return registry.create_case(
        case_id=case_id,
        source_id="LOG_-0001",
        source_name="WebAccessLog",
        file_path="/var/log/nginx/access.log",
        raw_lines=list(DEMO_RAW_LINES),
        rule=ACCESS_RULE_V1,
    )


# 模块级单例台账（与 store 一样随进程存活，可经 snapshot/restore 模拟重开）。
registry = QualityRemediationRegistry()
build_demo_case(registry)

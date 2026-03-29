# AUDITING AGENT SPECIFICATION v1.0
## Complete Result Validation & Fault Detection System (23 Steps)

**Document Version:** 1.0
**Date:** March 28, 2026
**Status:** Design (Quality Assurance Layer)
**Scope:** Complete result auditing, fault detection, failure diagnosis
**Placement:** Company Pi (monitors retail agent outputs before use)
**Model:** Built with full logic; optimization/distribution later

---

## EXECUTIVE SUMMARY

The AuditingAgent is a quality control system that validates outputs from all other agents before they influence trading decisions. It runs on the Company Pi and acts as a gatekeeper, detecting faults, inconsistencies, anomalies, and policy violations in real time.

**Role:**
- Validates ExecutionAgent trade decisions
- Validates DisclosureResearchAgent signals
- Validates SocialRumorAgent signals
- Validates MarketSentimentAgent recommendations
- Detects systematic failures
- Provides feedback for model improvement

**Output:**
- `pass` / `pass_with_warnings` / `review_recommended` / `fail` / `block_output` / `escalate_systemic_issue`
- Detailed fault report with root cause
- Remediation recommendations

**Flow:**
```
Agent Output
     ↓
AuditingAgent (23 steps) ← validates
     ↓
Result? (pass/fail/block/escalate)
     ↓
If PASS: forward to downstream (ExecutionAgent, Portal, etc.)
If WARNING: forward with warning log
If REVIEW: send to manual queue
If FAIL: reject, don't use
If BLOCK: prevent release entirely
If ESCALATE: trigger system alert
```

---

## PART 1: AUDITING AGENT (23 Steps)

### 1. System Gate (Input Validation)

```
IF result input missing
  → result_payload = null
  → state = reject_result

IF expected reference input missing
  → reference_payload = null AND reference_required = true
  → state = incomplete_evaluation

IF parse failure on result
  → parsed_result = null
  → state = reject_result

IF schema invalid
  → schema_validation(result_payload) = false
  → state = invalid_structure

IF evaluation rules unavailable
  → evaluation_rule_set = null
  → state = halt_audit

IF duplicate result detected
  → hash(result_payload) IN audited_result_store
  → state = suppress_duplicate_audit

IF result timestamp missing
  → result_timestamp = null
  → state = low_traceability

IF provenance missing
  → source_provenance = null
  → state = unverifiable_origin
```

**Gate Output:**
- `input_status` ∈ {OK, REJECT, INCOMPLETE, PARSE_ERROR, INVALID_SCHEMA, HALT, DUPLICATE, NO_TIMESTAMP, UNKNOWN_SOURCE}

---

### 2. Scope & Audit Mode Controls

```
IF audit mode = correctness
  → selected_audit_mode = correctness
  → audit_state = factual_validation

IF audit mode = consistency
  → selected_audit_mode = consistency
  → audit_state = internal_consistency_check

IF audit mode = completeness
  → selected_audit_mode = completeness
  → audit_state = coverage_check

IF audit mode = compliance
  → selected_audit_mode = compliance
  → audit_state = rule_conformance_check

IF audit mode = robustness
  → selected_audit_mode = robustness
  → audit_state = stress_check

IF audit mode = anomaly detection
  → selected_audit_mode = anomaly
  → audit_state = outlier_scan

IF multiple audit modes active
  → count(selected_audit_modes) > 1
  → audit_state = multi_pass_audit
```

**Mode Output:**
- `active_audit_modes` = [correctness, consistency, completeness, compliance, robustness, anomaly]
- `multi_pass` ∈ {True, False}

---

### 3. Result Structure Controls

```
IF required field missing
  → required_field_count_present < required_field_count_total
  → fault_state = missing_required_fields

IF unexpected field present
  → unexpected_field_count > 0
  → fault_state = extra_fields_detected

IF field type mismatch
  → actual_type(field_i) != expected_type(field_i)
  → fault_state = type_mismatch

IF null value in mandatory field
  → field_i = null AND field_i_required = true
  → fault_state = null_mandatory_value

IF field format invalid
  → format_check(field_i) = false
  → fault_state = format_error

IF nested structure malformed
  → recursive_schema_validation(subtree_i) = false
  → fault_state = malformed_substructure

IF key ordering matters and is wrong
  → ordered_field_sequence != expected_order
  → fault_state = ordering_error
```

**Structure Output:**
- `structure_fault_count` = integer
- `fault_fields` = [list of problematic fields]
- `structure_valid` ∈ {True, False}

---

### 4. Logical Consistency Controls

```
IF two fields conflict
  → constraint(field_a, field_b) = false
  → fault_state = internal_conflict

IF summary conflicts with detailed values
  → summary_value != aggregate(detail_values)
  → fault_state = summary_detail_mismatch

IF totals do not reconcile
  → reported_total != sum(component_values)
  → fault_state = reconciliation_error

IF chronology impossible
  → timestamp_t2 < timestamp_t1 where t2 should follow t1
  → fault_state = temporal_inconsistency

IF classification conflicts with evidence
  → assigned_label != inferred_label_from_inputs
  → fault_state = label_evidence_conflict

IF declared status conflicts with state variables
  → status_label incompatible with state_vector
  → fault_state = status_conflict

IF result violates domain rule
  → domain_constraint(result_payload) = false
  → fault_state = domain_logic_failure
```

**Consistency Output:**
- `consistency_fault_count` = integer
- `conflicting_pairs` = [list of field conflicts]
- `internal_consistency_valid` ∈ {True, False}

---

### 5. Completeness Controls

```
IF expected section missing
  → required_section_i absent
  → fault_state = incomplete_result

IF expected explanation missing
  → explanation_required = true AND explanation_text = null
  → fault_state = missing_explanation

IF supporting evidence missing
  → evidence_required = true AND evidence_count = 0
  → fault_state = unsupported_conclusion

IF edge cases not addressed
  → expected_edge_case_count > addressed_edge_case_count
  → fault_state = edge_case_coverage_gap

IF output truncated
  → output_length = max_length_limit AND truncation_marker_detected = true
  → fault_state = truncated_output

IF optional fields absent but required by mode
  → mode_specific_required_fields missing
  → fault_state = mode_completeness_failure

IF confidence absent
  → confidence_required = true AND confidence_score = null
  → fault_state = missing_confidence
```

**Completeness Output:**
- `completeness_score` ∈ {0.0 to 1.0}
- `missing_sections` = [list]
- `completeness_valid` ∈ {True, False}

---

### 6. Correctness Controls

```
IF result differs from known truth
  → distance(result_value, ground_truth_value) > correctness_tolerance
  → fault_state = incorrect_result

IF categorical label wrong
  → predicted_class != actual_class
  → fault_state = misclassification

IF numeric output outside tolerance
  → abs(result_value - expected_value) > numeric_tolerance
  → fault_state = numeric_error

IF unit incorrect
  → reported_unit != expected_unit
  → fault_state = unit_error

IF sign incorrect
  → sign(result_value) != sign(expected_value)
  → fault_state = sign_error

IF magnitude implausible
  → abs(result_value) > max_plausible_bound OR < min_plausible_bound where nonzero expected
  → fault_state = magnitude_error

IF direction-of-change wrong
  → predicted_delta_sign != actual_delta_sign
  → fault_state = directional_error
```

**Correctness Output:**
- `correctness_fault_count` = integer
- `error_magnitude` = distance from truth
- `correctness_valid` ∈ {True, False}

---

### 7. Reference Comparison Controls

```
IF reference available and exact mismatch
  → result_payload != reference_payload under exact_match_mode
  → fault_state = exact_mismatch

IF reference available and partial mismatch
  → match_score(result_payload, reference_payload) < partial_match_threshold
  → fault_state = partial_mismatch

IF high deviation from benchmark result
  → deviation_metric(result_payload, benchmark_payload) > deviation_threshold
  → fault_state = benchmark_deviation

IF result is closer to prior faulty pattern than correct pattern
  → similarity(result_payload, fault_pattern_library) > similarity_to_correct_pattern
  → fault_state = known_fault_signature

IF result disagrees with majority ensemble
  → ensemble_agreement_ratio < consensus_threshold
  → fault_state = ensemble_outlier

IF result matches stale reference not current reference
  → similarity(result_payload, stale_reference) > similarity(result_payload, current_reference)
  → fault_state = outdated_alignment
```

**Reference Output:**
- `reference_match_score` ∈ {0.0 to 1.0}
- `deviation_from_benchmark` = metric value
- `reference_comparison_valid` ∈ {True, False}

---

### 8. Statistical / Distribution Controls

```
IF result is an outlier
  → z_score(result_value, historical_distribution) > outlier_threshold
  → fault_state = statistical_outlier

IF variance collapse detected
  → variance(result_batch) < min_variance_threshold
  → fault_state = suspicious_uniformity

IF drift detected from baseline
  → distribution_distance(current_batch, baseline_batch) > drift_threshold
  → fault_state = distribution_drift

IF batch mean shifted unexpectedly
  → abs(mean(current_batch) - mean(baseline_batch)) > mean_shift_threshold
  → fault_state = mean_shift

IF error cluster detected
  → fault_density(similar_results_window) > cluster_threshold
  → fault_state = clustered_failure

IF result frequency impossible
  → event_frequency > max_domain_frequency
  → fault_state = rate_anomaly
```

**Statistical Output:**
- `z_score` = outlier metric
- `distribution_status` = nominal/drift/cluster
- `statistical_valid` ∈ {True, False}

---

### 9. Robustness Controls

```
IF small input perturbation causes large output change
  → sensitivity_metric(delta_input_small, delta_output_large) > instability_threshold
  → fault_state = unstable_result

IF repeated run yields inconsistent result
  → same_input_repeat_variance > reproducibility_threshold
  → fault_state = non_reproducible

IF adversarial input breaks output
  → attack_case_success_rate > adversarial_threshold
  → fault_state = adversarial_vulnerability

IF missing optional input causes catastrophic failure
  → optional_input_removed AND output_invalid = true
  → fault_state = brittle_dependency

IF boundary condition fails
  → boundary_case_test = fail
  → fault_state = boundary_failure

IF stress load degrades correctness
  → performance_under_load < load_performance_threshold
  → fault_state = load_sensitivity
```

**Robustness Output:**
- `stability_score` ∈ {0.0 to 1.0}
- `reproducibility_score` ∈ {0.0 to 1.0}
- `robustness_valid` ∈ {True, False}

---

### 10. Reasoning / Causality Controls

```
IF conclusion unsupported by premises
  → inference_path_score < support_threshold
  → fault_state = unsupported_inference

IF chain contains non sequitur
  → step_i logically disconnected from step_i_minus_1
  → fault_state = reasoning_gap

IF causal claim made from correlation only
  → causal_language_present = true AND causal_evidence_absent = true
  → fault_state = causal_overreach

IF explanation omits decisive variable
  → important_feature_importance > importance_threshold AND not mentioned
  → fault_state = omitted_key_factor

IF rationale contradicts output
  → explanation_implication != stated_result
  → fault_state = rationale_output_conflict

IF explanation post-hoc only
  → output_generated before rationale evidence linkage AND rationale_match_score low
  → fault_state = posthoc_justification_risk
```

**Reasoning Output:**
- `reasoning_validity_score` ∈ {0.0 to 1.0}
- `causal_validity` ∈ {True, False}
- `reasoning_valid` ∈ {True, False}

---

### 11. Constraint / Policy Controls

```
IF output violates hard rule
  → hard_constraint(result_payload) = false
  → fault_state = hard_rule_violation

IF output violates soft preference threshold
  → preference_score < soft_threshold
  → fault_state = soft_preference_failure

IF prohibited content present
  → content_policy_match = true
  → fault_state = prohibited_output

IF required disclaimer missing
  → disclaimer_required = true AND disclaimer_present = false
  → fault_state = missing_disclaimer

IF restricted action recommended
  → restricted_action_flag = true
  → fault_state = restricted_recommendation

IF audit trail noncompliant
  → trace_log_completeness < trace_minimum
  → fault_state = audit_trail_failure
```

**Compliance Output:**
- `hard_rule_violations_count` = integer
- `soft_preference_violations_count` = integer
- `compliance_valid` ∈ {True, False}

---

### 12. Data Provenance Controls

```
IF source provenance unverifiable
  → source_provenance_confidence < provenance_threshold
  → fault_state = unverifiable_source

IF source freshness insufficient
  → now - source_timestamp > freshness_limit
  → fault_state = stale_source

IF conflicting sources used without resolution
  → source_conflict_count > 0 AND resolution_note = null
  → fault_state = unresolved_source_conflict

IF citation missing for external claim
  → external_claim_count > cited_claim_count
  → fault_state = missing_citation

IF citation does not support claim
  → claim_support_score(citation, claim) < support_threshold
  → fault_state = unsupported_citation

IF source hierarchy violated
  → low_priority_source_used while higher_priority_source available
  → fault_state = source_priority_failure
```

**Provenance Output:**
- `source_verification_score` ∈ {0.0 to 1.0}
- `freshness_status` ∈ {FRESH, AGED, STALE}
- `provenance_valid` ∈ {True, False}

---

### 13. Temporal Controls

```
IF result uses outdated assumption
  → assumption_timestamp < validity_window_start
  → fault_state = outdated_assumption

IF future information leaked into historical result
  → feature_timestamp > decision_timestamp
  → fault_state = data_leakage

IF latency exceeds acceptable audit window
  → audit_completion_time > max_audit_latency
  → fault_state = late_detection

IF time window misapplied
  → evaluation_window != intended_window
  → fault_state = window_misapplication

IF periodic result missing expected cadence
  → actual_interval != expected_interval beyond cadence_tolerance
  → fault_state = cadence_failure
```

**Temporal Output:**
- `temporal_validity` ∈ {True, False}
- `data_leakage_detected` ∈ {True, False}
- `latency_status` ∈ {ACCEPTABLE, MARGINAL, LATE}

---

### 14. Explanation Quality Controls

```
IF explanation absent
  → explanation_text = null
  → fault_state = no_explanation

IF explanation too vague
  → specificity_score(explanation_text) < specificity_threshold
  → fault_state = vague_explanation

IF explanation too confident for evidence level
  → language_certainty > evidence_strength + confidence_gap_threshold
  → fault_state = overclaimed_certainty

IF explanation omits uncertainty
  → uncertainty_required = true AND uncertainty_terms absent
  → fault_state = hidden_uncertainty

IF explanation overly long but low information
  → info_density(explanation_text) < density_threshold AND length > verbosity_threshold
  → fault_state = low_information_explanation

IF explanation not aligned with requested format
  → format_match(explanation_text, required_format) = false
  → fault_state = explanation_format_failure
```

**Explanation Output:**
- `explanation_quality_score` ∈ {0.0 to 1.0}
- `explanation_valid` ∈ {True, False}

---

### 15. Ranking / Prioritization Controls

```
IF top-ranked item incorrect
  → rank_1 != true_best_item
  → fault_state = top_rank_failure

IF ranking order inconsistent with scores
  → ranking_order != descending(score_vector)
  → fault_state = score_rank_inconsistency

IF important item omitted from top-k
  → true_relevant_item NOT IN top_k
  → fault_state = recall_failure_topk

IF irrelevant item included in top-k
  → irrelevant_item_count_in_topk > 0
  → fault_state = precision_failure_topk

IF score ties unresolved improperly
  → tie_count > 0 AND tie_break_rule not applied
  → fault_state = tie_break_failure
```

**Ranking Output:**
- `ranking_validity` ∈ {True, False}
- `top_k_precision` ∈ {0.0 to 1.0}
- `top_k_recall` ∈ {0.0 to 1.0}

---

### 16. Confidence Calibration Controls

```
IF confidence high and result wrong
  → confidence_score >= high_confidence_threshold AND fault_detected = true
  → fault_state = overconfidence

IF confidence low and result correct
  → confidence_score <= low_confidence_threshold AND fault_detected = false
  → fault_state = underconfidence

IF calibration drift detected
  → calibration_error(current_window) > calibration_threshold
  → fault_state = miscalibrated_confidence

IF confidence missing where required
  → confidence_required = true AND confidence_score = null
  → fault_state = missing_confidence

IF confidence inconsistent with evidence quality
  → abs(confidence_score - evidence_quality_score) > confidence_evidence_gap
  → fault_state = confidence_evidence_mismatch
```

**Calibration Output:**
- `calibration_error` = metric value
- `calibration_status` ∈ {WELL_CALIBRATED, OVERCONFIDENT, UNDERCONFIDENT, MISCALIBRATED}
- `calibration_valid` ∈ {True, False}

---

### 17. Fault Severity Controls

```
IF fault affects cosmetic output only
  → fault_impact_scope = presentation_only
  → severity = low

IF fault affects interpretation but not final answer
  → fault_impact_scope = explanation_or_format_only
  → severity = medium

IF fault changes final result materially
  → fault_impact_scope = decision_output
  → severity = high

IF fault could trigger unsafe action
  → risk_of_harm > harm_threshold
  → severity = critical

IF multiple medium faults co-occur
  → count(medium_faults) >= escalation_count
  → severity = escalated_high

IF repeated same fault pattern appears
  → repeat_fault_count(pattern_i) > repeat_threshold
  → severity = systemic
```

**Severity Output:**
- `max_severity` ∈ {LOW, MEDIUM, HIGH, CRITICAL, SYSTEMIC}
- `severity_justification` = explanation

---

### 18. Root Cause Attribution Controls

```
IF fault linked to bad input
  → input_quality_score < input_quality_threshold
  → root_cause = input_failure

IF fault linked to rule mismatch
  → rule_set_version incompatible with expected_rule_version
  → root_cause = rules_failure

IF fault linked to model instability
  → repeat_variance > reproducibility_threshold
  → root_cause = model_instability

IF fault linked to stale data
  → source_freshness_state = stale
  → root_cause = stale_data

IF fault linked to missing business logic
  → domain_constraint_absent = true AND domain_failure_detected = true
  → root_cause = missing_domain_logic

IF fault linked to unhandled edge case
  → edge_case_signature_match = true
  → root_cause = unhandled_edge_case

IF root cause unclear
  → max(root_cause_probabilities) < root_cause_confidence_threshold
  → root_cause = unresolved
```

**Root Cause Output:**
- `root_cause` ∈ {INPUT_FAILURE, RULES_FAILURE, MODEL_INSTABILITY, STALE_DATA, MISSING_LOGIC, EDGE_CASE, UNRESOLVED}
- `root_cause_confidence` ∈ {0.0 to 1.0}

---

### 19. Action Classification

```
IF no material fault detected
  → high_severity_fault_count = 0 AND critical_fault_count = 0 AND total_fault_score < accept_threshold
  → classification = pass

IF minor faults only
  → max_severity = low
  → classification = pass_with_warnings

IF medium fault detected
  → max_severity = medium
  → classification = review_recommended

IF high fault detected
  → max_severity = high
  → classification = fail

IF critical fault detected
  → max_severity = critical
  → classification = block_output

IF systemic pattern detected
  → severity = systemic
  → classification = escalate_systemic_issue

IF root cause unresolved and severity medium or higher
  → root_cause = unresolved AND max_severity >= medium
  → classification = manual_investigation
```

**Classification Output:**
- `classification` ∈ {PASS, PASS_WITH_WARNINGS, REVIEW_RECOMMENDED, FAIL, BLOCK_OUTPUT, ESCALATE_SYSTEMIC_ISSUE, MANUAL_INVESTIGATION}

---

### 20. Remediation Controls

```
IF missing field fault
  → fault_state = missing_required_fields
  → remediation = request_or_reconstruct_missing_fields

IF type mismatch fault
  → fault_state = type_mismatch
  → remediation = coerce_or_reparse_types

IF unsupported conclusion fault
  → fault_state = unsupported_conclusion
  → remediation = require_evidence_attachment

IF stale source fault
  → fault_state = stale_source
  → remediation = refresh_sources

IF reconciliation error fault
  → fault_state = reconciliation_error
  → remediation = recompute_aggregates

IF confidence calibration fault
  → fault_state = miscalibrated_confidence
  → remediation = recalibrate_confidence_model

IF systemic issue fault
  → classification = escalate_systemic_issue
  → remediation = quarantine_pipeline_segment

IF block output classification
  → classification = block_output
  → remediation = suppress_release
```

**Remediation Output:**
- `remediation_action` = recommended fix
- `remediation_difficulty` ∈ {EASY, MODERATE, DIFFICULT, IMPOSSIBLE}

---

### 21. Evaluation Loop

```
IF audited result stored
  → classification IN {pass, pass_with_warnings, review_recommended, fail, block_output, escalate_systemic_issue}
  → store audit_record

IF fault confirmed after review
  → manual_review_result = confirmed_fault
  → fault_library = update_with_pattern

IF false alarm detected
  → manual_review_result = false_positive
  → audit_thresholds = adjust_to_reduce_false_positives

IF repeated missed faults detected
  → missed_fault_rate > missed_fault_threshold
  → audit_rules = tighten

IF repeated false positives detected
  → false_positive_rate > false_positive_threshold
  → audit_rules = relax

IF specific fault pattern increases over time
  → trend(fault_pattern_i) > growth_threshold
  → state = emerging_system_issue

IF remediation succeeds
  → post_remediation_audit = pass
  → state = resolved

IF remediation fails
  → post_remediation_audit IN {fail, block_output}
  → state = unresolved_fault
```

**Evaluation Output:**
- `audit_record` = stored for review
- `pattern_updates` = new fault signatures
- `threshold_adjustments` = updated sensitivity

---

### 22. Output Controls

```
IF classification = pass
  → classification == pass
  → output_action = approve_result

IF classification = pass_with_warnings
  → classification == pass_with_warnings
  → output_action = approve_with_warning_log

IF classification = review_recommended
  → classification == review_recommended
  → output_action = send_to_manual_review

IF classification = fail
  → classification == fail
  → output_action = reject_result

IF classification = block_output
  → classification == block_output
  → output_action = block_downstream_use

IF classification = escalate_systemic_issue
  → classification == escalate_systemic_issue
  → output_action = trigger_system_escalation

IF confidence in audit low
  → audit_confidence < audit_confidence_threshold
  → output_action = mark_audit_uncertain

IF root cause unresolved
  → root_cause = unresolved
  → output_action = request_investigation
```

**Output Control:**
- `downstream_action` ∈ {APPROVE, APPROVE_WITH_WARNING, SEND_TO_REVIEW, REJECT, BLOCK, ESCALATE, MARK_UNCERTAIN, REQUEST_INVESTIGATION}

---

### 23. Final Composite Fault Score

```
IF composite fault score calculated
  → fault_score = w1*correctness_fault + w2*consistency_fault + w3*completeness_fault + w4*policy_fault + w5*robustness_fault + w6*provenance_fault + w7*calibration_fault
  → state = scored_result

IF composite fault score below warning threshold
  → fault_score < warning_threshold
  → final_audit_signal = clean

IF composite fault score between warning and fail threshold
  → warning_threshold <= fault_score < fail_threshold
  → final_audit_signal = warning

IF composite fault score above fail threshold
  → fault_score >= fail_threshold
  → final_audit_signal = faulty

IF critical override active
  → critical_fault_count > 0
  → final_audit_signal = blocked

IF systemic override active
  → classification = escalate_systemic_issue
  → final_audit_signal = systemic_failure
```

**Final Output:**
```json
{
  "audit_timestamp": "2026-03-28T10:30:00Z",
  "source_agent": "ExecutionAgent",
  "source_result_id": "trade_12345",
  "input_status": "OK",
  "audit_modes_applied": ["correctness", "consistency", "completeness"],
  "structure_valid": true,
  "consistency_valid": true,
  "completeness_valid": true,
  "correctness_valid": true,
  "reference_match_score": 0.98,
  "statistical_valid": true,
  "robustness_valid": true,
  "reasoning_valid": true,
  "compliance_valid": true,
  "provenance_valid": true,
  "temporal_validity": true,
  "explanation_valid": true,
  "calibration_status": "WELL_CALIBRATED",
  "max_severity": "LOW",
  "fault_count": 0,
  "root_cause": "none",
  "classification": "PASS",
  "fault_score": 0.05,
  "final_audit_signal": "clean",
  "downstream_action": "APPROVE",
  "audit_confidence": 0.99,
  "remediation": "none",
  "audit_notes": "All checks passed. Result approved for use."
}
```

---

## AUDIT MODES EXPLAINED

### Mode: Correctness
Validates that the result is factually correct against known truth or acceptable error bounds.

### Mode: Consistency
Validates that the result has no internal contradictions, reconciliations errors, or logical conflicts.

### Mode: Completeness
Validates that all required information is present, formatted correctly, and within expected bounds.

### Mode: Compliance
Validates that the result complies with policies, constraints, and regulatory requirements.

### Mode: Robustness
Validates that the result is stable under input perturbations, reproducible, and not brittle.

### Mode: Anomaly Detection
Validates that the result is not a statistical outlier or part of an anomalous pattern.

---

## DATABASE SCHEMA ADDITIONS

### audit_records table (new)
```sql
CREATE TABLE audit_records (
  id INTEGER PRIMARY KEY,
  audit_timestamp DATETIME,
  source_agent TEXT,  -- ExecutionAgent, DisclosureResearchAgent, SocialRumorAgent, MarketSentimentAgent
  source_result_id TEXT,  -- ID of result being audited
  input_status TEXT,  -- OK, REJECT, INCOMPLETE, etc.
  audit_modes_applied TEXT,  -- JSON: [correctness, consistency, ...]
  structure_valid BOOLEAN,
  consistency_valid BOOLEAN,
  completeness_valid BOOLEAN,
  correctness_valid BOOLEAN,
  reference_match_score DECIMAL(3,2),
  statistical_valid BOOLEAN,
  robustness_valid BOOLEAN,
  reasoning_valid BOOLEAN,
  compliance_valid BOOLEAN,
  provenance_valid BOOLEAN,
  temporal_validity BOOLEAN,
  explanation_valid BOOLEAN,
  calibration_status TEXT,  -- WELL_CALIBRATED, OVERCONFIDENT, etc.
  max_severity TEXT,  -- LOW, MEDIUM, HIGH, CRITICAL, SYSTEMIC
  fault_count INTEGER DEFAULT 0,
  fault_details TEXT,  -- JSON: list of faults detected
  root_cause TEXT,
  root_cause_confidence DECIMAL(3,2),
  classification TEXT,  -- PASS, FAIL, BLOCK_OUTPUT, etc.
  fault_score DECIMAL(3,2),
  final_audit_signal TEXT,  -- clean, warning, faulty, blocked, systemic
  downstream_action TEXT,  -- APPROVE, REJECT, BLOCK, ESCALATE
  audit_confidence DECIMAL(3,2),
  remediation_action TEXT,
  remediation_difficulty TEXT,
  audit_notes TEXT,
  processed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_source_agent ON audit_records(source_agent);
CREATE INDEX idx_classification ON audit_records(classification);
CREATE INDEX idx_fault_score ON audit_records(fault_score);
CREATE INDEX idx_max_severity ON audit_records(max_severity);
```

### fault_patterns table (new)
```sql
CREATE TABLE fault_patterns (
  id INTEGER PRIMARY KEY,
  pattern_signature TEXT UNIQUE,  -- hash of fault characteristics
  fault_type TEXT,
  source_agent TEXT,
  first_detected DATETIME,
  last_detected DATETIME,
  occurrence_count INTEGER DEFAULT 1,
  resolution_status TEXT,  -- open, investigating, resolved, chronic
  root_cause TEXT,
  recommended_action TEXT,
  severity_level TEXT,
  updated_at DATETIME
);

CREATE INDEX idx_pattern_signature ON fault_patterns(pattern_signature);
CREATE INDEX idx_resolution_status ON fault_patterns(resolution_status);
```

### audit_calibration table (new)
```sql
CREATE TABLE audit_calibration (
  id INTEGER PRIMARY KEY,
  audit_mode TEXT,
  threshold_name TEXT,
  current_threshold DECIMAL(5,3),
  adjusted_at DATETIME,
  adjustment_reason TEXT,  -- false_positive_reduction, sensitivity_tightening, etc.
  baseline_threshold DECIMAL(5,3),
  false_positive_rate DECIMAL(5,3),
  false_negative_rate DECIMAL(5,3),
  optimal_threshold DECIMAL(5,3)
);

CREATE INDEX idx_audit_mode ON audit_calibration(audit_mode);
```

---

## ERROR HANDLING & SPECIAL CASES

### When Audit Rules Unavailable
- Log error
- Use fallback rule set (conservative defaults)
- Flag result as "audit_uncertain"
- Mark for manual review

### When Reference Not Available
- Skip reference comparison step
- Rely on other validation modes
- Lower overall confidence if reference is critical

### When Fault Pattern Unknown
- Store as new pattern
- Flag for investigation
- Don't suppress; let severity determine action

### When Root Cause Unresolved
- Mark as "unresolved"
- Escalate if severity high or medium
- Request manual investigation
- Don't block if only medium severity

### When Multiple Critical Faults
- Aggregate all into single block action
- Provide full fault report
- Escalate to system alert

---

## AUDIT-DRIVEN FEEDBACK LOOP

The AuditingAgent not only validates but also drives improvement:

1. **Pattern Detection:** New fault patterns added to library
2. **Threshold Tuning:** Repeated false positives reduce threshold sensitivity
3. **Rule Evolution:** Confirmed faults tighten rules for that agent/mode
4. **Model Learning:** Calibration updates based on false alarm rates
5. **System Alerts:** Systemic issues trigger architectural review

---

**Version:** 1.0 (Complete Specification, Pre-Optimization)
**Last Updated:** March 28, 2026
**Next Steps:** Map to Company Pi operations, integrate with agent pipeline, implement


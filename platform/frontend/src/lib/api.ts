import type { components } from "./generated/openapi-types";
import { AppProtocolClient, isRpcConnectionError, type RpcNotificationHandler } from "./appProtocolClient";

type ApiSchemas = components["schemas"];
type RequiredKeys<T, K extends keyof T> = T & { [P in K]-?: NonNullable<T[P]> };

export type Workspace = {
  workspace_id: string;
  name: string;
  description?: string | null;
  template_cloned: boolean;
  current_revision_id?: string | null;
  revisions?: Array<{
    revision_id: string;
    commit_sha: string;
    message: string;
    source: string;
    created_at: string;
  }>;
};

export type PreviewRuntimeInfo = {
  url?: string | null;
  role_urls?: Record<string, string>;
  runtime_mode?: string;
  status?: string;
  stage?: string;
  progress_percent?: number;
  draft_run_id?: string | null;
  latency_breakdown?: Record<string, number>;
  last_error?: string | null;
};

export type Run = {
  run_id: string;
  workspace_id: string;
  prompt: string;
  mode?: "generate" | "fix";
  edit_mode?: "default" | "improve";
  generation_mode?: "fast" | "balanced" | "quality" | "production" | "basic";
  intent: "create" | "edit" | "refine" | "role_only_change";
  apply_strategy: "staged_auto_apply" | "manual_approve";
  target_role_scope: Array<"client" | "specialist" | "manager">;
  model_profile: string;
  llm_provider?: string | null;
  llm_model?: string | null;
  linked_job_id?: string | null;
  resume_from_run_id?: string | null;
  session_id?: string | null;
  resume_bookmark_id?: string | null;
  forked_from_run_id?: string | null;
  source_revision_id?: string | null;
  result_revision_id?: string | null;
  candidate_revision_id?: string | null;
  status: "pending" | "running" | "awaiting_approval" | "completed" | "blocked" | "failed";
  apply_status: "pending" | "applied" | "awaiting_approval" | "blocked" | "failed" | "rolled_back" | "noop";
  draft_status: "none" | "ready" | "approved" | "discarded" | "failed";
  draft_ready: boolean;
  approval_required: boolean;
  iteration_count: number;
  current_stage: string;
  progress_percent: number;
  summary?: string | null;
  failure_reason?: string | null;
  failure_class?: string | null;
  failure_signature?: string | null;
  root_cause_summary?: string | null;
  current_fix_phase?: string | null;
  current_failing_command?: string | null;
  current_exit_code?: number | null;
  fix_targets?: string[];
  handoff_from_failed_generate?: {
    mode?: "generate" | "fix";
    prompt?: string;
    error_context?: {
      raw_error: string;
      source?: "build" | "preview" | "miniapp" | "frontend" | "runtime" | null;
      failing_target?: string | null;
    } | null;
    failure_class?: string | null;
  } | null;
  error_context?: {
    raw_error: string;
    source?: "build" | "preview" | "miniapp" | "frontend" | "runtime" | null;
    failing_target?: string | null;
  } | null;
  checks_summary: {
    validators: "pending" | "passed" | "failed" | "blocked" | "skipped";
    build: "pending" | "passed" | "failed" | "blocked" | "skipped";
    preview: "pending" | "passed" | "failed" | "blocked" | "skipped";
    issues: Array<{ code?: string; message?: string; severity?: string }>;
  };
  touched_files: string[];
  role_coverage?: Record<string, unknown>;
  generated_tests?: Record<string, unknown>;
  neutral_template_findings?: Array<Record<string, unknown>>;
  orchestration_phases?: Array<Record<string, unknown>>;
  implementation_plan?: Record<string, unknown>;
  product_blueprint?: Record<string, unknown> | null;
  agent_activity_events?: Array<{
    type?: string;
    message?: string;
    created_at?: string;
    details?: Record<string, unknown>;
    batch_id?: string;
    worker?: string | null;
    worker_id?: string | null;
    owner_scope?: string | null;
    tool_use_id?: string;
    phase?: string;
    elapsed_ms?: number;
    artifact_ref?: string | null;
    label?: string;
    summary?: string;
    duration_ms?: number;
    status?: string;
    hook?: string;
    semantic_status?: string;
  }>;
  agent_memory?: Record<string, unknown>;
  acceptance_contract?: Record<string, unknown>;
  worker_summaries?: Array<Record<string, unknown>>;
  flow_coverage?: Record<string, unknown>;
  browser_flow_proof?: Record<string, unknown>;
  agent_transcript_ref?: string | null;
  tool_trace_ref?: string | null;
  file_change_history_ref?: string | null;
  browser_proof_ref?: string | null;
  browser_replay_proof_ref?: string | null;
  acceptance_tests_ref?: string | null;
  acceptance_test_files?: string[];
  acceptance_replay_source_ref?: string | null;
  large_tool_outputs_ref?: string | null;
  file_state_cache_ref?: string | null;
  turn_diff_ref?: string | null;
  environment_snapshot_ref?: string | null;
  tool_batch_summaries_ref?: string | null;
  worker_mailbox_ref?: string | null;
  scratchpad_ref?: string | null;
  memory_ref?: string | null;
  worker_drafts_ref?: string | null;
  worker_merge_ref?: string | null;
  trace_bundle_ref?: string | null;
  trace_reducer_ref?: string | null;
  command_policy_ref?: string | null;
  verification_report_ref?: string | null;
  rollout_trace_ref?: string | null;
  exec_trace_ref?: string | null;
  process_outputs_ref?: string | null;
  tool_result_messages_ref?: string | null;
  active_processes?: Array<Record<string, unknown>>;
  artifact_read_trace_ref?: string | null;
  resume_checkpoint_ref?: string | null;
  worker_branch_refs?: Array<string>;
  verifier_review_ref?: string | null;
  browser_step_refs?: Array<string>;
  active_tool_uses?: Array<Record<string, unknown>>;
  context_pressure_ref?: string | null;
  existing_app_map_ref?: string | null;
  improve_slice_ref?: string | null;
  hook_trace_ref?: string | null;
  semantic_graph_ref?: string | null;
  worker_prefix_ref?: string | null;
  replay_trace_ref?: string | null;
  product_blueprint_ref?: string | null;
  miniapp_contract_ref?: string | null;
  route_registry_ref?: string | null;
  contract_compile_ref?: string | null;
  repair_recipes_ref?: string | null;
  repair_issue_signatures?: Array<Record<string, unknown>>;
  mobile_layout_report?: Record<string, unknown>;
  artifacts: Record<string, string>;
  repair_iterations?: Array<Record<string, unknown>>;
  token_usage?: {
    input_tokens?: number;
    output_tokens?: number;
    reasoning_tokens?: number;
    total_tokens?: number;
    turn_count?: number;
    last_turn?: Record<string, unknown>;
  };
  rolled_back: boolean;
  rolled_back_at?: string | null;
  created_at: string;
  updated_at: string;
};

export type RunArtifacts = {
  run: Run;
  job?: {
    job_id: string;
    status: string;
    compile_summary?: Record<string, number | string>;
    summary?: string | null;
    assumptions_report?: Array<{ text?: string; rationale?: string }>;
    validation_snapshot?: {
      platform_valid: boolean;
      checks_valid: boolean;
      build_valid: boolean;
      blocking: boolean;
      issues: Array<{ code: string; message: string; severity?: string }>;
    } | null;
  };
  validation?: Record<string, unknown> | null;
  trace?: { entries?: Array<{ stage: string; message: string; created_at?: string }> } | null;
  agent_diagnostics?: { items?: Array<Record<string, unknown>> } | null;
  iterations?: Array<{
    iteration_id: string;
    assistant_message: string;
    files_read: string[];
    file_changes: Array<{ file_path: string; operation: "create" | "replace" | "delete" | "patch"; reason: string }>;
    check_results: Array<{ name: string; status: string; details?: string | null }>;
    diff_summary?: string | null;
    role_scope: Array<"client" | "specialist" | "manager">;
    created_at: string;
  }>;
  check_results?: Array<{ name: string; status: string; details?: string | null }>;
  role_coverage?: Record<string, unknown>;
  generated_tests?: Record<string, unknown>;
  neutral_template_findings?: Array<Record<string, unknown>>;
  orchestration_phases?: Array<Record<string, unknown>>;
  implementation_plan?: Record<string, unknown>;
  product_blueprint?: Record<string, unknown> | null;
  agent_activity_events?: Array<{
    type?: string;
    message?: string;
    created_at?: string;
    details?: Record<string, unknown>;
    batch_id?: string;
    worker?: string | null;
    worker_id?: string | null;
    owner_scope?: string | null;
    tool_use_id?: string;
    phase?: string;
    elapsed_ms?: number;
    artifact_ref?: string | null;
    label?: string;
    summary?: string;
    duration_ms?: number;
    status?: string;
    hook?: string;
    semantic_status?: string;
  }>;
  agent_memory?: Record<string, unknown>;
  acceptance_contract?: Record<string, unknown>;
  worker_summaries?: Array<Record<string, unknown>>;
  flow_coverage?: Record<string, unknown>;
  browser_flow_proof?: Record<string, unknown>;
  agent_transcript_ref?: string | null;
  tool_trace_ref?: string | null;
  file_change_history_ref?: string | null;
  browser_proof_ref?: string | null;
  browser_replay_proof_ref?: string | null;
  acceptance_tests_ref?: string | null;
  acceptance_test_files?: string[];
  acceptance_replay_source_ref?: string | null;
  large_tool_outputs_ref?: string | null;
  file_state_cache_ref?: string | null;
  turn_diff_ref?: string | null;
  environment_snapshot_ref?: string | null;
  tool_batch_summaries_ref?: string | null;
  worker_mailbox_ref?: string | null;
  scratchpad_ref?: string | null;
  memory_ref?: string | null;
  worker_drafts_ref?: string | null;
  worker_merge_ref?: string | null;
  trace_bundle_ref?: string | null;
  trace_reducer_ref?: string | null;
  command_policy_ref?: string | null;
  verification_report_ref?: string | null;
  rollout_trace_ref?: string | null;
  exec_trace_ref?: string | null;
  process_outputs_ref?: string | null;
  tool_result_messages_ref?: string | null;
  active_processes?: Array<Record<string, unknown>>;
  artifact_read_trace_ref?: string | null;
  resume_checkpoint_ref?: string | null;
  worker_branch_refs?: Array<string>;
  verifier_review_ref?: string | null;
  browser_step_refs?: Array<string>;
  active_tool_uses?: Array<Record<string, unknown>>;
  context_pressure_ref?: string | null;
  hook_trace_ref?: string | null;
  semantic_graph_ref?: string | null;
  worker_prefix_ref?: string | null;
  replay_trace_ref?: string | null;
  product_blueprint_ref?: string | null;
  repair_issue_signatures?: Array<Record<string, unknown>>;
  mobile_layout_report?: Record<string, unknown>;
  draft_preview?: {
    status: string;
    runtime_mode: string;
    url?: string | null;
    role_urls?: Record<string, string>;
  };
  diff?: string;
  failure_analysis?: {
    mode?: "generate" | "fix";
    failure_class?: string | null;
    failure_signature?: string | null;
    root_cause_summary?: string | null;
    fix_targets?: string[];
    handoff_from_failed_generate?: Run["handoff_from_failed_generate"] | null;
    error_context?: Run["error_context"] | null;
    current_fix_phase?: string | null;
    current_failing_command?: string | null;
    current_exit_code?: number | null;
    executed_checks?: Array<Record<string, unknown>>;
    container_statuses?: Array<Record<string, unknown>>;
  } | null;
  fix_case?: Record<string, unknown> | null;
  fix_runtime?: Record<string, unknown> | null;
  acceptance_tests?: Record<string, unknown> | null;
  preview?: {
    status: string;
    runtime_mode: string;
    url?: string | null;
    role_urls?: Record<string, string>;
    logs?: string[];
    draft_run_id?: string | null;
  };
};

export type WorkspaceLogs = {
  workspace_id: string;
  job: {
    job_id: string;
    status: string;
    generation_mode?: string;
    fidelity?: string;
    llm_model?: string | null;
    llm_provider?: string | null;
    failure_reason?: string | null;
    failure_class?: string | null;
    failure_signature?: string | null;
    current_fix_phase?: string | null;
    current_failing_command?: string | null;
    current_exit_code?: number | null;
  } | null;
  events: Array<{
    event_id: string;
    event_type: string;
    message: string;
    created_at: string;
    details?: Record<string, unknown>;
  }>;
  workspace_logs?: string[];
  preview: {
    status: string;
    runtime_mode: string;
    url: string | null;
    logs: string[];
    draft_run_id?: string | null;
    mini_app_logs?: string[];
  };
  reports: {
    trace?: {
      workspace_id: string;
      entries: Array<{
        stage: string;
        message: string;
        created_at: string;
        payload?: Record<string, unknown>;
      }>;
    } | null;
    validation?: Record<string, unknown> | null;
    iterations?: Record<string, unknown> | null;
    candidate_diff?: Record<string, unknown> | null;
    check_results?: Record<string, unknown> | null;
    fix_case?: Record<string, unknown> | null;
    fix_runtime?: Record<string, unknown> | null;
  };
};

export type SystemConfiguration = {
  llm: {
    enabled: boolean;
    provider?: string | null;
    models?: Record<string, unknown>;
    task_profiles?: Record<string, unknown>;
    routing?: Record<string, unknown>;
    provider_routing?: Record<string, unknown>;
    model_manager?: Record<string, unknown>;
    model_catalog?: Record<string, unknown>;
  };
  defaults: {
    generation_mode: "fast" | "balanced" | "quality" | "production" | "basic";
    model_profile: string;
  };
  default_coding_profile: string;
  supports_staged_apply: boolean;
  research_artifacts_enabled: boolean;
};

export type ObservabilityReport = {
  schema: string;
  status: string;
  workspace_id?: string | null;
  generated_at: string;
  run_count: number;
  completed_runs: number;
  failed_runs: number;
  blocked_runs: number;
  running_runs?: number;
  awaiting_approval_runs?: number;
  token_usage_total: number;
  latency_ms_total: number;
  token_usage: {
    input_tokens: number;
    output_tokens: number;
    reasoning_tokens: number;
    total_tokens: number;
    turn_count: number;
    cached_tokens?: number;
    cache_write_tokens?: number;
  };
  cost: {
    estimated_cost_usd: number;
    explicit_cost_usd?: number;
    estimated_from_tokens_usd?: number;
    unpriced_tokens?: number;
    pricing_source?: string;
    by_model?: Array<Record<string, unknown>>;
  };
  latency: {
    total_ms: number;
    average_ms: number;
    p50_ms: number;
    p95_ms: number;
    phase_totals_ms: Record<string, number>;
    slowest_runs?: Array<Record<string, unknown>>;
  };
  green_rate_by_generation_mode: Array<{
    generation_mode: string;
    run_count: number;
    terminal_count: number;
    green_count: number;
    green_rate: number;
    status_counts: Record<string, number>;
    average_total_tokens?: number;
    estimated_cost_usd?: number;
  }>;
  failure_classes: Array<{
    failure_class: string;
    count: number;
    latest_run_id?: string | null;
    latest_at?: string | null;
    generation_modes?: Record<string, number>;
    examples?: Array<Record<string, unknown>>;
  }>;
  repair_success: {
    fix_run_count: number;
    successful_fix_runs: number;
    fix_success_rate: number;
    repair_case_count: number;
    resolved_case_count: number;
    case_resolution_rate: number;
    attempt_count: number;
    successful_attempt_count: number;
    attempt_success_rate: number;
    status_counts?: Record<string, number>;
  };
  by_status: Record<string, number>;
};

export type AgentThread = {
  thread_id: string;
  workspace_id: string;
  title: string;
  status: "active" | "running" | "idle" | "archived" | "failed";
  archived: boolean;
  current_turn_id?: string | null;
  created_at: string;
  updated_at: string;
};

export type AgentTurn = {
  turn_id: string;
  thread_id: string;
  workspace_id: string;
  kind: "user" | "agent" | "review" | "compaction" | "repair";
  status: "queued" | "running" | "completed" | "failed" | "interrupted";
  prompt: string;
  linked_run_id?: string | null;
  metadata?: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

export type AgentItem = {
  item_id: string;
  thread_id: string;
  turn_id?: string | null;
  item_type: string;
  status: "started" | "completed" | "failed";
  sequence: number;
  payload: Record<string, unknown>;
  created_at: string;
};

export type RunTimeline = RequiredKeys<ApiSchemas["RunTimelineReport"], "items">;
export type RunTraceView = RequiredKeys<ApiSchemas["RunTraceViewReport"], "timeline" | "artifact_refs" | "reduced_trace">;
export type TraceBundleReport = RequiredKeys<ApiSchemas["TraceBundleReport"], "state">;
export type TraceBundleState = ApiSchemas["TraceState"];
export type RolloutTraceEvidence = {
  schema: string;
  run_id: string;
  workspace_id: string;
  status: string;
  principle: string;
  raw_events: Array<Record<string, unknown>>;
  payload_refs: Array<Record<string, unknown>>;
  evidence_streams: Record<string, unknown>;
  reduced_graph: { nodes?: Array<Record<string, unknown>>; edges?: Array<Record<string, unknown>>; [key: string]: unknown };
  inference_calls: Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  terminal_ops: Array<Record<string, unknown>>;
  child_agents: Array<Record<string, unknown>>;
  interpretations: Record<string, unknown>;
  repair_learning_hooks: Record<string, unknown>;
  counts: Record<string, number>;
};
export type RunProtocolEvent = ApiSchemas["RunProtocolEvent"];
export type RunBookmark = ApiSchemas["RunBookmark"];
export type RunProtocolReport = RequiredKeys<ApiSchemas["RunProtocolReport"], "items" | "bookmarks">;
export type RunEventsReport = RequiredKeys<ApiSchemas["RunEventsReport"], "items" | "protocol_events" | "compaction_events" | "state_snapshots">;
export type RunEventV2 = ApiSchemas["RunEventV2"];
export type ThreadEventV2 = ApiSchemas["ThreadEventV2"];
export type EventJournalPage = RequiredKeys<ApiSchemas["EventJournalPage"], "items">;
export type EventJournalPayload = ApiSchemas["EventJournalPayload"];
export type RunJournalState = RequiredKeys<ApiSchemas["RunJournalState"], "timeline">;
export type ThreadJournalState = RequiredKeys<ApiSchemas["ThreadJournalState"], "events">;
export type RunBookmarksReport = RequiredKeys<ApiSchemas["RunBookmarksReport"], "items">;
export type ArtifactRef = ApiSchemas["ArtifactRef"];
export type CheckResult = ApiSchemas["CheckResult"];

export type RunCompactionReport = {
  schema: string;
  status: string;
  run_id: string;
  workspace_id?: string;
  boundary_id?: string;
  compaction_ref?: string;
  boundary_ref?: string;
  post_compact_message_ref?: string;
  post_compact_status?: string;
  consumed_by_turn_id?: string | null;
  sections?: Record<string, unknown>;
  refs?: Record<string, string | null | undefined>;
  preserved_tail?: Record<string, unknown>;
  stale_path_refs?: Array<Record<string, unknown>>;
  phase_budget?: Record<string, unknown>;
  created_at?: string;
};

export type ContextPressureReport = RequiredKeys<ApiSchemas["ContextPressureReport"], "items" | "recommendations">;
export type OutputArtifactIndex = RequiredKeys<ApiSchemas["OutputArtifactIndex"], "items">;
export type PromptSuggestion = ApiSchemas["PromptSuggestion"];
export type PromptSuggestionsReport = RequiredKeys<ApiSchemas["PromptSuggestionsReport"], "items">;

export type RunCompactionBoundaries = {
  schema: string;
  status: string;
  run_id: string;
  items: Array<Record<string, unknown>>;
};

export type DoctorReport = {
  status: string;
  checks: Array<{
    name: string;
    status: string;
    details?: string;
    command?: string;
    required?: boolean;
  }>;
  created_at?: string;
};

export type WorkerReport = {
  schema?: string;
  run_id: string;
  workspace_id?: string;
  workers: Array<{
    worker_id: string;
    worker_type?: string;
    alias_ids?: string[];
    status: string;
    badge?: string;
    owner_scope: string;
    ownership?: Record<string, unknown>;
    changed_files: string[];
    summaries?: Array<Record<string, unknown>>;
    merge_reports?: Array<Record<string, unknown>>;
    disabled_reason?: string | null;
    context_ref?: string | null;
    memory_snapshot_ref?: string | null;
    output_ref?: string | null;
    task_id?: string | null;
    proof_refs?: string[];
    merge_decision_ref?: string | null;
    merge_decision?: Record<string, unknown> | null;
  }>;
  worker_branch_refs?: string[];
  merge_decision_ref?: string | null;
  mailbox?: Record<string, unknown>;
};

export type WorkerOrchestrationReport = {
  schema?: string;
  branch_schema?: string;
  run_id: string;
  workspace_id: string;
  artifact_run_id?: string;
  status: string;
  write_coordination?: string | null;
  write_scope_report?: Record<string, unknown>;
  worker_drafts_ref?: string | null;
  worker_drafts?: Record<string, unknown>;
  worker_merge_ref?: string | null;
  worker_merge?: Record<string, unknown>;
  merge_decision_ref?: string | null;
  merge_decision?: Record<string, unknown>;
  workers?: WorkerReport["workers"];
  worker_memory_refs?: Array<string | null | undefined>;
  worker_artifact_refs?: Array<string | null | undefined>;
  post_merge_verifier?: Record<string, unknown>;
  mailbox?: Record<string, unknown>;
  branch_plan?: Array<Record<string, unknown>>;
};

export type SubagentForkContract = {
  schema: string;
  status: string;
  generation_mode?: string;
  principle?: string;
  lanes: Array<{
    lane_id: string;
    worker_ids: string[];
    branch_role: string;
    stage: string;
    ownership: string;
    tool_allowlist: string[];
    file_scope: {
      allowed_paths: string[];
      forbidden_paths: string[];
      exclusive_write: boolean;
    };
    writes: boolean;
    dependencies: string[];
    required_proof: string[];
    child_lanes?: Array<Record<string, unknown>>;
  }>;
  execution_order: string[];
  quality_mode_policy?: Record<string, unknown>;
  ownership_matrix?: Array<Record<string, unknown>>;
  conflict_policy?: Record<string, unknown>;
  plan_bindings?: Record<string, unknown>;
};

export type RunTaskReport = {
  schema?: string;
  run_id: string;
  workspace_id: string;
  status: string;
  items: Array<{
    task_id: string;
    title: string;
    phase?: string;
    status: "planned" | "in_progress" | "blocked" | "completed" | string;
    owner?: string;
    files?: string[];
    proof?: Record<string, unknown> | string;
    proof_status?: string;
    proof_checks?: string[];
    role?: string | null;
    blocker?: Record<string, unknown> | string | null;
    artifact_refs?: Record<string, string | null | undefined>;
    updated_at?: string | null;
    source?: string;
    background_status?: string;
    attempt?: number;
    max_attempts?: number;
    output_summary?: string | null;
    linked_refs?: Record<string, unknown>;
  }>;
  task_ledger?: {
    schema?: string;
    source?: string;
    status?: string;
    counts?: Record<string, number>;
    items?: RunTaskReport["items"];
    updated_at?: string;
  };
  repair_cases?: RunRepairCases;
};

export type SkillifyReport = {
  schema: string;
  status: string;
  write_status: "preview" | "written" | string;
  run_id: string;
  workspace_id: string;
  skill_id: string;
  title: string;
  scope: string;
  target_path: string;
  content: string;
  evidence?: Record<string, unknown>;
  warnings?: Array<Record<string, unknown>>;
  created_at?: string;
};

export type SimplifyReport = {
  schema: string;
  run_id: string;
  workspace_id: string;
  status: string;
  green_required: boolean;
  green_status: Record<string, unknown>;
  changed_files: string[];
  reviewed_files: string[];
  summary: {
    finding_count: number;
    safe_task_count: number;
    categories: Record<string, number>;
  };
  findings: Array<Record<string, unknown>>;
  safe_refactor_tasks: Array<Record<string, unknown>>;
  completion_gate: Record<string, unknown>;
};

export type DiagnosticWorkflowReport = {
  schema: string;
  mode: "debug_run" | "stuck_run" | "doctor_workspace" | string;
  workspace_id: string;
  run_id?: string | null;
  status: string;
  workspace_root?: string;
  evidence: Record<string, unknown>;
  diagnosis: Record<string, unknown>;
  repair_packet: {
    schema: string;
    source: string;
    failure_class: string;
    failure_signature: string;
    issue_code: string;
    severity: string;
    summary?: string;
    instruction: string;
    target_files: string[];
    owner: string;
    required_next_tool: string;
    suggested_tool_after_read: string;
    proof_required: string[];
    evidence?: Record<string, unknown>;
  };
};

export type BackgroundTask = {
  task_id: string;
  workspace_id: string;
  run_id?: string | null;
  parent_task_id?: string | null;
  type: string;
  status: string;
  title: string;
  owner?: string;
  input?: Record<string, unknown>;
  output_summary?: string | null;
  error?: string | null;
  attempt?: number;
  max_attempts?: number;
  linked_refs?: Record<string, unknown>;
  created_at?: string;
  started_at?: string | null;
  completed_at?: string | null;
  updated_at?: string;
};

export type BackgroundTaskOutput = {
  schema: string;
  task_id: string;
  items: Array<{
    sequence: number;
    event_type: string;
    message: string;
    payload?: Record<string, unknown>;
    created_at: string;
  }>;
  next_cursor: number;
  has_more: boolean;
};

export type PrBabysitterReport = {
  schema: string;
  workspace_id: string;
  run_id?: string | null;
  export_id?: string | null;
  status: string;
  pr?: Record<string, unknown>;
  checks?: Record<string, unknown>;
  failed_runs?: Array<Record<string, unknown>>;
  new_review_items?: Array<Record<string, unknown>>;
  actions?: string[];
  retry_state?: Record<string, unknown>;
  failure_diagnostics?: Record<string, unknown>;
  automation_plan?: Record<string, unknown>;
  blocker?: Record<string, unknown>;
};

export type ReviewReport = {
  schema?: string;
  run_id: string;
  workspace_id?: string;
  status: string;
  summary?: {
    finding_count?: number;
    blocker_count?: number;
    severity_counts?: Record<string, number>;
    missing_tests?: number;
    stale_test_risks?: number;
    browser_proof_gaps?: number;
    contract_mismatches?: number;
  };
  findings: Array<{
    code?: string;
    severity?: string;
    category?: string;
    source?: string;
    message?: string;
    file_path?: string;
    path?: string;
    line?: number;
    is_blocker_for_product_acceptance?: boolean;
    evidence?: Record<string, unknown>;
  } & Record<string, unknown>>;
  evidence?: Record<string, unknown>;
};

export type TestMatrixReport = {
  run_id: string;
  workspace_id: string;
  status: string;
  items: Array<{
    key: string;
    label: string;
    status: string;
    required: boolean;
    evidence?: Array<Record<string, unknown>>;
  }>;
};

export type PromptContractReport = {
  run_id: string;
  status: string;
  analysis_source?: string | null;
  analysis_status?: string | null;
  resource_hint?: string | null;
  field_hints?: string[];
  role_field_hints?: Record<string, string[]>;
  findings: Array<Record<string, unknown>>;
};

export type MiniAppContractReport = {
  run_id: string;
  workspace_id: string;
  status: string;
  contract: Record<string, unknown>;
  registry_snapshot: Record<string, unknown>;
  drift_issues: Array<Record<string, unknown>>;
  repair_recipes: Array<Record<string, unknown>>;
  artifact_refs: Record<string, string | null>;
};

export type RunStateReport = {
  schema: string;
  run_id: string;
  workspace_id: string;
  status: string;
  blocking: boolean;
  terminal: boolean;
  apply_ok: boolean;
  manual_approval_ok: boolean;
  gate_status: string;
  gate_blocking: boolean;
  issues: Array<Record<string, unknown>>;
  invariant_issues: Array<Record<string, unknown>>;
  source_state: Record<string, unknown>;
  artifact_refs?: Record<string, string | null | undefined>;
  created_at?: string;
};

export type WorkspaceMemory = {
  workspace_id: string;
  items: Array<{
    memory_id?: string;
    kind: string;
    text: string;
    status?: string;
    confidence?: Record<string, unknown>;
    payload?: Record<string, unknown>;
    citation?: Record<string, unknown> | null;
    created_at?: string;
  }>;
  project_rules?: Array<string | Record<string, unknown>>;
  user_preferences?: Array<string | Record<string, unknown>>;
  product_decisions?: Array<string | Record<string, unknown>>;
  failure_shields?: Array<string | Record<string, unknown>>;
  reusable_workflows?: Array<string | Record<string, unknown>>;
  platform_constraints?: Array<string | Record<string, unknown>>;
  repeated_fixes?: Array<string | Record<string, unknown>>;
  memory_summary?: MemorySummaryReport | null;
  session_memory?: SessionMemoryReport | null;
  pipeline?: MemoryPipelineReport;
  stale_check?: Record<string, unknown>;
};

export type SessionMemoryReport = {
  schema?: string;
  workspace_id: string;
  status: string;
  sections: Array<{
    id: string;
    title: string;
    items: Array<{
      text?: string;
      source?: string;
      ref?: string | null;
      [key: string]: unknown;
    }>;
  }>;
  counts?: Record<string, number>;
  text?: string;
  source_refs?: Record<string, unknown>;
  generated_at?: string;
};

export type MemorySummaryReport = {
  schema?: string;
  workspace_id: string;
  status: string;
  always_loaded?: boolean;
  generated_at?: string;
  text?: string;
  sections?: Array<{
    kind: string;
    title: string;
    items: Array<Record<string, unknown>>;
  }>;
  counts?: Record<string, number>;
  stale?: Record<string, unknown>;
  detail_retrieval?: Record<string, unknown>;
  source_refs?: Record<string, unknown>;
};

export type MemoryPipelineReport = {
  schema?: string;
  workspace_id: string;
  status: string;
  stage1_count?: number;
  stage1_items?: number;
  active_count?: number;
  stale_count?: number;
  expired_count?: number;
  superseded_count?: number;
  retrieval_schema?: string;
  summary_schema?: string;
  summary?: MemorySummaryReport;
  consolidated_at?: string | null;
  items?: Array<Record<string, unknown>>;
};

export type ApprovalRecord = {
  approval_id: string;
  status: "pending" | "approved" | "rejected" | string;
  kind?: string;
  risk?: string;
  summary?: string;
  input?: Record<string, unknown>;
  policy_decision?: Record<string, unknown>;
  created_at?: string;
  decided_at?: string;
};

export type ToolEventEnvelope = RequiredKeys<
  ApiSchemas["ToolEnvelope"],
  "input" | "approval" | "progress" | "result" | "artifacts" | "timing" | "changed_files" | "repair_recipe_ids" | "retry" | "truncation"
>;
export type ToolEventsReport = RequiredKeys<ApiSchemas["ToolEventsReport"], "events">;

export type ProductReadinessChecklistItem = {
  key: string;
  label: string;
  status: string;
  required?: boolean;
  check?: string | null;
  details?: string | null;
  evidence?: Record<string, unknown>;
};

export type ProductReadinessResult = {
  schema?: string;
  status: string;
  acceptance_required?: boolean;
  required_checks?: ProductReadinessChecklistItem[];
  checklist?: ProductReadinessChecklistItem[];
  evidence?: Record<string, unknown>;
  blocking_reasons?: Array<Record<string, unknown>>;
  repair_case_ids?: string[];
  next_forced_action?: Record<string, unknown>;
};

export type BrowserProofScenario = {
  scenario_id?: string;
  title?: string;
  status?: string;
  role?: string | null;
  route?: string | null;
  action?: string;
  marker?: unknown;
  evidence?: Record<string, unknown>;
};

export type BrowserProofReport = {
  schema?: string;
  run_id: string;
  workspace_id: string;
  status: string;
  blocking?: boolean;
  summary?: Record<string, unknown>;
  proof_statement?: string;
  final_artifact?: boolean;
  scenarios?: BrowserProofScenario[];
  issues?: Array<Record<string, unknown>>;
  roles_checked?: string[];
  screenshots?: string[];
  screenshot_before?: string | null;
  screenshot_after?: string | null;
  playwright_scenario?: Record<string, unknown>;
  failed_step_context?: Record<string, unknown>;
  dom_selector?: string | null;
  video_refs?: string[];
  console_errors?: string[];
  network_errors?: string[];
  mobile_layout?: Record<string, unknown>;
  viewports?: string[];
  artifact_refs?: Record<string, string | null | undefined>;
  created_at?: string;
};

export type BrowserReplayReport = {
  schema?: string;
  run_id: string;
  workspace_id: string;
  status: string;
  items?: Array<Record<string, unknown>>;
  latest_packet?: Record<string, unknown>;
  replay_first?: boolean;
  replay_plan?: Record<string, unknown>;
};

export type VisualRegressionReport = ApiSchemas["VisualRegressionReport"];
export type GenerationModeSlaManifest = ApiSchemas["GenerationModeSlaManifest"];

export type RunGateReport = RequiredKeys<
  ApiSchemas["GateReport"],
  "issues" | "repair_packets" | "repair_history" | "next_forced_action" | "blocking_repair_packet" | "requirements" | "artifact_refs" | "run_state"
> & {
  product_readiness?: ProductReadinessResult | null;
  auto_repair_continuation?: Record<string, unknown>;
};

export type RunRepairSignatures = {
  run_id: string;
  status: string;
  blocking: boolean;
  items: Array<Record<string, unknown>>;
  catalog: Array<Record<string, unknown>>;
  repair_cases?: RunRepairCases;
};

export type RunRepairCase = RequiredKeys<
  ApiSchemas["RepairCase"],
  "target_files" | "forbidden_files" | "allowed_edit_slice" | "expected_proof" | "retry_policy" | "attempts" | "evidence" | "next_action"
>;

export type RunRepairCases = ApiSchemas["RepairCasesReport"] & {
  schema: string;
  run_id: string;
  status: string;
  items: RunRepairCase[];
  case_refs: string[];
  active_case?: RunRepairCase | null;
};
export type RepairAttemptsReport = RequiredKeys<ApiSchemas["RepairAttemptsReport"], "items">;

export type RunFinalReport = {
  run_id: string;
  workspace_id: string;
  status: string;
  blocking: boolean;
  prompt: string;
  summary?: string | null;
  acceptance_contract: Record<string, unknown>;
  diff_summary: Record<string, unknown>;
  checks: Array<Record<string, unknown>>;
  product_readiness?: ProductReadinessResult;
  browser_proof: Record<string, unknown>;
  repair_signatures: Array<Record<string, unknown>>;
  preview: Record<string, unknown>;
  artifact_refs: Record<string, string | null | undefined>;
  created_at: string;
};

export type StagedApplyState = {
  run_id: string;
  files: string[];
  categories?: Record<string, string>;
  status: string;
  updated_at?: string;
};

export type FileSearchResult = {
  workspace_id: string;
  run_id?: string | null;
  query: string;
  items: Array<{
    path: string;
    hits: Array<{ line: number; text: string }>;
    score?: number;
    language?: string;
    symbols?: Array<{ kind: string; name: string; line: number }>;
  }>;
  symbols?: Array<{ path: string; kind: string; name: string; line: number }>;
};

export type LspDiagnosticsReport = {
  schema?: string;
  workspace_id: string;
  run_id?: string | null;
  status: string;
  engine?: string;
  items: Array<{ path: string; file?: string; severity: string; message: string; source: string; line?: number; column?: number; code?: string; jump?: { path: string; line: number; column?: number; label?: string } }>;
  symbols?: Array<{ path: string; kind: string; name: string; line: number; column?: number; jump?: { path: string; line: number; column?: number; label?: string } }>;
  tool_status?: Record<string, unknown>;
  diagnostic_stream?: Array<{ phase: string; status: string; created_at?: string; issue_count?: number; error_count?: number; warning_count?: number; file_count?: number } & Record<string, unknown>>;
  route_graph?: LspRouteGraphReport;
  error_count?: number;
  warning_count?: number;
  changed_only?: boolean;
  changed_files?: string[];
};

export type LspDiagnosticsTaskReport = {
  schema: string;
  status: string;
  workspace_id: string;
  run_id?: string | null;
  task?: BackgroundTask | null;
  output?: BackgroundTaskOutput;
  diagnostics?: LspDiagnosticsReport | null;
  diagnostics_ref?: string | null;
  task_diagnostics_ref?: string | null;
};

export type LspDefinitionReport = {
  schema?: string;
  workspace_id?: string;
  run_id?: string | null;
  symbol: string;
  items: Array<{ path: string; kind: string; name: string; line: number; column?: number; excerpt?: string; definition_kind?: string; jump?: { path: string; line: number; column?: number; label?: string } }>;
};

export type LspRouteGraphReport = {
  schema?: string;
  workspace_id?: string;
  run_id?: string | null;
  nodes: Array<Record<string, unknown>>;
  edges: Array<{ from?: string; to?: string; kind?: string; status?: string; file?: string; method?: string; path?: string } & Record<string, unknown>>;
  missing_edges?: Array<Record<string, unknown>>;
  api_mismatches?: Array<Record<string, unknown>>;
  summary?: Record<string, unknown>;
  static_context?: Record<string, unknown>;
};

export type CommandPaletteAction = {
  id: string;
  label: string;
  description?: string;
  disabled?: boolean;
};

export type SlashCommand = {
  id: string;
  name: string;
  kind: string;
  description: string;
  requires: string[];
};

export type SlashCommandList = {
  schema: string;
  items: SlashCommand[];
};

export type SlashCommandExecution = GeneratedReport & {
  execution_id?: string;
  command_id?: string;
  workflow?: string;
  workspace_id?: string;
  run_id?: string | null;
  run?: Run;
  report?: Record<string, unknown>;
  exports?: Record<string, Record<string, unknown>>;
  artifact_refs?: Record<string, string | null | undefined>;
  next_forced_action?: Record<string, unknown>;
};

export type GeneratedReport = {
  schema?: string;
  status?: string;
  items?: unknown[];
  issues?: unknown[];
  content?: string;
  [key: string]: unknown;
};

const rpcClient = new AppProtocolClient();
const workspaceThreadIds = new Map<string, string>();
const runTurnRefs = new Map<string, { threadId: string; turnId: string }>();

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
    ...init,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function listWorkspaces(): Promise<Workspace[]> {
  return request<Workspace[]>("/workspaces");
}

export async function ensureWorkspace(): Promise<Workspace> {
  return request<Workspace>("/workspaces", {
    method: "POST",
    body: JSON.stringify({
      name: "Research Workspace",
      description: "Single-user grounded research session",
      target_platform: "telegram_mini_app",
      preview_profile: "telegram_mock",
    }),
  });
}

export async function openWorkspace(workspaceId: string): Promise<Workspace> {
  return request<Workspace>(`/workspaces/${workspaceId}`);
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  await request<{ deleted: string }>(`/workspaces/${workspaceId}`, {
    method: "DELETE",
  });
}

export async function listRuns(workspaceId: string): Promise<Run[]> {
  return request<Run[]>(`/workspaces/${workspaceId}/runs`);
}

export async function getObservabilitySummary(): Promise<ObservabilityReport> {
  return request<ObservabilityReport>("/system/observability");
}

export async function getWorkspaceObservability(workspaceId: string): Promise<ObservabilityReport> {
  return request<ObservabilityReport>(`/workspaces/${workspaceId}/observability`);
}

export async function getGenerationModes(): Promise<GenerationModeSlaManifest> {
  return request<GenerationModeSlaManifest>("/system/generation-modes");
}

export async function createRun(
  workspaceId: string,
  payload: {
    prompt: string;
    mode?: "generate" | "fix";
    edit_mode?: "default" | "improve";
    intent?: "auto" | "create" | "edit" | "refine" | "role_only_change";
    apply_strategy?: "staged_auto_apply" | "manual_approve";
    target_role_scope?: Array<"client" | "specialist" | "manager">;
    model_profile?: string;
    generation_mode?: "fast" | "balanced" | "quality" | "production" | "basic";
    target_platform?: "telegram_mini_app" | "max_mini_app";
    preview_profile?: "telegram_mock" | "max_mock" | "web_preview";
    resume_from_run_id?: string;
    error_context?: {
      raw_error: string;
      source?: "build" | "preview" | "miniapp" | "frontend" | "runtime";
      failing_target?: string;
    };
  },
): Promise<Run> {
  const runPayload = {
    mode: "generate",
    edit_mode: "default",
    intent: "auto",
    apply_strategy: "staged_auto_apply",
    target_role_scope: [],
    generation_mode: "balanced",
    target_platform: "telegram_mini_app",
    preview_profile: "telegram_mock",
    ...payload,
  };
  try {
    const threadId = await getOrCreateWorkspaceThread(workspaceId);
    const turn = await rpcClient.call<AgentTurn>("turn/start", {
      thread_id: threadId,
      ...runPayload,
    });
    const run = turn.metadata?.run as Run | undefined;
    if (!run?.run_id) {
      throw new Error("RPC turn did not return a linked run.");
    }
    runTurnRefs.set(run.run_id, { threadId, turnId: turn.turn_id });
    return run;
  } catch (error) {
    if (!isRpcConnectionError(error)) {
      throw error;
    }
    return request<Run>(`/workspaces/${workspaceId}/runs`, {
      method: "POST",
      body: JSON.stringify(runPayload),
    });
  }
}

export async function getRunArtifacts(runId: string): Promise<RunArtifacts> {
  return request<RunArtifacts>(`/runs/${runId}/artifacts`);
}

export async function getRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}`);
}

export async function getRunTimeline(runId: string): Promise<RunTimeline> {
  return request<RunTimeline>(`/runs/${runId}/timeline`);
}

export async function getRunTraceView(runId: string): Promise<RunTraceView> {
  return request<RunTraceView>(`/runs/${runId}/trace-view`);
}

export async function getRunRolloutTrace(runId: string): Promise<RolloutTraceEvidence> {
  return request<RolloutTraceEvidence>(`/runs/${runId}/rollout-trace`);
}

export async function getRunTraceBundle(runId: string): Promise<TraceBundleReport> {
  return request<TraceBundleReport>(`/runs/${runId}/trace-bundle`);
}

export async function getRunTraceBundleState(runId: string): Promise<TraceBundleState> {
  return request<TraceBundleState>(`/runs/${runId}/trace-bundle/state`);
}

export async function getRunProtocol(runId: string): Promise<RunProtocolReport> {
  return request<RunProtocolReport>(`/runs/${runId}/protocol`);
}

function journalQuery(params: { after_sequence?: number; limit?: number } = {}): string {
  const search = new URLSearchParams();
  if (params.after_sequence !== undefined) search.set("after_sequence", String(params.after_sequence));
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  const suffix = search.toString();
  return suffix ? `?${suffix}` : "";
}

export async function getRunEventsV2(runId: string, params: { after_sequence?: number; limit?: number } = {}): Promise<EventJournalPage> {
  return request<EventJournalPage>(`/runs/${runId}/events-v2${journalQuery(params)}`);
}

export async function getRunJournalState(runId: string): Promise<RunJournalState> {
  return request<RunJournalState>(`/runs/${runId}/journal/state`);
}

export async function getThreadEventsV2(threadId: string, params: { after_sequence?: number; limit?: number } = {}): Promise<EventJournalPage> {
  return request<EventJournalPage>(`/threads/${threadId}/events-v2${journalQuery(params)}`);
}

export async function getThreadJournalState(threadId: string): Promise<ThreadJournalState> {
  return request<ThreadJournalState>(`/threads/${threadId}/journal/state`);
}

export async function getEventJournalPayload(payloadRef: string): Promise<EventJournalPayload> {
  return request<EventJournalPayload>(`/event-payloads/${encodeURIComponent(payloadRef)}`);
}

export async function getRunBookmarks(runId: string): Promise<RunBookmarksReport> {
  return request<RunBookmarksReport>(`/runs/${runId}/bookmarks`);
}

export async function getRunTasks(runId: string): Promise<RunTaskReport> {
  return request<RunTaskReport>(`/runs/${runId}/tasks`);
}

export async function skillifyRun(runId: string, payload: { skill_id?: string; title?: string; write?: boolean; scope?: string } = {}): Promise<SkillifyReport> {
  return request<SkillifyReport>(`/runs/${runId}/skillify`, { method: "POST", body: JSON.stringify(payload) });
}

export async function listBackgroundTasks(params: { workspace_id?: string; run_id?: string; status?: string } = {}): Promise<{ schema: string; status: string; items: BackgroundTask[] }> {
  const search = new URLSearchParams();
  if (params.workspace_id) search.set("workspace_id", params.workspace_id);
  if (params.run_id) search.set("run_id", params.run_id);
  if (params.status) search.set("status", params.status);
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(`/tasks${suffix}`);
}

export async function getBackgroundTask(taskId: string): Promise<BackgroundTask> {
  return request<BackgroundTask>(`/tasks/${taskId}`);
}

export async function createBackgroundTask(payload: {
  workspace_id: string;
  type: string;
  title?: string;
  run_id?: string | null;
  parent_task_id?: string | null;
  input?: Record<string, unknown>;
  owner?: string;
  max_attempts?: number;
  auto_start?: boolean;
}): Promise<BackgroundTask> {
  return request<BackgroundTask>("/tasks", { method: "POST", body: JSON.stringify(payload) });
}

export async function updateBackgroundTask(taskId: string, payload: { title?: string; status?: string; metadata?: Record<string, unknown> }): Promise<BackgroundTask> {
  return request<BackgroundTask>(`/tasks/${taskId}`, { method: "PATCH", body: JSON.stringify(payload) });
}

export async function stopBackgroundTask(taskId: string): Promise<BackgroundTask> {
  return request<BackgroundTask>(`/tasks/${taskId}/stop`, { method: "POST" });
}

export async function retryBackgroundTask(taskId: string): Promise<BackgroundTask> {
  return request<BackgroundTask>(`/tasks/${taskId}/retry`, { method: "POST" });
}

export async function requeueBackgroundTask(taskId: string): Promise<BackgroundTask> {
  return request<BackgroundTask>(`/tasks/${taskId}/requeue`, { method: "POST" });
}

export async function getBackgroundTaskOutput(taskId: string, cursor = 0, limit = 100): Promise<BackgroundTaskOutput> {
  return request<BackgroundTaskOutput>(`/tasks/${taskId}/output?cursor=${cursor}&limit=${limit}`);
}

export async function prBabysitterSnapshot(workspaceId: string, payload: Record<string, unknown> = {}): Promise<PrBabysitterReport> {
  return request<PrBabysitterReport>(`/workspaces/${workspaceId}/pr-babysitter/snapshot`, { method: "POST", body: JSON.stringify(payload) });
}

export async function startPrBabysitter(workspaceId: string, payload: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/workspaces/${workspaceId}/pr-babysitter/watch`, { method: "POST", body: JSON.stringify(payload) });
}

export async function getPrBabysitterReports(workspaceId: string, runId?: string): Promise<Record<string, unknown>> {
  const query = runId ? `?run_id=${encodeURIComponent(runId)}` : "";
  return request<Record<string, unknown>>(`/workspaces/${workspaceId}/pr-babysitter${query}`);
}

export async function getRunGate(runId: string): Promise<RunGateReport> {
  return request<RunGateReport>(`/runs/${runId}/gate`);
}

export async function getRunBrowserProof(runId: string): Promise<BrowserProofReport> {
  return request<BrowserProofReport>(`/runs/${runId}/browser-proof`);
}

export async function getRunBrowserReplay(runId: string): Promise<BrowserReplayReport> {
  return request<BrowserReplayReport>(`/runs/${runId}/browser-replay`);
}

export async function getRunFinalReport(runId: string): Promise<RunFinalReport> {
  return request<RunFinalReport>(`/runs/${runId}/final-report`);
}

export async function getRunRepairSignatures(runId: string): Promise<RunRepairSignatures> {
  return request<RunRepairSignatures>(`/runs/${runId}/repair-signatures`);
}

export async function getRunRepairCases(runId: string): Promise<RunRepairCases> {
  return request<RunRepairCases>(`/runs/${runId}/repair-cases`);
}

export async function getRunRepairCaseAttempts(runId: string, caseId: string): Promise<RepairAttemptsReport> {
  return request<RepairAttemptsReport>(`/runs/${runId}/repair-cases/${encodeURIComponent(caseId)}/attempts`);
}

export async function retryRunRepairCase(runId: string, caseId: string): Promise<Record<string, unknown>> {
  return request(`/runs/${runId}/repair-cases/${encodeURIComponent(caseId)}/retry`, { method: "POST" });
}

export async function resumeRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/resume`, { method: "POST" });
}

export async function getRunApprovals(runId: string): Promise<{ run_id: string; items: ApprovalRecord[] }> {
  return request<{ run_id: string; items: ApprovalRecord[] }>(`/runs/${runId}/approvals`);
}

export async function approveRunApproval(runId: string, approvalId: string): Promise<ApprovalRecord> {
  return request<ApprovalRecord>(`/runs/${runId}/approvals/${approvalId}/approve`, { method: "POST" });
}

export async function rejectRunApproval(runId: string, approvalId: string): Promise<ApprovalRecord> {
  return request<ApprovalRecord>(`/runs/${runId}/approvals/${approvalId}/reject`, { method: "POST" });
}

export async function stageRunFiles(runId: string, files: string[]): Promise<StagedApplyState> {
  return request<StagedApplyState>(`/runs/${runId}/stage/files`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
}

export async function discardRunFiles(runId: string, files: string[]): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/discard/files`, {
    method: "POST",
    body: JSON.stringify({ files }),
  });
}

export async function applyStagedRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/apply/staged`, { method: "POST" });
}

export async function compactRun(runId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/compact`, { method: "POST" });
}

export async function getRunCompaction(runId: string): Promise<RunCompactionReport> {
  return request<RunCompactionReport>(`/runs/${runId}/compaction`);
}

export async function getRunContextPressure(runId: string): Promise<ContextPressureReport> {
  return request<ContextPressureReport>(`/runs/${runId}/context-pressure`);
}

export async function getRunCompactionBoundaries(runId: string): Promise<RunCompactionBoundaries> {
  return request<RunCompactionBoundaries>(`/runs/${runId}/compaction/boundaries`);
}

export async function getRunMicrocompact(runId: string, digest: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/microcompact/${digest}`);
}

export async function getRunPostCompactMessage(runId: string, boundaryId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/compaction/post-message/${boundaryId}`);
}

export async function getRunOutputArtifacts(runId: string): Promise<OutputArtifactIndex> {
  return request<OutputArtifactIndex>(`/runs/${runId}/output-artifacts`);
}

export async function startReviewFix(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/review/fix`, { method: "POST" });
}

export async function getDoctorReport(): Promise<DoctorReport> {
  return request<DoctorReport>("/doctor");
}

export async function getRunWorkers(runId: string): Promise<WorkerReport> {
  return request<WorkerReport>(`/runs/${runId}/workers`);
}

export async function getRunWorkerOrchestration(runId: string): Promise<WorkerOrchestrationReport> {
  return request<WorkerOrchestrationReport>(`/runs/${runId}/workers/orchestration`);
}

export async function getRunWorkerContext(runId: string, workerId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/workers/${encodeURIComponent(workerId)}/context`);
}

export async function getRunWorkerMemory(runId: string, workerId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/workers/${encodeURIComponent(workerId)}/memory`);
}

export async function getRunWorkerOutput(runId: string, workerId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/workers/${encodeURIComponent(workerId)}/output`);
}

export async function getRunWorkerMergeDecision(runId: string): Promise<Record<string, unknown>> {
  return request<Record<string, unknown>>(`/runs/${runId}/workers/merge-decision`);
}

export async function getRunReview(runId: string): Promise<ReviewReport> {
  return request<ReviewReport>(`/runs/${runId}/review`);
}

export async function getRunPromptSuggestions(runId: string): Promise<PromptSuggestionsReport> {
  return request<PromptSuggestionsReport>(`/runs/${runId}/prompt-suggestions`);
}

export async function getRunTestMatrix(runId: string): Promise<TestMatrixReport> {
  return request<TestMatrixReport>(`/runs/${runId}/test-matrix`);
}

export async function getRunPromptContract(runId: string): Promise<PromptContractReport> {
  return request<PromptContractReport>(`/runs/${runId}/prompt-contract`);
}

export async function getRunMiniAppContract(runId: string): Promise<MiniAppContractReport> {
  return request<MiniAppContractReport>(`/runs/${runId}/miniapp-contract`);
}

export async function getRunAcceptanceScenarios(runId: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/runs/${runId}/acceptance-scenarios`);
}

export async function getRunVisualQa(runId: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/runs/${runId}/visual-qa`);
}

export async function getRunVisualRegression(runId: string): Promise<VisualRegressionReport> {
  return request<VisualRegressionReport>(`/runs/${runId}/visual-regression`);
}

export async function getRunTraceReducer(runId: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/runs/${runId}/trace-reducer`);
}

export async function getRunSimplify(runId: string): Promise<SimplifyReport> {
  return request<SimplifyReport>(`/runs/${runId}/simplify`);
}

export async function runSimplify(runId: string): Promise<SimplifyReport> {
  return request<SimplifyReport>(`/runs/${runId}/simplify`, { method: "POST" });
}

export async function getRunDebug(runId: string): Promise<DiagnosticWorkflowReport> {
  return request<DiagnosticWorkflowReport>(`/runs/${runId}/debug`);
}

export async function getRunStuck(runId: string): Promise<DiagnosticWorkflowReport> {
  return request<DiagnosticWorkflowReport>(`/runs/${runId}/stuck`);
}

export async function getDoctorWorkspace(workspaceId: string): Promise<DiagnosticWorkflowReport> {
  return request<DiagnosticWorkflowReport>(`/workspaces/${workspaceId}/doctor-workspace`);
}

export async function extractRunMemory(runId: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/runs/${runId}/memory/extract`, { method: "POST" });
}

export async function getWorkspaceMemoryPipeline(workspaceId: string): Promise<MemoryPipelineReport> {
  return request<MemoryPipelineReport>(`/workspaces/${workspaceId}/memory/pipeline`);
}

export async function getWorkspaceMemorySummary(workspaceId: string): Promise<MemorySummaryReport> {
  return request<MemorySummaryReport>(`/workspaces/${workspaceId}/memory/summary`);
}

export async function getWorkspaceSessionMemory(workspaceId: string): Promise<SessionMemoryReport> {
  return request<SessionMemoryReport>(`/workspaces/${workspaceId}/session-memory`);
}

export async function consolidateWorkspaceMemory(workspaceId: string): Promise<WorkspaceMemory> {
  return request<WorkspaceMemory>(`/workspaces/${workspaceId}/memory/consolidate`, { method: "POST" });
}

export async function getProjectInstructions(): Promise<GeneratedReport> {
  return request<GeneratedReport>("/system/project-instructions");
}

export async function getWorkerRoles(): Promise<GeneratedReport> {
  return request<GeneratedReport>("/system/worker-roles");
}

export async function getSubagentForkContract(): Promise<SubagentForkContract> {
  return request<SubagentForkContract>("/system/subagents");
}

export async function getSlashCommands(): Promise<SlashCommandList> {
  return request<SlashCommandList>("/slash-commands");
}

export async function resolveSlashCommand(commandId: string, prompt?: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/slash-commands/${commandId}/resolve`, {
    method: "POST",
    body: JSON.stringify({ prompt }),
  });
}

export async function executeSlashCommand(commandId: string, payload: {
  workspace_id?: string;
  run_id?: string;
  prompt?: string;
  detail?: string;
  target_role_scope?: string[];
  model_profile?: string;
  generation_mode?: string;
  metadata?: Record<string, unknown>;
}): Promise<SlashCommandExecution> {
  return request<SlashCommandExecution>(`/slash-commands/${commandId}/execute`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export async function getWorkspaceMagicDoc(workspaceId: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/workspaces/${workspaceId}/magic-docs/product-architecture`);
}

export async function updateWorkspaceMagicDoc(workspaceId: string): Promise<GeneratedReport> {
  return request<GeneratedReport>(`/workspaces/${workspaceId}/magic-docs/product-architecture`, { method: "POST" });
}

export async function getRunState(runId: string): Promise<RunStateReport> {
  return request<RunStateReport>(`/runs/${runId}/state`);
}

export async function getWorkspaceMemory(workspaceId: string): Promise<WorkspaceMemory> {
  return request<WorkspaceMemory>(`/workspaces/${workspaceId}/memory`);
}

export async function searchWorkspaceFiles(workspaceId: string, query: string, runId?: string): Promise<FileSearchResult> {
  const params = new URLSearchParams({ q: query });
  if (runId) {
    params.set("run_id", runId);
  }
  return request<FileSearchResult>(`/workspaces/${workspaceId}/files/search?${params.toString()}`);
}

export async function getLspDiagnostics(workspaceId: string, runId?: string, options?: { changedOnly?: boolean; files?: string[] }): Promise<LspDiagnosticsReport> {
  const params = new URLSearchParams();
  if (runId) {
    params.set("run_id", runId);
  }
  if (options?.changedOnly) {
    params.set("changed_only", "true");
  }
  if (options?.files?.length) {
    params.set("files", options.files.join(","));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<LspDiagnosticsReport>(`/workspaces/${workspaceId}/diagnostics/lsp${suffix}`);
}

export async function startLspDiagnostics(workspaceId: string, payload: { runId?: string; changedOnly?: boolean; files?: string[] } = {}): Promise<LspDiagnosticsTaskReport> {
  return request<LspDiagnosticsTaskReport>(`/workspaces/${workspaceId}/diagnostics/lsp/async`, {
    method: "POST",
    body: JSON.stringify({
      run_id: payload.runId,
      changed_only: Boolean(payload.changedOnly),
      files: payload.files ?? [],
    }),
  });
}

export async function getLspDiagnosticsTask(workspaceId: string, taskId: string): Promise<LspDiagnosticsTaskReport> {
  return request<LspDiagnosticsTaskReport>(`/workspaces/${workspaceId}/diagnostics/lsp/async/${taskId}`);
}

export async function getLspDefinition(workspaceId: string, symbol: string, runId?: string, targets?: string[]): Promise<LspDefinitionReport> {
  const params = new URLSearchParams({ symbol });
  if (runId) {
    params.set("run_id", runId);
  }
  if (targets?.length) {
    params.set("targets", targets.join(","));
  }
  return request<LspDefinitionReport>(`/workspaces/${workspaceId}/lsp/definition?${params.toString()}`);
}

export async function getLspRouteGraph(workspaceId: string, runId?: string, targets?: string[]): Promise<LspRouteGraphReport> {
  const params = new URLSearchParams();
  if (runId) {
    params.set("run_id", runId);
  }
  if (targets?.length) {
    params.set("targets", targets.join(","));
  }
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request<LspRouteGraphReport>(`/workspaces/${workspaceId}/lsp/route-graph${suffix}`);
}

export async function stopRun(runId: string): Promise<Run> {
  const ref = runTurnRefs.get(runId);
  if (ref) {
    try {
      await rpcClient.call("turn/interrupt", { thread_id: ref.threadId, turn_id: ref.turnId });
    } catch (error) {
      if (!isRpcConnectionError(error)) {
        throw error;
      }
    }
  }
  return request<Run>(`/runs/${runId}/stop`, {
    method: "POST",
  });
}

export async function getRunIterations(runId: string): Promise<Array<Record<string, unknown>>> {
  return request<Array<Record<string, unknown>>>(`/runs/${runId}/iterations`);
}

export async function approveRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/approve`, {
    method: "POST",
  });
}

export async function discardRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/discard`, {
    method: "POST",
  });
}

export async function rebuildPreview(workspaceId: string): Promise<PreviewRuntimeInfo> {
  return request<PreviewRuntimeInfo>(`/workspaces/${workspaceId}/preview/rebuild`, {
    method: "POST",
  });
}

export async function startPreview(workspaceId: string): Promise<PreviewRuntimeInfo> {
  return request<PreviewRuntimeInfo>(`/workspaces/${workspaceId}/preview/start`, {
    method: "POST",
  });
}

export async function ensurePreview(workspaceId: string): Promise<PreviewRuntimeInfo> {
  return request<PreviewRuntimeInfo>(`/workspaces/${workspaceId}/preview/ensure`, {
    method: "POST",
  });
}

export async function rollbackRun(runId: string): Promise<Run> {
  return request<Run>(`/runs/${runId}/rollback`, {
    method: "POST",
  });
}

export async function getWorkspaceLogs(workspaceId: string): Promise<WorkspaceLogs> {
  return request<WorkspaceLogs>(`/workspaces/${workspaceId}/logs`);
}

export function subscribeRpcNotifications(handler: RpcNotificationHandler): () => void {
  return rpcClient.subscribe(handler);
}

export async function execWorkspaceCommand(payload: {
  workspace_id: string;
  command: string;
  thread_id?: string;
  turn_id?: string;
  timeout?: number;
  managed?: boolean;
  approval_id?: string;
  preset?: string;
}): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec", payload);
}

export async function writeExecStdin(processId: string, data: string): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/write", { process_id: processId, data });
}

export async function resizeExec(processId: string, cols: number, rows: number): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/resize", { process_id: processId, cols, rows });
}

export async function terminateExec(processId: string): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/terminate", { process_id: processId });
}

export async function readExecOutput(processId: string, stream: "stdout" | "stderr" = "stdout"): Promise<Record<string, unknown>> {
  return rpcClient.call("command/exec/read", { process_id: processId, stream });
}

export async function listThreads(workspaceId: string): Promise<AgentThread[]> {
  const page = await rpcClient.call<{ items: AgentThread[]; next_cursor?: string | null }>("thread/list", { workspace_id: workspaceId });
  return page.items;
}

export async function readThread(threadId: string): Promise<{ thread: AgentThread; turns: AgentTurn[]; items: AgentItem[] }> {
  return rpcClient.call("thread/read", { thread_id: threadId });
}

async function getOrCreateWorkspaceThread(workspaceId: string): Promise<string> {
  const cached = workspaceThreadIds.get(workspaceId);
  if (cached) {
    return cached;
  }
  const existing = await listThreads(workspaceId);
  const thread = existing[0] ?? await rpcClient.call<AgentThread>("thread/start", {
    workspace_id: workspaceId,
    title: "Mini-app generation",
  });
  workspaceThreadIds.set(workspaceId, thread.thread_id);
  return thread.thread_id;
}

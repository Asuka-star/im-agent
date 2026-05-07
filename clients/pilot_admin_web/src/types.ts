export type TaskRunSummary = {
  task_run_id: string;
  requirement_id?: string | null;
  session_id: string;
  session_label?: string | null;
  source_type: string;
  source_ref?: string | null;
  trigger_message_id?: string | null;
  intent?: string | null;
  title: string;
  stage: string;
  status: string;
  latest_summary?: string | null;
  latest_reply_preview?: string | null;
  latest_error?: string | null;
  run_kind?: string | null;
  primary_object?: string | null;
  lifecycle_stage?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  completed_at?: string | null;
};

export type TaskRunStepRecord = {
  step_key: string;
  title: string;
  step_type: string;
  status: string;
  input_json?: string | null;
  output_json?: string | null;
  error?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type ArtifactRecord = {
  artifact_id: string;
  artifact_type: string;
  provider: string;
  title: string;
  status: string;
  url?: string | null;
  version: number;
  preview_json?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type SessionDocumentRecord = {
  session_id: string;
  document_id: string;
  url?: string | null;
  title: string;
  version: number;
  sync_mode: string;
  task_run_id?: string | null;
  updated_at?: string | null;
  is_current: boolean;
};

export type ArtifactCheckRecord = {
  key: string;
  label: string;
  status: string;
  detail: string;
  category: string;
};

export type ContextPackItemRecord = {
  kind: string;
  label: string;
  detail: string;
  status: string;
  url?: string | null;
};

export type ContextPackRecord = {
  summary: string;
  used_sources: ContextPackItemRecord[];
  missing_items: ContextPackItemRecord[];
  suggested_inputs: string[];
};

export type ConfirmationRequestRecord = {
  confirmation_id: string;
  prompt: string;
  options_json?: string | null;
  status: string;
  answer_value?: string | null;
  answered_by?: string | null;
  answered_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type TaskRunDetail = TaskRunSummary & {
  metadata_json?: string | null;
  steps: TaskRunStepRecord[];
  artifacts: ArtifactRecord[];
  artifact_checks: ArtifactCheckRecord[];
  context_pack?: ContextPackRecord | null;
  confirmations: ConfirmationRequestRecord[];
  session_documents: SessionDocumentRecord[];
};

export type RequirementSourceRecord = {
  source_id: string;
  requirement_id: string;
  session_id: string;
  session_label?: string | null;
  session_type?: string | null;
  message_id?: string | null;
  sender_id?: string | null;
  sender_label?: string | null;
  message_text?: string | null;
  message_status?: string | null;
  source_type: string;
  created_at?: string | null;
};

export type RequirementTimelineItem = {
  item_id: string;
  item_type: string;
  title: string;
  status: string;
  stage?: string | null;
  summary?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  metadata?: Record<string, unknown>;
};

export type OfflineSyncRecord = {
  submission_id: string;
  task_run_id: string;
  duplicate_of_submission_id?: string | null;
  requirement_id?: string | null;
  title?: string | null;
  file_name?: string | null;
  file_extension?: string | null;
  status: string;
  stage?: string | null;
  latest_summary?: string | null;
  confirmation_id?: string | null;
  confirmation_status?: string | null;
  confirmation_options: string[];
  answer_value?: string | null;
  available_follow_up_targets: string[];
  merge_summary: Record<string, unknown>;
  merge_plan: Record<string, unknown>;
  created_at?: string | null;
  updated_at?: string | null;
};

export type RequirementSummary = {
  requirement_id: string;
  title: string;
  status: string;
  summary?: string | null;
  primary_session_id: string;
  primary_session_label?: string | null;
  source_count?: number;
  task_run_count?: number;
  latest_source_type?: string | null;
  current_document_id?: string | null;
  current_slides_artifact_id?: string | null;
  current_canvas_artifact_id?: string | null;
  current_delivery_artifact_id?: string | null;
  created_by?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

export type RequirementDetail = RequirementSummary & {
  sources: RequirementSourceRecord[];
  task_runs: TaskRunSummary[];
  timeline: RequirementTimelineItem[];
  offline_syncs: OfflineSyncRecord[];
  current_document?: SessionDocumentRecord | null;
  current_slides?: ArtifactRecord | null;
  current_canvas?: ArtifactRecord | null;
  current_delivery?: ArtifactRecord | null;
  recommendations?: NextActionBundle | null;
};

export type NextActionRecommendation = {
  action_id: string;
  title: string;
  description?: string;
  action_type: string;
  priority: string;
  confidence: number;
  reason?: string;
  source?: string;
  requires_confirmation?: boolean;
  command?: string | null;
  target_kind?: string | null;
  target_id?: string | null;
  metadata?: Record<string, unknown>;
};

export type NextActionBundle = {
  task_run_id: string;
  session_id: string;
  generated_at?: string;
  summary?: string;
  recommendations: NextActionRecommendation[];
};

export type RealtimeEvent = {
  type?: string;
  requirement?: RequirementDetail | RequirementSummary | null;
  requirements?: RequirementSummary[];
  task_run_id?: string;
  session_id?: string;
  task_run?: TaskRunDetail | TaskRunSummary | null;
  task_runs?: TaskRunSummary[];
};

export type JsonMap = Record<string, unknown>;

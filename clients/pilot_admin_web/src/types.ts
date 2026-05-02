export type TaskRunSummary = {
  task_run_id: string;
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
  confirmations: ConfirmationRequestRecord[];
  session_documents: SessionDocumentRecord[];
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
  task_run_id?: string;
  session_id?: string;
  task_run?: TaskRunDetail | TaskRunSummary | null;
  task_runs?: TaskRunSummary[];
};

export type JsonMap = Record<string, unknown>;

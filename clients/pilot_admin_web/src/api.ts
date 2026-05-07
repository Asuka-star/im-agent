import type {
  NextActionBundle,
  OfflineSyncRecord,
  RealtimeEvent,
  RequirementDetail,
  RequirementSummary,
  TaskRunDetail,
  TaskRunSummary,
} from './types';

const envBase = import.meta.env.VITE_WORKBENCH_API_BASE_URL as string | undefined;
export const apiBaseUrl = normalizeBaseUrl(envBase || '/api');

function normalizeBaseUrl(value: string): string {
  return value.replace(/\/+$/, '');
}

function buildUrl(path: string, params?: Record<string, string | undefined | null>): string {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const base = apiBaseUrl.startsWith('http') ? apiBaseUrl : window.location.origin + apiBaseUrl;
  const url = new URL(`${base}${cleanPath}`);
  for (const [key, value] of Object.entries(params || {})) {
    if (value) url.searchParams.set(key, value);
  }
  return apiBaseUrl.startsWith('http') ? url.toString() : `${url.pathname}${url.search}`;
}

async function requestJson<T>(path: string, init?: RequestInit, params?: Record<string, string | undefined | null>): Promise<T> {
  const response = await fetch(buildUrl(path, params), {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(`接口请求失败：状态码 ${response.status}`);
  }
  return response.json() as Promise<T>;
}

export function listTaskRuns(options: {
  sessionQuery?: string;
  requirementId?: string;
  status?: string;
  limit?: number;
}): Promise<TaskRunSummary[]> {
  return requestJson<TaskRunSummary[]>('/task-runs/', undefined, {
    session_query: options.sessionQuery,
    requirement_id: options.requirementId,
    status: options.status === 'all' ? undefined : options.status,
    limit: String(options.limit || 50),
  });
}

export function listRequirements(options: {
  query?: string;
  sessionId?: string;
  limit?: number;
} = {}): Promise<RequirementSummary[]> {
  return requestJson<RequirementSummary[]>('/requirements/', undefined, {
    query: options.query,
    session_id: options.sessionId,
    limit: String(options.limit || 50),
  });
}

export function getRequirement(requirementId: string): Promise<RequirementDetail> {
  return requestJson<RequirementDetail>(`/requirements/${encodeURIComponent(requirementId)}`);
}

export function createRequirement(payload: {
  title: string;
  summary?: string | null;
  primarySessionId: string;
  createdBy?: string | null;
}): Promise<RequirementSummary> {
  return requestJson<RequirementSummary>('/requirements/', {
    method: 'POST',
    body: JSON.stringify({
      title: payload.title,
      summary: payload.summary || null,
      primary_session_id: payload.primarySessionId,
      created_by: payload.createdBy || 'pilot_admin_web',
    }),
  });
}

export function updateRequirement(
  requirementId: string,
  payload: {
    title?: string | null;
    summary?: string | null;
    status?: string | null;
  },
): Promise<RequirementDetail> {
  return requestJson<RequirementDetail>(`/requirements/${encodeURIComponent(requirementId)}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  });
}

export function updateRequirementCurrentProducts(
  requirementId: string,
  payload: {
    currentDocumentId?: string | null;
    currentSlidesArtifactId?: string | null;
    currentCanvasArtifactId?: string | null;
    currentDeliveryArtifactId?: string | null;
  },
): Promise<RequirementDetail> {
  return requestJson<RequirementDetail>(`/requirements/${encodeURIComponent(requirementId)}/current-products`, {
    method: 'PATCH',
    body: JSON.stringify({
      current_document_id: payload.currentDocumentId,
      current_slides_artifact_id: payload.currentSlidesArtifactId,
      current_canvas_artifact_id: payload.currentCanvasArtifactId,
      current_delivery_artifact_id: payload.currentDeliveryArtifactId,
    }),
  });
}

export function reassignTaskRunRequirement(requirementId: string, taskRunId: string): Promise<TaskRunSummary> {
  return requestJson<TaskRunSummary>(`/requirements/${encodeURIComponent(requirementId)}/task-runs/${encodeURIComponent(taskRunId)}/reassign`, {
    method: 'POST',
    body: JSON.stringify({ requirement_id: requirementId }),
  });
}

export function getTaskRun(taskRunId: string): Promise<TaskRunDetail> {
  return requestJson<TaskRunDetail>(`/task-runs/${encodeURIComponent(taskRunId)}`);
}

export function getRecommendations(taskRunId: string): Promise<NextActionBundle> {
  return requestJson<NextActionBundle>(`/task-runs/${encodeURIComponent(taskRunId)}/recommendations`);
}

export function confirmTaskRun(
  taskRunId: string,
  confirmationId: string,
  answerValue: string,
  overrideInstruction?: string | null,
): Promise<unknown> {
  return requestJson(`/task-runs/${encodeURIComponent(taskRunId)}/confirm`, {
    method: 'POST',
    body: JSON.stringify({
      confirmation_id: confirmationId,
      answer_value: answerValue,
      answered_by: 'pilot_admin_web',
      override_instruction: overrideInstruction?.trim() || null,
    }),
  });
}

export function confirmOfflineSync(
  submissionId: string,
  answerValue: string,
  overrideInstruction?: string | null,
): Promise<unknown> {
  return requestJson(`/offline-syncs/${encodeURIComponent(submissionId)}/confirm`, {
    method: 'POST',
    body: JSON.stringify({
      answer_value: answerValue,
      answered_by: 'pilot_admin_web',
      override_instruction: overrideInstruction?.trim() || null,
    }),
  });
}

export function getOfflineSync(submissionId: string): Promise<OfflineSyncRecord> {
  return requestJson<OfflineSyncRecord>(`/offline-syncs/${encodeURIComponent(submissionId)}`);
}

export function reviseDocument(taskRunId: string, instruction: string, documentId?: string): Promise<TaskRunDetail> {
  return requestJson<TaskRunDetail>(`/task-runs/${encodeURIComponent(taskRunId)}/revise-document`, {
    method: 'POST',
    body: JSON.stringify({
      instruction,
      requested_by: 'pilot_admin_web',
      document_id: documentId || null,
    }),
  });
}

export function reviseSlides(taskRunId: string, instruction: string, artifactId?: string): Promise<TaskRunDetail> {
  return requestJson<TaskRunDetail>(`/task-runs/${encodeURIComponent(taskRunId)}/revise-slides`, {
    method: 'POST',
    body: JSON.stringify({
      instruction,
      requested_by: 'pilot_admin_web',
      artifact_id: artifactId || null,
    }),
  });
}

export function reviseCanvas(taskRunId: string, instruction: string, artifactId?: string): Promise<TaskRunDetail> {
  return requestJson<TaskRunDetail>(`/task-runs/${encodeURIComponent(taskRunId)}/revise-canvas`, {
    method: 'POST',
    body: JSON.stringify({
      instruction,
      requested_by: 'pilot_admin_web',
      artifact_id: artifactId || null,
    }),
  });
}

export function bundleDelivery(taskRunId: string): Promise<TaskRunDetail> {
  return requestJson<TaskRunDetail>(`/task-runs/${encodeURIComponent(taskRunId)}/bundle-delivery`, {
    method: 'POST',
    body: JSON.stringify({ requested_by: 'pilot_admin_web' }),
  });
}

export function artifactUrl(value?: string | null): string {
  if (!value) return '';
  if (value.startsWith('http://') || value.startsWith('https://')) return value;
  const base = apiBaseUrl.startsWith('http') ? new URL(apiBaseUrl).origin : window.location.origin;
  return `${base}${value.startsWith('/') ? value : `/${value}`}`;
}

export function wsUrl(path: string): string {
  const cleanPath = path.startsWith('/') ? path : `/${path}`;
  const [pathname, search = ''] = cleanPath.split('?');
  if (apiBaseUrl.startsWith('http')) {
    const base = new URL(apiBaseUrl);
    base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
    base.pathname = `${base.pathname.replace(/\/+$/, '')}${pathname}`;
    base.search = search ? `?${search}` : '';
    return base.toString();
  }
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}${apiBaseUrl}${cleanPath}`;
}

export function connectSocket(path: string, onEvent: (event: RealtimeEvent) => void, onState: (state: string) => void): () => void {
  let closed = false;
  let heartbeat: number | undefined;
  let reconnectTimer: number | undefined;
  let reconnectAttempt = 0;
  let socket: WebSocket | undefined;

  const clearHeartbeat = () => {
    if (heartbeat) window.clearInterval(heartbeat);
    heartbeat = undefined;
  };

  const clearReconnect = () => {
    if (reconnectTimer) window.clearTimeout(reconnectTimer);
    reconnectTimer = undefined;
  };

  const scheduleReconnect = () => {
    if (closed || reconnectTimer) return;
    onState('polling');
    const delay = Math.min(12000, 1000 * 2 ** reconnectAttempt);
    reconnectAttempt += 1;
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = undefined;
      open();
    }, delay);
  };

  const open = () => {
    if (closed) return;
    clearReconnect();
    onState('connecting');
    socket = new WebSocket(wsUrl(path));
    socket.onopen = () => {
      reconnectAttempt = 0;
      onState('live');
      clearHeartbeat();
      heartbeat = window.setInterval(() => {
        try {
          if (socket?.readyState === WebSocket.OPEN) socket.send('ping');
        } catch {
          socket?.close();
        }
      }, 25000);
    };
    socket.onmessage = (message) => {
      if (typeof message.data !== 'string') return;
      try {
        onEvent(JSON.parse(message.data) as RealtimeEvent);
      } catch {
        return;
      }
    };
    socket.onerror = () => {
      onState('error');
      if (socket?.readyState !== WebSocket.CLOSING && socket?.readyState !== WebSocket.CLOSED) {
        socket?.close();
      }
    };
    socket.onclose = () => {
      clearHeartbeat();
      if (closed) return;
      scheduleReconnect();
    };
  };

  open();

  return () => {
    closed = true;
    clearHeartbeat();
    clearReconnect();
    socket?.close();
  };
}

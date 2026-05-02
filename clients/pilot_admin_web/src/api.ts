import type {
  NextActionBundle,
  RealtimeEvent,
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
    throw new Error(`接口请求失败：${response.status} ${response.statusText}`);
  }
  return response.json() as Promise<T>;
}

export function listTaskRuns(options: {
  sessionQuery?: string;
  status?: string;
  limit?: number;
}): Promise<TaskRunSummary[]> {
  return requestJson<TaskRunSummary[]>('/task-runs/', undefined, {
    session_query: options.sessionQuery,
    status: options.status === 'all' ? undefined : options.status,
    limit: String(options.limit || 50),
  });
}

export function getTaskRun(taskRunId: string): Promise<TaskRunDetail> {
  return requestJson<TaskRunDetail>(`/task-runs/${encodeURIComponent(taskRunId)}`);
}

export function getRecommendations(taskRunId: string): Promise<NextActionBundle> {
  return requestJson<NextActionBundle>(`/task-runs/${encodeURIComponent(taskRunId)}/recommendations`);
}

export function confirmTaskRun(taskRunId: string, confirmationId: string, answerValue: string): Promise<unknown> {
  return requestJson(`/task-runs/${encodeURIComponent(taskRunId)}/confirm`, {
    method: 'POST',
    body: JSON.stringify({
      confirmation_id: confirmationId,
      answer_value: answerValue,
      answered_by: 'pilot_admin_web',
    }),
  });
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
  let socket: WebSocket | undefined;

  const open = () => {
    if (closed) return;
    onState('connecting');
    socket = new WebSocket(wsUrl(path));
    socket.onopen = () => {
      onState('live');
      heartbeat = window.setInterval(() => socket?.readyState === WebSocket.OPEN && socket.send('ping'), 25000);
    };
    socket.onmessage = (message) => {
      if (typeof message.data !== 'string') return;
      try {
        onEvent(JSON.parse(message.data) as RealtimeEvent);
      } catch {
        return;
      }
    };
    socket.onerror = () => onState('error');
    socket.onclose = () => {
      if (heartbeat) window.clearInterval(heartbeat);
      if (closed) return;
      onState('polling');
    };
  };

  open();

  return () => {
    closed = true;
    if (heartbeat) window.clearInterval(heartbeat);
    socket?.close();
  };
}

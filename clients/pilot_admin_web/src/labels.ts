export function statusLabel(value?: string | null): string {
  const map: Record<string, string> = {
    queued: '排队中',
    running: '运行中',
    completed: '已完成',
    failed: '失败',
    waiting_confirmation: '待确认',
    pending: '待处理',
    open: '待处理',
    answered: '已确认',
    ready: '可用',
    done: '完成',
  };
  return map[value || ''] || '未知状态';
}

export function stageLabel(value?: string | null): string {
  const map: Record<string, string> = {
    queued: '排队',
    building_context: '构建上下文',
    routing: '意图路由',
    planning: '执行规划',
    semantic_recall: '记忆召回',
    intent_resolution: '意图解析',
    delivered: '已交付',
    failed: '失败',
    awaiting_user_confirmation: '等待确认',
    recommendation: '推荐下一步',
  };
  return map[value || ''] || '未知阶段';
}

export function intentLabel(value?: string | null): string {
  const map: Record<string, string> = {
    doc: '文档',
    slides: '演示稿',
    canvas: '画布',
    status: '状态',
    tasks: '任务',
    summary: '总结',
    risks: '风险',
    help: '帮助',
  };
  return map[value || ''] || '协作';
}

export function sourceLabel(value?: string | null): string {
  const map: Record<string, string> = {
    group: '群聊',
    p2p: '私聊',
    workbench: '工作台',
    im: 'IM',
    im_passive: '群聊讨论',
    im_passive_group: '群聊讨论',
    task_run: '任务运行',
    manual: '手动创建',
    manual_reassign: '手动归属',
    unknown: '未知来源',
  };
  return map[value || ''] || '来源';
}

export function artifactLabel(value?: string | null): string {
  const map: Record<string, string> = {
    document: '文档',
    slides_package: 'PPT',
    slides: '演示稿',
    canvas: '画布',
    delivery_bundle: '交付包',
    plan: '计划',
    note: '记录',
  };
  return map[value || ''] || '产物';
}

export function providerLabel(value?: string | null): string {
  const map: Record<string, string> = {
    llm: 'LLM',
    fallback: '规则兜底',
    local: '本地',
    feishu_doc: '飞书文档',
  };
  return map[value || ''] || '服务';
}

export function stepTypeLabel(value?: string | null): string {
  const map: Record<string, string> = {
    input: '输入',
    context: '上下文',
    artifact: '产物',
    confirm: '确认',
    confirmation: '确认',
    system: '系统',
    reply: '回复',
    route: '路由',
    planner: '规划',
    reviewer: '复核',
    workflow: '工作流',
    graph: '流程编排',
    graph_worker: '子任务',
    generate_canvas: '画布生成',
  };
  return map[value || ''] || '步骤';
}

export function stepTitleLabel(step: {
  step_key?: string | null;
  title?: string | null;
  step_type?: string | null;
  output_json?: string | null;
}): string {
  const title = String(step.title || '').trim();
  const graphTitle = graphStepTitle(title, step);
  if (graphTitle) return graphTitle;
  if (title && !isInternalStepText(title)) return title;
  return stepKeyTitle(step.step_key) || stepTypeLabel(step.step_type);
}

function graphStepTitle(title: string, step: { step_key?: string | null; output_json?: string | null }): string {
  if (/^LangGraph execution$/i.test(title) || step.step_key === 'graph.execution') {
    return '流程编排完成';
  }
  const workerFromTitle = title.match(/^LangGraph worker\s+(.+)$/i)?.[1];
  const parsed = parseGraphWorker(step.step_key) || parseGraphWorkerPayload(step.output_json);
  const worker = workerFromTitle || parsed?.worker;
  if (!worker) return '';
  return workerActionLabel(worker, parsed?.operation);
}

function stepKeyTitle(value?: string | null): string {
  const key = String(value || '').trim();
  const map: Record<string, string> = {
    request_received: '接收用户请求',
    workspace_context: '构建协作上下文',
    response_generated: '生成处理结果',
    artifact_persisted: '记录协作产物',
    delivery_bundle: '生成交付包',
  };
  if (map[key]) return map[key];
  if (key === 'graph.execution') return '流程编排完成';
  const parsed = parseGraphWorker(key);
  return parsed ? workerActionLabel(parsed.worker, parsed.operation) : '';
}

function parseGraphWorker(value?: string | null): { worker: string; operation?: string } | null {
  const key = String(value || '').trim();
  const match = key.match(/^graph\.worker\.([a-z]+)(?:_(.+))?$/i);
  if (!match) return null;
  return { worker: match[1], operation: match[2] };
}

function parseGraphWorkerPayload(value?: string | null): { worker: string; operation?: string } | null {
  try {
    const payload = JSON.parse(String(value || '{}')) as { worker?: unknown; operation?: unknown };
    const worker = String(payload.worker || '').trim();
    if (!worker) return null;
    return { worker, operation: String(payload.operation || '').trim() || undefined };
  } catch {
    return null;
  }
}

function workerActionLabel(workerValue: string, operationValue?: string): string {
  const worker = workerValue.toLowerCase();
  const operation = String(operationValue || '').toLowerCase();
  const generateMap: Record<string, string> = {
    doc: '生成文档',
    slides: '生成演示稿',
    canvas: '生成画布',
    delivery: '整理交付包',
    reply: '生成回复',
  };
  const reviseMap: Record<string, string> = {
    doc: '修订文档',
    slides: '修订演示稿',
    canvas: '修订画布',
  };
  if (operation === 'generate' && generateMap[worker]) return generateMap[worker];
  if ((operation === 'revise' || operation === 'update') && reviseMap[worker]) return reviseMap[worker];
  if (operation === 'read' && worker === 'task') return '读取任务状态';
  if (operation === 'analyze' || worker === 'analysis') return '分析协作需求';
  if (worker === 'review') return '复核结果质量';
  if (worker === 'help') return '生成帮助说明';
  return workerLabel(worker);
}

function workerLabel(worker: string): string {
  const map: Record<string, string> = {
    task: '任务处理',
    analysis: '需求分析',
    doc: '文档处理',
    slides: '演示稿处理',
    canvas: '画布处理',
    delivery: '交付整理',
    review: '质量复核',
    reply: '回复处理',
    help: '帮助说明',
  };
  return map[worker] || '子任务处理';
}

function isInternalStepText(value: string): boolean {
  return /^(LangGraph|graph\.|[a-z]+[_-][a-z0-9_.-]+$)/i.test(value);
}

export function priorityLabel(value?: string | null): string {
  const map: Record<string, string> = {
    high: '高优先级',
    medium: '中优先级',
    normal: '普通优先级',
    low: '低优先级',
  };
  return map[value || ''] || '建议';
}

export function statusTone(value?: string | null): string {
  if (value === 'completed' || value === 'done' || value === 'ready' || value === 'answered') return 'ok';
  if (value === 'running') return 'run';
  if (value === 'waiting_confirmation' || value === 'pending' || value === 'open') return 'wait';
  if (value === 'failed') return 'bad';
  return 'muted';
}

export function shortTime(value?: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function sessionLabel(label?: string | null, id?: string | null): string {
  return label?.trim() || id || '未知会话';
}

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
  return map[value || ''] || value || '未知';
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
  return map[value || ''] || value || '未知阶段';
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
  return map[value || ''] || value || '协作';
}

export function sourceLabel(value?: string | null): string {
  const map: Record<string, string> = {
    group: '群聊',
    p2p: '私聊',
    workbench: '工作台',
    unknown: '未知来源',
  };
  return map[value || ''] || value || '来源';
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
  return map[value || ''] || value || '产物';
}

export function providerLabel(value?: string | null): string {
  const map: Record<string, string> = {
    llm: 'LLM',
    fallback: '规则兜底',
    local: '本地',
    feishu_doc: '飞书文档',
  };
  return map[value || ''] || value || 'provider';
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

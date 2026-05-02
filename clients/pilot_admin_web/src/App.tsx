import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Boxes,
  CheckCircle2,
  Clipboard,
  Clock3,
  Download,
  FileText,
  Filter,
  Gauge,
  Layers3,
  Loader2,
  MessageSquare,
  PackageCheck,
  PlayCircle,
  Presentation,
  RefreshCw,
  Route,
  Search,
  Send,
  Sparkles,
  SquarePen,
} from 'lucide-react';
import {
  apiBaseUrl,
  artifactUrl,
  bundleDelivery,
  confirmTaskRun,
  connectSocket,
  getRecommendations,
  getTaskRun,
  listTaskRuns,
  reviseDocument,
  reviseSlides,
} from './api';
import {
  artifactLabel,
  intentLabel,
  providerLabel,
  sessionLabel,
  shortTime,
  sourceLabel,
  stageLabel,
  statusLabel,
  statusTone,
} from './labels';
import type {
  ArtifactRecord,
  ConfirmationRequestRecord,
  JsonMap,
  NextActionBundle,
  RealtimeEvent,
  TaskRunDetail,
  TaskRunStepRecord,
  TaskRunSummary,
} from './types';

type ConnectionState = 'idle' | 'connecting' | 'live' | 'polling' | 'error';

type SessionSummary = {
  sessionId: string;
  label: string;
  total: number;
  running: number;
  waiting: number;
  latestTitle: string;
  updatedAt?: string | null;
};

export function App() {
  const [taskRuns, setTaskRuns] = useState<TaskRunSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>('');
  const [detail, setDetail] = useState<TaskRunDetail | null>(null);
  const [recommendations, setRecommendations] = useState<NextActionBundle | null>(null);
  const [sessionQuery, setSessionQuery] = useState('');
  const [searchText, setSearchText] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [submitting, setSubmitting] = useState('');
  const [error, setError] = useState('');
  const [feedState, setFeedState] = useState<ConnectionState>('idle');
  const [taskState, setTaskState] = useState<ConnectionState>('idle');
  const [lastUpdatedAt, setLastUpdatedAt] = useState<Date | null>(null);

  const mergeSummary = useCallback((incoming: TaskRunSummary) => {
    setTaskRuns((current) => {
      const next = [...current];
      const index = next.findIndex((item) => item.task_run_id === incoming.task_run_id);
      if (index >= 0) next[index] = incoming;
      else next.unshift(incoming);
      return next.sort(compareRunTime);
    });
  }, []);

  const loadDetail = useCallback(async (taskRunId: string, quiet = false) => {
    if (!quiet) setDetailLoading(true);
    setError('');
    try {
      const [nextDetail, nextRecommendations] = await Promise.all([
        getTaskRun(taskRunId),
        getRecommendations(taskRunId).catch(() => null),
      ]);
      setSelectedId(taskRunId);
      setDetail(nextDetail);
      setRecommendations(nextRecommendations);
      mergeSummary(summaryFromDetail(nextDetail));
      setLastUpdatedAt(new Date());
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setDetailLoading(false);
    }
  }, [mergeSummary]);

  const refreshList = useCallback(async (keepSelection = true) => {
    setListLoading(true);
    setError('');
    try {
      const runs = await listTaskRuns({
        sessionQuery: sessionQuery.trim() || undefined,
        limit: 80,
      });
      const sorted = runs.sort(compareRunTime);
      setTaskRuns(sorted);
      setLastUpdatedAt(new Date());
      const selectedStillExists = keepSelection && selectedId && sorted.some((item) => item.task_run_id === selectedId);
      if (selectedStillExists) {
        await loadDetail(selectedId, true);
      } else if (sorted[0]) {
        await loadDetail(sorted[0].task_run_id, true);
      } else {
        setSelectedId('');
        setDetail(null);
        setRecommendations(null);
      }
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setListLoading(false);
    }
  }, [loadDetail, selectedId, sessionQuery]);

  useEffect(() => {
    refreshList(false);
  }, []);

  useEffect(() => {
    const close = connectSocket('/ws/task-runs-feed?limit=80', (event) => {
      handleFeedEvent(event, setTaskRuns, selectedId, loadDetail);
      setLastUpdatedAt(new Date());
    }, (state) => setFeedState(state as ConnectionState));
    return close;
  }, [loadDetail, selectedId]);

  useEffect(() => {
    if (!selectedId) return undefined;
    const close = connectSocket(`/ws/task-runs/${selectedId}`, (event) => {
      const payload = event.task_run;
      if (!payload || !('steps' in payload)) return;
      setDetail(payload as TaskRunDetail);
      mergeSummary(summaryFromDetail(payload as TaskRunDetail));
      getRecommendations(selectedId).then(setRecommendations).catch(() => undefined);
      setLastUpdatedAt(new Date());
    }, (state) => setTaskState(state as ConnectionState));
    return close;
  }, [mergeSummary, selectedId]);

  useEffect(() => {
    if (feedState === 'polling' || feedState === 'error' || taskState === 'polling' || taskState === 'error') {
      const timer = window.setInterval(() => refreshList(true), 8000);
      return () => window.clearInterval(timer);
    }
    return undefined;
  }, [feedState, refreshList, taskState]);

  const visibleRuns = useMemo(() => {
    const keyword = searchText.trim().toLowerCase();
    const sessionKeyword = sessionQuery.trim().toLowerCase();
    return taskRuns.filter((item) => {
      if (statusFilter !== 'all' && item.status !== statusFilter) return false;
      if (sessionKeyword) {
        const sessionHaystack = [
          item.session_id,
          item.session_label || '',
        ].join(' ').toLowerCase();
        if (!sessionHaystack.includes(sessionKeyword)) return false;
      }
      if (!keyword) return true;
      return [
        item.title,
        item.session_id,
        item.session_label || '',
        item.intent || '',
        intentLabel(item.intent),
        item.status,
        statusLabel(item.status),
        item.stage,
        stageLabel(item.stage),
        item.latest_summary || '',
        item.latest_reply_preview || '',
      ].join(' ').toLowerCase().includes(keyword);
    });
  }, [searchText, statusFilter, taskRuns]);

  const sessionSummaries = useMemo(() => buildSessionSummaries(taskRuns), [taskRuns]);
  const statusOptions = useMemo(() => buildStatusOptions(taskRuns), [taskRuns]);

  const runAction = async (key: string, action: () => Promise<unknown>) => {
    setSubmitting(key);
    setError('');
    try {
      const result = await action();
      if (result && typeof result === 'object' && 'task_run_id' in result) {
        const nextDetail = result as TaskRunDetail;
        setDetail(nextDetail);
        setSelectedId(nextDetail.task_run_id);
        mergeSummary(summaryFromDetail(nextDetail));
      }
      await refreshList(true);
    } catch (nextError) {
      setError(errorText(nextError));
    } finally {
      setSubmitting('');
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark"><Gauge size={22} /></div>
          <div>
            <h1>飞书智能体工作台</h1>
            <p className="hero-copy">把飞书里的智能体运行态摊开来，让桌面端和移动端都能看见计划、步骤、产物和确认节点。</p>
          </div>
        </div>
        <div className="hero-status">
          <ConnectionPill state={taskState} label="任务流" />
          <ConnectionPill state={feedState} label="会话流" />
        </div>
        <div className="hero-controls">
          <label className="hero-input">
            <span>会话名称查询</span>
            <input value={sessionQuery} onChange={(event) => setSessionQuery(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && refreshList(false)} placeholder="输入群名或人名关键词" />
          </label>
          <button className="hero-button primary" onClick={() => refreshList(false)}>
            <Filter size={17} />应用过滤
          </button>
          <button className="hero-button ghost" onClick={() => refreshList(true)} disabled={listLoading}>
            {listLoading ? <Loader2 className="spin" size={17} /> : <RefreshCw size={17} />}
            {listLoading ? '同步中...' : '刷新任务'}
          </button>
          <span className="info-chip"><CheckCircle2 size={16} />{apiBaseUrl}</span>
          <span className="info-chip"><Clock3 size={16} />{lastUpdatedAt ? `最后同步 ${shortTime(lastUpdatedAt.toISOString())}` : '尚未同步'}</span>
        </div>
      </header>

      {error && (
        <div className="error-strip">
          <AlertCircle size={18} />
          <span>{error}</span>
          <button onClick={() => setError('')}>关闭</button>
        </div>
      )}

      <section className="workspace-grid">
        <aside className="run-list-panel">
          <PanelHeading title="任务运行面板" subtitle="展示当前会话里的任务实例、阶段和运行状态。" />
          <StatsStrip runs={taskRuns} />

          <SessionOverview
            items={sessionSummaries}
            activeSession={sessionQuery.trim()}
            onSelect={(value) => setSessionQuery(value)}
            onClear={() => setSessionQuery('')}
          />

          <div className="filter-row">
            <label className="input-shell">
              <Search size={16} />
              <input value={searchText} onChange={(event) => setSearchText(event.target.value)} placeholder="搜索标题、会话名称、意图或摘要" />
            </label>
            <div className="status-chip-row" aria-label="任务状态筛选">
              {statusOptions.map((item) => (
                <button
                  key={item.key}
                  className={`status-choice ${statusFilter === item.key ? 'active' : ''}`}
                  onClick={() => setStatusFilter(item.key)}
                >
                  <Filter size={14} />
                  {item.label} {item.count}
                </button>
              ))}
            </div>
          </div>

          <div className="run-list">
            {listLoading && taskRuns.length === 0 ? <EmptyState title="加载任务运行中" /> : null}
            {!listLoading && visibleRuns.length === 0 ? <EmptyState title="没有匹配的任务" /> : null}
            {visibleRuns.map((item) => (
              <RunListItem key={item.task_run_id} item={item} selected={item.task_run_id === selectedId} onClick={() => loadDetail(item.task_run_id)} />
            ))}
          </div>
        </aside>

        <section className="detail-panel">
          <PanelHeading title="任务详情与产物" subtitle="步骤时间线、生成产物、确认请求都会从这里实时更新。" />
          {detailLoading ? (
            <div className="center-state"><Loader2 className="spin" />加载详情</div>
          ) : detail ? (
            <TaskDetail
              detail={detail}
              recommendations={recommendations}
              submitting={submitting}
              onBundle={() => runAction('bundle', () => bundleDelivery(detail.task_run_id))}
              onConfirm={(confirmation, option) => runAction(`confirm:${confirmation.confirmation_id}`, () => confirmTaskRun(detail.task_run_id, confirmation.confirmation_id, option))}
              onReviseDocument={(instruction, documentId) => runAction('revise-doc', () => reviseDocument(detail.task_run_id, instruction, documentId))}
              onReviseSlides={(artifactId, instruction) => runAction(`revise-slides:${artifactId}`, () => reviseSlides(detail.task_run_id, instruction, artifactId))}
            />
          ) : (
            <EmptyState title="未选择任务" />
          )}
        </section>
      </section>
    </main>
  );
}

function TaskDetail(props: {
  detail: TaskRunDetail;
  recommendations: NextActionBundle | null;
  submitting: string;
  onBundle: () => void;
  onConfirm: (confirmation: ConfirmationRequestRecord, option: string) => void;
  onReviseDocument: (instruction: string, documentId?: string) => void;
  onReviseSlides: (artifactId: string, instruction: string) => void;
}) {
  const { detail, recommendations } = props;
  return (
    <div className="detail-stack">
      <section className="summary-band">
        <div className="summary-main">
          <div className="title-row">
            <h2>{detail.title}</h2>
            <Badge tone={statusTone(detail.status)}>{statusLabel(detail.status)}</Badge>
          </div>
          <div className="meta-row">
            <span>{sessionLabel(detail.session_label, detail.session_id)}</span>
            <Dot />
            <span>{stageLabel(detail.stage)}</span>
            <Dot />
            <span>{intentLabel(detail.intent)}</span>
            <Dot />
            <span>{sourceLabel(detail.source_type)}</span>
          </div>
          {detail.latest_summary && <p className="summary-text">{detail.latest_summary}</p>}
        </div>
        <div className="summary-actions">
          <button className="primary-button" onClick={props.onBundle} disabled={!detail.artifacts.length || props.submitting === 'bundle'}>
            {props.submitting === 'bundle' ? <Loader2 className="spin" size={17} /> : <PackageCheck size={17} />}
            生成交付包
          </button>
        </div>
      </section>

      <NextActions bundle={recommendations} />
      <ReplyPreview text={detail.latest_reply_preview} error={detail.latest_error} />
      <Timeline steps={detail.steps} />
      <Artifacts detail={detail} submitting={props.submitting} onReviseDocument={props.onReviseDocument} onReviseSlides={props.onReviseSlides} />
      <Confirmations detail={detail} submitting={props.submitting} onConfirm={props.onConfirm} />
    </div>
  );
}

function NextActions({ bundle }: { bundle: NextActionBundle | null }) {
  const items = bundle?.recommendations || [];
  if (!items.length) return null;
  return (
    <section className="section-block">
      <SectionTitle icon={<Sparkles size={17} />} title="推荐下一步" count={items.length} />
      <div className="next-action-grid">
        {items.map((item) => (
          <div className="next-action" key={item.action_id}>
            <div className="row-between">
              <b>{item.title}</b>
              <Badge tone={item.priority === 'high' ? 'wait' : 'muted'}>{item.priority}</Badge>
            </div>
            {item.reason && <p>{item.reason}</p>}
            {item.command && <code>{item.command}</code>}
          </div>
        ))}
      </div>
    </section>
  );
}

function ReplyPreview({ text, error }: { text?: string | null; error?: string | null }) {
  if (!text && !error) return null;
  return (
    <section className="section-block">
      <SectionTitle icon={<MessageSquare size={17} />} title="最近回复" />
      {text && <pre className="reply-preview">{text}</pre>}
      {error && <div className="error-box">{error}</div>}
    </section>
  );
}

function Timeline({ steps }: { steps: TaskRunStepRecord[] }) {
  return (
    <section className="section-block">
      <SectionTitle icon={<Route size={17} />} title="执行步骤" count={steps.length} />
      {steps.length === 0 ? <EmptyState title="暂无步骤" /> : (
        <div className="step-list">
          {steps.map((step) => (
            <div className="step-row" key={step.step_key}>
              <div className={`step-dot tone-${statusTone(step.status)}`} />
              <div>
                <div className="row-between">
                  <b>{step.title || step.step_key}</b>
                  <Badge tone={statusTone(step.status)}>{statusLabel(step.status)}</Badge>
                </div>
                <div className="meta-row">
                  <span>{step.step_type}</span>
                  <Dot />
                  <span>{shortTime(step.updated_at || step.finished_at || step.created_at)}</span>
                </div>
                {step.error && <div className="error-box">{step.error}</div>}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Artifacts(props: {
  detail: TaskRunDetail;
  submitting: string;
  onReviseDocument: (instruction: string, documentId?: string) => void;
  onReviseSlides: (artifactId: string, instruction: string) => void;
}) {
  const { detail } = props;
  return (
    <section className="section-block">
      <SectionTitle icon={<Boxes size={17} />} title="产物" count={detail.artifacts.length} />
      {detail.artifacts.length === 0 ? <EmptyState title="暂无产物" /> : (
        <div className="artifact-grid">
          {detail.artifacts.map((artifact) => (
            <ArtifactCard key={artifact.artifact_id} artifact={artifact} detail={detail} submitting={props.submitting} onReviseDocument={props.onReviseDocument} onReviseSlides={props.onReviseSlides} />
          ))}
        </div>
      )}
    </section>
  );
}

function ArtifactCard(props: {
  artifact: ArtifactRecord;
  detail: TaskRunDetail;
  submitting: string;
  onReviseDocument: (instruction: string, documentId?: string) => void;
  onReviseSlides: (artifactId: string, instruction: string) => void;
}) {
  const { artifact, detail } = props;
  const preview = parseJsonMap(artifact.preview_json);
  const exportsMap = asMap(preview?.exports);
  const htmlUrl = artifactUrl((exportsMap?.html as string | undefined) || artifact.url);
  const pptxUrl = artifactUrl(exportsMap?.pptx as string | undefined);
  const doc = detail.session_documents.find((item) => item.is_current) || detail.session_documents[0];

  return (
    <article className="artifact-card">
      <div className="row-between">
        <div className="artifact-title">
          {artifactIcon(artifact.artifact_type)}
          <b>{artifact.title || artifactLabel(artifact.artifact_type)}</b>
        </div>
        <Badge tone={statusTone(artifact.status)}>{artifactLabel(artifact.artifact_type)}</Badge>
      </div>
      <div className="meta-row">
        <span>{providerLabel(artifact.provider)}</span>
        <Dot />
        <span>v{artifact.version}</span>
        <Dot />
        <span>{statusLabel(artifact.status)}</span>
      </div>
      <ArtifactPreview artifact={artifact} preview={preview} />
      <div className="button-row">
        {htmlUrl && <a className="line-button" href={htmlUrl} target="_blank" rel="noreferrer"><PlayCircle size={16} />预览</a>}
        {pptxUrl && <a className="line-button" href={pptxUrl} target="_blank" rel="noreferrer"><Download size={16} />PPT</a>}
        {artifact.artifact_type === 'document' && (
          <button className="line-button" onClick={() => {
            const instruction = window.prompt('文档修订指令');
            if (instruction?.trim()) props.onReviseDocument(instruction.trim(), doc?.document_id);
          }}>
            <SquarePen size={16} />修订
          </button>
        )}
        {artifact.artifact_type === 'slides_package' && (
          <button className="line-button" disabled={props.submitting.startsWith('revise-slides')} onClick={() => {
            const instruction = window.prompt('演示稿修订指令');
            if (instruction?.trim()) props.onReviseSlides(artifact.artifact_id, instruction.trim());
          }}>
            <Presentation size={16} />修订
          </button>
        )}
        {artifact.url && <button className="line-button" onClick={() => navigator.clipboard.writeText(artifactUrl(artifact.url))}><Clipboard size={16} />复制</button>}
      </div>
    </article>
  );
}

function ArtifactPreview({ artifact, preview }: { artifact: ArtifactRecord; preview: JsonMap | null }) {
  if (!preview) return <p className="muted-text">{artifact.url || '无结构化预览'}</p>;
  if (artifact.artifact_type === 'slides_package') {
    const slides = Array.isArray(preview.slides) ? preview.slides.slice(0, 4) as JsonMap[] : [];
    return <div className="mini-list">{slides.map((slide, index) => <span key={index}>P{index + 1}. {String(slide.title || `第 ${index + 1} 页`)}</span>)}</div>;
  }
  if (artifact.artifact_type === 'document') {
    const sections = Array.isArray(preview.sections) ? preview.sections.slice(0, 4) as JsonMap[] : [];
    return <div className="mini-list">{sections.map((section, index) => <span key={index}>{String(section.heading || `章节 ${index + 1}`)}</span>)}</div>;
  }
  if (artifact.artifact_type === 'canvas') {
    const shapes = Array.isArray(preview.shapes) ? preview.shapes : [];
    return <p className="muted-text">{shapes.length} 个节点/连线</p>;
  }
  return <pre className="json-snippet">{JSON.stringify(preview, null, 2).slice(0, 360)}</pre>;
}

function Confirmations(props: {
  detail: TaskRunDetail;
  submitting: string;
  onConfirm: (confirmation: ConfirmationRequestRecord, option: string) => void;
}) {
  return (
    <section className="section-block">
      <SectionTitle icon={<CheckCircle2 size={17} />} title="确认节点" count={props.detail.confirmations.length} />
      {props.detail.confirmations.length === 0 ? <EmptyState title="暂无确认节点" /> : (
        <div className="confirmation-list">
          {props.detail.confirmations.map((confirmation) => (
            <div className="confirmation-row" key={confirmation.confirmation_id}>
              <div>
                <b>{confirmation.prompt}</b>
                {confirmation.answer_value && <p>已选择：{confirmation.answer_value}</p>}
              </div>
              <div className="button-row">
                {confirmationOptions(confirmation).map((option) => (
                  <button key={option} className="line-button" disabled={confirmation.status === 'answered' || props.submitting.startsWith('confirm')} onClick={() => props.onConfirm(confirmation, option)}>
                    <Send size={15} />{option}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function SessionOverview(props: {
  items: SessionSummary[];
  activeSession: string;
  onSelect: (value: string) => void;
  onClear: () => void;
}) {
  if (!props.items.length) return null;
  return (
    <section className="session-overview">
      <div className="session-overview-head">
        <h3>会话概览</h3>
        <button className="text-button" onClick={props.onClear}>查看全部</button>
      </div>
      <div className="session-row">
        {props.items.map((item) => {
          const value = item.label || item.sessionId;
          const active = props.activeSession && props.activeSession === value;
          return (
            <button key={item.sessionId} className={`session-chip ${active ? 'active' : ''}`} onClick={() => props.onSelect(value)}>
              <span className="session-chip-main">
                <strong>{item.label}</strong>
                <small>{item.sessionId}</small>
              </span>
              <p>{item.latestTitle || '等待更多上下文...'}</p>
              <span className="tiny-pills">
                <span>{item.total} 任务</span>
                {item.running > 0 && <span>{item.running} 运行中</span>}
                {item.waiting > 0 && <span>{item.waiting} 待确认</span>}
              </span>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function RunListItem({ item, selected, onClick }: { item: TaskRunSummary; selected: boolean; onClick: () => void }) {
  return (
    <button className={`run-item ${selected ? 'selected' : ''}`} onClick={onClick}>
      <div className="row-between">
        <b>{item.title}</b>
        <Badge tone={statusTone(item.status)}>{statusLabel(item.status)}</Badge>
      </div>
      <div className="run-badge-row">
        <Badge tone="muted">{stageLabel(item.stage)}</Badge>
        <Badge tone="wait">{sourceLabel(item.source_type)}</Badge>
        {item.intent && <Badge tone="run">{intentLabel(item.intent)}</Badge>}
      </div>
      <div className="meta-row">
        <span>{shortTime(item.updated_at || item.created_at)}</span>
      </div>
      <p>{item.latest_summary || item.latest_reply_preview || '等待执行结果'}</p>
      <span className="session-line">{sessionLabel(item.session_label, item.session_id)}</span>
      <small className="session-id-line">会话：{item.session_id} · 更新于 {shortTime(item.updated_at || item.created_at)}</small>
    </button>
  );
}

function StatsStrip({ runs }: { runs: TaskRunSummary[] }) {
  const running = runs.filter((item) => item.status === 'running').length;
  const waiting = runs.filter((item) => item.status === 'waiting_confirmation').length;
  const completed = runs.filter((item) => item.status === 'completed').length;
  return (
    <div className="stats-strip">
      <Stat label="全部任务" value={runs.length} />
      <Stat label="运行中" value={running} />
      <Stat label="待确认" value={waiting} />
      <Stat label="已完成" value={completed} />
    </div>
  );
}

function SectionTitle({ icon, title, count }: { icon: React.ReactNode; title: string; count?: number }) {
  return <div className="section-title">{icon}<h3>{title}</h3>{typeof count === 'number' && <span>{count}</span>}</div>;
}

function PanelHeading({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="panel-heading">
      <h2>{title}</h2>
      <p>{subtitle}</p>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return <div className="stat"><b>{value}</b><span>{label}</span></div>;
}

function Badge({ children, tone }: { children: React.ReactNode; tone: string }) {
  return <span className={`badge tone-${tone}`}>{children}</span>;
}

function Dot() {
  return <span className="dot" />;
}

function EmptyState({ title }: { title: string }) {
  return <div className="empty-state">{title}</div>;
}

function ConnectionPill({ state, label }: { state: ConnectionState; label: string }) {
  return <span className={`connection-pill ${state}`}><Clock3 size={14} />{label} {connectionLabel(state)}</span>;
}

function artifactIcon(type: string) {
  if (type === 'document') return <FileText size={18} />;
  if (type === 'slides_package' || type === 'slides') return <Presentation size={18} />;
  if (type === 'delivery_bundle') return <PackageCheck size={18} />;
  return <Layers3 size={18} />;
}

function connectionLabel(state: ConnectionState) {
  const map: Record<ConnectionState, string> = {
    idle: '待机',
    connecting: '连接',
    live: '实时',
    polling: '轮询',
    error: '异常',
  };
  return map[state];
}

function handleFeedEvent(
  event: RealtimeEvent,
  setTaskRuns: (value: React.SetStateAction<TaskRunSummary[]>) => void,
  selectedId: string,
  loadDetail: (taskRunId: string, quiet?: boolean) => Promise<void>,
) {
  if (Array.isArray(event.task_runs)) {
    setTaskRuns(event.task_runs.sort(compareRunTime));
    return;
  }
  if (event.task_run && !('steps' in event.task_run)) {
    setTaskRuns((current) => {
      const incoming = event.task_run as TaskRunSummary;
      const next = [...current];
      const index = next.findIndex((item) => item.task_run_id === incoming.task_run_id);
      if (index >= 0) next[index] = incoming;
      else next.unshift(incoming);
      return next.sort(compareRunTime);
    });
    if (event.task_run.task_run_id === selectedId) void loadDetail(selectedId, true);
  }
}

function buildSessionSummaries(runs: TaskRunSummary[]): SessionSummary[] {
  const groups = new Map<string, TaskRunSummary[]>();
  for (const run of runs) {
    const current = groups.get(run.session_id) || [];
    current.push(run);
    groups.set(run.session_id, current);
  }
  return [...groups.entries()].map(([sessionId, items]) => {
    const sorted = [...items].sort(compareRunTime);
    return {
      sessionId,
      label: sessionLabel(sorted[0]?.session_label, sessionId),
      total: items.length,
      running: items.filter((item) => item.status === 'running').length,
      waiting: items.filter((item) => item.status === 'waiting_confirmation').length,
      latestTitle: sorted[0]?.title || '',
      updatedAt: sorted[0]?.updated_at || sorted[0]?.created_at,
    };
  }).sort((a, b) => timeValue(b.updatedAt) - timeValue(a.updatedAt));
}

function buildStatusOptions(runs: TaskRunSummary[]) {
  const counts = new Map<string, number>();
  for (const run of runs) counts.set(run.status, (counts.get(run.status) || 0) + 1);
  return [
    { key: 'all', label: '全部', count: runs.length },
    ...[...counts.entries()].sort((a, b) => b[1] - a[1]).map(([key, count]) => ({ key, label: statusLabel(key), count })),
  ];
}

function summaryFromDetail(detail: TaskRunDetail): TaskRunSummary {
  const { steps, artifacts, confirmations, session_documents, metadata_json, ...summary } = detail;
  void steps; void artifacts; void confirmations; void session_documents; void metadata_json;
  return summary;
}

function compareRunTime(a: TaskRunSummary, b: TaskRunSummary) {
  return timeValue(b.updated_at || b.created_at) - timeValue(a.updated_at || a.created_at);
}

function timeValue(value?: string | null) {
  if (!value) return 0;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function parseJsonMap(value?: string | null): JsonMap | null {
  if (!value) return null;
  try {
    const parsed = JSON.parse(value) as unknown;
    return asMap(parsed);
  } catch {
    return null;
  }
}

function asMap(value: unknown): JsonMap | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonMap : null;
}

function confirmationOptions(confirmation: ConfirmationRequestRecord): string[] {
  if (!confirmation.options_json) return [];
  try {
    const parsed = JSON.parse(confirmation.options_json) as unknown;
    return Array.isArray(parsed) ? parsed.map(String).filter(Boolean) : [];
  } catch {
    return [];
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

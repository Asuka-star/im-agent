import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Boxes,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
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
  priorityLabel,
  providerLabel,
  sessionLabel,
  shortTime,
  sourceLabel,
  stageLabel,
  stepTypeLabel,
  statusLabel,
  statusTone,
} from './labels';
import type {
  ArtifactRecord,
  ArtifactCheckRecord,
  ConfirmationRequestRecord,
  ContextPackRecord,
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

type SectionToggleProps = {
  collapsed: boolean;
  onToggle: () => void;
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
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>({});
  const deliveryArtifact = detail.artifacts.find((artifact) => artifact.artifact_type === 'delivery_bundle' && artifact.url);
  const deliveryUrl = deliveryArtifact?.url ? artifactUrl(deliveryArtifact.url) : '';
  const toggleSection = (key: string) => {
    setCollapsedSections((current) => ({ ...current, [key]: !current[key] }));
  };
  const sectionToggle = (key: string): SectionToggleProps => ({
    collapsed: Boolean(collapsedSections[key]),
    onToggle: () => toggleSection(key),
  });
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
            {deliveryArtifact ? '更新交付包' : '生成交付包'}
          </button>
          {deliveryUrl && (
            <>
              <a className="line-button" href={deliveryUrl} target="_blank" rel="noreferrer">
                <PlayCircle size={16} />打开交付包
              </a>
              <button className="line-button" onClick={() => navigator.clipboard.writeText(deliveryUrl)}>
                <Clipboard size={16} />复制链接
              </button>
            </>
          )}
        </div>
      </section>

      <NextActions
        bundle={recommendations}
        detail={detail}
        submitting={props.submitting}
        onBundle={props.onBundle}
        onReviseSlides={props.onReviseSlides}
        toggle={sectionToggle('next-actions')}
      />
      <ReplyPreview text={detail.latest_reply_preview} error={detail.latest_error} toggle={sectionToggle('reply-preview')} />
      <ContextPackPanel pack={detail.context_pack} toggle={sectionToggle('context-pack')} />
      <ArtifactChecks checks={detail.artifact_checks || []} toggle={sectionToggle('artifact-checks')} />
      <Timeline steps={detail.steps} toggle={sectionToggle('timeline')} />
      <Artifacts detail={detail} submitting={props.submitting} onReviseDocument={props.onReviseDocument} onReviseSlides={props.onReviseSlides} toggle={sectionToggle('artifacts')} />
      <Confirmations detail={detail} submitting={props.submitting} onConfirm={props.onConfirm} toggle={sectionToggle('confirmations')} />
    </div>
  );
}

function NextActions({
  bundle,
  detail,
  submitting,
  onBundle,
  onReviseSlides,
  toggle,
}: {
  bundle: NextActionBundle | null;
  detail: TaskRunDetail;
  submitting: string;
  onBundle: () => void;
  onReviseSlides: (artifactId: string, instruction: string) => void;
  toggle: SectionToggleProps;
}) {
  const items = bundle?.recommendations || [];
  const slidesArtifact = detail.artifacts.find((artifact) => artifact.artifact_type === 'slides_package');
  if (!items.length) return null;
  return (
    <section className="section-block">
      <SectionTitle icon={<Sparkles size={17} />} title="推荐下一步" count={items.length} collapsed={toggle.collapsed} onToggle={toggle.onToggle} />
      {!toggle.collapsed && <div className="next-action-grid">
        {items.map((item) => {
          const command = item.command?.trim() || '';
          const canBundle = item.action_type === 'bundle_delivery' && detail.artifacts.length > 0;
          const canReviseSlides = item.action_type === 'revise_slides' && Boolean(slidesArtifact?.artifact_id && command);
          return (
            <div className="next-action" key={item.action_id}>
              <div className="row-between">
                <b>{item.title}</b>
                <Badge tone={item.priority === 'high' ? 'wait' : 'muted'}>{priorityLabel(item.priority)}</Badge>
              </div>
              {item.reason && <p>{item.reason}</p>}
              {command && <code>{command}</code>}
              <div className="button-row">
                {canBundle && (
                  <button className="line-button" disabled={submitting === 'bundle'} onClick={onBundle}>
                    {submitting === 'bundle' ? <Loader2 className="spin" size={15} /> : <PackageCheck size={15} />}
                    打包
                  </button>
                )}
                {canReviseSlides && slidesArtifact && (
                  <button
                    className="line-button"
                    disabled={submitting.startsWith('revise-slides')}
                    onClick={() => onReviseSlides(slidesArtifact.artifact_id, command)}
                  >
                    {submitting.startsWith('revise-slides') ? <Loader2 className="spin" size={15} /> : <Presentation size={15} />}
                    修订 PPT
                  </button>
                )}
                {command && (
                  <button className="line-button" onClick={() => navigator.clipboard.writeText(command)}>
                    <Clipboard size={15} />复制指令
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>}
    </section>
  );
}

function ContextPackPanel({ pack, toggle }: { pack?: ContextPackRecord | null; toggle: SectionToggleProps }) {
  if (!pack) return null;
  const usedSources = pack.used_sources || [];
  const missingItems = pack.missing_items || [];
  const suggestions = pack.suggested_inputs || [];
  return (
    <section className="section-block">
      <SectionTitle icon={<Gauge size={17} />} title="上下文依据" collapsed={toggle.collapsed} onToggle={toggle.onToggle} />
      {!toggle.collapsed && (
        <>
          <p className="context-summary">{pack.summary}</p>
          <div className="context-pack-grid">
            <ContextPackColumn title="已使用材料" items={usedSources} empty="暂无可追溯材料" />
            <ContextPackColumn title="建议补充" items={missingItems} empty="上下文较完整" />
          </div>
          {suggestions.length > 0 && (
            <div className="context-suggestions">
              {suggestions.slice(0, 4).map((item, index) => (
                <span key={index}>{item}</span>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function ContextPackColumn({
  title,
  items,
  empty,
}: {
  title: string;
  items: Array<{ kind: string; label: string; detail: string; status: string; url?: string | null }>;
  empty: string;
}) {
  return (
    <div className="context-column">
      <h4>{title}</h4>
      {items.length === 0 ? <p className="muted-text">{empty}</p> : (
        <div className="context-item-list">
          {items.slice(0, 6).map((item, index) => {
            const url = item.url ? artifactUrl(item.url) : '';
            return (
              <article className={`context-item tone-${artifactCheckTone(item.status)}`} key={`${item.kind}-${index}`}>
                <div>
                  <b>{item.label}</b>
                  <span>{contextKindLabel(item.kind)} · {artifactCheckLabel(item.status)}</span>
                </div>
                <p>{item.detail}</p>
                {url && <a href={url} target="_blank" rel="noreferrer">打开</a>}
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ArtifactChecks({ checks, toggle }: { checks: ArtifactCheckRecord[]; toggle: SectionToggleProps }) {
  if (!checks.length) return null;
  const ready = checks.filter((item) => item.status === 'ready').length;
  const sceneChecks = checks.filter((item) => item.category === 'scene_c' || item.category === 'scene_d' || item.category === 'scene_cd');
  return (
    <section className="section-block">
      <SectionTitle icon={<CheckCircle2 size={17} />} title="验收检查" count={checks.length} collapsed={toggle.collapsed} onToggle={toggle.onToggle} />
      {!toggle.collapsed && (
        <>
          <div className="check-summary-row">
            <Badge tone={ready === checks.length ? 'ok' : 'wait'}>{ready}/{checks.length} 已满足</Badge>
            <span>场景 C/D：{sceneChecks.filter((item) => item.status === 'ready').length}/{sceneChecks.length} 已满足</span>
          </div>
          <div className="check-grid">
            {checks.map((check) => (
              <article className={`check-card tone-${artifactCheckTone(check.status)}`} key={check.key}>
                <div className="check-card-head">
                  {artifactCheckIcon(check.status)}
                  <b>{check.label}</b>
                  <Badge tone={artifactCheckTone(check.status)}>{artifactCheckLabel(check.status)}</Badge>
                </div>
                <p>{check.detail || '等待检查结果'}</p>
              </article>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function ReplyPreview({ text, error, toggle }: { text?: string | null; error?: string | null; toggle: SectionToggleProps }) {
  if (!text && !error) return null;
  return (
    <section className="section-block">
      <SectionTitle icon={<MessageSquare size={17} />} title="最近回复" collapsed={toggle.collapsed} onToggle={toggle.onToggle} />
      {!toggle.collapsed && (
        <>
          {text && <pre className="reply-preview">{text}</pre>}
          {error && <div className="error-box">{error}</div>}
        </>
      )}
    </section>
  );
}

function Timeline({ steps, toggle }: { steps: TaskRunStepRecord[]; toggle: SectionToggleProps }) {
  return (
    <section className="section-block">
      <SectionTitle icon={<Route size={17} />} title="执行步骤" count={steps.length} collapsed={toggle.collapsed} onToggle={toggle.onToggle} />
      {!toggle.collapsed && (steps.length === 0 ? <EmptyState title="暂无步骤" /> : (
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
                  <span>{stepTypeLabel(step.step_type)}</span>
                  <Dot />
                  <span>{shortTime(step.updated_at || step.finished_at || step.created_at)}</span>
                </div>
                {step.error && <div className="error-box">{step.error}</div>}
              </div>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

function Artifacts(props: {
  detail: TaskRunDetail;
  submitting: string;
  onReviseDocument: (instruction: string, documentId?: string) => void;
  onReviseSlides: (artifactId: string, instruction: string) => void;
  toggle: SectionToggleProps;
}) {
  const { detail } = props;
  return (
    <section className="section-block">
      <SectionTitle icon={<Boxes size={17} />} title="产物" count={detail.artifacts.length} collapsed={props.toggle.collapsed} onToggle={props.toggle.onToggle} />
      {!props.toggle.collapsed && (detail.artifacts.length === 0 ? <EmptyState title="暂无产物" /> : (
        <div className="artifact-grid">
          {detail.artifacts.map((artifact) => (
            <ArtifactCard key={artifact.artifact_id} artifact={artifact} detail={detail} submitting={props.submitting} onReviseDocument={props.onReviseDocument} onReviseSlides={props.onReviseSlides} />
          ))}
        </div>
      ))}
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
  const pdfUrl = artifactUrl(exportsMap?.pdf as string | undefined);
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
      <ArtifactPreview
        artifact={artifact}
        preview={preview}
        quickRevisionBusy={props.submitting.startsWith('revise-slides')}
        onQuickRevise={artifact.artifact_type === 'slides_package'
          ? (instruction) => props.onReviseSlides(artifact.artifact_id, instruction)
          : undefined}
      />
      <div className="button-row">
        {htmlUrl && <a className="line-button" href={htmlUrl} target="_blank" rel="noreferrer"><PlayCircle size={16} />预览</a>}
        {pptxUrl && <a className="line-button" href={pptxUrl} target="_blank" rel="noreferrer"><Download size={16} />PPT</a>}
        {pdfUrl && <a className="line-button" href={pdfUrl} target="_blank" rel="noreferrer"><Download size={16} />PDF</a>}
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

function ArtifactPreview({
  artifact,
  preview,
  quickRevisionBusy,
  onQuickRevise,
}: {
  artifact: ArtifactRecord;
  preview: JsonMap | null;
  quickRevisionBusy?: boolean;
  onQuickRevise?: (instruction: string) => void;
}) {
  if (!preview) return <p className="muted-text">{artifact.url || '无结构化预览'}</p>;
  if (artifact.artifact_type === 'slides_package') {
    return <SlidesRehearsalPreview preview={preview} quickRevisionBusy={quickRevisionBusy} onQuickRevise={onQuickRevise} />;
  }
  if (artifact.artifact_type === 'document') {
    const sections = Array.isArray(preview.sections) ? preview.sections.slice(0, 4) as JsonMap[] : [];
    return <div className="mini-list">{sections.map((section, index) => <span key={index}>{String(section.heading || `章节 ${index + 1}`)}</span>)}</div>;
  }
  if (artifact.artifact_type === 'canvas') {
    return <CanvasArtifactPreview preview={preview} />;
  }
  if (artifact.artifact_type === 'delivery_bundle') {
    return <DeliveryBundlePreview preview={preview} />;
  }
  return <pre className="json-snippet">{JSON.stringify(preview, null, 2).slice(0, 360)}</pre>;
}

function DeliveryBundlePreview({ preview }: { preview: JsonMap }) {
  const checks = Array.isArray(preview.checks) ? preview.checks.map(asMap).filter((item): item is JsonMap => Boolean(item)) : [];
  const summaries = Array.isArray(preview.artifact_summaries)
    ? preview.artifact_summaries.map(asMap).filter((item): item is JsonMap => Boolean(item))
    : [];
  const contextPack = asMap(preview.context_pack);
  const missingItems = Array.isArray(contextPack?.missing_items)
    ? contextPack.missing_items.map(asMap).filter((item): item is JsonMap => Boolean(item))
    : [];
  const highlights = Array.isArray(preview.highlights) ? preview.highlights.map(String).filter(Boolean) : [];
  const ready = checks.filter((item) => stringValue(item.status) === 'ready').length;
  return (
    <div className="delivery-preview">
      <div className="rehearsal-metrics">
        <span><PackageCheck size={15} />{ready}/{checks.length} 项验收</span>
        <span><Layers3 size={15} />{summaries.length} 个交付物</span>
        <span><Gauge size={15} />{missingItems.length} 项待补依据</span>
      </div>
      {highlights.length > 0 && (
        <div className="mini-list">
          {highlights.slice(0, 3).map((item, index) => <span key={index}>{item}</span>)}
        </div>
      )}
      <div className="delivery-summary-list">
        {summaries.slice(0, 4).map((item, index) => {
          const metrics = Array.isArray(item.metrics) ? item.metrics.map(String).filter(Boolean) : [];
          const warnings = Array.isArray(item.warnings) ? item.warnings.map(String).filter(Boolean) : [];
          return (
            <div className="delivery-summary-row" key={stringValue(item.artifact_id) || index}>
              <b>{stringValue(item.label) || artifactLabel(stringValue(item.artifact_type))}</b>
              <span>{stringValue(item.title) || '协作产物'}</span>
              {metrics.length > 0 && <small>{metrics.slice(0, 3).join(' · ')}</small>}
              {warnings.length > 0 && <em>{warnings[0]}</em>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function CanvasArtifactPreview({ preview }: { preview: JsonMap }) {
  const shapes = Array.isArray(preview.shapes) ? preview.shapes.filter((item) => asMap(item)).map((item) => item as JsonMap) : [];
  const nodes = shapes.filter((shape) => stringValue(shape.type) !== 'arrow');
  const arrows = shapes.filter((shape) => stringValue(shape.type) === 'arrow');
  const summary = asMap(preview.summary);
  const nodeCount = numberValue(summary?.node_count) || nodes.length;
  const arrowCount = numberValue(summary?.arrow_count) || arrows.length;
  const groups = Array.isArray(summary?.groups)
    ? summary.groups.map(String).filter(Boolean)
    : [...new Set(nodes.map((shape) => stringValue(shape.group)).filter(Boolean))];
  const template = stringValue(preview.template || summary?.template) || 'flow';
  const view = canvasViewBox(nodes);
  const nodeById = new Map(nodes.map((node, index) => [stringValue(node.id) || `node-${index}`, node]));
  return (
    <div className="canvas-preview">
      <div className="rehearsal-metrics">
        <span><Layers3 size={15} />{canvasTemplateLabel(template)}</span>
        <span><Boxes size={15} />{nodeCount} 节点</span>
        <span><Route size={15} />{arrowCount} 连线</span>
      </div>
      {groups.length > 0 && <p className="muted-text">分组：{groups.slice(0, 6).join('、')}</p>}
      {nodes.length > 0 ? (
        <div className="canvas-inline-board">
          <svg viewBox={`0 0 ${view.width} ${view.height}`} role="img" aria-label={stringValue(preview.title) || 'Canvas 预览'}>
            <defs>
              <marker id="inline-canvas-arrow" markerWidth="10" markerHeight="8" refX="9" refY="4" orient="auto">
                <path d="M0,0 L10,4 L0,8 Z" fill="#2f7f8a" />
              </marker>
            </defs>
            <rect width="100%" height="100%" rx="14" fill="#f8fbfc" />
            {arrows.map((arrow, index) => {
              const source = nodeById.get(stringValue(arrow.from));
              const target = nodeById.get(stringValue(arrow.to));
              if (!source || !target) return null;
              const sourceBox = canvasNodeBox(source, view);
              const targetBox = canvasNodeBox(target, view);
              const x1 = sourceBox.x + sourceBox.w;
              const y1 = sourceBox.y + sourceBox.h / 2;
              const x2 = targetBox.x;
              const y2 = targetBox.y + targetBox.h / 2;
              const label = stringValue(arrow.label);
              return (
                <g key={stringValue(arrow.id) || `arrow-${index}`}>
                  <line x1={x1} y1={y1} x2={x2} y2={y2} stroke={hexColor(arrow.color, '#2f7f8a')} strokeWidth="2.5" markerEnd="url(#inline-canvas-arrow)" />
                  {label && <text className="canvas-arrow-label" x={(x1 + x2) / 2} y={(y1 + y2) / 2 - 8} textAnchor="middle">{label}</text>}
                </g>
              );
            })}
            {nodes.map((node, index) => {
              const box = canvasNodeBox(node, view);
              const group = stringValue(node.group);
              const text = stringValue(node.text) || `节点 ${index + 1}`;
              const lines = wrapCanvasText(text, Math.max(8, Math.floor(box.w / 13)), group ? 3 : 4);
              return (
                <g key={stringValue(node.id) || `node-${index}`}>
                  <rect x={box.x} y={box.y} width={box.w} height={box.h} rx="8" fill={hexColor(node.color, '#eaf5ff')} stroke={hexColor(node.stroke, '#5a9fd6')} strokeWidth="2" />
                  {group && <text className="canvas-group-label" x={box.x + 12} y={box.y + 18}>{group}</text>}
                  <text className="canvas-node-label" x={box.x + 12} y={box.y + (group ? 42 : 34)}>
                    {lines.map((line, lineIndex) => (
                      <tspan key={lineIndex} x={box.x + 12} dy={lineIndex === 0 ? 0 : 18}>{line}</tspan>
                    ))}
                  </text>
                </g>
              );
            })}
          </svg>
        </div>
      ) : (
        <EmptyState title="暂无可渲染节点" />
      )}
    </div>
  );
}

function SlidesRehearsalPreview({
  preview,
  quickRevisionBusy,
  onQuickRevise,
}: {
  preview: JsonMap;
  quickRevisionBusy?: boolean;
  onQuickRevise?: (instruction: string) => void;
}) {
  const slides = Array.isArray(preview.slides)
    ? preview.slides.map(asMap).filter((item): item is JsonMap => Boolean(item))
    : [];
  const notesCount = slides.filter((slide) => stringValue(slide.speaker_notes).length > 0).length;
  const totalDuration = slides.reduce((sum, slide) => sum + numberValue(slide.duration_sec), 0);
  const missingNotes = slides
    .map((slide, index) => ({ index: index + 1, hasNotes: stringValue(slide.speaker_notes).length > 0 }))
    .filter((item) => !item.hasNotes)
    .map((item) => item.index);
  const denseSlides = slides
    .map((slide, index) => ({ index: index + 1, bullets: Array.isArray(slide.bullets) ? slide.bullets.length : 0 }))
    .filter((item) => item.bullets > 5)
    .map((item) => item.index);
  return (
    <div className="slides-rehearsal">
      <div className="rehearsal-metrics">
        <span><Presentation size={15} />{slides.length} 页</span>
        <span><MessageSquare size={15} />{notesCount}/{slides.length} 页备注</span>
        <span><Clock3 size={15} />{totalDuration ? `${Math.ceil(totalDuration / 60)} 分钟` : '未设置时长'}</span>
      </div>
      {(missingNotes.length > 0 || denseSlides.length > 0) && (
        <div className="rehearsal-alerts">
          {missingNotes.length > 0 && <span>缺少讲稿：第 {missingNotes.join('、第 ')} 页</span>}
          {denseSlides.length > 0 && <span>内容偏密：第 {denseSlides.join('、第 ')} 页</span>}
        </div>
      )}
      <div className="slide-quick-list">
        {slides.slice(0, 6).map((slide, index) => (
          <SlideQuickRevisionRow
            key={index}
            slide={slide}
            page={index + 1}
            disabled={quickRevisionBusy || !onQuickRevise}
            onQuickRevise={onQuickRevise}
          />
        ))}
      </div>
    </div>
  );
}

function SlideQuickRevisionRow({
  slide,
  page,
  disabled,
  onQuickRevise,
}: {
  slide: JsonMap;
  page: number;
  disabled?: boolean;
  onQuickRevise?: (instruction: string) => void;
}) {
  const title = String(slide.title || `第 ${page} 页`);
  const notes = stringValue(slide.speaker_notes);
  const bulletCount = Array.isArray(slide.bullets) ? slide.bullets.length : 0;
  const run = (kind: 'notes' | 'judge' | 'concise') => {
    if (!onQuickRevise) return;
    onQuickRevise(slideRevisionInstruction(kind, page, title));
  };
  return (
    <div className="slide-quick-row">
      <div className="slide-quick-main">
        <b>第 {page} 页：{title}</b>
        <span>{notes ? notes.slice(0, 54) : '待补讲者备注'}{bulletCount > 0 ? ` · ${bulletCount} 条要点` : ''}</span>
      </div>
      <div className="slide-quick-actions">
        <button className="mini-action-button" disabled={disabled} onClick={() => run('notes')}>
          <MessageSquare size={14} />{notes ? '润色备注' : '补备注'}
        </button>
        <button className="mini-action-button" disabled={disabled} onClick={() => run('judge')}>
          <Sparkles size={14} />评委视角
        </button>
        <button className="mini-action-button" disabled={disabled} onClick={() => run('concise')}>
          <SquarePen size={14} />精简此页
        </button>
      </div>
    </div>
  );
}

function Confirmations(props: {
  detail: TaskRunDetail;
  submitting: string;
  onConfirm: (confirmation: ConfirmationRequestRecord, option: string) => void;
  toggle: SectionToggleProps;
}) {
  return (
    <section className="section-block">
      <SectionTitle icon={<CheckCircle2 size={17} />} title="确认节点" count={props.detail.confirmations.length} collapsed={props.toggle.collapsed} onToggle={props.toggle.onToggle} />
      {!props.toggle.collapsed && (props.detail.confirmations.length === 0 ? <EmptyState title="暂无确认节点" /> : (
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
      ))}
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

function SectionTitle({
  icon,
  title,
  count,
  collapsed,
  onToggle,
}: {
  icon: React.ReactNode;
  title: string;
  count?: number;
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  const content = (
    <>
      {icon}
      <h3>{title}</h3>
      {typeof count === 'number' && <span>{count}</span>}
      {onToggle && (collapsed ? <ChevronRight className="collapse-icon" size={16} /> : <ChevronDown className="collapse-icon" size={16} />)}
    </>
  );
  if (onToggle) {
    return (
      <button className="section-title section-toggle" type="button" onClick={onToggle} aria-expanded={!collapsed}>
        {content}
      </button>
    );
  }
  return <div className="section-title">{content}</div>;
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

function artifactCheckIcon(status: string) {
  if (status === 'ready') return <CheckCircle2 size={17} />;
  if (status === 'partial') return <Clock3 size={17} />;
  return <AlertCircle size={17} />;
}

function artifactCheckLabel(status: string) {
  if (status === 'ready') return '已满足';
  if (status === 'partial') return '部分满足';
  if (status === 'missing') return '待补齐';
  return status || '未知';
}

function artifactCheckTone(status: string) {
  if (status === 'ready') return 'ok';
  if (status === 'partial') return 'wait';
  if (status === 'missing') return 'bad';
  return 'muted';
}

function slideRevisionInstruction(kind: 'notes' | 'judge' | 'concise', page: number, title: string) {
  if (kind === 'notes') {
    return `请为第 ${page} 页「${title}」补充或润色讲者备注，并给出建议讲述时长。`;
  }
  if (kind === 'judge') {
    return `请把第 ${page} 页「${title}」改成评委视角，突出赛题价值、完成度和验收证据。`;
  }
  return `请精简第 ${page} 页「${title}」的要点，保留最多 4 条，并保持讲者备注可用。`;
}

function canvasTemplateLabel(template: string) {
  if (template === 'risk') return '风险应对图';
  if (template === 'module') return '模块分工图';
  return '流程图';
}

function contextKindLabel(kind: string) {
  const labels: Record<string, string> = {
    im: 'IM',
    plan: '编排',
    document: '文档',
    document_link: '文档',
    canvas: '白板',
    slides: 'PPT',
    slides_package: 'PPT',
    im_trace: 'IM',
    confirmation: '确认',
  };
  return labels[kind] || kind || '上下文';
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
  const { steps, artifacts, artifact_checks, context_pack, confirmations, session_documents, metadata_json, ...summary } = detail;
  void steps; void artifacts; void artifact_checks; void context_pack; void confirmations; void session_documents; void metadata_json;
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

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function numberValue(value: unknown): number {
  if (typeof value === 'number') return Number.isFinite(value) ? Math.max(value, 0) : 0;
  if (typeof value === 'string') {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(parsed, 0) : 0;
  }
  return 0;
}

function canvasViewBox(nodes: JsonMap[]) {
  if (!nodes.length) return { width: 640, height: 360, offsetX: 40, offsetY: 40 };
  const boxes = nodes.map((node) => ({
    x: numberValue(node.x) || 80,
    y: numberValue(node.y) || 140,
    w: numberValue(node.w || node.width) || 168,
    h: numberValue(node.h || node.height) || 72,
  }));
  const minX = Math.min(...boxes.map((box) => box.x));
  const minY = Math.min(...boxes.map((box) => box.y));
  const maxX = Math.max(...boxes.map((box) => box.x + box.w));
  const maxY = Math.max(...boxes.map((box) => box.y + box.h));
  return {
    width: Math.max(640, maxX - minX + 80),
    height: Math.max(360, maxY - minY + 80),
    offsetX: 40 - minX,
    offsetY: 40 - minY,
  };
}

function canvasNodeBox(node: JsonMap, view: { offsetX: number; offsetY: number }) {
  return {
    x: (numberValue(node.x) || 80) + view.offsetX,
    y: (numberValue(node.y) || 140) + view.offsetY,
    w: numberValue(node.w || node.width) || 168,
    h: numberValue(node.h || node.height) || 72,
  };
}

function hexColor(value: unknown, fallback: string): string {
  const text = stringValue(value);
  return /^#[0-9a-f]{6}$/i.test(text) ? text : fallback;
}

function wrapCanvasText(text: string, maxChars: number, maxLines: number): string[] {
  const source = text.trim();
  if (!source) return [''];
  const lines: string[] = [];
  let current = '';
  for (const char of source) {
    const next = `${current}${char}`;
    if (current && visualLength(next) > maxChars) {
      lines.push(current);
      current = char;
      if (lines.length >= maxLines) break;
    } else {
      current = next;
    }
  }
  if (lines.length < maxLines && current) lines.push(current);
  if (lines.length > maxLines) lines.length = maxLines;
  if (lines.length && source !== lines.join('')) {
    lines[lines.length - 1] = `${lines[lines.length - 1].slice(0, Math.max(1, maxChars - 1))}…`;
  }
  return lines;
}

function visualLength(text: string): number {
  return [...text].reduce((sum, char) => sum + (char.charCodeAt(0) < 128 ? 1 : 2), 0);
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

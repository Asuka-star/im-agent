import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:pilot_workbench/src/config/app_config.dart';
import 'package:pilot_workbench/src/models/task_run_models.dart';
import 'package:pilot_workbench/src/state/workbench_controller.dart';
import 'package:pilot_workbench/src/utils/workbench_labels.dart';

class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key, this.autoInitialize = true});

  final bool autoInitialize;

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  late final WorkbenchController _controller;
  late final TextEditingController _sessionController;
  late final TextEditingController _searchController;
  String _statusFilter = 'all';

  @override
  void initState() {
    super.initState();
    _controller = WorkbenchController();
    _sessionController = TextEditingController(text: _controller.sessionQuery);
    _searchController = TextEditingController();
    if (widget.autoInitialize) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _controller.initialize();
      });
    }
  }

  @override
  void dispose() {
    _sessionController.dispose();
    _searchController.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
        final sessionSummaries = _buildSessionSummaries(_controller.taskRuns);
        final visibleTaskRuns = _buildVisibleTaskRuns(_controller.taskRuns);
        return Scaffold(
          body: Container(
            decoration: const BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topLeft,
                end: Alignment.bottomRight,
                colors: [
                  Color(0xFFF7F3EA),
                  Color(0xFFF1ECE1),
                  Color(0xFFE3EEF3),
                ],
              ),
            ),
            child: SafeArea(
              child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                  children: [
                    _HeroPanel(
                      controller: _controller,
                      sessionController: _sessionController,
                    ),
                    if (_controller.errorMessage != null) ...[
                      const SizedBox(height: 12),
                      _ErrorBanner(
                        message: _controller.errorMessage!,
                        onRetry: _controller.retryCurrentView,
                        onDismiss: _controller.clearError,
                      ),
                    ],
                    const SizedBox(height: 16),
                    Expanded(
                      child: LayoutBuilder(
                        builder: (context, constraints) {
                          final isCompact = constraints.maxWidth < 980;
                          if (isCompact) {
                            return Column(
                              children: [
                                Expanded(
                                  flex: 4,
                                  child: _TaskListPanel(
                                    controller: _controller,
                                    items: visibleTaskRuns,
                                    sessionSummaries: sessionSummaries,
                                    sessionController: _sessionController,
                                    searchController: _searchController,
                                    statusFilter: _statusFilter,
                                    onStatusFilterChanged: (value) {
                                      setState(() {
                                        _statusFilter = value;
                                      });
                                    },
                                    onSearchChanged: (_) {
                                      setState(() {});
                                    },
                                    onSelectSession: _applySessionFilter,
                                  ),
                                ),
                                const SizedBox(height: 16),
                                Expanded(
                                  flex: 5,
                                  child: _DetailPanel(controller: _controller),
                                ),
                              ],
                            );
                          }
                          return Row(
                            children: [
                              Expanded(
                                flex: 5,
                                child: _TaskListPanel(
                                  controller: _controller,
                                  items: visibleTaskRuns,
                                  sessionSummaries: sessionSummaries,
                                  sessionController: _sessionController,
                                  searchController: _searchController,
                                  statusFilter: _statusFilter,
                                  onStatusFilterChanged: (value) {
                                    setState(() {
                                      _statusFilter = value;
                                    });
                                  },
                                  onSearchChanged: (_) {
                                    setState(() {});
                                  },
                                  onSelectSession: _applySessionFilter,
                                ),
                              ),
                              const SizedBox(width: 16),
                              Expanded(
                                flex: 6,
                                child: _DetailPanel(controller: _controller),
                              ),
                            ],
                          );
                        },
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ),
        );
      },
    );
  }

  List<TaskRunSummary> _buildVisibleTaskRuns(List<TaskRunSummary> taskRuns) {
    final keyword = _searchController.text.trim().toLowerCase();
    return taskRuns.where((item) {
      final matchesStatus =
          _statusFilter == 'all' || item.status == _statusFilter;
      final haystack = [
        item.title,
        item.sessionId,
        item.sessionLabel ?? '',
        item.intent ?? '',
        localizeIntent(item.intent),
        item.stage,
        localizeStage(item.stage),
        item.status,
        localizeStatus(item.status),
        item.sourceType,
        localizeSourceType(item.sourceType),
        item.latestSummary ?? '',
        item.latestReplyPreview ?? '',
      ].join(' ').toLowerCase();
      final matchesKeyword = keyword.isEmpty || haystack.contains(keyword);
      return matchesStatus && matchesKeyword;
    }).toList();
  }

  List<_SessionSummary> _buildSessionSummaries(List<TaskRunSummary> taskRuns) {
    final grouped = <String, List<TaskRunSummary>>{};
    for (final item in taskRuns) {
      grouped.putIfAbsent(item.sessionId, () => <TaskRunSummary>[]).add(item);
    }
    final summaries = grouped.entries.map((entry) {
      final runs = entry.value;
      runs.sort((a, b) {
        final aTime =
            a.updatedAt ??
            a.createdAt ??
            DateTime.fromMillisecondsSinceEpoch(0);
        final bTime =
            b.updatedAt ??
            b.createdAt ??
            DateTime.fromMillisecondsSinceEpoch(0);
        return bTime.compareTo(aTime);
      });
      return _SessionSummary(
        sessionId: entry.key,
        sessionLabel: runs.first.sessionLabel,
        totalCount: runs.length,
        runningCount: runs.where((item) => item.status == 'running').length,
        waitingCount: runs
            .where((item) => item.status == 'waiting_confirmation')
            .length,
        completedCount: runs.where((item) => item.status == 'completed').length,
        latestTitle: runs.first.title,
        updatedAt: runs.first.updatedAt ?? runs.first.createdAt,
      );
    }).toList();
    summaries.sort((a, b) {
      final aTime = a.updatedAt ?? DateTime.fromMillisecondsSinceEpoch(0);
      final bTime = b.updatedAt ?? DateTime.fromMillisecondsSinceEpoch(0);
      return bTime.compareTo(aTime);
    });
    return summaries;
  }

  void _applySessionFilter(String sessionId) {
    _sessionController.text = sessionId;
    _controller.applySessionFilter(sessionId);
  }
}

class _HeroPanel extends StatelessWidget {
  const _HeroPanel({required this.controller, required this.sessionController});

  final WorkbenchController controller;
  final TextEditingController sessionController;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(24),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(28),
        gradient: const LinearGradient(
          colors: [Color(0xFF172026), Color(0xFF213848), Color(0xFF2D5668)],
        ),
        boxShadow: [
          BoxShadow(
            color: const Color(0xFF172026).withValues(alpha: 0.18),
            blurRadius: 30,
            offset: const Offset(0, 18),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      AppConfig.appName,
                      style: theme.textTheme.displaySmall?.copyWith(
                        color: Colors.white,
                      ),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      '把飞书里的智能体运行态摊开来，让桌面端和移动端都能看见计划、步骤、产物和确认节点。',
                      style: theme.textTheme.bodyLarge?.copyWith(
                        color: Colors.white.withValues(alpha: 0.82),
                        height: 1.45,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 16),
              Wrap(
                spacing: 10,
                runSpacing: 10,
                children: [
                  _StatusPill(
                    label:
                        '任务流 ${_connectionLabel(controller.taskConnectionState)}',
                    accent: _connectionAccent(controller.taskConnectionState),
                  ),
                  _StatusPill(
                    label:
                        '会话流 ${_connectionLabel(controller.sessionConnectionState)}',
                    accent: _connectionAccent(
                      controller.sessionConnectionState,
                    ),
                  ),
                ],
              ),
            ],
          ),
          const SizedBox(height: 20),
          Wrap(
            spacing: 12,
            runSpacing: 12,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              SizedBox(
                width: 280,
                child: TextField(
                  controller: sessionController,
                  style: const TextStyle(color: Colors.white),
                  decoration: InputDecoration(
                    labelText: '会话名称查询',
                    labelStyle: TextStyle(
                      color: Colors.white.withValues(alpha: 0.75),
                    ),
                    hintText: '输入群名或人名关键词',
                    hintStyle: TextStyle(
                      color: Colors.white.withValues(alpha: 0.45),
                    ),
                    filled: true,
                    fillColor: Colors.white.withValues(alpha: 0.08),
                    border: OutlineInputBorder(
                      borderRadius: BorderRadius.circular(18),
                      borderSide: BorderSide.none,
                    ),
                  ),
                  onSubmitted: controller.applySessionFilter,
                ),
              ),
              FilledButton.icon(
                onPressed: () =>
                    controller.applySessionFilter(sessionController.text),
                icon: const Icon(Icons.filter_alt_rounded),
                label: const Text('应用过滤'),
              ),
              OutlinedButton.icon(
                onPressed: controller.isLoadingList
                    ? null
                    : () => controller.refreshTaskRuns(),
                icon: Icon(
                  controller.isLoadingList
                      ? Icons.hourglass_top_rounded
                      : Icons.sync_rounded,
                ),
                label: Text(controller.isLoadingList ? '同步中...' : '刷新任务'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.white,
                  disabledForegroundColor: Colors.white70,
                  side: BorderSide(color: Colors.white.withValues(alpha: 0.28)),
                ),
              ),
              _InfoChip(
                icon: Icons.cloud_done_rounded,
                text: controller.apiBaseUrl,
              ),
              _InfoChip(
                icon: Icons.schedule_rounded,
                text: controller.lastUpdatedAt == null
                    ? '尚未同步'
                    : '最后同步 ${_formatDateTime(controller.lastUpdatedAt)}',
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _TaskListPanel extends StatelessWidget {
  const _TaskListPanel({
    required this.controller,
    required this.items,
    required this.sessionSummaries,
    required this.sessionController,
    required this.searchController,
    required this.statusFilter,
    required this.onStatusFilterChanged,
    required this.onSearchChanged,
    required this.onSelectSession,
  });

  final WorkbenchController controller;
  final List<TaskRunSummary> items;
  final List<_SessionSummary> sessionSummaries;
  final TextEditingController sessionController;
  final TextEditingController searchController;
  final String statusFilter;
  final ValueChanged<String> onStatusFilterChanged;
  final ValueChanged<String> onSearchChanged;
  final ValueChanged<String> onSelectSession;

  @override
  Widget build(BuildContext context) {
    final statusOptions = _buildStatusOptions(controller.taskRuns);
    return _PanelShell(
      title: '任务运行面板',
      subtitle: '展示当前会话里的任务实例、阶段和运行状态。',
      child: controller.isLoadingList && controller.taskRuns.isEmpty
          ? const Center(child: CircularProgressIndicator())
          : controller.taskRuns.isEmpty
          ? const _EmptyState(
              title: '还没有任务运行数据',
              message: '先从飞书侧触发一次 @机器人 请求，工作台就会开始出现任务轨迹。',
            )
          : items.isEmpty
          ? const _EmptyState(
              title: '当前筛选下没有结果',
              message: '试试清空搜索词、切换状态筛选，或者查看其他会话。',
            )
          : ListView.separated(
              itemCount: items.length + 3,
              separatorBuilder: (context, index) => const SizedBox(height: 14),
              itemBuilder: (context, index) {
                if (index == 0) {
                  return _TaskQuickStats(items: controller.taskRuns);
                }
                if (index == 1) {
                  return _SessionOverview(
                    sessionSummaries: sessionSummaries,
                    activeSessionId: sessionController.text.trim(),
                    onSelectSession: onSelectSession,
                  );
                }
                if (index == 2) {
                  return _TaskFilterBar(
                    searchController: searchController,
                    statusOptions: statusOptions,
                    activeStatus: statusFilter,
                    onSearchChanged: onSearchChanged,
                    onStatusChanged: onStatusFilterChanged,
                  );
                }

                final item = items[index - 3];
                final isSelected =
                    controller.selectedTaskRun?.taskRunId == item.taskRunId;
                return InkWell(
                  borderRadius: BorderRadius.circular(20),
                  onTap: () => controller.selectTaskRun(item.taskRunId),
                  child: AnimatedContainer(
                    duration: const Duration(milliseconds: 180),
                    padding: const EdgeInsets.all(18),
                    decoration: BoxDecoration(
                      color: isSelected
                          ? const Color(0xFFEAF5FB)
                          : Colors.white,
                      borderRadius: BorderRadius.circular(20),
                      border: Border.all(
                        color: isSelected
                            ? const Color(0xFF116A7B)
                            : const Color(0xFFD9E3E8),
                        width: isSelected ? 1.5 : 1,
                      ),
                    ),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(
                          children: [
                            Expanded(
                              child: Text(
                                item.title,
                                style: Theme.of(context).textTheme.titleLarge,
                              ),
                            ),
                            _Badge(
                              label: localizeStatus(item.status),
                              color: _statusColor(item.status),
                            ),
                          ],
                        ),
                        const SizedBox(height: 10),
                        Wrap(
                          spacing: 8,
                          runSpacing: 8,
                          children: [
                            _Badge(
                              label: localizeStage(item.stage),
                              color: const Color(0xFF213848),
                            ),
                            _Badge(
                              label: localizeSourceType(item.sourceType),
                              color: const Color(0xFF8B5E34),
                            ),
                            if ((item.intent ?? '').isNotEmpty)
                              _Badge(
                                label: localizeIntent(item.intent),
                                color: const Color(0xFF116A7B),
                              ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        Text(
                          item.latestSummary?.trim().isNotEmpty == true
                              ? item.latestSummary!
                              : item.latestReplyPreview?.trim().isNotEmpty ==
                                    true
                              ? item.latestReplyPreview!
                              : '等待更多上下文...',
                          maxLines: 3,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.bodyMedium
                              ?.copyWith(
                                color: const Color(0xFF5B6770),
                                height: 1.5,
                              ),
                        ),
                        const SizedBox(height: 12),
                        Text(
                          _sessionDisplayLabel(
                            item.sessionLabel,
                            item.sessionId,
                          ),
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: Theme.of(context).textTheme.titleSmall
                              ?.copyWith(color: const Color(0xFF213848)),
                        ),
                        const SizedBox(height: 4),
                        Text(
                          '会话：${item.sessionId} · 更新于 ${_formatDateTime(item.updatedAt ?? item.createdAt)}',
                          style: Theme.of(context).textTheme.bodySmall
                              ?.copyWith(color: const Color(0xFF72808A)),
                        ),
                      ],
                    ),
                  ),
                );
              },
            ),
    );
  }

  List<_StatusOption> _buildStatusOptions(List<TaskRunSummary> taskRuns) {
    final counts = <String, int>{};
    for (final item in taskRuns) {
      counts[item.status] = (counts[item.status] ?? 0) + 1;
    }
    final options = <_StatusOption>[
      _StatusOption(key: 'all', label: '全部', count: taskRuns.length),
    ];
    for (final entry
        in counts.entries.toList()
          ..sort((a, b) => b.value.compareTo(a.value))) {
      options.add(
        _StatusOption(
          key: entry.key,
          label: _statusLabel(entry.key),
          count: entry.value,
        ),
      );
    }
    return options;
  }
}

class _TaskQuickStats extends StatelessWidget {
  const _TaskQuickStats({required this.items});

  final List<TaskRunSummary> items;

  @override
  Widget build(BuildContext context) {
    final total = items.length;
    final running = items.where((item) => item.status == 'running').length;
    final waiting = items
        .where((item) => item.status == 'waiting_confirmation')
        .length;
    final completed = items.where((item) => item.status == 'completed').length;

    return Row(
      children: [
        Expanded(
          child: _StatCard(
            label: '全部任务',
            value: '$total',
            accent: const Color(0xFF213848),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: _StatCard(
            label: '运行中',
            value: '$running',
            accent: const Color(0xFF8B5E34),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: _StatCard(
            label: '待确认',
            value: '$waiting',
            accent: const Color(0xFFC85D3A),
          ),
        ),
        const SizedBox(width: 10),
        Expanded(
          child: _StatCard(
            label: '已完成',
            value: '$completed',
            accent: const Color(0xFF116A7B),
          ),
        ),
      ],
    );
  }
}

class _StatCard extends StatelessWidget {
  const _StatCard({
    required this.label,
    required this.value,
    required this.accent,
  });

  final String label;
  final String value;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: Theme.of(
              context,
            ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
          ),
          const SizedBox(height: 8),
          Text(
            value,
            style: Theme.of(context).textTheme.titleLarge?.copyWith(
              color: accent,
              fontWeight: FontWeight.w800,
            ),
          ),
        ],
      ),
    );
  }
}

class _SessionOverview extends StatelessWidget {
  const _SessionOverview({
    required this.sessionSummaries,
    required this.activeSessionId,
    required this.onSelectSession,
  });

  final List<_SessionSummary> sessionSummaries;
  final String activeSessionId;
  final ValueChanged<String> onSelectSession;

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Text('会话概览', style: Theme.of(context).textTheme.titleLarge),
            const Spacer(),
            TextButton(
              onPressed: () => onSelectSession(''),
              child: const Text('查看全部'),
            ),
          ],
        ),
        const SizedBox(height: 10),
        SizedBox(
          height: 136,
          child: ListView.separated(
            scrollDirection: Axis.horizontal,
            itemCount: sessionSummaries.length,
            separatorBuilder: (context, index) => const SizedBox(width: 10),
            itemBuilder: (context, index) {
              final session = sessionSummaries[index];
              final isActive =
                  activeSessionId.isNotEmpty &&
                  activeSessionId ==
                      _sessionDisplayLabel(
                        session.sessionLabel,
                        session.sessionId,
                      );
              return _SessionCard(
                summary: session,
                isActive: isActive,
                onTap: () => onSelectSession(
                  _sessionDisplayLabel(session.sessionLabel, session.sessionId),
                ),
              );
            },
          ),
        ),
      ],
    );
  }
}

class _SessionCard extends StatelessWidget {
  const _SessionCard({
    required this.summary,
    required this.isActive,
    required this.onTap,
  });

  final _SessionSummary summary;
  final bool isActive;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return InkWell(
      onTap: onTap,
      borderRadius: BorderRadius.circular(18),
      child: Container(
        width: 224,
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          color: isActive ? const Color(0xFFEAF5FB) : Colors.white,
          borderRadius: BorderRadius.circular(18),
          border: Border.all(
            color: isActive ? const Color(0xFF116A7B) : const Color(0xFFD9E3E8),
            width: isActive ? 1.4 : 1,
          ),
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _sessionDisplayLabel(summary.sessionLabel, summary.sessionId),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
            ),
            const SizedBox(height: 4),
            Text(
              summary.sessionId,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: Text(
                summary.latestTitle,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  height: 1.4,
                  color: const Color(0xFF5B6770),
                ),
              ),
            ),
            const SizedBox(height: 10),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                _TinyPill(
                  label: '${summary.totalCount} 任务',
                  color: const Color(0xFF213848),
                ),
                if (summary.runningCount > 0)
                  _TinyPill(
                    label: '${summary.runningCount} 运行中',
                    color: const Color(0xFF8B5E34),
                  ),
                if (summary.waitingCount > 0)
                  _TinyPill(
                    label: '${summary.waitingCount} 待确认',
                    color: const Color(0xFFC85D3A),
                  ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _TaskFilterBar extends StatelessWidget {
  const _TaskFilterBar({
    required this.searchController,
    required this.statusOptions,
    required this.activeStatus,
    required this.onSearchChanged,
    required this.onStatusChanged,
  });

  final TextEditingController searchController;
  final List<_StatusOption> statusOptions;
  final String activeStatus;
  final ValueChanged<String> onSearchChanged;
  final ValueChanged<String> onStatusChanged;

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        TextField(
          controller: searchController,
          onChanged: onSearchChanged,
          decoration: InputDecoration(
            hintText: '搜索标题、会话名称、意图或摘要',
            prefixIcon: const Icon(Icons.search_rounded),
            filled: true,
            fillColor: Colors.white,
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(16),
              borderSide: const BorderSide(color: Color(0xFFD9E3E8)),
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(16),
              borderSide: const BorderSide(color: Color(0xFFD9E3E8)),
            ),
          ),
        ),
        const SizedBox(height: 10),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: statusOptions
                .map(
                  (option) => Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text('${option.label} ${option.count}'),
                      selected: activeStatus == option.key,
                      onSelected: (_) => onStatusChanged(option.key),
                    ),
                  ),
                )
                .toList(),
          ),
        ),
      ],
    );
  }
}

class _TinyPill extends StatelessWidget {
  const _TinyPill({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 5),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 11,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

class _DetailPanel extends StatelessWidget {
  const _DetailPanel({required this.controller});

  final WorkbenchController controller;

  @override
  Widget build(BuildContext context) {
    final detail = controller.selectedTaskRun;
    return _PanelShell(
      title: '任务详情与产物',
      subtitle: '步骤时间线、生成产物、确认请求都会从这里实时更新。',
      child: detail == null
          ? const _EmptyState(
              title: '选择一个任务运行',
              message: '左侧点开任意任务后，这里会展示步骤、产物和确认节点。',
            )
          : controller.isLoadingDetail
          ? const Center(child: CircularProgressIndicator())
          : SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  _SummaryCard(controller: controller, detail: detail),
                  const SizedBox(height: 16),
                  _StageTimeline(detail: detail),
                  const SizedBox(height: 16),
                  _SectionCard(
                    title: '执行步骤',
                    child: detail.steps.isEmpty
                        ? const Text('当前还没有记录步骤。')
                        : Column(
                            children: detail.steps
                                .map(
                                  (step) => Padding(
                                    padding: const EdgeInsets.only(bottom: 12),
                                    child: _StepTile(step: step),
                                  ),
                                )
                                .toList(),
                          ),
                  ),
                  const SizedBox(height: 16),
                  _SectionCard(
                    title: '产物预览',
                    child: detail.artifacts.isEmpty
                        ? const Text('当前还没有产物。')
                        : Column(
                            children: detail.artifacts
                                .map(
                                  (artifact) => Padding(
                                    padding: const EdgeInsets.only(bottom: 12),
                                    child: _ArtifactTile(artifact: artifact),
                                  ),
                                )
                                .toList(),
                          ),
                  ),
                  const SizedBox(height: 16),
                  _SectionCard(
                    title: '确认节点',
                    child: detail.confirmations.isEmpty
                        ? const Text('当前没有待确认节点。')
                        : Column(
                            children: detail.confirmations
                                .map(
                                  (confirmation) => Padding(
                                    padding: const EdgeInsets.only(bottom: 12),
                                    child: _ConfirmationTile(
                                      controller: controller,
                                      taskRunId: detail.taskRunId,
                                      confirmation: confirmation,
                                    ),
                                  ),
                                )
                                .toList(),
                          ),
                  ),
                ],
              ),
            ),
    );
  }
}

class _SummaryCard extends StatelessWidget {
  const _SummaryCard({required this.controller, required this.detail});

  final WorkbenchController controller;
  final TaskRunDetail detail;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final documentArtifact = _latestDocumentArtifact(detail);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        color: const Color(0xFF172026),
        borderRadius: BorderRadius.circular(24),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  detail.title,
                  style: theme.textTheme.headlineSmall?.copyWith(
                    color: Colors.white,
                  ),
                ),
              ),
              _Badge(
                label: localizeStatus(detail.status),
                color: _statusColor(detail.status),
              ),
            ],
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _Badge(
                label: localizeStage(detail.stage),
                color: const Color(0xFFEF8354),
              ),
              _Badge(
                label: _sessionDisplayLabel(
                  detail.sessionLabel,
                  detail.sessionId,
                ),
                color: const Color(0xFF73C8A9),
              ),
              if ((detail.intent ?? '').isNotEmpty)
                _Badge(
                  label: localizeIntent(detail.intent),
                  color: const Color(0xFF8CCDEB),
                ),
            ],
          ),
          if (_sessionDisplayLabel(detail.sessionLabel, detail.sessionId) !=
              detail.sessionId) ...[
            const SizedBox(height: 10),
            Text(
              '会话 ID：${detail.sessionId}',
              style: theme.textTheme.bodySmall?.copyWith(
                color: Colors.white.withValues(alpha: 0.62),
              ),
            ),
          ],
          const SizedBox(height: 16),
          if ((detail.latestSummary ?? '').isNotEmpty)
            Text(
              detail.latestSummary!,
              style: theme.textTheme.bodyLarge?.copyWith(
                color: Colors.white.withValues(alpha: 0.9),
                height: 1.55,
              ),
            ),
          const SizedBox(height: 14),
          _ActionHintCard(detail: detail),
          if (documentArtifact != null) ...[
            const SizedBox(height: 14),
            _CurrentDocumentCard(
              controller: controller,
              sourceTaskRunId: detail.taskRunId,
              artifact: documentArtifact,
              sessionDocuments: detail.sessionDocuments,
            ),
          ],
          if ((detail.latestReplyPreview ?? '').isNotEmpty) ...[
            const SizedBox(height: 14),
            SelectionArea(
              child: Text(
                detail.latestReplyPreview!,
                style: theme.textTheme.bodyMedium?.copyWith(
                  color: Colors.white.withValues(alpha: 0.7),
                  height: 1.55,
                ),
              ),
            ),
          ],
          if ((detail.latestError ?? '').isNotEmpty) ...[
            const SizedBox(height: 14),
            Text(
              '错误: ${detail.latestError}',
              style: theme.textTheme.bodyMedium?.copyWith(
                color: const Color(0xFFFFB4A2),
              ),
            ),
          ],
        ],
      ),
    );
  }
}

class _CurrentDocumentCard extends StatelessWidget {
  const _CurrentDocumentCard({
    required this.controller,
    required this.sourceTaskRunId,
    required this.artifact,
    required this.sessionDocuments,
  });

  final WorkbenchController controller;
  final String sourceTaskRunId;
  final ArtifactRecord artifact;
  final List<SessionDocumentRecord> sessionDocuments;

  @override
  Widget build(BuildContext context) {
    final selectedDocument = _preferredSessionDocument(sessionDocuments);
    final preview = artifact.preview;
    final sync = preview?['sync'] is Map<String, dynamic>
        ? preview!['sync'] as Map<String, dynamic>
        : const <String, dynamic>{};
    final url = _stringValue(selectedDocument?.url ?? sync['url'] ?? artifact.url);
    final syncTitle = _stringValue(selectedDocument?.title ?? sync['title'] ?? artifact.title);
    final title = syncTitle.isNotEmpty ? syncTitle : artifact.title;
    final version = selectedDocument?.version ?? ((sync['version'] is num) ? (sync['version'] as num).toInt() : artifact.version);
    final mode = _stringValue(selectedDocument?.syncMode ?? sync['mode']);
    final updatedAt = selectedDocument?.updatedAt ?? DateTime.tryParse(_stringValue(sync['updated_at']));
    final targetLabel = selectedDocument?.isCurrent == false ? '已选历史文档' : '当前协作文档';

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: Colors.white.withValues(alpha: 0.12)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.description_rounded, color: Color(0xFF8CCDEB)),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  targetLabel,
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              _Badge(label: 'v$version', color: const Color(0xFF8CCDEB)),
            ],
          ),
          const SizedBox(height: 10),
          Text(
            title,
            style: Theme.of(context).textTheme.bodyLarge?.copyWith(
              color: Colors.white.withValues(alpha: 0.92),
              fontWeight: FontWeight.w600,
            ),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _Badge(
                label: _localizeDocSyncMode(mode),
                color: _docSyncModeColor(mode, synced: url.isNotEmpty),
              ),
              if (updatedAt != null)
                _Badge(
                  label: '更新于 ${_formatDateTime(updatedAt)}',
                  color: const Color(0xFF73C8A9),
                ),
              if (sessionDocuments.length > 1)
                _Badge(
                  label: '共 ${sessionDocuments.length} 份文档',
                  color: const Color(0xFFB89B5E),
                ),
            ],
          ),
          if (url.isNotEmpty) ...[
            const SizedBox(height: 12),
            SelectionArea(
              child: Text(
                url,
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: const Color(0xFFBFE6F1),
                  height: 1.45,
                ),
              ),
            ),
            const SizedBox(height: 12),
            _ActionStrip(
              actions: [
                _InlineAction(
                  icon: Icons.copy_rounded,
                  label: '复制文档链接',
                  onPressed: () async {
                    await Clipboard.setData(ClipboardData(text: url));
                    if (context.mounted) {
                      ScaffoldMessenger.of(
                        context,
                      ).showSnackBar(const SnackBar(content: Text('文档链接已复制')));
                    }
                  },
                ),
                _InlineAction(
                  icon: controller.isSubmittingDocumentRevision
                      ? Icons.hourglass_top_rounded
                      : Icons.edit_note_rounded,
                  label: controller.isSubmittingDocumentRevision
                      ? '修订中...'
                      : '提交修订',
                  onPressed: controller.isSubmittingDocumentRevision
                      ? null
                      : () => _openRevisionDialog(context, selectedDocument),
                ),
              ],
            ),
          ] else ...[
            const SizedBox(height: 12),
            _ActionStrip(
              actions: [
                _InlineAction(
                  icon: controller.isSubmittingDocumentRevision
                      ? Icons.hourglass_top_rounded
                      : Icons.edit_note_rounded,
                  label: controller.isSubmittingDocumentRevision
                      ? '修订中...'
                      : '提交修订',
                  onPressed: controller.isSubmittingDocumentRevision
                      ? null
                      : () => _openRevisionDialog(context, selectedDocument),
                ),
              ],
            ),
          ],
          if (sessionDocuments.length > 1) ...[
            const SizedBox(height: 16),
            Row(
              children: [
                Expanded(
                  child: Text(
                    '最近文档',
                    style: Theme.of(context).textTheme.titleSmall?.copyWith(
                      color: Colors.white.withValues(alpha: 0.92),
                      fontWeight: FontWeight.w700,
                    ),
                  ),
                ),
                TextButton.icon(
                  onPressed: () => _openHistoryDialog(context),
                  icon: const Icon(Icons.timeline_rounded, size: 18),
                  label: const Text('查看全部'),
                  style: TextButton.styleFrom(
                    foregroundColor: const Color(0xFFBFE6F1),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              '按更新时间查看当前会话下的文档版本，可以从任意一份继续发起修订。',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: Colors.white.withValues(alpha: 0.62),
                height: 1.45,
              ),
            ),
            const SizedBox(height: 10),
            ...sessionDocuments
                .take(4)
                .map(
                  (document) => Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: _DocumentHistoryTile(
                      document: document,
                      isBusy: controller.isSubmittingDocumentRevision,
                      onRevise: () => _openRevisionDialog(context, document),
                    ),
                  ),
                ),
          ],
        ],
      ),
    );
  }

  Future<void> _openHistoryDialog(BuildContext context) async {
    await showDialog<void>(
      context: context,
      builder: (context) {
        return Dialog(
          backgroundColor: const Color(0xFF10181D),
          insetPadding: const EdgeInsets.symmetric(horizontal: 24, vertical: 32),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 760, maxHeight: 720),
            child: Padding(
              padding: const EdgeInsets.all(20),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          '文档历史时间线',
                          style: Theme.of(context).textTheme.titleLarge?.copyWith(
                            color: Colors.white,
                            fontWeight: FontWeight.w700,
                          ),
                        ),
                      ),
                      IconButton(
                        onPressed: () => Navigator.of(context).pop(),
                        icon: const Icon(Icons.close_rounded, color: Colors.white70),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    '共 ${sessionDocuments.length} 份文档。这里会保留当前会话里生成过或继续修订过的协作文档，你可以直接从任意版本继续修改。',
                    style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                      color: Colors.white.withValues(alpha: 0.68),
                      height: 1.45,
                    ),
                  ),
                  const SizedBox(height: 18),
                  Expanded(
                    child: ListView.separated(
                      itemCount: sessionDocuments.length,
                      separatorBuilder: (_, _) => const SizedBox(height: 12),
                      itemBuilder: (context, index) {
                        final document = sessionDocuments[index];
                        return _DocumentTimelineTile(
                          index: sessionDocuments.length - index,
                          document: document,
                          isBusy: controller.isSubmittingDocumentRevision,
                          onRevise: () {
                            Navigator.of(context).pop();
                            _openRevisionDialog(context, document);
                          },
                        );
                      },
                    ),
                  ),
                ],
              ),
            ),
          ),
        );
      },
    );
  }

  Future<void> _openRevisionDialog(
    BuildContext context,
    SessionDocumentRecord? selectedDocument,
  ) async {
    final textController = TextEditingController();
    var selectedDocumentId =
        selectedDocument?.documentId ??
        (sessionDocuments.isNotEmpty ? sessionDocuments.first.documentId : '');
    final request = await showDialog<_DocumentRevisionRequest>(
      context: context,
      builder: (context) {
        return StatefulBuilder(
          builder: (context, setState) {
            return AlertDialog(
              backgroundColor: const Color(0xFFFFFCF6),
              title: const Text('修订协作文档'),
              content: SizedBox(
                width: 440,
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      '用一句自然语言描述要怎么改。若当前会话里已有多份文档，可以先选定目标文档，再生成新的修订任务。',
                      style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                        color: const Color(0xFF72808A),
                        height: 1.45,
                      ),
                    ),
                    if (sessionDocuments.isNotEmpty) ...[
                      const SizedBox(height: 14),
                      DropdownButtonFormField<String>(
                        initialValue: selectedDocumentId.isNotEmpty ? selectedDocumentId : null,
                        decoration: const InputDecoration(
                          labelText: '目标文档',
                          border: OutlineInputBorder(),
                        ),
                        items: sessionDocuments
                            .map(
                              (item) => DropdownMenuItem<String>(
                                value: item.documentId,
                                child: Text(
                                  item.isCurrent
                                      ? '${item.title} · 当前'
                                      : '${item.title} · v${item.version}',
                                  overflow: TextOverflow.ellipsis,
                                ),
                              ),
                            )
                            .toList(),
                        onChanged: (value) {
                          setState(() {
                            selectedDocumentId = value ?? '';
                          });
                        },
                      ),
                    ],
                    const SizedBox(height: 14),
                    TextField(
                      controller: textController,
                      minLines: 3,
                      maxLines: 5,
                      autofocus: true,
                      decoration: const InputDecoration(
                        hintText: '例如：补充风险部分，并把表达改得更适合评委阅读',
                        border: OutlineInputBorder(),
                      ),
                    ),
                  ],
                ),
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(context).pop(),
                  child: const Text('取消'),
                ),
                FilledButton.icon(
                  onPressed: () => Navigator.of(context).pop(
                    _DocumentRevisionRequest(
                      instruction: textController.text.trim(),
                      documentId: selectedDocumentId.trim().isEmpty
                          ? null
                          : selectedDocumentId.trim(),
                    ),
                  ),
                  icon: const Icon(Icons.send_rounded),
                  label: const Text('提交修订'),
                ),
              ],
            );
          },
        );
      },
    );
    textController.dispose();
    if (request == null || request.instruction.isEmpty || !context.mounted) {
      return;
    }

    try {
      final detail = await controller.reviseDocument(
        sourceTaskRunId: sourceTaskRunId,
        instruction: request.instruction,
        documentId: request.documentId,
      );
      if (!context.mounted) {
        return;
      }
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('已创建文档修订任务：${detail.title}')));
    } catch (_) {
      if (!context.mounted) {
        return;
      }
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('提交文档修订失败，请稍后重试')));
    }
  }
}

class _DocumentRevisionRequest {
  const _DocumentRevisionRequest({
    required this.instruction,
    required this.documentId,
  });

  final String instruction;
  final String? documentId;
}

class _DocumentHistoryTile extends StatelessWidget {
  const _DocumentHistoryTile({
    required this.document,
    required this.isBusy,
    required this.onRevise,
  });

  final SessionDocumentRecord document;
  final bool isBusy;
  final VoidCallback onRevise;

  @override
  Widget build(BuildContext context) {
    final url = _stringValue(document.url);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Colors.white.withValues(alpha: 0.08)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  document.title.isNotEmpty ? document.title : document.documentId,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              _Badge(
                label: document.isCurrent ? '当前' : 'v${document.version}',
                color: document.isCurrent
                    ? const Color(0xFF8CCDEB)
                    : const Color(0xFFB89B5E),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _Badge(
                label: _localizeDocSyncMode(document.syncMode),
                color: _docSyncModeColor(
                  document.syncMode,
                  synced: url.isNotEmpty,
                ),
              ),
              if (document.updatedAt != null)
                _Badge(
                  label: _formatDateTime(document.updatedAt),
                  color: const Color(0xFF73C8A9),
                ),
            ],
          ),
          if (url.isNotEmpty) ...[
            const SizedBox(height: 8),
            SelectableText(
              url,
              maxLines: 2,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                color: const Color(0xFFBFE6F1),
                height: 1.4,
              ),
            ),
          ],
          const SizedBox(height: 10),
          Align(
            alignment: Alignment.centerLeft,
            child: FilledButton.tonalIcon(
              onPressed: isBusy ? null : onRevise,
              icon: Icon(
                isBusy ? Icons.hourglass_top_rounded : Icons.edit_note_rounded,
                size: 18,
              ),
              label: Text(isBusy ? '修订中...' : '修订这份'),
            ),
          ),
        ],
      ),
    );
  }
}

class _DocumentTimelineTile extends StatelessWidget {
  const _DocumentTimelineTile({
    required this.index,
    required this.document,
    required this.isBusy,
    required this.onRevise,
  });

  final int index;
  final SessionDocumentRecord document;
  final bool isBusy;
  final VoidCallback onRevise;

  @override
  Widget build(BuildContext context) {
    final url = _stringValue(document.url);
    final title = document.title.isNotEmpty ? document.title : document.documentId;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.05),
        borderRadius: BorderRadius.circular(16),
        border: Border.all(color: Colors.white.withValues(alpha: 0.09)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 36,
            height: 36,
            alignment: Alignment.center,
            decoration: BoxDecoration(
              color: document.isCurrent
                  ? const Color(0xFF8CCDEB).withValues(alpha: 0.18)
                  : const Color(0xFFB89B5E).withValues(alpha: 0.16),
              borderRadius: BorderRadius.circular(10),
            ),
            child: Text(
              '$index',
              style: Theme.of(context).textTheme.labelLarge?.copyWith(
                color: Colors.white,
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          const SizedBox(width: 14),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        title,
                        style: Theme.of(context).textTheme.titleSmall?.copyWith(
                          color: Colors.white,
                          fontWeight: FontWeight.w700,
                        ),
                      ),
                    ),
                    _Badge(
                      label: document.isCurrent ? '当前' : 'v${document.version}',
                      color: document.isCurrent
                          ? const Color(0xFF8CCDEB)
                          : const Color(0xFFB89B5E),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    _Badge(
                      label: _localizeDocSyncMode(document.syncMode),
                      color: _docSyncModeColor(document.syncMode, synced: url.isNotEmpty),
                    ),
                    if (document.updatedAt != null)
                      _Badge(
                        label: _formatDateTime(document.updatedAt),
                        color: const Color(0xFF73C8A9),
                      ),
                    if ((document.taskRunId ?? '').isNotEmpty)
                      _Badge(
                        label: 'Task ${document.taskRunId}',
                        color: const Color(0xFFEF8354),
                      ),
                  ],
                ),
                const SizedBox(height: 10),
                SelectableText(
                  document.documentId,
                  style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: Colors.white.withValues(alpha: 0.6),
                    height: 1.4,
                  ),
                ),
                if (url.isNotEmpty) ...[
                  const SizedBox(height: 8),
                  SelectableText(
                    url,
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: const Color(0xFFBFE6F1),
                      height: 1.4,
                    ),
                  ),
                ],
                const SizedBox(height: 12),
                Wrap(
                  spacing: 10,
                  runSpacing: 10,
                  children: [
                    FilledButton.tonalIcon(
                      onPressed: isBusy ? null : onRevise,
                      icon: Icon(
                        isBusy ? Icons.hourglass_top_rounded : Icons.edit_note_rounded,
                        size: 18,
                      ),
                      label: Text(isBusy ? '修订中...' : '从这份继续修订'),
                    ),
                    if (url.isNotEmpty)
                      OutlinedButton.icon(
                        onPressed: () async {
                          await Clipboard.setData(ClipboardData(text: url));
                          if (context.mounted) {
                            ScaffoldMessenger.of(context).showSnackBar(
                              const SnackBar(content: Text('文档链接已复制')),
                            );
                          }
                        },
                        icon: const Icon(Icons.copy_rounded, size: 18),
                        label: const Text('复制链接'),
                        style: OutlinedButton.styleFrom(
                          foregroundColor: const Color(0xFFBFE6F1),
                          side: BorderSide(color: Colors.white.withValues(alpha: 0.14)),
                        ),
                      ),
                  ],
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _StageTimeline extends StatelessWidget {
  const _StageTimeline({required this.detail});

  final TaskRunDetail detail;

  @override
  Widget build(BuildContext context) {
    final stages = _buildStageItems(detail);
    return _SectionCard(
      title: '执行阶段',
      child: SizedBox(
        height: 142,
        child: ListView.separated(
          scrollDirection: Axis.horizontal,
          itemCount: stages.length,
          separatorBuilder: (context, index) => const SizedBox(width: 10),
          itemBuilder: (context, index) {
            final item = stages[index];
            return _StageCard(item: item);
          },
        ),
      ),
    );
  }
}

class _StageCard extends StatelessWidget {
  const _StageCard({required this.item});

  final _StageItem item;

  @override
  Widget build(BuildContext context) {
    final accent = item.state == _StageVisualState.current
        ? const Color(0xFF116A7B)
        : item.state == _StageVisualState.completed
        ? const Color(0xFF213848)
        : const Color(0xFFA5B3BB);

    return Container(
      width: 180,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: item.state == _StageVisualState.current
            ? const Color(0xFFEAF5FB)
            : item.state == _StageVisualState.completed
            ? const Color(0xFFF4F7F8)
            : const Color(0xFFFAFBFB),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(
          color: item.state == _StageVisualState.pending
              ? const Color(0xFFD9E3E8)
              : accent,
          width: item.state == _StageVisualState.current ? 1.5 : 1,
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: 30,
            height: 30,
            decoration: BoxDecoration(
              color: accent.withValues(alpha: 0.14),
              shape: BoxShape.circle,
            ),
            child: Icon(item.icon, color: accent, size: 16),
          ),
          const SizedBox(height: 8),
          Text(
            item.label,
            style: Theme.of(
              context,
            ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 4),
          Text(
            item.caption,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
              color: const Color(0xFF72808A),
              height: 1.4,
            ),
          ),
        ],
      ),
    );
  }
}

class _ActionHintCard extends StatelessWidget {
  const _ActionHintCard({required this.detail});

  final TaskRunDetail detail;

  @override
  Widget build(BuildContext context) {
    final hint = _buildActionHint(detail);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: Colors.white.withValues(alpha: 0.12)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.tips_and_updates_rounded, color: Color(0xFFF6B26B)),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  '当前建议动作',
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    color: Colors.white,
                    fontWeight: FontWeight.w700,
                  ),
                ),
                const SizedBox(height: 6),
                Text(
                  hint,
                  style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: Colors.white.withValues(alpha: 0.86),
                    height: 1.45,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _StepTile extends StatelessWidget {
  const _StepTile({required this.step});

  final TaskRunStepRecord step;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFFF8FBFC),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFD8E2E8)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  step.title,
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              _Badge(
                label: localizeStatus(step.status),
                color: _statusColor(step.status),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '${localizeStepType(step.stepType)} · ${localizeStage(step.stepKey)}',
            style: Theme.of(
              context,
            ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
          ),
          if ((step.outputJson ?? '').isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              step.outputJson!,
              maxLines: 4,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(
                context,
              ).textTheme.bodyMedium?.copyWith(height: 1.45),
            ),
          ],
          if ((step.error ?? '').isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              step.error!,
              style: Theme.of(
                context,
              ).textTheme.bodyMedium?.copyWith(color: const Color(0xFFC85D3A)),
            ),
          ],
        ],
      ),
    );
  }
}

class _ArtifactTile extends StatelessWidget {
  const _ArtifactTile({required this.artifact});

  final ArtifactRecord artifact;

  @override
  Widget build(BuildContext context) {
    final preview = artifact.preview;
    final previewBody = _buildArtifactPreview(context, artifact, preview);
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFD8E2E8)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  artifact.title,
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              _Badge(
                label: localizeArtifactType(artifact.artifactType),
                color: const Color(0xFF116A7B),
              ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '${localizeProvider(artifact.provider)} · 版本 ${artifact.version} · ${localizeStatus(artifact.status)}',
            style: Theme.of(
              context,
            ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
          ),
          const SizedBox(height: 12),
          previewBody,
          if ((artifact.url ?? '').isNotEmpty) ...[
            const SizedBox(height: 12),
            _ActionStrip(
              actions: [
                _InlineAction(
                  icon: Icons.copy_rounded,
                  label: '复制链接',
                  onPressed: () async {
                    await Clipboard.setData(ClipboardData(text: artifact.url!));
                    if (context.mounted) {
                      ScaffoldMessenger.of(
                        context,
                      ).showSnackBar(const SnackBar(content: Text('产物链接已复制')));
                    }
                  },
                ),
              ],
            ),
          ],
          if (preview != null && preview.isNotEmpty) ...[
            const SizedBox(height: 12),
            _ActionStrip(
              actions: [
                _InlineAction(
                  icon: Icons.unfold_more_rounded,
                  label: '查看原始数据',
                  onPressed: () => _showRawPreview(context, artifact, preview),
                ),
                _InlineAction(
                  icon: Icons.copy_all_rounded,
                  label: '复制预览数据',
                  onPressed: () async {
                    await Clipboard.setData(
                      ClipboardData(
                        text: const JsonEncoder.withIndent(
                          '  ',
                        ).convert(preview),
                      ),
                    );
                    if (context.mounted) {
                      ScaffoldMessenger.of(
                        context,
                      ).showSnackBar(const SnackBar(content: Text('预览数据已复制')));
                    }
                  },
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }

  Widget _buildArtifactPreview(
    BuildContext context,
    ArtifactRecord artifact,
    Map<String, dynamic>? preview,
  ) {
    if (preview == null || preview.isEmpty) {
      if ((artifact.url ?? '').isNotEmpty) {
        return SelectableText(
          artifact.url!,
          style: Theme.of(
            context,
          ).textTheme.bodyMedium?.copyWith(color: const Color(0xFF116A7B)),
        );
      }
      return Text(
        '当前没有可预览的结构化内容。',
        style: Theme.of(
          context,
        ).textTheme.bodyMedium?.copyWith(color: const Color(0xFF72808A)),
      );
    }

    switch (artifact.artifactType) {
      case 'document':
        return _DocumentPreview(preview: preview, url: artifact.url);
      case 'slides_package':
        return _SlidesPreview(preview: preview);
      case 'canvas':
        return _CanvasPreview(preview: preview, url: artifact.url);
      default:
        return _GenericPreview(preview: preview);
    }
  }

  void _showRawPreview(
    BuildContext context,
    ArtifactRecord artifact,
    Map<String, dynamic> preview,
  ) {
    final prettyJson = const JsonEncoder.withIndent('  ').convert(preview);
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: const Color(0xFFFFFCF6),
      builder: (context) {
        return SafeArea(
          child: Padding(
            padding: const EdgeInsets.fromLTRB(20, 18, 20, 20),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        '${artifact.title} · 原始预览',
                        style: Theme.of(context).textTheme.titleLarge,
                      ),
                    ),
                    IconButton(
                      onPressed: () => Navigator.of(context).pop(),
                      icon: const Icon(Icons.close_rounded),
                    ),
                  ],
                ),
                Flexible(
                  child: Container(
                    width: double.infinity,
                    padding: const EdgeInsets.all(16),
                    decoration: BoxDecoration(
                      color: const Color(0xFFF7F8FA),
                      borderRadius: BorderRadius.circular(18),
                    ),
                    child: SingleChildScrollView(
                      child: SelectableText(
                        prettyJson,
                        style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                          fontFamily: 'Consolas',
                          height: 1.5,
                        ),
                      ),
                    ),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    );
  }
}

class _DocumentPreview extends StatelessWidget {
  const _DocumentPreview({required this.preview, required this.url});

  final Map<String, dynamic> preview;
  final String? url;

  @override
  Widget build(BuildContext context) {
    final sections =
        (preview['sections'] as List?)
            ?.whereType<Map<String, dynamic>>()
            .toList() ??
        const <Map<String, dynamic>>[];
    final title = (preview['title'] as String?)?.trim();
    final statsAsOf = (preview['stats_as_of'] as String?)?.trim();

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFFF8FBFC),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if ((title ?? '').isNotEmpty)
            Text(
              title!,
              style: Theme.of(
                context,
              ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
            ),
          if ((statsAsOf ?? '').isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              '统计截至 $statsAsOf',
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
            ),
          ],
          const SizedBox(height: 10),
          _DocSyncStatus(url: url),
          if ((url ?? '').isNotEmpty) ...[
            const SizedBox(height: 10),
            SelectableText(
              url!,
              style: Theme.of(
                context,
              ).textTheme.bodyMedium?.copyWith(color: const Color(0xFF116A7B)),
            ),
          ],
          if (sections.isNotEmpty) ...[
            const SizedBox(height: 14),
            ...sections
                .take(4)
                .map(
                  (section) => Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: _DocSectionTile(section: section),
                  ),
                ),
            if (sections.length > 4)
              Text(
                '还有 ${sections.length - 4} 个章节未展开',
                style: Theme.of(
                  context,
                ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
              ),
          ],
        ],
      ),
    );
  }
}

class _DocSyncStatus extends StatelessWidget {
  const _DocSyncStatus({required this.url});

  final String? url;

  @override
  Widget build(BuildContext context) {
    final synced = (url ?? '').isNotEmpty;
    final color = synced ? const Color(0xFF116A7B) : const Color(0xFFB7791F);
    final label = synced ? '已同步到飞书文档' : '当前仅有本地结构化产物';

    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(
            synced ? Icons.cloud_done_rounded : Icons.pending_actions_rounded,
            size: 16,
            color: color,
          ),
          const SizedBox(width: 6),
          Text(
            label,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
              color: color,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }
}

class _DocSectionTile extends StatelessWidget {
  const _DocSectionTile({required this.section});

  final Map<String, dynamic> section;

  @override
  Widget build(BuildContext context) {
    final heading = (section['heading'] as String?)?.trim();
    final paragraphs =
        (section['paragraphs'] as List?)
            ?.map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
            .toList() ??
        const <String>[];

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFD9E3E8)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            (heading ?? '').isNotEmpty ? heading! : '未命名章节',
            style: Theme.of(
              context,
            ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 8),
          ...paragraphs
              .take(2)
              .map(
                (paragraph) => Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Text(
                    paragraph,
                    style: Theme.of(
                      context,
                    ).textTheme.bodyMedium?.copyWith(height: 1.45),
                  ),
                ),
              ),
        ],
      ),
    );
  }
}

class _SlidesPreview extends StatelessWidget {
  const _SlidesPreview({required this.preview});

  final Map<String, dynamic> preview;

  @override
  Widget build(BuildContext context) {
    final theme = (preview['theme'] as String?)?.trim();
    final audience = (preview['audience'] as String?)?.trim();
    final slides =
        (preview['slides'] as List?)
            ?.whereType<Map<String, dynamic>>()
            .toList() ??
        const <Map<String, dynamic>>[];
    final emphasis =
        (preview['emphasis'] as List?)
            ?.map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
            .toList() ??
        const <String>[];
    final assets =
        (preview['assets'] as List?)
            ?.map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
            .toList() ??
        const <String>[];

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          colors: [Color(0xFF213848), Color(0xFF305B6B)],
        ),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if ((theme ?? '').isNotEmpty)
            Text(
              theme!,
              style: Theme.of(
                context,
              ).textTheme.titleLarge?.copyWith(color: Colors.white),
            ),
          if ((audience ?? '').isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              '适用场景：$audience',
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: Colors.white.withValues(alpha: 0.82),
              ),
            ),
          ],
          if (slides.isNotEmpty) ...[
            const SizedBox(height: 14),
            SizedBox(
              height: 182,
              child: ListView.separated(
                scrollDirection: Axis.horizontal,
                itemCount: slides.length.clamp(0, 5),
                separatorBuilder: (context, index) => const SizedBox(width: 10),
                itemBuilder: (context, index) {
                  return _SlideCard(index: index, slide: slides[index]);
                },
              ),
            ),
          ],
          if (emphasis.isNotEmpty) ...[
            const SizedBox(height: 14),
            Text(
              '演示重点',
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                color: Colors.white,
                fontWeight: FontWeight.w700,
              ),
            ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: emphasis
                  .take(4)
                  .map(
                    (item) => Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 10,
                        vertical: 8,
                      ),
                      decoration: BoxDecoration(
                        color: Colors.white.withValues(alpha: 0.1),
                        borderRadius: BorderRadius.circular(999),
                      ),
                      child: Text(
                        item,
                        style: Theme.of(context).textTheme.bodySmall?.copyWith(
                          color: Colors.white.withValues(alpha: 0.9),
                        ),
                      ),
                    ),
                  )
                  .toList(),
            ),
          ],
          if (slides.isNotEmpty) ...[
            const SizedBox(height: 14),
            _ActionStrip(
              actions: [
                _InlineAction(
                  icon: Icons.slideshow_rounded,
                  label: '进入排练模式',
                  onPressed: () => _openRehearsalMode(
                    context,
                    slides: slides,
                    theme: theme,
                    audience: audience,
                    emphasis: emphasis,
                    assets: assets,
                  ),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }

  void _openRehearsalMode(
    BuildContext context, {
    required List<Map<String, dynamic>> slides,
    required String? theme,
    required String? audience,
    required List<String> emphasis,
    required List<String> assets,
  }) {
    showDialog<void>(
      context: context,
      builder: (context) {
        return Dialog(
          insetPadding: const EdgeInsets.all(20),
          backgroundColor: Colors.transparent,
          child: _SlidesRehearsalDialog(
            slides: slides,
            theme: theme,
            audience: audience,
            emphasis: emphasis,
            assets: assets,
          ),
        );
      },
    );
  }
}

class _CanvasPreview extends StatelessWidget {
  const _CanvasPreview({required this.preview, required this.url});

  final Map<String, dynamic> preview;
  final String? url;

  @override
  Widget build(BuildContext context) {
    final title = _stringValue(preview['title']);
    final schema = _stringValue(preview['schema']);
    final version = _stringValue(preview['version']);
    final exports = preview['exports'] is Map<String, dynamic>
        ? preview['exports'] as Map<String, dynamic>
        : const <String, dynamic>{};
    final svgUrl = _stringValue(exports['svg']);
    final shapes =
        (preview['shapes'] as List?)
            ?.whereType<Map<String, dynamic>>()
            .toList() ??
        const <Map<String, dynamic>>[];
    final nodes = shapes
        .where((shape) => _stringValue(shape['type']) != 'arrow')
        .toList();
    final arrows = shapes
        .where((shape) => _stringValue(shape['type']) == 'arrow')
        .toList();

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFFF8FBFC),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFD8E2E8)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.account_tree_rounded, color: Color(0xFF116A7B)),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  title.isNotEmpty ? title : 'Canvas',
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              IconButton(
                tooltip: 'Open canvas preview',
                onPressed: () => _openCanvasDialog(
                  context,
                  title: title.isNotEmpty ? title : 'Canvas',
                  shapes: shapes,
                  svgUrl: svgUrl,
                ),
                icon: const Icon(Icons.open_in_full_rounded),
                color: const Color(0xFF116A7B),
              ),
            ],
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _CanvasMetric(label: 'Nodes', value: nodes.length.toString()),
              _CanvasMetric(label: 'Links', value: arrows.length.toString()),
              if (version.isNotEmpty)
                _CanvasMetric(label: 'Version', value: version),
              if (schema.isNotEmpty)
                _CanvasMetric(label: 'Schema', value: schema),
              if (svgUrl.isNotEmpty) _CanvasMetric(label: 'Export', value: 'SVG'),
            ],
          ),
          if (nodes.isNotEmpty) ...[
            const SizedBox(height: 14),
            _CanvasBoard(shapes: shapes),
            const SizedBox(height: 12),
            ...nodes.take(3).map((shape) => _CanvasShapeTile(shape: shape)),
            if (nodes.length > 3)
              Text(
                '+ ${nodes.length - 3} more nodes',
                style: Theme.of(
                  context,
                ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
              ),
          ],
        ],
      ),
    );
  }

  void _openCanvasDialog(
    BuildContext context, {
    required String title,
    required List<Map<String, dynamic>> shapes,
    required String svgUrl,
  }) {
    showDialog<void>(
      context: context,
      builder: (context) => _CanvasDetailDialog(
        title: title,
        preview: preview,
        shapes: shapes,
        url: url,
        svgUrl: svgUrl,
      ),
    );
  }
}

class _CanvasMetric extends StatelessWidget {
  const _CanvasMetric({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
      decoration: BoxDecoration(
        color: const Color(0xFFEAF5F6),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        '$label: $value',
        style: Theme.of(context).textTheme.bodySmall?.copyWith(
          color: const Color(0xFF116A7B),
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

class _CanvasDetailDialog extends StatelessWidget {
  const _CanvasDetailDialog({
    required this.title,
    required this.preview,
    required this.shapes,
    required this.url,
    required this.svgUrl,
  });

  final String title;
  final Map<String, dynamic> preview;
  final List<Map<String, dynamic>> shapes;
  final String? url;
  final String svgUrl;

  @override
  Widget build(BuildContext context) {
    final size = MediaQuery.sizeOf(context);
    final width = math.min(math.max(size.width - 48, 360), 980).toDouble();
    final height = math.min(math.max(size.height - 80, 460), 760).toDouble();
    final nodes = shapes
        .where((shape) => _stringValue(shape['type']) != 'arrow')
        .toList();
    final arrows = shapes
        .where((shape) => _stringValue(shape['type']) == 'arrow')
        .toList();
    final jsonText = const JsonEncoder.withIndent('  ').convert(preview);

    return Dialog(
      backgroundColor: const Color(0xFFFFFCF6),
      insetPadding: const EdgeInsets.all(24),
      child: SizedBox(
        width: width,
        height: height,
        child: Padding(
          padding: const EdgeInsets.all(20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  const Icon(
                    Icons.account_tree_rounded,
                    color: Color(0xFF116A7B),
                  ),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: Theme.of(context).textTheme.titleLarge?.copyWith(
                        fontWeight: FontWeight.w800,
                      ),
                    ),
                  ),
                  IconButton(
                    onPressed: () => Navigator.of(context).pop(),
                    icon: const Icon(Icons.close_rounded),
                  ),
                ],
              ),
              const SizedBox(height: 10),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  _CanvasMetric(label: 'Nodes', value: nodes.length.toString()),
                  _CanvasMetric(
                    label: 'Links',
                    value: arrows.length.toString(),
                  ),
                  if ((url ?? '').isNotEmpty)
                    _CanvasMetric(label: 'Artifact', value: 'JSON'),
                  if (svgUrl.isNotEmpty)
                    _CanvasMetric(label: 'Export', value: 'SVG'),
                ],
              ),
              const SizedBox(height: 16),
              Expanded(
                child: LayoutBuilder(
                  builder: (context, constraints) {
                    final isNarrow = constraints.maxWidth < 760;
                    final board = _CanvasBoard(
                      shapes: shapes,
                      height: isNarrow
                          ? math.max(240, constraints.maxHeight * 0.48)
                          : constraints.maxHeight,
                    );
                    final raw = _CanvasRawPanel(
                      jsonText: jsonText,
                      url: url,
                      svgUrl: svgUrl,
                    );
                    if (isNarrow) {
                      return Column(
                        children: [
                          board,
                          const SizedBox(height: 14),
                          Expanded(child: raw),
                        ],
                      );
                    }
                    return Row(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        Expanded(flex: 7, child: board),
                        const SizedBox(width: 16),
                        Expanded(flex: 4, child: raw),
                      ],
                    );
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _CanvasRawPanel extends StatelessWidget {
  const _CanvasRawPanel({
    required this.jsonText,
    required this.url,
    required this.svgUrl,
  });

  final String jsonText;
  final String? url;
  final String svgUrl;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: const Color(0xFFE0E5E8)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  'Canvas JSON',
                  style: Theme.of(
                    context,
                  ).textTheme.titleSmall?.copyWith(fontWeight: FontWeight.w800),
                ),
              ),
              IconButton(
                tooltip: 'Copy JSON',
                onPressed: () async {
                  await Clipboard.setData(ClipboardData(text: jsonText));
                  if (context.mounted) {
                    ScaffoldMessenger.of(context).showSnackBar(
                      const SnackBar(content: Text('Canvas JSON 已复制')),
                    );
                  }
                },
                icon: const Icon(Icons.copy_rounded),
              ),
            ],
          ),
          if ((url ?? '').isNotEmpty) ...[
            const SizedBox(height: 4),
            SelectableText(
              url!,
              maxLines: 2,
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(color: const Color(0xFF116A7B)),
            ),
          ],
          if (svgUrl.isNotEmpty) ...[
            const SizedBox(height: 6),
            SelectableText(
              svgUrl,
              maxLines: 2,
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(color: const Color(0xFF7B5A11)),
            ),
          ],
          const SizedBox(height: 10),
          Expanded(
            child: SingleChildScrollView(
              child: SelectableText(
                jsonText,
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  fontFamily: 'Consolas',
                  height: 1.45,
                ),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _CanvasBoard extends StatelessWidget {
  const _CanvasBoard({required this.shapes, this.height = 220});

  final List<Map<String, dynamic>> shapes;
  final double height;

  @override
  Widget build(BuildContext context) {
    final nodes = shapes
        .where((shape) => _stringValue(shape['type']) != 'arrow')
        .map(_CanvasNode.fromShape)
        .where((node) => node.id.isNotEmpty)
        .toList();
    final arrows = shapes
        .where((shape) => _stringValue(shape['type']) == 'arrow')
        .map(_CanvasArrow.fromShape)
        .where((arrow) => arrow.from.isNotEmpty && arrow.to.isNotEmpty)
        .toList();

    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth.isFinite
            ? constraints.maxWidth
            : 520.0;
        final layout = _CanvasLayout.fit(nodes, arrows, Size(width, height));
        return Container(
          width: double.infinity,
          height: height,
          decoration: BoxDecoration(
            color: const Color(0xFFEFF7F7),
            borderRadius: BorderRadius.circular(14),
            border: Border.all(color: const Color(0xFFD2E0E5)),
          ),
          clipBehavior: Clip.antiAlias,
          child: Stack(
            children: [
              Positioned.fill(
                child: CustomPaint(painter: _CanvasArrowPainter(layout)),
              ),
              for (final node in layout.nodes)
                Positioned.fromRect(
                  rect: node.rect,
                  child: _CanvasBoardNode(node: node),
                ),
            ],
          ),
        );
      },
    );
  }
}

class _CanvasBoardNode extends StatelessWidget {
  const _CanvasBoardNode({required this.node});

  final _CanvasPlacedNode node;

  @override
  Widget build(BuildContext context) {
    return DecoratedBox(
      decoration: BoxDecoration(
        color: node.fill,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: node.stroke),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.08),
            blurRadius: 12,
            offset: const Offset(0, 6),
          ),
        ],
      ),
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
        child: Center(
          child: Text(
            node.text.isNotEmpty ? node.text : node.id,
            maxLines: 3,
            overflow: TextOverflow.ellipsis,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
              color: const Color(0xFF163B44),
              fontWeight: FontWeight.w700,
              height: 1.2,
            ),
          ),
        ),
      ),
    );
  }
}

class _CanvasArrowPainter extends CustomPainter {
  const _CanvasArrowPainter(this.layout);

  final _CanvasLayout layout;

  @override
  void paint(Canvas canvas, Size size) {
    final gridPaint = Paint()
      ..color = const Color(0xFFDDEBED)
      ..strokeWidth = 1;
    for (var x = 24.0; x < size.width; x += 32) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), gridPaint);
    }
    for (var y = 24.0; y < size.height; y += 32) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), gridPaint);
    }

    for (final group in layout.groups) {
      final fill = Paint()
        ..color = group.color.withValues(alpha: 0.12)
        ..style = PaintingStyle.fill;
      final border = Paint()
        ..color = group.color.withValues(alpha: 0.34)
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.2;
      final rect = RRect.fromRectAndRadius(
        group.rect,
        const Radius.circular(16),
      );
      canvas.drawRRect(rect, fill);
      canvas.drawRRect(rect, border);
      final labelPainter = TextPainter(
        text: TextSpan(
          text: group.label,
          style: TextStyle(
            color: group.color,
            fontSize: 11,
            fontWeight: FontWeight.w700,
          ),
        ),
        textDirection: TextDirection.ltr,
      )..layout(maxWidth: group.rect.width - 16);
      labelPainter.paint(canvas, group.rect.topLeft + const Offset(10, 6));
    }

    for (final arrow in layout.arrows) {
      final from = layout.nodeById[arrow.from]?.rect;
      final to = layout.nodeById[arrow.to]?.rect;
      if (from == null || to == null) {
        continue;
      }
      final arrowPaint = Paint()
        ..color = arrow.color
        ..strokeWidth = 2.2
        ..strokeCap = StrokeCap.round;
      final start = Offset(from.right, from.center.dy);
      final end = Offset(to.left, to.center.dy);
      final controlGap = math.max(28.0, (end.dx - start.dx).abs() / 2);
      final path = Path()
        ..moveTo(start.dx, start.dy)
        ..cubicTo(
          start.dx + controlGap,
          start.dy,
          end.dx - controlGap,
          end.dy,
          end.dx,
          end.dy,
        );
      canvas.drawPath(path, arrowPaint);
      _drawArrowHead(canvas, arrowPaint, start, end);
      if (arrow.label.isNotEmpty) {
        final labelPainter = TextPainter(
          text: TextSpan(
            text: arrow.label,
            style: TextStyle(
              color: arrow.color,
              fontSize: 10,
              fontWeight: FontWeight.w700,
            ),
          ),
          textDirection: TextDirection.ltr,
        )..layout(maxWidth: math.max(48, (end.dx - start.dx).abs()));
        labelPainter.paint(
          canvas,
          Offset(
            (start.dx + end.dx - labelPainter.width) / 2,
            (start.dy + end.dy) / 2 - 18,
          ),
        );
      }
    }
  }

  void _drawArrowHead(Canvas canvas, Paint paint, Offset start, Offset end) {
    final angle = math.atan2(end.dy - start.dy, end.dx - start.dx);
    const size = 8.0;
    final left = Offset(
      end.dx - size * math.cos(angle - math.pi / 6),
      end.dy - size * math.sin(angle - math.pi / 6),
    );
    final right = Offset(
      end.dx - size * math.cos(angle + math.pi / 6),
      end.dy - size * math.sin(angle + math.pi / 6),
    );
    canvas.drawLine(end, left, paint);
    canvas.drawLine(end, right, paint);
  }

  @override
  bool shouldRepaint(covariant _CanvasArrowPainter oldDelegate) {
    return oldDelegate.layout != layout;
  }
}

class _CanvasLayout {
  const _CanvasLayout({
    required this.nodes,
    required this.arrows,
    required this.groups,
  });

  final List<_CanvasPlacedNode> nodes;
  final List<_CanvasArrow> arrows;
  final List<_CanvasPlacedGroup> groups;

  Map<String, _CanvasPlacedNode> get nodeById => {
    for (final node in nodes) node.id: node,
  };

  static _CanvasLayout fit(
    List<_CanvasNode> sourceNodes,
    List<_CanvasArrow> arrows,
    Size boardSize,
  ) {
    if (sourceNodes.isEmpty) {
      return _CanvasLayout(nodes: const [], arrows: arrows, groups: const []);
    }
    final minX = sourceNodes.map((node) => node.x).reduce(math.min);
    final minY = sourceNodes.map((node) => node.y).reduce(math.min);
    final maxX = sourceNodes
        .map((node) => node.x + node.width)
        .reduce(math.max);
    final maxY = sourceNodes
        .map((node) => node.y + node.height)
        .reduce(math.max);
    final sourceWidth = math.max(maxX - minX, 1);
    final sourceHeight = math.max(maxY - minY, 1);
    final scale = math
        .min(
          (boardSize.width - 32) / sourceWidth,
          (boardSize.height - 32) / sourceHeight,
        )
        .clamp(0.45, 1.2);
    final contentWidth = sourceWidth * scale;
    final contentHeight = sourceHeight * scale;
    final offset = Offset(
      (boardSize.width - contentWidth) / 2 - minX * scale,
      (boardSize.height - contentHeight) / 2 - minY * scale,
    );
    final placedNodes = [
      for (final node in sourceNodes)
        _CanvasPlacedNode(
          id: node.id,
          text: node.text,
          group: node.group,
          fill: node.fill,
          stroke: node.stroke,
          rect: Rect.fromLTWH(
            offset.dx + node.x * scale,
            offset.dy + node.y * scale,
            node.width * scale,
            node.height * scale,
          ),
        ),
    ];
    return _CanvasLayout(
      nodes: placedNodes,
      arrows: arrows,
      groups: _CanvasPlacedGroup.fromNodes(placedNodes),
    );
  }
}

class _CanvasNode {
  const _CanvasNode({
    required this.id,
    required this.text,
    required this.x,
    required this.y,
    required this.width,
    required this.height,
    required this.fill,
    required this.stroke,
    required this.group,
  });

  final String id;
  final String text;
  final double x;
  final double y;
  final double width;
  final double height;
  final Color fill;
  final Color stroke;
  final String group;

  factory _CanvasNode.fromShape(Map<String, dynamic> shape) {
    return _CanvasNode(
      id: _stringValue(shape['id']),
      text: _stringValue(shape['text']),
      x: _doubleValue(shape['x'], 80),
      y: _doubleValue(shape['y'], 120),
      width: _doubleValue(shape['w'] ?? shape['width'], 168),
      height: _doubleValue(shape['h'] ?? shape['height'], 72),
      fill: _colorValue(
        shape['color'] ?? shape['fill'],
        const Color(0xFFEAF5FF),
      ),
      stroke: _colorValue(
        shape['stroke'] ?? shape['border'],
        const Color(0xFF5A9FD6),
      ),
      group: _stringValue(shape['group']),
    );
  }
}

class _CanvasPlacedNode {
  const _CanvasPlacedNode({
    required this.id,
    required this.text,
    required this.group,
    required this.fill,
    required this.stroke,
    required this.rect,
  });

  final String id;
  final String text;
  final String group;
  final Color fill;
  final Color stroke;
  final Rect rect;
}

class _CanvasPlacedGroup {
  const _CanvasPlacedGroup({
    required this.label,
    required this.color,
    required this.rect,
  });

  final String label;
  final Color color;
  final Rect rect;

  static List<_CanvasPlacedGroup> fromNodes(List<_CanvasPlacedNode> nodes) {
    final grouped = <String, List<_CanvasPlacedNode>>{};
    for (final node in nodes) {
      if (node.group.isEmpty) {
        continue;
      }
      grouped.putIfAbsent(node.group, () => []).add(node);
    }
    return [
      for (final entry in grouped.entries)
        if (entry.value.length > 1)
          _CanvasPlacedGroup(
            label: entry.key,
            color: entry.value.first.stroke,
            rect: _boundsFor(entry.value).inflate(14),
          ),
    ];
  }

  static Rect _boundsFor(List<_CanvasPlacedNode> nodes) {
    var rect = nodes.first.rect;
    for (final node in nodes.skip(1)) {
      rect = rect.expandToInclude(node.rect);
    }
    return rect;
  }
}

class _CanvasArrow {
  const _CanvasArrow({
    required this.from,
    required this.to,
    required this.color,
    required this.label,
  });

  final String from;
  final String to;
  final Color color;
  final String label;

  factory _CanvasArrow.fromShape(Map<String, dynamic> shape) {
    return _CanvasArrow(
      from: _stringValue(shape['from']),
      to: _stringValue(shape['to']),
      color: _colorValue(
        shape['color'] ?? shape['stroke'],
        const Color(0xFF2F7F8A),
      ),
      label: _stringValue(shape['label'] ?? shape['text']),
    );
  }
}

class _CanvasShapeTile extends StatelessWidget {
  const _CanvasShapeTile({required this.shape});

  final Map<String, dynamic> shape;

  @override
  Widget build(BuildContext context) {
    final text = _stringValue(shape['text']);
    final type = _stringValue(shape['type']);
    final id = _stringValue(shape['id']);
    final group = _stringValue(shape['group']);
    final stroke = _colorValue(shape['stroke'], const Color(0xFF72808A));
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: stroke.withValues(alpha: 0.42)),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(Icons.crop_square_rounded, size: 18, color: stroke),
          const SizedBox(width: 8),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  text.isNotEmpty ? text : (id.isNotEmpty ? id : 'Node'),
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: Theme.of(
                    context,
                  ).textTheme.bodyMedium?.copyWith(fontWeight: FontWeight.w700),
                ),
                if (type.isNotEmpty || id.isNotEmpty || group.isNotEmpty)
                  Text(
                    [
                      type,
                      id,
                      if (group.isNotEmpty) group,
                    ].where((item) => item.isNotEmpty).join(' / '),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: const Color(0xFF72808A),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _SlidesRehearsalDialog extends StatefulWidget {
  const _SlidesRehearsalDialog({
    required this.slides,
    required this.theme,
    required this.audience,
    required this.emphasis,
    required this.assets,
  });

  final List<Map<String, dynamic>> slides;
  final String? theme;
  final String? audience;
  final List<String> emphasis;
  final List<String> assets;

  @override
  State<_SlidesRehearsalDialog> createState() => _SlidesRehearsalDialogState();
}

class _SlidesRehearsalDialogState extends State<_SlidesRehearsalDialog> {
  late final PageController _pageController;
  int _currentIndex = 0;

  @override
  void initState() {
    super.initState();
    _pageController = PageController();
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final currentSlide = widget.slides[_currentIndex];
    final currentBullets =
        (currentSlide['bullets'] as List?)
            ?.map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
            .toList() ??
        const <String>[];

    return Container(
      constraints: const BoxConstraints(maxWidth: 1180, maxHeight: 860),
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF111920), Color(0xFF1C2D38), Color(0xFF274755)],
        ),
        borderRadius: BorderRadius.circular(30),
        boxShadow: [
          BoxShadow(
            color: Colors.black.withValues(alpha: 0.22),
            blurRadius: 32,
            offset: const Offset(0, 18),
          ),
        ],
      ),
      child: Padding(
        padding: const EdgeInsets.all(20),
        child: LayoutBuilder(
          builder: (context, constraints) {
            final compact = constraints.maxWidth < 940;
            final slideStage = _RehearsalStage(
              pageController: _pageController,
              slides: widget.slides,
              currentIndex: _currentIndex,
              onPageChanged: (index) {
                setState(() {
                  _currentIndex = index;
                });
              },
              onPrevious: _currentIndex == 0
                  ? null
                  : () => _goToPage(_currentIndex - 1),
              onNext: _currentIndex == widget.slides.length - 1
                  ? null
                  : () => _goToPage(_currentIndex + 1),
            );
            final notePanel = _RehearsalNotesPanel(
              currentIndex: _currentIndex,
              totalSlides: widget.slides.length,
              theme: widget.theme,
              audience: widget.audience,
              currentSlide: currentSlide,
              currentBullets: currentBullets,
              emphasis: widget.emphasis,
              assets: widget.assets,
            );

            return Column(
              children: [
                _RehearsalTopBar(
                  title: widget.theme,
                  audience: widget.audience,
                ),
                const SizedBox(height: 18),
                Expanded(
                  child: compact
                      ? Column(
                          children: [
                            Expanded(flex: 5, child: slideStage),
                            const SizedBox(height: 14),
                            Expanded(flex: 4, child: notePanel),
                          ],
                        )
                      : Row(
                          children: [
                            Expanded(flex: 8, child: slideStage),
                            const SizedBox(width: 16),
                            Expanded(flex: 5, child: notePanel),
                          ],
                        ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }

  void _goToPage(int index) {
    _pageController.animateToPage(
      index,
      duration: const Duration(milliseconds: 220),
      curve: Curves.easeOutCubic,
    );
  }
}

class _RehearsalTopBar extends StatelessWidget {
  const _RehearsalTopBar({required this.title, required this.audience});

  final String? title;
  final String? audience;

  @override
  Widget build(BuildContext context) {
    return Row(
      children: [
        Expanded(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                (title ?? '').isNotEmpty ? title! : '演示稿排练模式',
                style: Theme.of(
                  context,
                ).textTheme.headlineSmall?.copyWith(color: Colors.white),
              ),
              if ((audience ?? '').isNotEmpty) ...[
                const SizedBox(height: 6),
                Text(
                  '面向 $audience',
                  style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: Colors.white.withValues(alpha: 0.72),
                  ),
                ),
              ],
            ],
          ),
        ),
        FilledButton.tonalIcon(
          onPressed: () => Navigator.of(context).pop(),
          icon: const Icon(Icons.close_rounded),
          label: const Text('关闭'),
        ),
      ],
    );
  }
}

class _RehearsalStage extends StatelessWidget {
  const _RehearsalStage({
    required this.pageController,
    required this.slides,
    required this.currentIndex,
    required this.onPageChanged,
    required this.onPrevious,
    required this.onNext,
  });

  final PageController pageController;
  final List<Map<String, dynamic>> slides;
  final int currentIndex;
  final ValueChanged<int> onPageChanged;
  final VoidCallback? onPrevious;
  final VoidCallback? onNext;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: const Color(0xFFF9F6EF),
        borderRadius: BorderRadius.circular(26),
      ),
      child: Column(
        children: [
          Expanded(
            child: PageView.builder(
              controller: pageController,
              itemCount: slides.length,
              onPageChanged: onPageChanged,
              itemBuilder: (context, index) {
                return _SlideDeckPage(slide: slides[index], index: index);
              },
            ),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              FilledButton.tonalIcon(
                onPressed: onPrevious,
                icon: const Icon(Icons.arrow_back_rounded),
                label: const Text('上一页'),
              ),
              const Spacer(),
              Text(
                '第 ${currentIndex + 1} / ${slides.length} 页',
                style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                  color: const Color(0xFF37444C),
                ),
              ),
              const Spacer(),
              FilledButton.icon(
                onPressed: onNext,
                icon: const Icon(Icons.arrow_forward_rounded),
                label: const Text('下一页'),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _SlideDeckPage extends StatelessWidget {
  const _SlideDeckPage({required this.slide, required this.index});

  final Map<String, dynamic> slide;
  final int index;

  @override
  Widget build(BuildContext context) {
    final title = (slide['title'] as String?)?.trim();
    final bullets =
        (slide['bullets'] as List?)
            ?.map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
            .toList() ??
        const <String>[];

    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFF172026), Color(0xFF243946), Color(0xFF315768)],
        ),
        borderRadius: BorderRadius.circular(24),
      ),
      padding: const EdgeInsets.all(28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.1),
              borderRadius: BorderRadius.circular(999),
            ),
            child: Text(
              '第 ${index + 1} 页',
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: Colors.white.withValues(alpha: 0.88),
                fontWeight: FontWeight.w700,
              ),
            ),
          ),
          const SizedBox(height: 18),
          Text(
            (title ?? '').isNotEmpty ? title! : '未命名页面',
            style: Theme.of(context).textTheme.displaySmall?.copyWith(
              color: Colors.white,
              fontSize: 32,
              height: 1.18,
            ),
          ),
          const SizedBox(height: 22),
          Expanded(
            child: ListView(
              children: bullets
                  .map(
                    (bullet) => Padding(
                      padding: const EdgeInsets.only(bottom: 14),
                      child: Row(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Container(
                            width: 10,
                            height: 10,
                            margin: const EdgeInsets.only(top: 8),
                            decoration: const BoxDecoration(
                              color: Color(0xFFF6B26B),
                              shape: BoxShape.circle,
                            ),
                          ),
                          const SizedBox(width: 12),
                          Expanded(
                            child: Text(
                              bullet,
                              style: Theme.of(context).textTheme.bodyLarge
                                  ?.copyWith(
                                    color: Colors.white.withValues(alpha: 0.94),
                                    height: 1.5,
                                  ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  )
                  .toList(),
            ),
          ),
        ],
      ),
    );
  }
}

class _RehearsalNotesPanel extends StatelessWidget {
  const _RehearsalNotesPanel({
    required this.currentIndex,
    required this.totalSlides,
    required this.theme,
    required this.audience,
    required this.currentSlide,
    required this.currentBullets,
    required this.emphasis,
    required this.assets,
  });

  final int currentIndex;
  final int totalSlides;
  final String? theme;
  final String? audience;
  final Map<String, dynamic> currentSlide;
  final List<String> currentBullets;
  final List<String> emphasis;
  final List<String> assets;

  @override
  Widget build(BuildContext context) {
    final title = (currentSlide['title'] as String?)?.trim();
    return Container(
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(26),
      ),
      child: SingleChildScrollView(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('排练笔记', style: Theme.of(context).textTheme.headlineSmall),
            const SizedBox(height: 10),
            _Badge(
              label: '当前页面 ${currentIndex + 1}/$totalSlides',
              color: const Color(0xFF116A7B),
            ),
            const SizedBox(height: 14),
            _NoteBlock(
              title: '当前页面主题',
              lines: [
                if ((title ?? '').isNotEmpty) title!,
                ...currentBullets.take(3),
              ],
            ),
            if ((theme ?? '').isNotEmpty || (audience ?? '').isNotEmpty) ...[
              const SizedBox(height: 12),
              _NoteBlock(
                title: '整体定调',
                lines: [
                  if ((theme ?? '').isNotEmpty) '主线：$theme',
                  if ((audience ?? '').isNotEmpty) '受众：$audience',
                ],
              ),
            ],
            if (emphasis.isNotEmpty) ...[
              const SizedBox(height: 12),
              _NoteBlock(title: '需要刻意强调', lines: emphasis.take(4).toList()),
            ],
            if (assets.isNotEmpty) ...[
              const SizedBox(height: 12),
              _NoteBlock(title: '建议补充素材', lines: assets.take(4).toList()),
            ],
          ],
        ),
      ),
    );
  }
}

class _NoteBlock extends StatelessWidget {
  const _NoteBlock({required this.title, required this.lines});

  final String title;
  final List<String> lines;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFFF7F8FA),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFE1E6EA)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            title,
            style: Theme.of(
              context,
            ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 10),
          ...lines.map(
            (line) => Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                '• $line',
                style: Theme.of(
                  context,
                ).textTheme.bodyMedium?.copyWith(height: 1.45),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _SlideCard extends StatelessWidget {
  const _SlideCard({required this.index, required this.slide});

  final int index;
  final Map<String, dynamic> slide;

  @override
  Widget build(BuildContext context) {
    final title = (slide['title'] as String?)?.trim();
    final bullets =
        (slide['bullets'] as List?)
            ?.map((item) => item.toString().trim())
            .where((item) => item.isNotEmpty)
            .toList() ??
        const <String>[];

    return Container(
      width: 220,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            '第 ${index + 1} 页',
            style: Theme.of(
              context,
            ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
          ),
          const SizedBox(height: 6),
          Text(
            (title ?? '').isNotEmpty ? title! : '未命名页面',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: Theme.of(
              context,
            ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
          ),
          const SizedBox(height: 10),
          ...bullets
              .take(4)
              .map(
                (bullet) => Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Text(
                    '• $bullet',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: Theme.of(
                      context,
                    ).textTheme.bodySmall?.copyWith(height: 1.45),
                  ),
                ),
              ),
        ],
      ),
    );
  }
}

class _GenericPreview extends StatelessWidget {
  const _GenericPreview({required this.preview});

  final Map<String, dynamic> preview;

  @override
  Widget build(BuildContext context) {
    final lines = preview.entries
        .take(6)
        .map(
          (entry) =>
              '${localizePreviewKey(entry.key)}：${_stringifyPreviewValue(entry.value)}',
        )
        .toList();

    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: const Color(0xFFF8FBFC),
        borderRadius: BorderRadius.circular(16),
      ),
      child: Text(
        lines.join('\n'),
        style: Theme.of(context).textTheme.bodyMedium?.copyWith(height: 1.45),
      ),
    );
  }
}

class _ActionStrip extends StatelessWidget {
  const _ActionStrip({required this.actions});

  final List<_InlineAction> actions;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 10,
      runSpacing: 10,
      children: actions
          .map(
            (action) => OutlinedButton.icon(
              onPressed: action.onPressed,
              icon: Icon(action.icon, size: 18),
              label: Text(action.label),
            ),
          )
          .toList(),
    );
  }
}

class _InlineAction {
  const _InlineAction({
    required this.icon,
    required this.label,
    required this.onPressed,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onPressed;
}

class _ConfirmationTile extends StatelessWidget {
  const _ConfirmationTile({
    required this.controller,
    required this.taskRunId,
    required this.confirmation,
  });

  final WorkbenchController controller;
  final String taskRunId;
  final ConfirmationRequestRecord confirmation;

  @override
  Widget build(BuildContext context) {
    final options = confirmation.options;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: const Color(0xFFFFFCF6),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFE4DDD0)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  confirmation.prompt,
                  style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
                ),
              ),
              _Badge(
                label: localizeStatus(confirmation.status),
                color: confirmation.status == 'answered'
                    ? const Color(0xFF116A7B)
                    : const Color(0xFFC85D3A),
              ),
            ],
          ),
          if (confirmation.status == 'answered') ...[
            const SizedBox(height: 10),
            Text(
              '已选择：${confirmation.answerValue ?? '-'}${confirmation.answeredBy == null ? '' : ' · ${localizeActor(confirmation.answeredBy)}'}',
              style: Theme.of(context).textTheme.bodyMedium,
            ),
            if (confirmation.answeredAt != null) ...[
              const SizedBox(height: 6),
              Text(
                '确认时间：${_formatDateTime(confirmation.answeredAt)}',
                style: Theme.of(
                  context,
                ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
              ),
            ],
          ] else if (options.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(
              '待你确认后，智能体才会继续推进后续动作。',
              style: Theme.of(
                context,
              ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
            ),
            const SizedBox(height: 12),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: options
                  .map(
                    (option) => FilledButton.tonalIcon(
                      onPressed: controller.isSubmittingConfirmation
                          ? null
                          : () => _openConfirmationDialog(
                              context,
                              option: option,
                            ),
                      icon: Icon(
                        option.contains('取消') || option.contains('稍后')
                            ? Icons.pause_circle_outline_rounded
                            : Icons.task_alt_rounded,
                      ),
                      label: Text(option),
                    ),
                  )
                  .toList(),
            ),
          ],
        ],
      ),
    );
  }

  Future<void> _openConfirmationDialog(
    BuildContext context, {
    required String option,
  }) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) {
        return AlertDialog(
          backgroundColor: const Color(0xFFFFFCF6),
          title: const Text('确认执行选择'),
          content: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                confirmation.prompt,
                style: Theme.of(
                  context,
                ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w700),
              ),
              const SizedBox(height: 12),
              Container(
                width: double.infinity,
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: const Color(0xFFF7F8FA),
                  borderRadius: BorderRadius.circular(14),
                ),
                child: Text(
                  '本次将提交的选择：$option',
                  style: Theme.of(context).textTheme.bodyMedium,
                ),
              ),
              const SizedBox(height: 12),
              Text(
                '确认后，工作台和后端任务运行态都会同步刷新。',
                style: Theme.of(
                  context,
                ).textTheme.bodySmall?.copyWith(color: const Color(0xFF72808A)),
              ),
            ],
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.of(context).pop(false),
              child: const Text('再想想'),
            ),
            FilledButton(
              onPressed: () => Navigator.of(context).pop(true),
              child: const Text('确认提交'),
            ),
          ],
        );
      },
    );

    if (confirmed != true || !context.mounted) {
      return;
    }

    try {
      await controller.answerConfirmation(
        taskRunId: taskRunId,
        confirmationId: confirmation.confirmationId,
        answerValue: option,
      );
      if (!context.mounted) {
        return;
      }
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('已提交确认：$option')));
    } catch (_) {
      if (!context.mounted) {
        return;
      }
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('提交确认失败，请稍后重试')));
    }
  }
}

class _PanelShell extends StatelessWidget {
  const _PanelShell({
    required this.title,
    required this.subtitle,
    required this.child,
  });

  final String title;
  final String subtitle;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: const Color(0xFFFFFCF6),
        borderRadius: BorderRadius.circular(28),
        border: Border.all(color: const Color(0xFFE1DDD2)),
        boxShadow: [
          BoxShadow(
            color: const Color(0xFF172026).withValues(alpha: 0.06),
            blurRadius: 28,
            offset: const Offset(0, 14),
          ),
        ],
      ),
      padding: const EdgeInsets.all(20),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: Theme.of(context).textTheme.headlineSmall),
          const SizedBox(height: 6),
          Text(
            subtitle,
            style: Theme.of(
              context,
            ).textTheme.bodyMedium?.copyWith(color: const Color(0xFF72808A)),
          ),
          const SizedBox(height: 16),
          Expanded(child: child),
        ],
      ),
    );
  }
}

class _SectionCard extends StatelessWidget {
  const _SectionCard({required this.title, required this.child});

  final String title;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(22),
        border: Border.all(color: const Color(0xFFE1E6EA)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 14),
          child,
        ],
      ),
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({required this.label, required this.accent});

  final String label;
  final Color accent;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: accent.withValues(alpha: 0.16),
        borderRadius: BorderRadius.circular(999),
        border: Border.all(color: accent.withValues(alpha: 0.38)),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: Colors.white.withValues(alpha: 0.92),
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _InfoChip extends StatelessWidget {
  const _InfoChip({required this.icon, required this.text});

  final IconData icon;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        color: Colors.white.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Colors.white.withValues(alpha: 0.12)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, color: Colors.white.withValues(alpha: 0.9), size: 18),
          const SizedBox(width: 8),
          Text(
            text,
            style: TextStyle(color: Colors.white.withValues(alpha: 0.9)),
          ),
        ],
      ),
    );
  }
}

class _Badge extends StatelessWidget {
  const _Badge({required this.label, required this.color});

  final String label;
  final Color color;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        label,
        style: TextStyle(
          color: color,
          fontSize: 12,
          fontWeight: FontWeight.w700,
        ),
      ),
    );
  }
}

class _ErrorBanner extends StatelessWidget {
  const _ErrorBanner({
    required this.message,
    required this.onRetry,
    required this.onDismiss,
  });

  final String message;
  final Future<void> Function() onRetry;
  final VoidCallback onDismiss;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: const Color(0xFFFFEEE8),
        borderRadius: BorderRadius.circular(18),
        border: Border.all(color: const Color(0xFFF0C2AF)),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              message,
              style: Theme.of(
                context,
              ).textTheme.bodyMedium?.copyWith(color: const Color(0xFF8B3720)),
            ),
          ),
          const SizedBox(width: 12),
          TextButton(onPressed: onDismiss, child: const Text('关闭')),
          FilledButton.tonal(
            onPressed: () => onRetry(),
            child: const Text('重试'),
          ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.title, required this.message});

  final String title;
  final String message;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Container(
              width: 64,
              height: 64,
              decoration: const BoxDecoration(
                color: Color(0xFFEAF1F4),
                shape: BoxShape.circle,
              ),
              child: const Icon(
                Icons.dashboard_customize_rounded,
                size: 30,
                color: Color(0xFF45606E),
              ),
            ),
            const SizedBox(height: 14),
            Text(
              title,
              style: Theme.of(context).textTheme.titleLarge,
              textAlign: TextAlign.center,
            ),
            const SizedBox(height: 8),
            Text(
              message,
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: const Color(0xFF72808A),
                height: 1.5,
              ),
              textAlign: TextAlign.center,
            ),
          ],
        ),
      ),
    );
  }
}

Color _statusColor(String status) {
  switch (status) {
    case 'completed':
    case 'done':
    case 'answered':
      return const Color(0xFF116A7B);
    case 'failed':
      return const Color(0xFFC85D3A);
    case 'running':
      return const Color(0xFF8B5E34);
    case 'waiting_confirmation':
      return const Color(0xFFB7791F);
    default:
      return const Color(0xFF56636D);
  }
}

Color _connectionAccent(String state) {
  switch (state) {
    case 'live':
      return const Color(0xFF73C8A9);
    case 'connecting':
      return const Color(0xFFF6B26B);
    case 'polling':
      return const Color(0xFF7EC8E3);
    case 'error':
      return const Color(0xFFC85D3A);
    case 'closed':
      return const Color(0xFFB7791F);
    default:
      return const Color(0xFF8EA5B0);
  }
}

String _connectionLabel(String state) {
  switch (state) {
    case 'live':
      return '实时在线';
    case 'connecting':
      return '连接中';
    case 'polling':
      return '轮询模式';
    case 'error':
      return '连接异常';
    case 'closed':
      return '已断开，重连中';
    default:
      return '未连接';
  }
}

String _formatDateTime(DateTime? value) {
  if (value == null) {
    return '-';
  }
  final local = value.toLocal();
  final month = local.month.toString().padLeft(2, '0');
  final day = local.day.toString().padLeft(2, '0');
  final hour = local.hour.toString().padLeft(2, '0');
  final minute = local.minute.toString().padLeft(2, '0');
  return '$month-$day $hour:$minute';
}

ArtifactRecord? _latestDocumentArtifact(TaskRunDetail detail) {
  for (final artifact in detail.artifacts.reversed) {
    if (artifact.artifactType == 'document') {
      return artifact;
    }
  }
  return null;
}

SessionDocumentRecord? _preferredSessionDocument(
  List<SessionDocumentRecord> documents,
) {
  for (final document in documents) {
    if (document.isCurrent) {
      return document;
    }
  }
  return documents.isNotEmpty ? documents.first : null;
}

String _localizeDocSyncMode(String mode) {
  switch (mode) {
    case 'created':
      return '已创建主文档';
    case 'updated':
      return '已更新主文档';
    case 'noop':
      return '文档已是最新';
    case 'local_only':
      return '仅本地生成';
    default:
      return '文档状态未知';
  }
}

String _stringValue(Object? value) {
  final text = value?.toString().trim() ?? '';
  return text == 'null' ? '' : text;
}

double _doubleValue(Object? value, double fallback) {
  if (value is num) {
    return value.toDouble();
  }
  final parsed = double.tryParse(_stringValue(value));
  return parsed ?? fallback;
}

Color _colorValue(Object? value, Color fallback) {
  final text = _stringValue(value);
  final match = RegExp(r'^#?([0-9a-fA-F]{6})$').firstMatch(text);
  if (match == null) {
    return fallback;
  }
  return Color(int.parse('FF${match.group(1)}', radix: 16));
}

Color _docSyncModeColor(String mode, {required bool synced}) {
  switch (mode) {
    case 'updated':
      return const Color(0xFF73C8A9);
    case 'noop':
      return const Color(0xFFF6B26B);
    case 'created':
      return const Color(0xFF8CCDEB);
    case 'local_only':
      return const Color(0xFFC85D3A);
    default:
      return synced ? const Color(0xFF8CCDEB) : const Color(0xFFC85D3A);
  }
}

String _stringifyPreviewValue(Object? value) {
  if (value is List) {
    return '列表(${value.length})';
  }
  if (value is Map) {
    return '对象(${value.length})';
  }
  return value?.toString() ?? '-';
}

String _statusLabel(String status) {
  return localizeStatus(status);
}

class _StatusOption {
  const _StatusOption({
    required this.key,
    required this.label,
    required this.count,
  });

  final String key;
  final String label;
  final int count;
}

class _SessionSummary {
  const _SessionSummary({
    required this.sessionId,
    required this.sessionLabel,
    required this.totalCount,
    required this.runningCount,
    required this.waitingCount,
    required this.completedCount,
    required this.latestTitle,
    required this.updatedAt,
  });

  final String sessionId;
  final String? sessionLabel;
  final int totalCount;
  final int runningCount;
  final int waitingCount;
  final int completedCount;
  final String latestTitle;
  final DateTime? updatedAt;
}

String _sessionDisplayLabel(String? label, String sessionId) {
  final normalized = label?.trim() ?? '';
  return normalized.isNotEmpty ? normalized : sessionId;
}

List<_StageItem> _buildStageItems(TaskRunDetail detail) {
  final order = <_StageItem>[
    const _StageItem(
      key: 'queued',
      label: '请求进入',
      caption: '接收消息触发并建立任务实例',
      icon: Icons.inbox_rounded,
      state: _StageVisualState.pending,
    ),
    const _StageItem(
      key: 'context',
      label: '上下文整理',
      caption: '汇总讨论、记忆和待办信息',
      icon: Icons.hub_rounded,
      state: _StageVisualState.pending,
    ),
    const _StageItem(
      key: 'planning',
      label: '意图理解',
      caption: '识别当前要生成的产物和流程',
      icon: Icons.psychology_alt_rounded,
      state: _StageVisualState.pending,
    ),
    const _StageItem(
      key: 'artifact',
      label: '产物生成',
      caption: '输出文档、演示稿或回复内容',
      icon: Icons.description_rounded,
      state: _StageVisualState.pending,
    ),
    const _StageItem(
      key: 'confirm',
      label: '协作确认',
      caption: '等待人工确认或继续执行',
      icon: Icons.fact_check_rounded,
      state: _StageVisualState.pending,
    ),
    const _StageItem(
      key: 'delivered',
      label: '任务交付',
      caption: '工作流完成并同步结果',
      icon: Icons.rocket_launch_rounded,
      state: _StageVisualState.pending,
    ),
  ];

  final stage = detail.stage;
  final status = detail.status;
  final currentIndex = _resolveStageIndex(stage: stage, status: status);

  return [
    for (var index = 0; index < order.length; index++)
      order[index].copyWith(
        state: index < currentIndex
            ? _StageVisualState.completed
            : index == currentIndex
            ? _StageVisualState.current
            : _StageVisualState.pending,
      ),
  ];
}

int _resolveStageIndex({required String stage, required String status}) {
  if (status == 'completed' || stage == 'delivered') {
    return 5;
  }
  if (status == 'waiting_confirmation' || stage.contains('confirmation')) {
    return 4;
  }
  if (stage.contains('processing') || stage.contains('artifact')) {
    return 3;
  }
  if (stage.contains('intent') || stage.contains('fallback')) {
    return 2;
  }
  if (stage.contains('context') ||
      stage.contains('recall') ||
      stage.contains('building')) {
    return 1;
  }
  return 0;
}

String _buildActionHint(TaskRunDetail detail) {
  if (detail.status == 'waiting_confirmation') {
    return '工作流已经暂停在确认节点，建议先在下方“确认节点”区域给出下一步选择。';
  }
  if (detail.status == 'failed') {
    return '当前任务执行失败，建议先查看执行步骤里的报错信息，再决定是否重试或改写请求。';
  }
  if (detail.status == 'completed') {
    if (detail.intent == 'doc') {
      return '文档结果已经生成，建议继续检查章节结构，并决定是否推进到演示稿生成。';
    }
    if (detail.intent == 'slides') {
      return '演示稿内容包已经生成，建议进入排练模式检查页面顺序、重点表达和补充素材。';
    }
    return '当前任务已经完成，可以继续发起下一轮请求，或者围绕现有产物做确认和迭代。';
  }
  if (detail.status == 'running') {
    return '任务还在执行中，建议重点关注“执行阶段”和“执行步骤”的实时更新。';
  }
  return '当前任务已进入工作台，可继续观察状态变化或从飞书侧补充上下文。';
}

enum _StageVisualState { pending, current, completed }

class _StageItem {
  const _StageItem({
    required this.key,
    required this.label,
    required this.caption,
    required this.icon,
    required this.state,
  });

  final String key;
  final String label;
  final String caption;
  final IconData icon;
  final _StageVisualState state;

  _StageItem copyWith({_StageVisualState? state}) {
    return _StageItem(
      key: key,
      label: label,
      caption: caption,
      icon: icon,
      state: state ?? this.state,
    );
  }
}

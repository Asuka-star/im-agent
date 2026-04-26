import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:pilot_workbench/src/config/app_config.dart';
import 'package:pilot_workbench/src/models/task_run_models.dart';
import 'package:pilot_workbench/src/state/workbench_controller.dart';

class DashboardPage extends StatefulWidget {
  const DashboardPage({
    super.key,
    this.autoInitialize = true,
  });

  final bool autoInitialize;

  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  late final WorkbenchController _controller;
  late final TextEditingController _sessionController;

  @override
  void initState() {
    super.initState();
    _controller = WorkbenchController();
    _sessionController = TextEditingController(text: _controller.sessionFilter);
    if (widget.autoInitialize) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        _controller.initialize();
      });
    }
  }

  @override
  void dispose() {
    _sessionController.dispose();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AnimatedBuilder(
      animation: _controller,
      builder: (context, _) {
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
                      _ErrorBanner(message: _controller.errorMessage!),
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
                                  child: _TaskListPanel(controller: _controller),
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
                                child: _TaskListPanel(controller: _controller),
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
}

class _HeroPanel extends StatelessWidget {
  const _HeroPanel({
    required this.controller,
    required this.sessionController,
  });

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
          colors: [
            Color(0xFF172026),
            Color(0xFF213848),
            Color(0xFF2D5668),
          ],
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
                      style: theme.textTheme.displaySmall?.copyWith(color: Colors.white),
                    ),
                    const SizedBox(height: 8),
                    Text(
                      '把飞书里的 Agent 运行态摊开来，让桌面端和移动端都能看见计划、步骤、产物和确认节点。',
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
                    label: '任务流 ${controller.taskConnectionState}',
                    accent: const Color(0xFFF6B26B),
                  ),
                  _StatusPill(
                    label: '会话流 ${controller.sessionConnectionState}',
                    accent: const Color(0xFF73C8A9),
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
                    labelText: '会话过滤（session_id）',
                    labelStyle: TextStyle(color: Colors.white.withValues(alpha: 0.75)),
                    hintText: '留空时查看全部任务',
                    hintStyle: TextStyle(color: Colors.white.withValues(alpha: 0.45)),
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
                onPressed: () => controller.applySessionFilter(sessionController.text),
                icon: const Icon(Icons.filter_alt_rounded),
                label: const Text('应用过滤'),
              ),
              OutlinedButton.icon(
                onPressed: controller.isLoadingList ? null : () => controller.refreshTaskRuns(),
                icon: const Icon(Icons.sync_rounded),
                label: const Text('刷新任务'),
                style: OutlinedButton.styleFrom(
                  foregroundColor: Colors.white,
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
  const _TaskListPanel({required this.controller});

  final WorkbenchController controller;

  @override
  Widget build(BuildContext context) {
    final items = controller.taskRuns;
    return _PanelShell(
      title: '任务运行面板',
      subtitle: '展示当前会话里的任务实例、阶段和运行状态。',
      child: controller.isLoadingList && items.isEmpty
          ? const Center(child: CircularProgressIndicator())
          : items.isEmpty
              ? const _EmptyState(
                  title: '还没有任务运行数据',
                  message: '先从飞书侧触发一次 @机器人 请求，工作台就会开始出现任务轨迹。',
                )
              : ListView.separated(
                  itemCount: items.length,
                  separatorBuilder: (context, index) => const SizedBox(height: 12),
                  itemBuilder: (context, index) {
                    final item = items[index];
                    final isSelected = controller.selectedTaskRun?.taskRunId == item.taskRunId;
                    return InkWell(
                      borderRadius: BorderRadius.circular(20),
                      onTap: () => controller.selectTaskRun(item.taskRunId),
                      child: AnimatedContainer(
                        duration: const Duration(milliseconds: 180),
                        padding: const EdgeInsets.all(18),
                        decoration: BoxDecoration(
                          color: isSelected ? const Color(0xFFEAF5FB) : Colors.white,
                          borderRadius: BorderRadius.circular(20),
                          border: Border.all(
                            color: isSelected ? const Color(0xFF116A7B) : const Color(0xFFD9E3E8),
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
                                  label: item.status,
                                  color: _statusColor(item.status),
                                ),
                              ],
                            ),
                            const SizedBox(height: 10),
                            Wrap(
                              spacing: 8,
                              runSpacing: 8,
                              children: [
                                _Badge(label: item.stage, color: const Color(0xFF213848)),
                                _Badge(label: item.sourceType, color: const Color(0xFF8B5E34)),
                                if ((item.intent ?? '').isNotEmpty)
                                  _Badge(label: item.intent!, color: const Color(0xFF116A7B)),
                              ],
                            ),
                            const SizedBox(height: 12),
                            Text(
                              item.latestSummary?.trim().isNotEmpty == true
                                  ? item.latestSummary!
                                  : item.latestReplyPreview?.trim().isNotEmpty == true
                                      ? item.latestReplyPreview!
                                      : '等待更多上下文…',
                              maxLines: 3,
                              overflow: TextOverflow.ellipsis,
                              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                                    color: const Color(0xFF5B6770),
                                    height: 1.5,
                                  ),
                            ),
                            const SizedBox(height: 12),
                            Text(
                              'session: ${item.sessionId} · 更新于 ${_formatDateTime(item.updatedAt ?? item.createdAt)}',
                              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                                    color: const Color(0xFF72808A),
                                  ),
                            ),
                          ],
                        ),
                      ),
                    );
                  },
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
                      _SummaryCard(detail: detail),
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
  const _SummaryCard({required this.detail});

  final TaskRunDetail detail;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
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
                  style: theme.textTheme.headlineSmall?.copyWith(color: Colors.white),
                ),
              ),
              _Badge(label: detail.status, color: _statusColor(detail.status)),
            ],
          ),
          const SizedBox(height: 12),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _Badge(label: detail.stage, color: const Color(0xFFEF8354)),
              _Badge(label: detail.sessionId, color: const Color(0xFF73C8A9)),
              if ((detail.intent ?? '').isNotEmpty)
                _Badge(label: detail.intent!, color: const Color(0xFF8CCDEB)),
            ],
          ),
          const SizedBox(height: 16),
          if ((detail.latestSummary ?? '').isNotEmpty)
            Text(
              detail.latestSummary!,
              style: theme.textTheme.bodyLarge?.copyWith(
                color: Colors.white.withValues(alpha: 0.9),
                height: 1.55,
              ),
            ),
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
              _Badge(label: step.status, color: _statusColor(step.status)),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '${step.stepType} · ${step.stepKey}',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: const Color(0xFF72808A),
                ),
          ),
          if ((step.outputJson ?? '').isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              step.outputJson!,
              maxLines: 4,
              overflow: TextOverflow.ellipsis,
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(height: 1.45),
            ),
          ],
          if ((step.error ?? '').isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(
              step.error!,
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: const Color(0xFFC85D3A),
                  ),
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
              _Badge(label: artifact.artifactType, color: const Color(0xFF116A7B)),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            '${artifact.provider} · v${artifact.version} · ${artifact.status}',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: const Color(0xFF72808A),
                ),
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
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('产物链接已复制')),
                      );
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
                  label: '查看原始预览',
                  onPressed: () => _showRawPreview(context, artifact, preview),
                ),
                _InlineAction(
                  icon: Icons.copy_all_rounded,
                  label: '复制预览 JSON',
                  onPressed: () async {
                    await Clipboard.setData(
                      ClipboardData(text: const JsonEncoder.withIndent('  ').convert(preview)),
                    );
                    if (context.mounted) {
                      ScaffoldMessenger.of(context).showSnackBar(
                        const SnackBar(content: Text('预览 JSON 已复制')),
                      );
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
          style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                color: const Color(0xFF116A7B),
              ),
        );
      }
      return Text(
        '当前没有可预览的结构化内容。',
        style: Theme.of(context).textTheme.bodyMedium?.copyWith(
              color: const Color(0xFF72808A),
            ),
      );
    }

    switch (artifact.artifactType) {
      case 'document':
        return _DocumentPreview(preview: preview, url: artifact.url);
      case 'slides_package':
        return _SlidesPreview(preview: preview);
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
  const _DocumentPreview({
    required this.preview,
    required this.url,
  });

  final Map<String, dynamic> preview;
  final String? url;

  @override
  Widget build(BuildContext context) {
    final sections = (preview['sections'] as List?)
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
              style: Theme.of(context).textTheme.titleMedium?.copyWith(
                    fontWeight: FontWeight.w700,
                  ),
            ),
          if ((statsAsOf ?? '').isNotEmpty) ...[
            const SizedBox(height: 6),
            Text(
              '统计截至 $statsAsOf',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: const Color(0xFF72808A),
                  ),
            ),
          ],
          if ((url ?? '').isNotEmpty) ...[
            const SizedBox(height: 10),
            SelectableText(
              url!,
              style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                    color: const Color(0xFF116A7B),
                  ),
            ),
          ],
          if (sections.isNotEmpty) ...[
            const SizedBox(height: 14),
            ...sections.take(4).map(
              (section) => Padding(
                padding: const EdgeInsets.only(bottom: 10),
                child: _DocSectionTile(section: section),
              ),
            ),
            if (sections.length > 4)
              Text(
                '还有 ${sections.length - 4} 个章节未展开',
                style: Theme.of(context).textTheme.bodySmall?.copyWith(
                      color: const Color(0xFF72808A),
                    ),
              ),
          ],
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
    final paragraphs = (section['paragraphs'] as List?)
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
            style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
          ),
          const SizedBox(height: 8),
          ...paragraphs.take(2).map(
            (paragraph) => Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Text(
                paragraph,
                style: Theme.of(context).textTheme.bodyMedium?.copyWith(height: 1.45),
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
    final slides = (preview['slides'] as List?)
            ?.whereType<Map<String, dynamic>>()
            .toList() ??
        const <Map<String, dynamic>>[];
    final emphasis = (preview['emphasis'] as List?)
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
              style: Theme.of(context).textTheme.titleLarge?.copyWith(
                    color: Colors.white,
                  ),
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
                  return _SlideCard(
                    index: index,
                    slide: slides[index],
                  );
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
                      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
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
        ],
      ),
    );
  }
}

class _SlideCard extends StatelessWidget {
  const _SlideCard({
    required this.index,
    required this.slide,
  });

  final int index;
  final Map<String, dynamic> slide;

  @override
  Widget build(BuildContext context) {
    final title = (slide['title'] as String?)?.trim();
    final bullets = (slide['bullets'] as List?)
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
            'P${index + 1}',
            style: Theme.of(context).textTheme.bodySmall?.copyWith(
                  color: const Color(0xFF72808A),
                ),
          ),
          const SizedBox(height: 6),
          Text(
            (title ?? '').isNotEmpty ? title! : '未命名页面',
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: Theme.of(context).textTheme.titleMedium?.copyWith(
                  fontWeight: FontWeight.w700,
                ),
          ),
          const SizedBox(height: 10),
          ...bullets.take(4).map(
            (bullet) => Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Text(
                '• $bullet',
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: Theme.of(context).textTheme.bodySmall?.copyWith(height: 1.45),
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
        .map((entry) => '${entry.key}: ${_stringifyPreviewValue(entry.value)}')
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
  final VoidCallback onPressed;
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
                label: confirmation.status,
                color: confirmation.status == 'answered'
                    ? const Color(0xFF116A7B)
                    : const Color(0xFFC85D3A),
              ),
            ],
          ),
          if (confirmation.status == 'answered') ...[
            const SizedBox(height: 10),
            Text(
              '已选择：${confirmation.answerValue ?? '-'}${confirmation.answeredBy == null ? '' : ' · ${confirmation.answeredBy}'}',
              style: Theme.of(context).textTheme.bodyMedium,
            ),
          ] else if (options.isNotEmpty) ...[
            const SizedBox(height: 12),
            Wrap(
              spacing: 10,
              runSpacing: 10,
              children: options
                  .map(
                    (option) => FilledButton.tonal(
                      onPressed: controller.isSubmittingConfirmation
                          ? null
                          : () => controller.answerConfirmation(
                                taskRunId: taskRunId,
                                confirmationId: confirmation.confirmationId,
                                answerValue: option,
                              ),
                      child: Text(option),
                    ),
                  )
                  .toList(),
            ),
          ],
        ],
      ),
    );
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
            style: Theme.of(context).textTheme.bodyMedium?.copyWith(
                  color: const Color(0xFF72808A),
                ),
          ),
          const SizedBox(height: 16),
          Expanded(child: child),
        ],
      ),
    );
  }
}

class _SectionCard extends StatelessWidget {
  const _SectionCard({
    required this.title,
    required this.child,
  });

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
          Text(
            title,
            style: Theme.of(context).textTheme.titleLarge,
          ),
          const SizedBox(height: 14),
          child,
        ],
      ),
    );
  }
}

class _StatusPill extends StatelessWidget {
  const _StatusPill({
    required this.label,
    required this.accent,
  });

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
  const _InfoChip({
    required this.icon,
    required this.text,
  });

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
  const _Badge({
    required this.label,
    required this.color,
  });

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
  const _ErrorBanner({required this.message});

  final String message;

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
      child: Text(
        message,
        style: Theme.of(context).textTheme.bodyMedium?.copyWith(
              color: const Color(0xFF8B3720),
            ),
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({
    required this.title,
    required this.message,
  });

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

String _stringifyPreviewValue(Object? value) {
  if (value is List) {
    return '列表(${value.length})';
  }
  if (value is Map) {
    return '对象(${value.length})';
  }
  return value?.toString() ?? '-';
}

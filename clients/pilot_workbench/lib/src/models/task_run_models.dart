import 'dart:convert';

class TaskRunSummary {
  TaskRunSummary({
    required this.taskRunId,
    required this.sessionId,
    required this.sourceType,
    required this.title,
    required this.stage,
    required this.status,
    required this.createdAt,
    required this.updatedAt,
    this.intent,
    this.sessionLabel,
    this.sourceRef,
    this.triggerMessageId,
    this.latestSummary,
    this.latestReplyPreview,
    this.latestError,
    this.createdBy,
    this.completedAt,
  });

  final String taskRunId;
  final String sessionId;
  final String? sessionLabel;
  final String sourceType;
  final String title;
  final String stage;
  final String status;
  final DateTime? createdAt;
  final DateTime? updatedAt;
  final String? intent;
  final String? sourceRef;
  final String? triggerMessageId;
  final String? latestSummary;
  final String? latestReplyPreview;
  final String? latestError;
  final String? createdBy;
  final DateTime? completedAt;

  factory TaskRunSummary.fromJson(Map<String, dynamic> json) {
    return TaskRunSummary(
      taskRunId: json['task_run_id'] as String? ?? '',
      sessionId: json['session_id'] as String? ?? '',
      sessionLabel: json['session_label'] as String?,
      sourceType: json['source_type'] as String? ?? '',
      sourceRef: json['source_ref'] as String?,
      triggerMessageId: json['trigger_message_id'] as String?,
      intent: json['intent'] as String?,
      title: json['title'] as String? ?? '未命名任务',
      stage: json['stage'] as String? ?? 'queued',
      status: json['status'] as String? ?? 'queued',
      latestSummary: json['latest_summary'] as String?,
      latestReplyPreview: json['latest_reply_preview'] as String?,
      latestError: json['latest_error'] as String?,
      createdBy: json['created_by'] as String?,
      createdAt: _parseDateTime(json['created_at']),
      updatedAt: _parseDateTime(json['updated_at']),
      completedAt: _parseDateTime(json['completed_at']),
    );
  }
}

class TaskRunStepRecord {
  TaskRunStepRecord({
    required this.stepKey,
    required this.title,
    required this.stepType,
    required this.status,
    required this.createdAt,
    required this.updatedAt,
    this.inputJson,
    this.outputJson,
    this.error,
    this.startedAt,
    this.finishedAt,
  });

  final String stepKey;
  final String title;
  final String stepType;
  final String status;
  final String? inputJson;
  final String? outputJson;
  final String? error;
  final DateTime? startedAt;
  final DateTime? finishedAt;
  final DateTime? createdAt;
  final DateTime? updatedAt;

  factory TaskRunStepRecord.fromJson(Map<String, dynamic> json) {
    return TaskRunStepRecord(
      stepKey: json['step_key'] as String? ?? '',
      title: json['title'] as String? ?? '',
      stepType: json['step_type'] as String? ?? 'system',
      status: json['status'] as String? ?? 'pending',
      inputJson: json['input_json'] as String?,
      outputJson: json['output_json'] as String?,
      error: json['error'] as String?,
      startedAt: _parseDateTime(json['started_at']),
      finishedAt: _parseDateTime(json['finished_at']),
      createdAt: _parseDateTime(json['created_at']),
      updatedAt: _parseDateTime(json['updated_at']),
    );
  }
}

class ArtifactRecord {
  ArtifactRecord({
    required this.artifactId,
    required this.artifactType,
    required this.provider,
    required this.title,
    required this.status,
    required this.version,
    required this.createdAt,
    required this.updatedAt,
    this.url,
    this.previewJson,
  });

  final String artifactId;
  final String artifactType;
  final String provider;
  final String title;
  final String status;
  final int version;
  final String? url;
  final String? previewJson;
  final DateTime? createdAt;
  final DateTime? updatedAt;

  factory ArtifactRecord.fromJson(Map<String, dynamic> json) {
    return ArtifactRecord(
      artifactId: json['artifact_id'] as String? ?? '',
      artifactType: json['artifact_type'] as String? ?? '',
      provider: json['provider'] as String? ?? 'local',
      title: json['title'] as String? ?? '',
      status: json['status'] as String? ?? 'ready',
      version: json['version'] as int? ?? 1,
      url: json['url'] as String?,
      previewJson: json['preview_json'] as String?,
      createdAt: _parseDateTime(json['created_at']),
      updatedAt: _parseDateTime(json['updated_at']),
    );
  }

  Map<String, dynamic>? get preview => _decodeJsonMap(previewJson);
}

class SessionDocumentRecord {
  SessionDocumentRecord({
    required this.sessionId,
    required this.documentId,
    required this.title,
    required this.version,
    required this.syncMode,
    required this.isCurrent,
    this.url,
    this.taskRunId,
    this.updatedAt,
  });

  final String sessionId;
  final String documentId;
  final String title;
  final int version;
  final String syncMode;
  final bool isCurrent;
  final String? url;
  final String? taskRunId;
  final DateTime? updatedAt;

  factory SessionDocumentRecord.fromJson(Map<String, dynamic> json) {
    return SessionDocumentRecord(
      sessionId: json['session_id'] as String? ?? '',
      documentId: json['document_id'] as String? ?? '',
      title: json['title'] as String? ?? '',
      version: json['version'] as int? ?? 1,
      syncMode: json['sync_mode'] as String? ?? 'created',
      isCurrent: json['is_current'] as bool? ?? false,
      url: json['url'] as String?,
      taskRunId: json['task_run_id'] as String?,
      updatedAt: _parseDateTime(json['updated_at']),
    );
  }
}

class ConfirmationRequestRecord {
  ConfirmationRequestRecord({
    required this.confirmationId,
    required this.prompt,
    required this.status,
    required this.createdAt,
    required this.updatedAt,
    this.optionsJson,
    this.answerValue,
    this.answeredBy,
    this.answeredAt,
  });

  final String confirmationId;
  final String prompt;
  final String status;
  final String? optionsJson;
  final String? answerValue;
  final String? answeredBy;
  final DateTime? answeredAt;
  final DateTime? createdAt;
  final DateTime? updatedAt;

  factory ConfirmationRequestRecord.fromJson(Map<String, dynamic> json) {
    return ConfirmationRequestRecord(
      confirmationId: json['confirmation_id'] as String? ?? '',
      prompt: json['prompt'] as String? ?? '',
      optionsJson: json['options_json'] as String?,
      status: json['status'] as String? ?? 'pending',
      answerValue: json['answer_value'] as String?,
      answeredBy: json['answered_by'] as String?,
      answeredAt: _parseDateTime(json['answered_at']),
      createdAt: _parseDateTime(json['created_at']),
      updatedAt: _parseDateTime(json['updated_at']),
    );
  }

  List<String> get options {
    final decoded = _decodeJsonList(optionsJson);
    return decoded
        .map((item) => item.toString())
        .where((item) => item.isNotEmpty)
        .toList();
  }
}

class TaskRunDetail extends TaskRunSummary {
  TaskRunDetail({
    required super.taskRunId,
    required super.sessionId,
    required super.sourceType,
    required super.title,
    required super.stage,
    required super.status,
    required super.createdAt,
    required super.updatedAt,
    required this.steps,
    required this.artifacts,
    required this.confirmations,
    required this.sessionDocuments,
    this.metadataJson,
    super.intent,
    super.sessionLabel,
    super.sourceRef,
    super.triggerMessageId,
    super.latestSummary,
    super.latestReplyPreview,
    super.latestError,
    super.createdBy,
    super.completedAt,
  });

  final String? metadataJson;
  final List<TaskRunStepRecord> steps;
  final List<ArtifactRecord> artifacts;
  final List<ConfirmationRequestRecord> confirmations;
  final List<SessionDocumentRecord> sessionDocuments;

  factory TaskRunDetail.fromJson(Map<String, dynamic> json) {
    return TaskRunDetail(
      taskRunId: json['task_run_id'] as String? ?? '',
      sessionId: json['session_id'] as String? ?? '',
      sessionLabel: json['session_label'] as String?,
      sourceType: json['source_type'] as String? ?? '',
      sourceRef: json['source_ref'] as String?,
      triggerMessageId: json['trigger_message_id'] as String?,
      intent: json['intent'] as String?,
      title: json['title'] as String? ?? '未命名任务',
      stage: json['stage'] as String? ?? 'queued',
      status: json['status'] as String? ?? 'queued',
      latestSummary: json['latest_summary'] as String?,
      latestReplyPreview: json['latest_reply_preview'] as String?,
      latestError: json['latest_error'] as String?,
      metadataJson: json['metadata_json'] as String?,
      createdBy: json['created_by'] as String?,
      createdAt: _parseDateTime(json['created_at']),
      updatedAt: _parseDateTime(json['updated_at']),
      completedAt: _parseDateTime(json['completed_at']),
      steps: _asList(json['steps']).map(TaskRunStepRecord.fromJson).toList(),
      artifacts: _asList(
        json['artifacts'],
      ).map(ArtifactRecord.fromJson).toList(),
      confirmations: _asList(
        json['confirmations'],
      ).map(ConfirmationRequestRecord.fromJson).toList(),
      sessionDocuments: _asList(
        json['session_documents'],
      ).map(SessionDocumentRecord.fromJson).toList(),
    );
  }
}

DateTime? _parseDateTime(Object? value) {
  if (value is! String || value.isEmpty) {
    return null;
  }
  return DateTime.tryParse(value);
}

List<Map<String, dynamic>> _asList(Object? value) {
  if (value is! List) {
    return const [];
  }
  return value.whereType<Map<String, dynamic>>().toList();
}

Map<String, dynamic>? _decodeJsonMap(String? source) {
  if (source == null || source.isEmpty) {
    return null;
  }
  try {
    final decoded = jsonDecode(source);
    return decoded is Map<String, dynamic> ? decoded : null;
  } on FormatException {
    return null;
  }
}

List<dynamic> _decodeJsonList(String? source) {
  if (source == null || source.isEmpty) {
    return const [];
  }
  try {
    final decoded = jsonDecode(source);
    return decoded is List ? decoded : const [];
  } on FormatException {
    return const [];
  }
}

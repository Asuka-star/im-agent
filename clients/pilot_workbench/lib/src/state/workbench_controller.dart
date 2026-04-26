import 'package:flutter/foundation.dart';
import 'package:pilot_workbench/src/config/app_config.dart';
import 'package:pilot_workbench/src/models/task_run_models.dart';
import 'package:pilot_workbench/src/services/workbench_api.dart';
import 'package:pilot_workbench/src/services/workbench_socket.dart';

class WorkbenchController extends ChangeNotifier {
  WorkbenchController({
    WorkbenchApi? api,
    WorkbenchSocket? socket,
  })  : _api = api ?? WorkbenchApi(),
        _socket = socket ?? WorkbenchSocket();

  final WorkbenchApi _api;
  final WorkbenchSocket _socket;

  final String apiBaseUrl = AppConfig.apiBaseUrl;

  List<TaskRunSummary> taskRuns = const [];
  TaskRunDetail? selectedTaskRun;

  String sessionFilter = AppConfig.defaultSessionId;
  String sessionConnectionState = 'idle';
  String taskConnectionState = 'idle';
  String? errorMessage;
  DateTime? lastUpdatedAt;
  bool isLoadingList = false;
  bool isLoadingDetail = false;
  bool isSubmittingConfirmation = false;

  SocketConnection? _sessionConnection;
  SocketConnection? _taskConnection;

  Future<void> initialize() async {
    await refreshTaskRuns(keepSelection: false);
  }

  Future<void> refreshTaskRuns({bool keepSelection = true}) async {
    isLoadingList = true;
    errorMessage = null;
    notifyListeners();

    try {
      final runs = await _api.listTaskRuns(
        sessionId: sessionFilter.trim().isEmpty ? null : sessionFilter.trim(),
        limit: 50,
      );
      taskRuns = runs;
      lastUpdatedAt = DateTime.now();

      if (sessionFilter.trim().isNotEmpty) {
        await _bindSessionSocket(sessionFilter.trim());
      } else {
        await _closeSessionSocket();
      }

      final selectedId = selectedTaskRun?.taskRunId;
      if (keepSelection && selectedId != null) {
        final stillExists = runs.any((item) => item.taskRunId == selectedId);
        if (stillExists) {
          await selectTaskRun(selectedId, quiet: true);
        } else {
          await _closeTaskSocket();
          selectedTaskRun = null;
          taskConnectionState = 'idle';
        }
      } else if (runs.isNotEmpty) {
        await selectTaskRun(runs.first.taskRunId, quiet: true);
      }
    } catch (error) {
      errorMessage = error.toString();
    } finally {
      isLoadingList = false;
      notifyListeners();
    }
  }

  Future<void> applySessionFilter(String value) async {
    sessionFilter = value.trim();
    await refreshTaskRuns(keepSelection: false);
  }

  Future<void> selectTaskRun(String taskRunId, {bool quiet = false}) async {
    if (!quiet) {
      isLoadingDetail = true;
      errorMessage = null;
      notifyListeners();
    }

    try {
      final detail = await _api.getTaskRun(taskRunId);
      selectedTaskRun = detail;
      _mergeSummary(_summaryFromDetail(detail));
      lastUpdatedAt = DateTime.now();
      await _bindTaskSocket(taskRunId);
    } catch (error) {
      errorMessage = error.toString();
    } finally {
      isLoadingDetail = false;
      notifyListeners();
    }
  }

  Future<void> answerConfirmation({
    required String taskRunId,
    required String confirmationId,
    required String answerValue,
  }) async {
    isSubmittingConfirmation = true;
    errorMessage = null;
    notifyListeners();

    try {
      await _api.confirmTaskRun(
        taskRunId: taskRunId,
        confirmationId: confirmationId,
        answerValue: answerValue,
      );
      await selectTaskRun(taskRunId, quiet: true);
    } catch (error) {
      errorMessage = error.toString();
    } finally {
      isSubmittingConfirmation = false;
      notifyListeners();
    }
  }

  Future<void> _bindSessionSocket(String sessionId) async {
    final current = _sessionConnection;
    if (current != null) {
      await current.close();
    }

    sessionConnectionState = 'connecting';
    notifyListeners();

    _sessionConnection = _socket.connect(
      uri: AppConfig.sessionSocketUri(sessionId),
      onEvent: _handleSessionEvent,
      onClosed: () {
        sessionConnectionState = 'closed';
        notifyListeners();
      },
      onError: (error) {
        sessionConnectionState = 'error';
        errorMessage = error.toString();
        notifyListeners();
      },
    );

    sessionConnectionState = 'live';
    _sessionConnection?.ping();
    notifyListeners();
  }

  Future<void> _bindTaskSocket(String taskRunId) async {
    final current = _taskConnection;
    if (current != null) {
      await current.close();
    }

    taskConnectionState = 'connecting';
    notifyListeners();

    _taskConnection = _socket.connect(
      uri: AppConfig.taskRunSocketUri(taskRunId),
      onEvent: _handleTaskRunEvent,
      onClosed: () {
        taskConnectionState = 'closed';
        notifyListeners();
      },
      onError: (error) {
        taskConnectionState = 'error';
        errorMessage = error.toString();
        notifyListeners();
      },
    );

    taskConnectionState = 'live';
    _taskConnection?.ping();
    notifyListeners();
  }

  Future<void> _closeSessionSocket() async {
    final connection = _sessionConnection;
    _sessionConnection = null;
    if (connection != null) {
      await connection.close();
    }
    sessionConnectionState = 'idle';
  }

  Future<void> _closeTaskSocket() async {
    final connection = _taskConnection;
    _taskConnection = null;
    if (connection != null) {
      await connection.close();
    }
    taskConnectionState = 'idle';
  }

  void _handleSessionEvent(Map<String, dynamic> event) {
    final payload = event['task_run'];
    if (event['type'] == 'session.snapshot') {
      final list = event['task_runs'];
      if (list is List) {
        taskRuns = list.whereType<Map<String, dynamic>>().map(TaskRunSummary.fromJson).toList();
      }
    } else if (payload is Map<String, dynamic>) {
      _mergeSummary(TaskRunSummary.fromJson(payload));
    }
    lastUpdatedAt = DateTime.now();
    notifyListeners();
  }

  void _handleTaskRunEvent(Map<String, dynamic> event) {
    final payload = event['task_run'];
    if (payload is! Map<String, dynamic>) {
      return;
    }
    final detail = TaskRunDetail.fromJson(payload);
    selectedTaskRun = detail;
    _mergeSummary(_summaryFromDetail(detail));
    lastUpdatedAt = DateTime.now();
    notifyListeners();
  }

  void _mergeSummary(TaskRunSummary incoming) {
    final items = [...taskRuns];
    final index = items.indexWhere((item) => item.taskRunId == incoming.taskRunId);
    if (index >= 0) {
      items[index] = incoming;
    } else {
      items.insert(0, incoming);
    }
    taskRuns = items;
  }

  TaskRunSummary _summaryFromDetail(TaskRunDetail detail) {
    return TaskRunSummary(
      taskRunId: detail.taskRunId,
      sessionId: detail.sessionId,
      sourceType: detail.sourceType,
      sourceRef: detail.sourceRef,
      triggerMessageId: detail.triggerMessageId,
      intent: detail.intent,
      title: detail.title,
      stage: detail.stage,
      status: detail.status,
      latestSummary: detail.latestSummary,
      latestReplyPreview: detail.latestReplyPreview,
      latestError: detail.latestError,
      createdBy: detail.createdBy,
      createdAt: detail.createdAt,
      updatedAt: detail.updatedAt,
      completedAt: detail.completedAt,
    );
  }

  @override
  void dispose() {
    _closeSessionSocket();
    _closeTaskSocket();
    super.dispose();
  }
}

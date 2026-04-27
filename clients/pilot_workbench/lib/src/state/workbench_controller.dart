import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:pilot_workbench/src/config/app_config.dart';
import 'package:pilot_workbench/src/models/task_run_models.dart';
import 'package:pilot_workbench/src/services/workbench_api.dart';
import 'package:pilot_workbench/src/services/workbench_socket.dart';
import 'package:pilot_workbench/src/utils/workbench_labels.dart';

class WorkbenchController extends ChangeNotifier {
  WorkbenchController({WorkbenchApi? api, WorkbenchSocket? socket})
    : _api = api ?? WorkbenchApi(),
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
  Timer? _sessionReconnectTimer;
  Timer? _taskReconnectTimer;
  Timer? _fallbackRefreshTimer;
  String? _activeSessionSocketId;
  String? _activeTaskSocketId;
  bool _sessionRealtimeEnabled = true;
  bool _taskRealtimeEnabled = true;

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
        if (_sessionRealtimeEnabled) {
          await _bindSessionSocket(sessionFilter.trim());
        } else {
          sessionConnectionState = 'polling';
        }
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
      } else if (runs.isEmpty) {
        await _closeTaskSocket();
        selectedTaskRun = null;
        taskConnectionState = 'idle';
      } else if (runs.isNotEmpty) {
        await selectTaskRun(runs.first.taskRunId, quiet: true);
      }
    } catch (error) {
      errorMessage = localizeWorkbenchError(error);
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
      if (_taskRealtimeEnabled) {
        await _bindTaskSocket(taskRunId);
      } else {
        taskConnectionState = 'polling';
      }
    } catch (error) {
      errorMessage = localizeWorkbenchError(error);
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
      await refreshTaskRuns(keepSelection: true);
    } catch (error) {
      errorMessage = localizeWorkbenchError(error);
      rethrow;
    } finally {
      isSubmittingConfirmation = false;
      notifyListeners();
    }
  }

  void clearError() {
    if (errorMessage == null) {
      return;
    }
    errorMessage = null;
    notifyListeners();
  }

  Future<void> retryCurrentView() async {
    _sessionRealtimeEnabled = true;
    _taskRealtimeEnabled = true;
    _updateFallbackRefresh();
    if (selectedTaskRun != null) {
      await selectTaskRun(selectedTaskRun!.taskRunId, quiet: false);
      return;
    }
    await refreshTaskRuns(keepSelection: false);
  }

  Future<void> _bindSessionSocket(String sessionId) async {
    _sessionReconnectTimer?.cancel();
    final current = _sessionConnection;
    _sessionConnection = null;
    _activeSessionSocketId = null;
    if (current != null) {
      await current.close();
    }

    sessionConnectionState = 'connecting';
    notifyListeners();

    try {
      var allowReconnectOnClose = true;
      _activeSessionSocketId = sessionId;
      _sessionConnection = _socket.connect(
        uri: AppConfig.sessionSocketUri(sessionId),
        onEvent: _handleSessionEvent,
        onClosed: () {
          if (_activeSessionSocketId != sessionId) {
            return;
          }
          sessionConnectionState = allowReconnectOnClose ? 'closed' : 'polling';
          notifyListeners();
          if (allowReconnectOnClose) {
            _scheduleSessionReconnect(sessionId);
          }
        },
        onError: (error) {
          if (_activeSessionSocketId != sessionId) {
            return;
          }
          final shouldRetry = _shouldRetrySocket(error);
          allowReconnectOnClose = shouldRetry;
          if (!shouldRetry) {
            _sessionRealtimeEnabled = false;
            _updateFallbackRefresh();
          }
          sessionConnectionState = shouldRetry ? 'error' : 'polling';
          errorMessage = localizeWorkbenchError(error);
          notifyListeners();
          if (shouldRetry) {
            _scheduleSessionReconnect(sessionId);
          }
        },
      );

      sessionConnectionState = 'live';
      _sessionRealtimeEnabled = true;
      _updateFallbackRefresh();
      _sessionConnection?.ping();
      notifyListeners();
    } catch (error) {
      final shouldRetry = _shouldRetrySocket(error);
      if (!shouldRetry) {
        _sessionRealtimeEnabled = false;
        _updateFallbackRefresh();
      }
      sessionConnectionState = shouldRetry ? 'error' : 'polling';
      errorMessage = localizeWorkbenchError(error);
      notifyListeners();
      if (shouldRetry) {
        _scheduleSessionReconnect(sessionId);
      }
    }
  }

  Future<void> _bindTaskSocket(String taskRunId) async {
    _taskReconnectTimer?.cancel();
    final current = _taskConnection;
    _taskConnection = null;
    _activeTaskSocketId = null;
    if (current != null) {
      await current.close();
    }

    taskConnectionState = 'connecting';
    notifyListeners();

    try {
      var allowReconnectOnClose = true;
      _activeTaskSocketId = taskRunId;
      _taskConnection = _socket.connect(
        uri: AppConfig.taskRunSocketUri(taskRunId),
        onEvent: _handleTaskRunEvent,
        onClosed: () {
          if (_activeTaskSocketId != taskRunId) {
            return;
          }
          taskConnectionState = allowReconnectOnClose ? 'closed' : 'polling';
          notifyListeners();
          if (allowReconnectOnClose) {
            _scheduleTaskReconnect(taskRunId);
          }
        },
        onError: (error) {
          if (_activeTaskSocketId != taskRunId) {
            return;
          }
          final shouldRetry = _shouldRetrySocket(error);
          allowReconnectOnClose = shouldRetry;
          if (!shouldRetry) {
            _taskRealtimeEnabled = false;
            _updateFallbackRefresh();
          }
          taskConnectionState = shouldRetry ? 'error' : 'polling';
          errorMessage = localizeWorkbenchError(error);
          notifyListeners();
          if (shouldRetry) {
            _scheduleTaskReconnect(taskRunId);
          }
        },
      );

      taskConnectionState = 'live';
      _taskRealtimeEnabled = true;
      _updateFallbackRefresh();
      _taskConnection?.ping();
      notifyListeners();
    } catch (error) {
      final shouldRetry = _shouldRetrySocket(error);
      if (!shouldRetry) {
        _taskRealtimeEnabled = false;
        _updateFallbackRefresh();
      }
      taskConnectionState = shouldRetry ? 'error' : 'polling';
      errorMessage = localizeWorkbenchError(error);
      notifyListeners();
      if (shouldRetry) {
        _scheduleTaskReconnect(taskRunId);
      }
    }
  }

  Future<void> _closeSessionSocket() async {
    _sessionReconnectTimer?.cancel();
    _sessionReconnectTimer = null;
    _activeSessionSocketId = null;
    final connection = _sessionConnection;
    _sessionConnection = null;
    if (connection != null) {
      await connection.close();
    }
    sessionConnectionState = 'idle';
    _updateFallbackRefresh();
  }

  Future<void> _closeTaskSocket() async {
    _taskReconnectTimer?.cancel();
    _taskReconnectTimer = null;
    _activeTaskSocketId = null;
    final connection = _taskConnection;
    _taskConnection = null;
    if (connection != null) {
      await connection.close();
    }
    taskConnectionState = 'idle';
    _updateFallbackRefresh();
  }

  void _scheduleSessionReconnect(String sessionId) {
    if (sessionId.isEmpty || sessionFilter != sessionId) {
      return;
    }
    _sessionReconnectTimer?.cancel();
    _sessionReconnectTimer = Timer(const Duration(seconds: 3), () {
      if (sessionFilter == sessionId) {
        _bindSessionSocket(sessionId);
      }
    });
  }

  void _scheduleTaskReconnect(String taskRunId) {
    final selectedId = selectedTaskRun?.taskRunId;
    if (selectedId == null || selectedId != taskRunId) {
      return;
    }
    _taskReconnectTimer?.cancel();
    _taskReconnectTimer = Timer(const Duration(seconds: 3), () {
      if (selectedTaskRun?.taskRunId == taskRunId) {
        _bindTaskSocket(taskRunId);
      }
    });
  }

  bool _shouldRetrySocket(Object error) {
    final text = error.toString().toLowerCase();
    if (text.contains('not upgraded to websocket')) {
      return false;
    }
    if (text.contains('full header was received')) {
      return false;
    }
    if (text.contains('400 bad request')) {
      return false;
    }
    return true;
  }

  void _updateFallbackRefresh() {
    final needsPolling = !_sessionRealtimeEnabled || !_taskRealtimeEnabled;
    if (!needsPolling) {
      _fallbackRefreshTimer?.cancel();
      _fallbackRefreshTimer = null;
      return;
    }
    if (_fallbackRefreshTimer != null) {
      return;
    }
    _fallbackRefreshTimer = Timer.periodic(
      const Duration(seconds: 8),
      (_) => _refreshFromPollingFallback(),
    );
  }

  Future<void> _refreshFromPollingFallback() async {
    if (isLoadingList || isLoadingDetail || isSubmittingConfirmation) {
      return;
    }
    try {
      final runs = await _api.listTaskRuns(
        sessionId: sessionFilter.trim().isEmpty ? null : sessionFilter.trim(),
        limit: 50,
      );
      taskRuns = runs;
      lastUpdatedAt = DateTime.now();

      final selectedId = selectedTaskRun?.taskRunId;
      if (selectedId != null) {
        final stillExists = runs.any((item) => item.taskRunId == selectedId);
        if (stillExists) {
          final detail = await _api.getTaskRun(selectedId);
          selectedTaskRun = detail;
          _mergeSummary(_summaryFromDetail(detail));
        } else {
          selectedTaskRun = null;
          taskConnectionState = _taskRealtimeEnabled
              ? taskConnectionState
              : 'idle';
        }
      }

      notifyListeners();
    } catch (error) {
      errorMessage ??= localizeWorkbenchError(error);
      notifyListeners();
    }
  }

  void _handleSessionEvent(Map<String, dynamic> event) {
    final payload = event['task_run'];
    if (event['type'] == 'session.snapshot') {
      final list = event['task_runs'];
      if (list is List) {
        taskRuns = list
            .whereType<Map<String, dynamic>>()
            .map(TaskRunSummary.fromJson)
            .toList();
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
    final index = items.indexWhere(
      (item) => item.taskRunId == incoming.taskRunId,
    );
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
    _sessionReconnectTimer?.cancel();
    _taskReconnectTimer?.cancel();
    _fallbackRefreshTimer?.cancel();
    _closeSessionSocket();
    _closeTaskSocket();
    super.dispose();
  }
}

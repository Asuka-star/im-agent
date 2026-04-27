import 'dart:convert';

import 'package:http/http.dart' as http;
import 'package:pilot_workbench/src/config/app_config.dart';
import 'package:pilot_workbench/src/models/task_run_models.dart';

class WorkbenchApi {
  WorkbenchApi({http.Client? client, String? baseUrl})
    : _client = client ?? http.Client(),
      _baseUrl = baseUrl ?? AppConfig.apiBaseUrl;

  final http.Client _client;
  final String _baseUrl;

  Future<List<TaskRunSummary>> listTaskRuns({
    String? sessionId,
    String? status,
    int limit = 20,
  }) async {
    final response = await _client.get(
      _buildUri(
        '/task-runs/',
        queryParameters: {
          'session_id': sessionId,
          'status': status,
          'limit': '$limit',
        },
      ),
    );
    _ensureSuccess(response);
    final decoded = jsonDecode(utf8.decode(response.bodyBytes));
    if (decoded is! List) {
      return const [];
    }
    return decoded
        .whereType<Map<String, dynamic>>()
        .map(TaskRunSummary.fromJson)
        .toList();
  }

  Future<TaskRunDetail> getTaskRun(String taskRunId) async {
    final response = await _client.get(_buildUri('/task-runs/$taskRunId'));
    _ensureSuccess(response);
    final decoded = jsonDecode(utf8.decode(response.bodyBytes));
    if (decoded is! Map<String, dynamic>) {
      throw const FormatException('任务详情返回的不是有效对象。');
    }
    return TaskRunDetail.fromJson(decoded);
  }

  Future<void> confirmTaskRun({
    required String taskRunId,
    required String confirmationId,
    required String answerValue,
    String answeredBy = 'pilot_workbench',
  }) async {
    final response = await _client.post(
      _buildUri('/task-runs/$taskRunId/confirm'),
      headers: {'Content-Type': 'application/json'},
      body: jsonEncode({
        'confirmation_id': confirmationId,
        'answer_value': answerValue,
        'answered_by': answeredBy,
      }),
    );
    _ensureSuccess(response);
  }

  Uri _buildUri(String path, {Map<String, String?>? queryParameters}) {
    final baseUri = Uri.parse(_baseUrl);
    final normalizedPath = path.startsWith('/') ? path.substring(1) : path;
    return baseUri.replace(
      pathSegments: <String>[
        ...baseUri.pathSegments.where((segment) => segment.isNotEmpty),
        ...normalizedPath.split('/').where((segment) => segment.isNotEmpty),
      ],
      queryParameters: {
        for (final entry in (queryParameters ?? const {}).entries)
          if (entry.value != null && entry.value!.isNotEmpty)
            entry.key: entry.value!,
      },
    );
  }

  void _ensureSuccess(http.Response response) {
    if (response.statusCode >= 200 && response.statusCode < 300) {
      return;
    }
    throw HttpException(
      '工作台接口请求失败：${response.statusCode} ${response.reasonPhrase ?? ''}'.trim(),
    );
  }
}

class HttpException implements Exception {
  const HttpException(this.message);

  final String message;

  @override
  String toString() => message;
}

import 'dart:io';

import 'package:pilot_workbench/src/utils/workbench_labels.dart';

class AppConfig {
  static const appName = workbenchAppName;
  static const defaultSessionId = String.fromEnvironment(
    'WORKBENCH_SESSION_ID',
    defaultValue: '',
  );

  static String get apiBaseUrl {
    const override = String.fromEnvironment(
      'WORKBENCH_API_BASE_URL',
      defaultValue: '',
    );
    if (override.isNotEmpty) {
      return override;
    }
    if (Platform.isAndroid) {
      return 'http://10.0.2.2:9000/api';
    }
    return 'http://127.0.0.1:9000/api';
  }

  static Uri taskRunSocketUri(String taskRunId) {
    return _buildSocketUri('ws/task-runs/$taskRunId');
  }

  static Uri sessionSocketUri(String sessionId) {
    return _buildSocketUri('ws/sessions/$sessionId');
  }

  static Uri _buildSocketUri(String relativePath) {
    final apiUri = Uri.parse(apiBaseUrl);
    final scheme = apiUri.scheme == 'https' ? 'wss' : 'ws';
    final baseSegments = apiUri.pathSegments
        .where((segment) => segment.isNotEmpty)
        .toList();
    final pathSegments = <String>[
      ...baseSegments,
      ...relativePath.split('/').where((segment) => segment.isNotEmpty),
    ];
    if (apiUri.hasPort) {
      return Uri(
        scheme: scheme,
        userInfo: apiUri.userInfo.isEmpty ? null : apiUri.userInfo,
        host: apiUri.host,
        port: apiUri.port,
        pathSegments: pathSegments,
      );
    }
    return Uri(
      scheme: scheme,
      userInfo: apiUri.userInfo.isEmpty ? null : apiUri.userInfo,
      host: apiUri.host,
      pathSegments: pathSegments,
    );
  }
}

const workbenchAppName = '飞书智能体工作台';

String localizeStatus(String status) {
  switch (status.trim().toLowerCase()) {
    case 'running':
      return '运行中';
    case 'waiting_confirmation':
      return '待确认';
    case 'completed':
    case 'done':
      return '已完成';
    case 'failed':
      return '失败';
    case 'queued':
      return '排队中';
    case 'pending':
      return '待处理';
    case 'answered':
      return '已确认';
    case 'ready':
      return '可用';
    case 'draft':
      return '草稿';
    default:
      return _humanizeIdentifier(status);
  }
}

String localizeStage(String stage) {
  final normalized = stage.trim().toLowerCase();
  switch (normalized) {
    case 'queued':
      return '请求进入';
    case 'building_context':
    case 'workspace_context':
      return '上下文整理';
    case 'planning':
    case 'fallback_planning':
      return '意图理解';
    case 'processing':
      return '产物生成';
    case 'confirmation_resolved':
      return '确认已处理';
    case 'delivered':
      return '任务交付';
    case 'failed':
      return '执行失败';
  }

  if (normalized.contains('context') ||
      normalized.contains('recall') ||
      normalized.contains('workspace') ||
      normalized.contains('memory')) {
    return '上下文整理';
  }
  if (normalized.contains('intent') ||
      normalized.contains('plan') ||
      normalized.contains('classif') ||
      normalized.contains('fallback')) {
    return '意图理解';
  }
  if (normalized.contains('artifact') ||
      normalized.contains('doc') ||
      normalized.contains('slide') ||
      normalized.contains('reply') ||
      normalized.contains('process')) {
    return '产物生成';
  }
  if (normalized.contains('confirm')) {
    return '协作确认';
  }
  if (normalized.contains('deliver') || normalized.contains('complete')) {
    return '任务交付';
  }
  return _humanizeIdentifier(stage);
}

String localizeIntent(String? intent) {
  switch ((intent ?? '').trim().toLowerCase()) {
    case 'summary':
      return '讨论总结';
    case 'tasks':
      return '任务整理';
    case 'risks':
      return '风险识别';
    case 'status':
      return '进度状态';
    case 'slides':
      return '演示稿';
    case 'doc':
      return '文档';
    case 'canvas':
      return 'Canvas';
    case 'help':
      return '帮助说明';
    case 'unknown':
      return '待识别';
    case '':
      return '';
    default:
      return _humanizeIdentifier(intent ?? '');
  }
}

String localizeSourceType(String sourceType) {
  switch (sourceType.trim().toLowerCase()) {
    case 'group':
      return '群聊';
    case 'p2p':
      return '单聊';
    case 'message':
      return '消息';
    case 'assistant_reply':
      return '助手回复';
    case 'summary':
      return '摘要';
    case 'unknown':
      return '未知来源';
    default:
      return _humanizeIdentifier(sourceType);
  }
}

String localizeStepType(String stepType) {
  switch (stepType.trim().toLowerCase()) {
    case 'context':
      return '上下文';
    case 'artifact':
      return '产物';
    case 'confirm':
    case 'confirmation':
      return '确认';
    case 'system':
      return '系统';
    case 'reply':
      return '回复';
    case 'generate_canvas':
      return 'Canvas';
    default:
      return _humanizeIdentifier(stepType);
  }
}

String localizeArtifactType(String artifactType) {
  switch (artifactType.trim().toLowerCase()) {
    case 'document':
      return '文档';
    case 'slides_package':
      return '演示稿包';
    case 'canvas':
      return 'Canvas';
    case 'note':
      return '备注';
    case 'reply':
      return '回复';
    default:
      return _humanizeIdentifier(artifactType);
  }
}

String localizeProvider(String provider) {
  switch (provider.trim().toLowerCase()) {
    case 'local':
      return '本地';
    case 'feishu':
      return '飞书';
    default:
      return _humanizeIdentifier(provider);
  }
}

String localizePreviewKey(String key) {
  switch (key.trim().toLowerCase()) {
    case 'title':
      return '标题';
    case 'sections':
      return '章节';
    case 'stats_as_of':
      return '统计时间';
    case 'theme':
      return '主题';
    case 'audience':
      return '受众';
    case 'slides':
      return '页面';
    case 'emphasis':
      return '强调点';
    case 'assets':
      return '素材';
    case 'heading':
      return '小节标题';
    case 'paragraphs':
      return '段落';
    case 'bullets':
      return '要点';
    default:
      return _humanizeIdentifier(key);
  }
}

String localizeActor(String? actor) {
  switch ((actor ?? '').trim().toLowerCase()) {
    case 'pilot_workbench':
      return '工作台';
    case '':
      return '-';
    default:
      return actor!;
  }
}

String localizeWorkbenchError(Object error) {
  final message = error.toString().trim();
  final normalized = message.toLowerCase();
  final apiError = RegExp(
    r'(?:workbench api request failed:|工作台接口请求失败：)\s*(\d{3})(.*)',
    caseSensitive: false,
  ).firstMatch(message);
  if (apiError != null) {
    final statusCode = apiError.group(1)!;
    final reasonPhrase = apiError.group(2)?.trim() ?? '';
    final reason = _localizeHttpReason(reasonPhrase);
    return reason.isEmpty
        ? '工作台接口请求失败（$statusCode）。'
        : '工作台接口请求失败（$statusCode，$reason）。';
  }

  if (normalized.contains('task run payload is not a json object')) {
    return '任务详情返回格式不正确。';
  }
  if (normalized.contains('not upgraded to websocket')) {
    return '服务端暂未开启实时推送，界面已切换为轮询刷新。';
  }
  if (normalized.contains('full header was received')) {
    return '实时连接建立失败，界面已切换为轮询刷新。';
  }
  if (normalized.contains('400 bad request')) {
    return '请求参数有误，请检查当前任务或会话是否仍然有效。';
  }
  if (normalized.contains('connection refused')) {
    return '无法连接到服务端，请确认后端是否正在运行。';
  }
  if (normalized.contains('socketexception')) {
    return '网络连接异常，请检查当前网络或服务端状态。';
  }
  if (normalized.contains('formatexception')) {
    return '服务端返回的数据格式暂时无法识别。';
  }
  if (normalized.contains('clientexception')) {
    return '客户端请求失败，请稍后重试。';
  }
  if (_containsChinese(message)) {
    return message;
  }
  return message;
}

String _localizeHttpReason(String reasonPhrase) {
  switch (reasonPhrase.trim().toLowerCase()) {
    case 'bad request':
      return '请求参数错误';
    case 'unauthorized':
      return '未授权';
    case 'forbidden':
      return '无权限';
    case 'not found':
      return '资源不存在';
    case 'internal server error':
      return '服务端异常';
    case 'service unavailable':
      return '服务暂不可用';
    default:
      return '';
  }
}

String _humanizeIdentifier(String value) {
  final text = value.trim();
  if (text.isEmpty) {
    return '-';
  }
  return text.replaceAll('_', ' ').replaceAll('-', ' ');
}

bool _containsChinese(String value) {
  return RegExp(r'[\u4e00-\u9fff]').hasMatch(value);
}

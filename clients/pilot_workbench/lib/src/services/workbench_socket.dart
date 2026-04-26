import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

class WorkbenchSocket {
  SocketConnection connect({
    required Uri uri,
    required void Function(Map<String, dynamic> event) onEvent,
    required void Function() onClosed,
    required void Function(Object error) onError,
  }) {
    final channel = WebSocketChannel.connect(uri);
    final subscription = channel.stream.listen(
      (data) {
        if (data is String) {
          final decoded = jsonDecode(data);
          if (decoded is Map<String, dynamic>) {
            onEvent(decoded);
          }
        }
      },
      onDone: onClosed,
      onError: onError,
      cancelOnError: false,
    );
    return SocketConnection(channel: channel, subscription: subscription);
  }
}

class SocketConnection {
  SocketConnection({
    required this.channel,
    required this.subscription,
  });

  final WebSocketChannel channel;
  final StreamSubscription<dynamic> subscription;

  Future<void> close() async {
    await subscription.cancel();
    await channel.sink.close();
  }

  void ping() {
    channel.sink.add('ping');
  }
}

import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

class WorkbenchSocket {
  static const Duration heartbeatInterval = Duration(seconds: 25);

  SocketConnection connect({
    required Uri uri,
    required void Function(Map<String, dynamic> event) onEvent,
    required void Function() onClosed,
    required void Function(Object error) onError,
  }) {
    final channel = WebSocketChannel.connect(uri);
    Timer? heartbeatTimer;
    var isClosed = false;

    void stopHeartbeat() {
      heartbeatTimer?.cancel();
      heartbeatTimer = null;
    }

    void sendPing() {
      if (isClosed) {
        return;
      }
      try {
        channel.sink.add('ping');
      } catch (error) {
        isClosed = true;
        stopHeartbeat();
        onError(error);
      }
    }

    heartbeatTimer = Timer.periodic(heartbeatInterval, (_) => sendPing());

    final subscription = channel.stream.listen(
      (data) {
        if (data is String) {
          final decoded = jsonDecode(data);
          if (decoded is Map<String, dynamic>) {
            onEvent(decoded);
          }
        }
      },
      onDone: () {
        if (isClosed) {
          return;
        }
        isClosed = true;
        stopHeartbeat();
        onClosed();
      },
      onError: (Object error) {
        if (isClosed) {
          return;
        }
        isClosed = true;
        stopHeartbeat();
        onError(error);
      },
      cancelOnError: false,
    );
    return SocketConnection(
      channel: channel,
      subscription: subscription,
      stopHeartbeat: stopHeartbeat,
    );
  }
}

class SocketConnection {
  SocketConnection({
    required this.channel,
    required this.subscription,
    required this.stopHeartbeat,
  });

  final WebSocketChannel channel;
  final StreamSubscription<dynamic> subscription;
  final void Function() stopHeartbeat;

  Future<void> close() async {
    stopHeartbeat();
    await subscription.cancel();
    await channel.sink.close();
  }

  void ping() {
    channel.sink.add('ping');
  }
}

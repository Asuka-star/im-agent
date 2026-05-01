import 'dart:ui';

import 'package:flutter_test/flutter_test.dart';
import 'package:pilot_workbench/src/app.dart';
import 'package:pilot_workbench/src/utils/workbench_labels.dart';

void main() {
  testWidgets('renders the pilot workbench shell', (tester) async {
    tester.view.physicalSize = const Size(1600, 1200);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);

    await tester.pumpWidget(const PilotWorkbenchApp(autoInitialize: false));
    await tester.pump();

    expect(find.text(workbenchAppName), findsOneWidget);
    expect(find.text('任务运行面板'), findsOneWidget);
    expect(find.text('任务详情与产物'), findsOneWidget);
  });
}

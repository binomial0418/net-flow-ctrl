import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:netflow_tv/main.dart';

void main() {
  testWidgets('status screen shows what the native side reports', (tester) async {
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('netflow_tv'),
      (call) async => call.method == 'status'
          ? {
              'granted': true,
              'connected': true,
              'server': 'http://192.168.50.1',
              'lastAt': 0,
              'lastResult': 'OK',
              'lastBody': '{"sessions":[{"package":"com.google.android.youtube.tv","state":"playing","title":"T","artist":"Channel"}]}',
            }
          : null,
    );
    await tester.pumpWidget(const NetflowTvApp());
    await tester.pump();
    expect(find.text('已開啟'), findsOneWidget);
    expect(find.text('執行中'), findsOneWidget);
    expect(find.textContaining('Channel'), findsOneWidget);
    await tester.pumpWidget(const SizedBox()); // dispose: stops the refresh timer
  });
}

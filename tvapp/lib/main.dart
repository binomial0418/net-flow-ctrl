// NetFlow TV: the status screen. The work is done by NowPlayingService on the
// native side, which runs whether or not this screen is open; this only shows
// whether it has the access it needs and what it last sent.
import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

const _channel = MethodChannel('netflow_tv');

void main() => runApp(const NetflowTvApp());

class NetflowTvApp extends StatelessWidget {
  const NetflowTvApp({super.key});

  @override
  Widget build(BuildContext context) {
    return Shortcuts(
      // The remote's centre button arrives as "select": make it press buttons.
      shortcuts: const {SingleActivator(LogicalKeyboardKey.select): ActivateIntent()},
      child: MaterialApp(
        title: 'NetFlow TV',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(colorSchemeSeed: Colors.amber, brightness: Brightness.dark, useMaterial3: true),
        home: const StatusPage(),
      ),
    );
  }
}

class StatusPage extends StatefulWidget {
  const StatusPage({super.key});

  @override
  State<StatusPage> createState() => _StatusPageState();
}

class _StatusPageState extends State<StatusPage> {
  Map<Object?, Object?> _s = const {};
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _refresh();
    _timer = Timer.periodic(const Duration(seconds: 2), (_) => _refresh());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    final s = await _channel.invokeMapMethod<Object?, Object?>('status');
    if (mounted && s != null) setState(() => _s = s);
  }

  String _lastAt() {
    final ms = (_s['lastAt'] as int?) ?? 0;
    if (ms == 0) return '尚未回報';
    final t = DateTime.fromMillisecondsSinceEpoch(ms);
    String two(int n) => n.toString().padLeft(2, '0');
    return '${two(t.hour)}:${two(t.minute)}:${two(t.second)}';
  }

  List<String> _playing() {
    final body = _s['lastBody'] as String?;
    if (body == null) return const [];
    try {
      final sessions = (jsonDecode(body)['sessions'] as List).cast<Map<String, dynamic>>();
      return [
        for (final s in sessions)
          '${s['state'] ?? '-'}  ${s['package']}\n    ${s['title'] ?? ''}${s['artist'] != null ? ' — ${s['artist']}' : ''}'
      ];
    } catch (_) {
      return const [];
    }
  }

  @override
  Widget build(BuildContext context) {
    final granted = _s['granted'] == true;
    final connected = _s['connected'] == true;
    final ok = _s['lastResult'] == 'OK';
    final text = Theme.of(context).textTheme;
    Widget row(String label, String value, {Color? color}) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Row(children: [
            SizedBox(width: 180, child: Text(label, style: text.titleMedium)),
            Expanded(child: Text(value, style: text.titleMedium?.copyWith(color: color))),
          ]),
        );
    return Scaffold(
      body: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 64, vertical: 40),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text('NetFlow TV', style: text.headlineMedium),
          const SizedBox(height: 4),
          Text('把電視正在播放的內容回報給上網管控路由器', style: text.bodyLarge),
          const SizedBox(height: 24),
          row('通知存取權', granted ? '已開啟' : '未開啟（請按下方按鈕開啟）',
              color: granted ? Colors.greenAccent : Colors.redAccent),
          row('背景服務', connected ? '執行中' : '未執行', color: connected ? Colors.greenAccent : Colors.orangeAccent),
          row('路由器', '${_s['server'] ?? '-'}'),
          row('上次回報', '${_lastAt()}  ${_s['lastResult'] ?? ''}', color: ok ? null : Colors.orangeAccent),
          const SizedBox(height: 16),
          Text('目前的媒體', style: text.titleMedium),
          for (final p in _playing()) Padding(padding: const EdgeInsets.only(top: 6), child: Text(p, style: text.bodyLarge)),
          const Spacer(),
          Row(children: [
            FilledButton(
              autofocus: !granted,
              onPressed: () => _channel.invokeMethod('openAccessSettings'),
              child: const Text('開啟通知存取權設定'),
            ),
            const SizedBox(width: 16),
            OutlinedButton(
              autofocus: granted,
              onPressed: () async {
                await _channel.invokeMethod('reportNow');
                await Future<void>.delayed(const Duration(seconds: 2));
                _refresh();
              },
              child: const Text('立即回報'),
            ),
          ]),
        ]),
      ),
    );
  }
}

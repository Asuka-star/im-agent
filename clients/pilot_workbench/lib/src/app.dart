import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:pilot_workbench/src/config/app_config.dart';
import 'package:pilot_workbench/src/ui/dashboard_page.dart';

class PilotWorkbenchApp extends StatelessWidget {
  const PilotWorkbenchApp({super.key, this.autoInitialize = true});

  final bool autoInitialize;

  @override
  Widget build(BuildContext context) {
    const background = Color(0xFFF7F3EA);
    const surface = Color(0xFFFFFCF6);
    const primary = Color(0xFFC85D3A);
    const secondary = Color(0xFF116A7B);

    final colorScheme = ColorScheme.fromSeed(
      seedColor: primary,
      brightness: Brightness.light,
    ).copyWith(primary: primary, secondary: secondary, surface: surface);

    final baseTextTheme = GoogleFonts.notoSansScTextTheme().apply(
      bodyColor: const Color(0xFF172026),
      displayColor: const Color(0xFF172026),
    );

    return MaterialApp(
      title: AppConfig.appName,
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: colorScheme,
        scaffoldBackgroundColor: background,
        textTheme: baseTextTheme.copyWith(
          displaySmall: baseTextTheme.displaySmall?.copyWith(
            fontSize: 34,
            fontWeight: FontWeight.w700,
            color: const Color(0xFF172026),
          ),
          headlineSmall: baseTextTheme.headlineSmall?.copyWith(
            fontSize: 22,
            fontWeight: FontWeight.w700,
            color: const Color(0xFF172026),
          ),
          titleLarge: baseTextTheme.titleLarge?.copyWith(
            fontSize: 18,
            fontWeight: FontWeight.w700,
            color: const Color(0xFF172026),
          ),
        ),
        useMaterial3: true,
      ),
      home: DashboardPage(autoInitialize: autoInitialize),
    );
  }
}

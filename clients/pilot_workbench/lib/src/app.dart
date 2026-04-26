import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:pilot_workbench/src/ui/dashboard_page.dart';

class PilotWorkbenchApp extends StatelessWidget {
  const PilotWorkbenchApp({
    super.key,
    this.autoInitialize = true,
  });

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
    ).copyWith(
      primary: primary,
      secondary: secondary,
      surface: surface,
    );

    final baseTextTheme = GoogleFonts.ibmPlexSansTextTheme();

    return MaterialApp(
      title: 'Pilot Workbench',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: colorScheme,
        scaffoldBackgroundColor: background,
        textTheme: baseTextTheme.copyWith(
          displaySmall: GoogleFonts.sora(
            fontSize: 34,
            fontWeight: FontWeight.w700,
            color: const Color(0xFF172026),
          ),
          headlineSmall: GoogleFonts.sora(
            fontSize: 22,
            fontWeight: FontWeight.w700,
            color: const Color(0xFF172026),
          ),
          titleLarge: GoogleFonts.sora(
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

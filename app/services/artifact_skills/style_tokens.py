from __future__ import annotations


ARTIFACT_STYLE = {
    "font": 'Inter, "Microsoft YaHei", "PingFang SC", Arial, sans-serif',
    "background": "#F6FAFA",
    "surface": "#FFFFFF",
    "ink": "#172026",
    "muted": "#667985",
    "primary": "#145C64",
    "primary_dark": "#103F46",
    "accent": "#F2A65A",
    "accent_soft": "#FFF4E5",
    "border": "#D8E5E8",
    "success_soft": "#EEF8F1",
    "info_soft": "#EAF5FF",
}


def css_color(name: str, fallback: str = "#172026") -> str:
    value = ARTIFACT_STYLE.get(name)
    return value if isinstance(value, str) and value.startswith("#") else fallback


def pptx_color(name: str, fallback: str = "172026") -> str:
    return css_color(name, f"#{fallback}").lstrip("#").upper()

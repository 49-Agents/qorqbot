"""The bundled agent icons a global worker can be given.

The trusted SVG glyphs are the dashboard's own (``AICONS``); a profile stores only an ID in ``presentation.icon`` and the
dashboard draws the glyph by that ID. No upload, URL or SVG payload is ever accepted.
"""

AGENT_ICONS = (
    ("bot", "Bot"), ("brain", "Brain"), ("sparkles", "Sparkles"), ("zap", "Zap"), ("rocket", "Rocket"),
    ("flame", "Flame"), ("ghost", "Ghost"), ("cat", "Cat"), ("dog", "Dog"), ("bird", "Bird"), ("fish", "Fish"),
    ("bug", "Bug"), ("cpu", "CPU"), ("terminal", "Terminal"), ("code", "Code"), ("pen-tool", "Pen tool"),
    ("megaphone", "Megaphone"), ("calculator", "Calculator"), ("chart-column", "Chart"), ("database", "Database"),
    ("search", "Search"), ("globe", "Globe"), ("shield", "Shield"), ("wrench", "Wrench"), ("triangle", "Triangle"),
    ("circle", "Circle"), ("square", "Square"), ("lightbulb", "Lightbulb"), ("flag", "Flag"), ("hand", "Hand"),
)
ICON_IDS = frozenset(icon for icon, _ in AGENT_ICONS)

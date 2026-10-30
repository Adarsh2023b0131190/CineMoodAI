
import os
import textwrap
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Preformatted,
    Spacer, PageBreak
)
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(PROJECT_DIR, "CineMood_AI_Source_Code.pdf")

# File types to include
EXTENSIONS = {
    ".js", ".jsx", ".ts", ".tsx",
    ".py", ".json", ".html", ".css",
    ".md", ".txt", ".yml", ".yaml",
    ".sql", ".java", ".cpp", ".h"
}

# Folders and files to exclude
EXCLUDE_DIRS = {
    "node_modules", ".venv", "venv",
    "__pycache__", ".git", "dist",
    "build", ".pytest_cache", ".next"
}

EXCLUDE_FILES = {
    ".env", "make_code_pdf.py",
    "package-lock.json"
}

doc = SimpleDocTemplate(
    OUTPUT,
    pagesize=landscape(A4),
    rightMargin=15*mm,
    leftMargin=15*mm,
    topMargin=15*mm,
    bottomMargin=15*mm
)

styles = getSampleStyleSheet()

title_style = ParagraphStyle(
    "ProjectTitle",
    parent=styles["Title"],
    alignment=TA_CENTER,
    fontSize=22,
    spaceAfter=15
)

heading_style = ParagraphStyle(
    "FileHeading",
    parent=styles["Heading2"],
    fontSize=12,
    spaceAfter=8
)

code_style = ParagraphStyle(
    "SourceCode",
    fontName="Courier",
    fontSize=6.5,
    leading=8,
    leftIndent=4,
    rightIndent=4
)

story = [
    Paragraph("CineMood AI", title_style),
    Paragraph("Complete Project Source Code", styles["Heading2"]),
    Spacer(1, 15),
]

files = []

for root, dirs, filenames in os.walk(PROJECT_DIR):
    dirs[:] = [
        d for d in dirs
        if d not in EXCLUDE_DIRS
    ]

    for filename in filenames:
        ext = os.path.splitext(filename)[1].lower()

        if ext in EXTENSIONS and filename not in EXCLUDE_FILES:
            path = os.path.join(root, filename)

            # Skip unusually large source files
            if os.path.getsize(path) > 2 * 1024 * 1024:
                continue

            files.append(path)

files.sort()

for index, path in enumerate(files):
    relative_path = os.path.relpath(path, PROJECT_DIR)

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        continue

    if index > 0:
        story.append(PageBreak())

    story.append(Paragraph(relative_path, heading_style))
    story.append(Spacer(1, 5))

    # Wrap long lines for readable PDF output
    wrapped_lines = []
    for line in content.splitlines():
        if len(line) > 115:
            indent = len(line) - len(line.lstrip())
            wrapped_lines.extend(
                textwrap.wrap(
                    line,
                    width=115,
                    subsequent_indent=" " * indent,
                    replace_whitespace=False,
                    drop_whitespace=False
                )
            )
        else:
            wrapped_lines.append(line)

    code = "\n".join(wrapped_lines) or "(Empty file)"

    story.append(
        Preformatted(code, code_style)
    )

doc.build(story)

print("PDF created successfully!")
print("Location:", OUTPUT)
print("Total source files:", len(files))

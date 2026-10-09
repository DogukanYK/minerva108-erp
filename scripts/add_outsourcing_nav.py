"""Add permission-gated Fason link to existing internal sidebar templates."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = ('{% if can(\'outsourcing\',\'view\') %}<a href="/outsourcing" class="nav-link-item">'
        '<i class="bi bi-box-seam nav-icon"></i>Fason Üretim</a>{% endif %}')
MARK = re.compile(r'<a href="/outsourcing"\s+class="nav-link-item')
PATTERN = re.compile(r'^([ \t]*)<a href="/production"\s*class="nav-link-item[^\"]*"[^>]*>.*?</a>[ \t]*$', re.M | re.S)


def main():
    changed = []
    for path in sorted((ROOT / "templates").glob("*.html")):
        text = path.read_text()
        if MARK.search(text):
            continue
        match = PATTERN.search(text)
        if match:
            path.write_text(text[:match.end()] + "\n" + match.group(1) + LINK + text[match.end():])
            changed.append(path.name)
    print("Fason bağlantısı eklendi:", ", ".join(changed) or "yok")


if __name__ == "__main__":
    main()

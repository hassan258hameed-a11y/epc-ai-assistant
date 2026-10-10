import re, sys, pathlib

def inspect(path):
    html = pathlib.Path(path).read_text(encoding='utf-8', errors='replace')
    name = pathlib.Path(path).name
    checks = {
        "tailwind vendored": 'vendor/tailwind.js' in html,
        "tailwind config": 'tailwind.config' in html,
        "brand palette": '#6E51F0' in html,
        "icon sprite": 'id="i-sparkles"' in html,
        "logo svg": 'epcMark' in html,
        "theme persistence": 'epcai-theme' in html,
        "no unrendered template tags": '{%' not in html and '{{' not in html,
        "no 'None' leaking into text": '>None<' not in html,
    }
    print(f"--- {name} ({len(html)} bytes) ---")
    for label, ok in checks.items():
        print(('  OK   ' if ok else '  FAIL '), label)
    for m in re.findall(r'\{[%{].{0,70}', html)[:4]:
        print("   STRAY:", m.replace('\n', ' '))

for p in sys.argv[1:]:
    inspect(p)

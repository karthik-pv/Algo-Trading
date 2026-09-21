"""One-time migration: build templates/app.html from the three standalone pages.

Extracts head_styles / content / scripts blocks from trade.html, orders.html and
appconfig.html, wraps them into tab panels, and writes templates/app.html.
Safe to re-run; original templates are left untouched.
"""
import re
from pathlib import Path

TPL = Path("templates")

PAGES = [
    ("trade", "trade.html"),
    ("orders", "orders.html"),
    ("appconfig", "appconfig.html"),
]


def extract(path: Path, block: str) -> str:
    raw = path.read_text(encoding="utf-8")
    opener = "{% block " + block + " %}"
    start = raw.index(opener) + len(opener)
    end = raw.index("{% endblock %}", start)
    return raw[start:end].strip("\n")


def strip_outer_tag(text: str, tag: str) -> str:
    """Remove an outer <tag ...>...</tag> wrapper (with optional whitespace)."""
    text = text.strip()
    open_re = re.compile(rf"^<{tag}[^>]*>\s*\n?")
    close_re = re.compile(rf"\s*</{tag}>\s*$")
    if not (open_re.match(text) and close_re.search(text)):
        raise ValueError(f"expected outer <{tag}> wrapper not found")
    text = open_re.sub("", text, count=1)
    return close_re.sub("", text, count=1)


def scope_main_content(css: str, panel: str) -> tuple[str, int]:
    """Prefix bare .main-content selectors with the panel id (the only rule set
    that conflicts across pages)."""
    pattern = re.compile(r"(?m)^(\s*)\.main-content(\s*[,{])")
    n = len(pattern.findall(css))

    def repl(m: re.Match) -> str:
        return f"{m.group(1)}#panel-{panel} .main-content{m.group(2)}"

    return pattern.sub(repl, css), n


def main() -> None:
    css_chunks, panels, script_chunks = [], [], []

    for name, fname in PAGES:
        path = TPL / fname
        css = extract(path, "head_styles")
        content = extract(path, "content")
        js = extract(path, "scripts")

        # the original blocks carry their own <style>/<script> wrappers;
        # strip them so the combined page wraps each chunk exactly once
        css = strip_outer_tag(css, "style")
        js = strip_outer_tag(js, "script")

        # sanity: no jinja control structures expected inside content/scripts
        leftovers = re.findall(r"\{%[^%]*%\}|\{\{[^}]*\}\}", content + js)
        if leftovers:
            print(f"[warn] {fname}: jinja remnants in content/scripts: {leftovers[:5]}")

        css, n = scope_main_content(css, name)
        print(f"[info] {fname}: scoped {n} .main-content rule group(s)")

        forms = re.findall(r"<form[^>]*>", content)
        for f in forms:
            if "action=" in f:
                print(f"[warn] {fname}: form with action= would navigate: {f}")

        css_chunks.append(f"<style>\n/* ===== CSS from {fname} ===== */\n{css}\n</style>")
        panels.append(
            f'    <section class="tab-panel" id="panel-{name}" data-tab="{name}">\n'
            f"        {content}\n"
            f"    </section>"
        )
        script_chunks.append(f"<script>\n(function () {{\n{js}\n}})();\n</script>")

    tab_switcher = """<script>
        // Client-side tab switching: panels live in one document, so switching
        // tabs never reloads the page and all JS state / sockets stay alive.
        document.addEventListener('DOMContentLoaded', function () {
            var panels = Array.prototype.slice.call(document.querySelectorAll('.tab-panel'));
            if (!panels.length) return; // standalone pages: let links navigate normally

            var tabLinks = Array.prototype.slice.call(document.querySelectorAll('#appTabs .app-tab'));

            function tabNameFromLink(a) {
                var href = a.getAttribute('href') || '';
                var name = href.replace(/^\\//, '');
                return name === '' ? 'trade' : name;
            }

            function activate(name, skipHash) {
                panels.forEach(function (p) {
                    p.classList.toggle('active', p.id === 'panel-' + name);
                });
                tabLinks.forEach(function (a) {
                    a.classList.toggle('active', tabNameFromLink(a) === name);
                });
                var activeLink = tabLinks.filter(function (a) { return a.classList.contains('active'); })[0];
                var strip = document.getElementById('appTabs');
                if (activeLink && strip && strip.scrollWidth > strip.clientWidth) {
                    activeLink.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                }
                if (!skipHash && location.hash !== '#' + name) {
                    location.hash = name; // history entry so Back/Forward moves between tabs
                }
            }

            tabLinks.forEach(function (a) {
                a.addEventListener('click', function (e) {
                    e.preventDefault();
                    activate(tabNameFromLink(a), false);
                });
            });

            window.addEventListener('hashchange', function () {
                var name = (location.hash || '').replace(/^#/, '');
                if (name && document.getElementById('panel-' + name)) {
                    activate(name, true);
                }
            });

            // Initial tab: URL hash wins, else the active_tab hint from the
            // server route, else the first panel.
            var initial = (location.hash || '').replace(/^#/, '');
            if (!initial || !document.getElementById('panel-' + initial)) {
                initial = document.body.getAttribute('data-active-tab') || '';
            }
            if (!initial || !document.getElementById('panel-' + initial)) {
                initial = panels[0].getAttribute('data-tab');
            }
            activate(initial, true);
        });
    </script>"""

    body_open = '<body data-active-tab="{{ active_tab|default(\'\') }}">'

    app_html = f"""{{% extends "base.html" %}}

{{% block title %}}TradeInvestAlgo{{% endblock %}}

{{% block head_styles %}}
{chr(10).join(css_chunks)}
{{% endblock %}}

{{% block content %}}
{chr(10).join(panels)}
{{% endblock %}}

{{% block scripts %}}
{chr(10).join(script_chunks)}
{tab_switcher}
{{% endblock %}}
"""

    (TPL / "app.html").write_text(app_html, encoding="utf-8")
    print(f"[done] wrote {TPL / 'app.html'} ({len(app_html.splitlines())} lines)")


if __name__ == "__main__":
    main()

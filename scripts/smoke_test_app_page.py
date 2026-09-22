"""Standalone Jinja render check for app.html (no server import needed)."""
import os
import re

import jinja2

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(_PROJECT_ROOT, "templates")))

ok = True
for path, tab in [("/", "trade"), ("/orders", "orders"), ("/appconfig", "appconfig")]:
    request = type("R", (), {"path": path})()
    html = env.get_template("app.html").render(request=request, active_tab=tab)
    links = re.findall(r'<a class="app-tab[^"]*" href="([^"]+)">', html)
    active_links = re.findall(r'<a class="app-tab active" href="([^"]+)">', html)
    main_rules = re.findall(r"(?m)^\s*\.main-content\b", html)  # unscoped rules only
    checks = {
        "panels3": html.count('class="tab-panel"') == 3,
        "body_tab": f'data-active-tab="{tab}"' in html,
        "sockets1": html.count("io('http://localhost:5001'") == 1,
        "shared_refs": html.count("window.__appSocket") == 4,  # 1 create + 3 panel refs
        "iifes3": html.count("(function () {") == 3,
        "script_tags": len(re.findall(r"<script>", html)) == 6,  # base scroll + shared + 3 pages + switcher (CDN has src=)
        "style_tags": len(re.findall(r"<style>", html)) == 4,  # base universal + 3 pages
        "tabbar3": links == ["/trade", "/orders", "/appconfig"],
        "active_link": active_links == ["/trade" if tab == "trade" else "/" + tab],
        "scoped_css": f"#panel-{tab} .main-content" in html,
        "unscoped_gone": not main_rules,
        "switcher": "hashchange" in html,
    }
    line = f"{path:12} " + " ".join(k + ("=OK" if v else "=FAIL") for k, v in checks.items())
    print(line)
    ok = ok and all(checks.values())

# active tab highlighting for the /orders route should be on the Orders link
request = type("R", (), {"path": "/orders"})()
html = env.get_template("app.html").render(request=request, active_tab="orders")
link_ok = 'href="/orders" class="app-tab active"' in html or ('href="/orders"' in html and "app-tab active" in html)
print("orders link active:", "=OK" if link_ok else "=FAIL")
ok = ok and link_ok

print("ALL PASS" if ok else "FAILURES PRESENT")

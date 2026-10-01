#!/usr/bin/env python3
"""Export an opencode session to a collapsible Markdown transcript.

Usage:
  python3 scripts/export_session.py                # most recent session
  python3 scripts/export_session.py <sessionID>    # specific session
  python3 scripts/export_session.py <file.json>    # pre-exported JSON

Output: data/session_transcript.md
 - your prompts are plain headings (always visible)
 - each AI response is inside <details><summary> (collapsible in
   VS Code preview, GitHub, Obsidian, IntelliJ, ...)
"""
import json, os, subprocess, sys, datetime, html, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "session_transcript.md")

def load_export():
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if os.path.isfile(arg):
            return arg, json.load(open(arg))
        sid = arg
    else:
        out = subprocess.run(["opencode", "session", "list"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("ses_"):
                sid = line.split()[0]
                break
        else:
            sys.exit("no sessions found")
    tmp = os.path.join(ROOT, "data", ".session_export_tmp.json")
    with open(tmp, "w") as fh:
        subprocess.run(["opencode", "export", sid], stdout=fh, stderr=subprocess.DEVNULL, check=True)
    with open(tmp) as fh:
        return tmp, json.load(fh)

def fmt_ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%d %b %Y %H:%M")

def clean(text):
    return text.strip()

def main():
    tmp, data = load_export()
    msgs = data["messages"]

    # group into turns: user message starts a turn, following assistant messages join it
    turns = []
    for m in msgs:
        role = m["info"]["role"]
        if role == "user" or not turns:
            turns.append({"user": None, "assistant": [], "t": m["info"]["time"]["created"], "role": role})
        if role == "user":
            turns[-1]["user"] = m
        else:
            turns[-1]["assistant"].append(m)

    lines = [
        f"# Session transcript — {fmt_ts(msgs[0]['info']['time']['created'] if msgs else 0)}",
        "",
        "> Collapsible: open in VS Code Markdown preview / GitHub / Obsidian.",
        "> Click a summary line to expand that response.",
        "",
    ]
    # table of contents (the "only prompts" view)
    toc = []
    for i, t in enumerate(turns, 1):
        if t["user"]:
            first = clean(t["user"]["parts"][0]["text"]).splitlines()[0][:90]
            toc.append(f"{i}. [{first}](#{i}-prompt)")
    lines += ["## Prompts", ""] + toc + ["", "---", ""]

    for i, t in enumerate(turns, 1):
        if t["user"]:
            first = clean(t["user"]["parts"][0]["text"])
            lines += [f'<a id="{i}-prompt"></a>', f"## {i}. You — {fmt_ts(t['t'])}", "", first, ""]
        for a in t["assistant"]:
            pass
        texts = [clean(p["text"]) for a in t["assistant"]
                 for p in a["parts"] if p["type"] == "text" and p.get("text", "").strip()]
        tools = [p["tool"] for a in t["assistant"] for p in a["parts"] if p["type"] == "tool"]
        if texts or tools:
            nlines = sum(x.count("\n") + 1 for x in texts)
            summary = f"🤖 response — {nlines} lines" + (f", {len(tools)} tool calls" if tools else "")
            body = "\n\n".join(texts)
            if tools:
                body += "\n\n---\n*tools used:* " + ", ".join(f"`{x}`" for x in tools)
            lines += ["<details>", f"<summary>{summary}</summary>", "", body, "", "</details>", ""]
        lines.append("")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        fh.write("\n".join(lines))
    if tmp.endswith(".json") and "tmp" in os.path.basename(tmp):
        os.remove(tmp)
    print(f"wrote {OUT}: {len(turns)} turns, {len(msgs)} messages")

if __name__ == "__main__":
    main()

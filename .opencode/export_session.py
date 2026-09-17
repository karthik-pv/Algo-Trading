r"""
Export an opencode session transcript to a readable Markdown file.

Usage (from the repo root, with the venv python):
    venv\Scripts\python.exe .opencode\export_session.py                # latest session for this directory
    venv\Scripts\python.exe .opencode\export_session.py ses_xxxxxxxx   # a specific session id

Output: .opencode\transcripts\<date>_<slug>.md

Includes: your messages, assistant replies, model reasoning (in
collapsible blocks), every tool call with its input and a truncated
output, and file-change records.
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(
    os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db"
)
OUTPUT_LIMIT = 1500   # max chars of tool output kept in the transcript
INPUT_LIMIT = 600     # max chars of tool input kept in the transcript


def fmt_ts(ms):
    return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def clip(text, limit):
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text)} chars total]"


def fence(text):
    # use a 4-backtick fence so embedded triple-backtick blocks survive
    return "````text\n" + (text or "") + "\n````"


def slugify(s, fallback="session"):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (s or "").strip().lower()).strip("-")
    return (s[:80] or fallback)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("session_id", nargs="?", default=None)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--outdir", default=os.path.join(REPO, ".opencode", "transcripts"))
    ap.add_argument("--directory", default=REPO.replace("\\", "/"))
    ap.add_argument("--full-output", action="store_true",
                    help="keep complete tool outputs (large file)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"opencode database not found: {args.db}")

    conn = sqlite3.connect(f"file:{args.db.replace(os.sep, '/')}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if args.session_id:
        session = cur.execute(
            "SELECT * FROM session WHERE id=?", (args.session_id,)
        ).fetchone()
    else:
        session = cur.execute(
            "SELECT * FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1",
            (args.directory,),
        ).fetchone()

    if not session:
        sys.exit("Session not found.")

    sid = session["id"]
    out_limit = None if args.full_output else OUTPUT_LIMIT

    lines = []
    lines.append(f"# {session['title'] or 'opencode session'}")
    lines.append("")
    lines.append(f"- **Session id:** `{sid}`")
    lines.append(f"- **Project:** {session['directory']}")
    lines.append(f"- **Started:** {fmt_ts(session['time_created'])}")
    lines.append(f"- **Last update:** {fmt_ts(session['time_updated'])}")
    if session["model"]:
        lines.append(f"- **Model:** {session['model']}")
    if session["cost"]:
        lines.append(f"- **Total cost:** ${float(session['cost']):.2f}")
    lines.append("")
    lines.append("---")
    lines.append("")

    turn = 0
    messages = cur.execute(
        "SELECT id, time_created, data FROM message WHERE session_id=? "
        "ORDER BY time_created, rowid",
        (sid,),
    ).fetchall()

    for msg in messages:
        try:
            mdata = json.loads(msg["data"])
        except Exception:
            mdata = {}
        role = mdata.get("role", "unknown")

        parts = cur.execute(
            "SELECT data FROM part WHERE message_id=? ORDER BY time_created, rowid",
            (msg["id"],),
        ).fetchall()

        rendered = []
        for p in parts:
            try:
                pd = json.loads(p["data"])
            except Exception:
                continue
            ptype = pd.get("type")

            if ptype == "text":
                rendered.append(("text", pd.get("text", "")))

            elif ptype == "reasoning":
                rendered.append(("reasoning", pd.get("text", "")))

            elif ptype == "tool":
                tool = pd.get("tool", "?")
                state = pd.get("state", {}) or {}
                inp = state.get("input", {}) or {}
                outp = state.get("output", "")
                if isinstance(outp, dict):
                    outp = json.dumps(outp, indent=2)
                rendered.append(("tool", tool, inp, outp))

            elif ptype == "patch":
                rendered.append(("patch", pd.get("files", [])))

        if not rendered:
            continue

        if role == "user":
            turn += 1
            lines.append(f"## Turn {turn} - You  ({fmt_ts(msg['time_created'])})")
            lines.append("")
        else:
            lines.append(f"### {role.capitalize()}  ({fmt_ts(msg['time_created'])})")
            lines.append("")

        for item in rendered:
            kind = item[0]
            if kind == "text":
                lines.append(item[1])
                lines.append("")
            elif kind == "reasoning":
                lines.append("<details><summary>Model reasoning</summary>")
                lines.append("")
                lines.append(item[1])
                lines.append("")
                lines.append("</details>")
                lines.append("")
            elif kind == "tool":
                _, tool, inp, outp = item
                lines.append(f"**[tool] {tool}**")
                inp_text = json.dumps(inp, indent=2, default=str)
                lines.append(fence(clip(inp_text, INPUT_LIMIT)))
                if outp:
                    lines.append(fence(clip(outp, out_limit)))
                lines.append("")
            elif kind == "patch":
                files = item[1]
                lines.append("**[files changed]** " + ", ".join(
                    os.path.basename(f) for f in files
                ))
                lines.append("")

    outdir = args.outdir
    os.makedirs(outdir, exist_ok=True)
    date = datetime.datetime.fromtimestamp(session["time_created"] / 1000).strftime("%Y-%m-%d")
    fname = f"{date}_{slugify(session['title'] or session['slug'])}.md"
    path = os.path.join(outdir, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    size_kb = os.path.getsize(path) / 1024
    print(f"Exported {len(messages)} messages to: {path} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()

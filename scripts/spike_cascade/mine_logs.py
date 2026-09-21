"""Pair real user utterances from Emma's logs with the tools Realtime fired after them.

Spike code: disposable. Output stays under data/ (gitignored: it holds real speech).
"""

import glob
import json
import os
import sys
from datetime import datetime

LOGS = sorted(glob.glob(os.path.expanduser("~/Library/Logs/Emma/emma.log*")))
events = []
for path in LOGS:
    with open(path, errors="replace") as f:
        for line in f:
            if not line.startswith("{"):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ev = e.get("event")
            ts = e.get("timestamp")
            if not ts:
                continue
            t = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if ev == "transcript_captured" and e.get("user"):
                events.append((t, "user", e["user"], path))
            elif ev in ("low_confidence_transcript", "stt_user_test"):
                events.append((t, "user", e["text"], path))
            elif ev == "transcript_corrected":
                events.append((t, "user", e["after"], path))
            elif ev == "tool_started":
                events.append((t, "tool", e["name"], path))
            elif ev in ("wake_detected", "conversation_start", "conversation_end"):
                events.append((t, "boundary", ev, path))
events.sort()

# Dedupe a user text appearing via two events within 3 s.
out, last = [], {}
for i, (t, kind, val, path) in enumerate(events):
    if kind != "user":
        continue
    if val in last and t - last[val] < 3:
        continue
    last[val] = t
    tools = []
    for t2, k2, v2, _ in events[i + 1 :]:
        if (
            t2 - t > 25
            or k2 == "user"
            or (k2 == "boundary" and v2 != "conversation_start")
        ):
            break
        if k2 == "tool":
            tools.append(v2)
    out.append(
        {
            "ts": datetime.utcfromtimestamp(t).isoformat(timespec="seconds"),
            "text": val,
            "realtime_tools": tools,
            "log": os.path.basename(path),
        }
    )

json.dump(out, open(sys.argv[1], "w"), ensure_ascii=False, indent=1)
print(
    len(out),
    "utterances;",
    sum(1 for o in out if o["realtime_tools"]),
    "followed by a tool",
)

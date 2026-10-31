"""Native Windows file dialogs. Each runs in a short-lived subprocess so Tk never touches the
web server's threads."""
import json
import subprocess
import sys

_SCRIPT = r"""
import json, sys, tkinter as tk
from tkinter import filedialog
args = json.loads(sys.argv[1])
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); root.update()
kind = args.pop('kind')
fn = {'folder': filedialog.askdirectory, 'save': filedialog.asksaveasfilename,
      'open': filedialog.askopenfilename}[kind]
if 'filetypes' in args: args['filetypes'] = [tuple(x) for x in args['filetypes']]
result = fn(parent=root, **args)
root.destroy()
print(json.dumps(result or ''))
"""


def ask(kind, **options):
    """kind: 'folder' | 'save' | 'open'. Returns the chosen path or '' if cancelled."""
    proc = subprocess.run([sys.executable, "-c", _SCRIPT, json.dumps({"kind": kind, **options})],
                          capture_output=True, text=True, timeout=600,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return ""

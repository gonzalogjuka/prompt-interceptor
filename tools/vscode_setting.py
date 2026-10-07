"""Point the Claude Code VS Code extension at the interceptor (or back to direct).

Adds or updates ANTHROPIC_BASE_URL in `claudeCode.environmentVariables` of the VS Code settings file
the extension reads on this machine, keeping every other setting. Writes a .bak next to it first.

    python tools/vscode_setting.py            # add/update (default http://127.0.0.1:8787)
    python tools/vscode_setting.py --remove   # back to direct
    python tools/vscode_setting.py --file PATH --url URL

Which file, when --file is not given:
- Windows: %APPDATA%\\Code\\User\\settings.json
- Linux with a VS Code Remote-SSH server (~/.vscode-server): its Machine settings, which the extension
  uses when you connect from another PC
- Linux/Mac desktop otherwise: ~/.config/Code/User/settings.json (Mac: ~/Library/Application Support/...)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

KEY, VAR = "claudeCode.environmentVariables", "ANTHROPIC_BASE_URL"


def default_file() -> Path:
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ["APPDATA"]) / "Code" / "User" / "settings.json"
    if (home / ".vscode-server").is_dir():
        return home / ".vscode-server" / "data" / "Machine" / "settings.json"
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / "Code" / "User" / "settings.json"
    return home / ".config" / "Code" / "User" / "settings.json"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--file", type=Path, default=None)
    p.add_argument("--url", default="http://127.0.0.1:8787")
    p.add_argument("--remove", action="store_true")
    args = p.parse_args()
    path = args.file or default_file()

    text = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        settings = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        print(f"{path} tiene comentarios o comas sobrantes (JSONC): no lo toco. Agregalo a mano:\n"
              f'  "{KEY}": [{{"name": "{VAR}", "value": "{args.url}"}}]')
        return 1

    env = [e for e in settings.get(KEY, []) if isinstance(e, dict) and e.get("name") != VAR]
    if not args.remove:
        env.append({"name": VAR, "value": args.url})
    if env:
        settings[KEY] = env
    else:
        settings.pop(KEY, None)

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.write_text(json.dumps(settings, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{path}: {VAR} {'quitado' if args.remove else '= ' + args.url}. "
          "Recarga VS Code (Ctrl+Shift+P > Developer: Reload Window).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

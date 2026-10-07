# Instalar el interceptor en otra máquina

Copiar y pegar en orden. Cada paso termina con un **chequeo**: si no da lo esperado, ver
[Si algo falla](#si-algo-falla) antes de seguir. El interceptor se instala **donde corre Claude Code**
(si usás VS Code Remote-SSH o una terminal SSH a una VM, en la VM).

Todo lo que define al interceptor viaja con el repo (`config.env`, `routes.json`, `perfil.md`).
No hace falta copiar nada más: `.env` se crea solo y vacío está bien.

---

## Windows

**1. Herramientas** (si ya están, saltear). Después **cerrar y abrir la terminal**.
```powershell
winget install --id Git.Git -e; winget install --id Python.Python.3.12 -e; winget install --id GitHub.cli -e
```
Chequeo: `git --version; python --version; gh --version` muestran versiones. Claude Code instalado y con sesión
(`claude --version`).

**2. Clonar** (iniciar sesión con **gonzalogjuka**, el repo es privado):
```powershell
gh auth login -h github.com -p https -w
gh auth switch -h github.com -u gonzalogjuka   # si hay mas de una cuenta: la activa tiene que ser gonzalogjuka
gh repo clone gonzalogjuka/prompt-interceptor "$HOME\Documents\Repos\prompt-interceptor"
cd "$HOME\Documents\Repos\prompt-interceptor"
```
Chequeo: `git log --oneline -1` muestra un commit.

**3. Instalar y dejarlo corriendo** (arranca ahora y en cada inicio de sesión, sin ventana):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\autostart.ps1 -Install
```
Chequeo: termina con `Interceptor corriendo en http://127.0.0.1:8787`, y
```powershell
Invoke-RestMethod http://127.0.0.1:8787/healthz; Select-String interceptor.log -Pattern '\[update\]|modo=' | Select-Object -Last 2
```
da `status ok`, una línea `[update] repo al dia (...)` y una `modo=... perfil=... chars`.

**4. Conectar Claude** (lo que uses):
```powershell
.\.venv\Scripts\python.exe tools\vscode_setting.py          # VS Code: despues Ctrl+Shift+P > Developer: Reload Window
setx ANTHROPIC_BASE_URL http://127.0.0.1:8787               # toda terminal nueva (solo con el paso 3 hecho)
```
O sin tocar nada: doble clic en `2-Claude-con-interceptor.cmd` (solo esa terminal). Retoma la ultima
conversacion de la carpeta, en bypass; si no hay ninguna, abre una nueva.

**5. Prueba final:**
```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"; claude -p "responde solo OK"
Select-String interceptor.log -Pattern '\[context\]' | Select-Object -Last 1
```
Chequeo: Claude responde `OK` y aparece `[context] perfil 1.0k + ...`. **Listo.**

---

## Linux (Debian / Ubuntu, también una VM por SSH)

**1. Herramientas** (si ya están, saltear):
```bash
sudo apt-get update && sudo apt-get install -y git python3 python3-venv curl wget
sudo mkdir -p -m 755 /etc/apt/keyrings && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null \
  && sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null \
  && sudo apt-get update && sudo apt-get install -y gh
```
Chequeo: `git --version; python3 --version; gh --version; claude --version` muestran versiones
(Python 3.10 o más nuevo; los tests corren en 3.12).

**2. Clonar** (con **gonzalogjuka**; por SSH, `-w` da un código para pegar en el navegador de tu PC):
```bash
gh auth login -h github.com -p https -w
gh auth setup-git                 # git pull sin preguntar: lo usa la actualizacion automatica
gh repo clone gonzalogjuka/prompt-interceptor ~/prompt-interceptor && cd ~/prompt-interceptor
```
Chequeo: `git log --oneline -1` muestra un commit.

**3. Instalar como servicio** (systemd de usuario: arranca solo, se reinicia si cae, sigue aunque cierres SSH):
```bash
./autostart.sh --install
```
Chequeo: termina con `Interceptor corriendo en http://127.0.0.1:8787`, y
```bash
curl -s http://127.0.0.1:8787/healthz; grep -E '\[update\]|modo=' interceptor.log | tail -2
```
da `{"status":"ok"}`, una línea `[update] repo al dia (...)` y una `modo=... perfil=... chars`.

**4. Conectar Claude** (lo que uses):
```bash
.venv/bin/python tools/vscode_setting.py      # VS Code (con Remote-SSH: settings de la VM); despues Reload Window
echo 'export ANTHROPIC_BASE_URL=http://127.0.0.1:8787' >> ~/.bashrc && source ~/.bashrc   # toda terminal nueva
```
O solo una terminal: `./claude-interceptor.sh`.

**5. Prueba final:**
```bash
ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "responde solo OK"; grep '\[context\]' interceptor.log | tail -1
```
Chequeo: Claude responde `OK` y aparece `[context] perfil 1.0k + ...`. **Listo.**

---

## Día a día

| Qué | Windows | Linux |
|---|---|---|
| Estado | `.\autostart.ps1 -Status` | `./autostart.sh --status` |
| Ver el log en vivo | `4-Ver-log.cmd` | `tail -f interceptor.log` |
| Tomar ya un PR mergeado (si no, entra solo al próximo arranque) | `.\autostart.ps1 -Stop; .\autostart.ps1 -Install` | `./autostart.sh --restart` |
| Detener / desinstalar | `.\autostart.ps1 -Stop` / `-Uninstall` | `./autostart.sh --stop` / `--uninstall` |
| Volver a Claude directo (sin interceptor) | `.\.venv\Scripts\python.exe tools\vscode_setting.py --remove` y `[Environment]::SetEnvironmentVariable("ANTHROPIC_BASE_URL", $null, "User")` | `.venv/bin/python tools/vscode_setting.py --remove` y borrar la línea de `~/.bashrc` |
| Tests | `.\test.ps1` | `.venv/bin/python -m pip install -r requirements-dev.txt && .venv/bin/python -m pytest` |

## Si algo falla

| Síntoma | Causa | Arreglo |
|---|---|---|
| `git`, `gh` o `python` "no se reconoce" | la terminal no tomó el PATH nuevo | cerrar y abrir la terminal |
| `no se puede cargar ... la ejecución de scripts está deshabilitada` | política de PowerShell | usar el comando del paso 3 tal cual (`-ExecutionPolicy Bypass`) |
| `gh repo clone`: `Could not resolve to a Repository` | la cuenta activa no es gonzalogjuka | `gh auth switch -h github.com -u gonzalogjuka` |
| Log: `[update] no se pudo actualizar ... Repository not found` | igual que arriba (no frena nada: arranca con lo que hay) | igual que arriba; en Linux además `gh auth setup-git` |
| `The virtual environment was not created successfully` | falta venv en Debian/Ubuntu | `sudo apt-get install -y python3-venv`, borrar `.venv` y repetir el paso 3 |
| Linux: el servicio muere al cerrar SSH | linger no quedó activo | `sudo loginctl enable-linger $USER` |
| Linux: no levanta | ver el motivo | `journalctl --user -u prompt-interceptor -e --no-pager` y `tail -50 interceptor.log` |
| Claude: `Unable to connect` / `ECONNREFUSED 127.0.0.1:8787` | el interceptor no corre y Claude apunta a él | paso 3 (o volver a directo, ver Día a día) |
| `tools/vscode_setting.py` dice JSONC | settings.json con comentarios | pegar a mano la línea que imprime |
| Puerto 8787 ocupado | otra instancia u otro programa | `PORT=8788` en `.env` y usar ese puerto en el paso 4 |

## Para Claude (guía autónoma)

Si una sesión de Claude Code instala esto sola: seguir la sección del sistema operativo paso a paso,
correr cada **chequeo** y no avanzar si falla (tabla de arriba). Lo que no puede hacer sola y tiene
que pedirle a Gonzalo: el login de `gh auth login -w` (el código se pega en un navegador) y, si falta,
el login de Claude Code. Nunca pedir ni guardar contraseñas o tokens; `.env` puede quedar vacío.
Al terminar, informar la salida de los chequeos 3 y 5.

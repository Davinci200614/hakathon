#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP="$ROOT/nouba-agriflow"
PIDS=()

case "${1:-}" in
  ''|--ngrok) ;;
  *) echo "Usage: $0 [--ngrok]"; exit 2 ;;
esac

cleanup() {
  trap - EXIT INT TERM
  echo "Stopping servers..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

cd "$APP/backend"
if [[ ! -f .venv/bin/activate ]]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
if ! python -c 'import uvicorn, fastapi, dotenv, httpx, google.genai' 2>/dev/null; then
  python -m pip install -r requirements.txt
fi
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Created backend/.env; fill in your API credentials for WhatsApp and voice."
fi
command -v npm >/dev/null || { echo "Install Node.js and npm first."; exit 1; }
if [[ ! -d "$APP/frontend/node_modules" ]]; then
  (cd "$APP/frontend" && npm install)
fi
if [[ "${1:-}" == --ngrok ]]; then
  command -v ngrok >/dev/null || { echo "Install and configure ngrok first."; exit 1; }
fi

# Read only non-secret settings through the same configuration as the API.
PROVIDER="$(python -c 'from app import config; from app.understanding import selected_provider; print(selected_provider() if config.CONVERSATION_CORE == "v2" else config.LLM_PROVIDER)')"
OLLAMA_URL="$(python -c 'from app.config import OLLAMA_BASE_URL; print(OLLAMA_BASE_URL)')"
ollama_ready() {
  python - "$OLLAMA_URL" <<'PY'
import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1] + '/api/version', timeout=1).close()
except Exception:
    sys.exit(1)
PY
}
if [[ "$PROVIDER" == ollama ]] && ! ollama_ready; then
  case "$OLLAMA_URL" in
    http://127.0.0.1:11434|http://localhost:11434)
      command -v ollama >/dev/null || { echo "Install Ollama first (see backend/OLLAMA.md)."; exit 1; }
      OLLAMA_HOST=127.0.0.1:11434 OLLAMA_NUM_PARALLEL=1 OLLAMA_MAX_LOADED_MODELS=1 OLLAMA_CONTEXT_LENGTH=1024 ollama serve &
      PIDS+=("$!")
      for ((attempt=0; attempt<30; attempt++)); do
        if ollama_ready; then break; fi
        sleep 1
      done
      ollama_ready || { echo "Ollama failed to start."; exit 1; }
      ;;
    *) echo "Configured Ollama server is unavailable: $OLLAMA_URL"; exit 1 ;;
  esac
fi

# Reuse recognized services; choose another dashboard port if necessary.
EXISTING="$(python - "$APP/frontend/index.html" <<'PY'
import json, os, re, signal, socket, sys, time, urllib.request
from pathlib import Path
title = re.search(r'<title>(.*?)</title>', Path(sys.argv[1]).read_text()).group(1)
# Ctrl+Z suspends the reload parent and its worker without releasing the port.
# Resume only processes belonging to this project's backend process tree.
backend_dir = Path(sys.argv[1]).resolve().parents[1] / 'backend'
processes = {}
for proc in Path('/proc').glob('[0-9]*'):
    try:
        status = dict(line.split(':', 1) for line in (proc / 'status').read_text().splitlines() if ':' in line)
        args = (proc / 'cmdline').read_bytes().split(b'\0')
        processes[int(proc.name)] = (int(status['PPid']), status['State'].strip()[0], args, proc / 'cwd')
    except (OSError, ValueError, KeyError):
        continue
roots = set()
for pid, (_, state, args, cwd) in processes.items():
    try:
        if state == 'T' and b'uvicorn' in args and b'app.main:app' in args and b'8000' in args and cwd.resolve() == backend_dir:
            roots.add(pid)
    except OSError:
        pass
family = set(roots)
while True:
    children = {pid for pid, (parent, _, _, _) in processes.items() if parent in family}
    if children <= family:
        break
    family.update(children)
for pid in family:
    if processes[pid][1] == 'T':
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
if roots:
    print('Resumed suspended backend (Ctrl+Z). Use Ctrl+C to stop servers.', file=sys.stderr)
    time.sleep(1)
found = []
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
dashboard_port = 5173
for port, name, path in [(8000, 'backend', '/openapi.json'), (5173, 'dashboard', '/')]:
    with socket.socket() as sock:
        occupied = sock.connect_ex(('127.0.0.1', port)) == 0
    if not occupied:
        continue
    try:
        with opener.open(f'http://127.0.0.1:{port}{path}', timeout=3) as response:
            body = response.read().decode()
        if name == 'backend':
            valid = json.loads(body).get('info', {}).get('title') == 'Nouba × AgriFlow API'
        else:
            valid = f'<title>{title}</title>' in body and '/@vite/client' in body
        if not valid:
            raise ValueError('unrecognized service')
    except Exception:
        if name == 'dashboard':
            for candidate in range(5174, 5200):
                try:
                    with socket.socket() as available:
                        available.bind(('0.0.0.0', candidate))
                    dashboard_port = candidate
                    break
                except OSError:
                    continue
            else:
                print('No free dashboard port between 5174 and 5199.', file=sys.stderr)
                sys.exit(1)
            print(f'Port 5173 is busy or unresponsive; using {dashboard_port} for the dashboard.', file=sys.stderr)
            continue
        print(f'Port {port} is occupied but the project API could not be verified ({type(sys.exception()).__name__}). If its terminal shows Stopped, run fg there; use Ctrl+C to stop it.', file=sys.stderr)
        sys.exit(1)
    found.append(name)
print(' '.join(found + [f'port={dashboard_port}']))
PY
)"
FRONTEND_PORT="${EXISTING##*port=}"
if [[ "$EXISTING" == *backend* && "$FRONTEND_PORT" != 5173 ]]; then
  echo "Using Vite's /api proxy to the existing backend."
fi
if [[ "$EXISTING" == *backend* ]]; then
  echo "Reusing the running backend on port 8000."
else
  API_CORS="$(python -c 'from app.config import CORS_ORIGINS; print(",".join(CORS_ORIGINS))')"
  CORS_ORIGINS="$API_CORS,http://localhost:$FRONTEND_PORT,http://127.0.0.1:$FRONTEND_PORT" python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload &
  PIDS+=("$!")
fi
if [[ "$EXISTING" == *dashboard* ]]; then
  echo "Reusing the running dashboard on port 5173."
else
  (cd "$APP/frontend" && exec npm run dev -- --port "$FRONTEND_PORT" --strictPort) &
  PIDS+=("$!")
fi
if [[ "${1:-}" == --ngrok ]]; then
  if python - <<'PY'
import json, sys, urllib.request
try:
    data = json.load(urllib.request.urlopen('http://127.0.0.1:4040/api/tunnels', timeout=2))
    for tunnel in data.get('tunnels', []):
        if tunnel.get('config', {}).get('addr') in ('http://localhost:8000', 'http://127.0.0.1:8000', 'localhost:8000', '8000'):
            print('Reusing ngrok:', tunnel['public_url'])
            sys.exit(0)
except Exception:
    pass
sys.exit(1)
PY
  then
    :
  else
    ngrok http 8000 &
    PIDS+=("$!")
  fi
fi

echo "Dashboard: http://localhost:$FRONTEND_PORT"
echo "API docs:  http://localhost:8000/docs"
echo "Press Ctrl+C to stop. Reused services will keep running."
if ((${#PIDS[@]})); then
  wait -n "${PIDS[@]}"
else
  echo "All requested services are already running."
fi

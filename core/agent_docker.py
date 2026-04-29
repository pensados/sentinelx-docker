# agent_docker.py — SentinelX variante Docker
#
# Soporta dos modos de ejecucion, controlados por la variable de entorno
# SENTINEL_EXEC_MODE (default: "host"):
#
#   host       — Los comandos se ejecutan en el HOST real via nsenter,
#                entrando en el PID namespace del proceso init (PID 1).
#                El container ve el mismo filesystem, red y servicios del host.
#                Requiere en docker-compose.yml: pid: "host" + privileged: true
#
#   container  — Los comandos se ejecutan DENTRO del container.
#                Util para agentes aislados, CI/CD, o entornos de desarrollo.
#                No requiere privilegios especiales.
#
# La allowlist de comandos y todos los endpoints son identicos en ambos modos.
# El modo activo se expone en GET /capabilities → "mode".

from fastapi import FastAPI, Request, Header, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, model_validator
from typing import Optional, Literal
import subprocess
import os
import time
import json
import uuid
import hashlib
import shutil
from pathlib import Path
from logger import log_exec
from context import context
from logger_exec import log_command

app = FastAPI(title="SentinelX", version="0.3.5-docker")

AGENT_TOKEN = os.getenv("SENTINEL_TOKEN", "changeme")

BASE_DIR = Path(__file__).resolve().parent
BIN_DIR = BASE_DIR / "bin"

# En modo host, pensa-safe-edit se resuelve via PATH del host (nsenter).
# En modo container, se usa la ruta relativa dentro del container.
_DEFAULT_SAFE_EDIT = "pensa-safe-edit" if os.getenv("SENTINEL_EXEC_MODE", "host").strip().lower() == "host" \
    else str(BIN_DIR / "sentinelx-safe-edit")
PENSA_SAFE_EDIT_BIN = os.getenv("SENTINEL_SAFE_EDIT_BIN", _DEFAULT_SAFE_EDIT)

UPLOAD_BASE_DIR = Path(os.getenv("SENTINEL_UPLOAD_DIR", "/var/lib/sentinelx/uploads")).resolve()
UPLOAD_TMP_DIR = UPLOAD_BASE_DIR / ".sentinelx_uploads"
MAX_UPLOAD_BYTES = int(os.getenv("SENTINEL_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024 * 1024)))

# ── Modo de ejecucion ────────────────────────────────────────────────────────
# SENTINEL_EXEC_MODE=host      → nsenter al host (default)
# SENTINEL_EXEC_MODE=container → corre local en el container

EXEC_MODE = os.getenv("SENTINEL_EXEC_MODE", "host").strip().lower()
if EXEC_MODE not in ("host", "container"):
    raise ValueError(f"SENTINEL_EXEC_MODE debe ser 'host' o 'container', recibido: '{EXEC_MODE}'")

NSENTER_BASE = [
    "nsenter",
    "--target", "1",
    "--mount",
    "--uts",
    "--ipc",
    "--net",
    "--pid",
    "--",
]

def _prefix(args: list[str]) -> list[str]:
    """Antepone nsenter si estamos en modo host, o devuelve args sin cambios."""
    return NSENTER_BASE + args if EXEC_MODE == "host" else args

PATH_INDEX = {
    "root":          {"path": "/",                          "description": "Raiz del filesystem del host"},
    "home":          {"path": "/home/carlos",               "description": "Home principal"},
    "projects":      {"path": "/home/carlos/projects",      "description": "Repos y proyectos"},
    "docker":        {"path": "/home/carlos/docker",        "description": "Stacks Docker"},
    "services":      {"path": "/home/carlos/services",      "description": "Servicios propios"},
    "pensainfra":    {"path": "/home/carlos/pensainfra",    "description": "Infra principal"},
    "var_log":       {"path": "/var/log",                   "description": "Logs del sistema"},
    "var_www":       {"path": "/var/www",                   "description": "Webroots"},
    "etc":           {"path": "/etc",                       "description": "Configuracion del sistema"},
    "nginx_sites":   {"path": "/etc/nginx/sites-available", "description": "Configs nginx"},
    "systemd_units": {"path": "/etc/systemd/system",        "description": "Servicios systemd"},
    "local_bin":     {"path": "/usr/local/bin",             "description": "Binarios instalados"},
    "opt":           {"path": "/opt",                       "description": "Software instalado"},
}

ALLOWED_COMMANDS = [
    "uptime", "pwd", "whoami", "id", "ls", "tree", "cat", "sudo cat", "head", "tail", "less",
    "grep", "find", "sort", "du -h", "df -h", "touch", "echo", "printf", "tee", "cp", "mv",
    "mkdir", "sudo touch", "getfacl", "rm", "ln -s", "unlink", "chmod", "chown",
    "docker", "sudo docker", "nginx -t", "sudo nginx -t", "ip a", "ip route", "ss -tuln",
    "netstat -tuln", "ping", "traceroute", "tcpdump", "dig", "nslookup",
    "cloudflared tunnel list", "cloudflared tunnel run", "cloudflared tunnel info",
    "sudo tee", "sudo cp", "sudo mv", "sudo mkdir", "sudo rm", "sudo ln -s", "sudo unlink",
    "sudo chmod", "sudo chown", "sudo systemctl", "curl", "wget", "nft", "sudo nft",
    "sed", "sudo sed", "python3", "sudo python3", "systemctl", "journalctl", "wc", "jq",
    "lsof", "stat", "namei", "realpath", "diff", "cmp", "apt", "tar", "gzip", "unzip", "zip",
    "git", "set", "if", "cd", "which", "ssh", "sudo ssh", "bash -lc",
    "pensa-safe-edit", "sudo pensa-safe-edit",
]

PLAYBOOKS = {
    "nginx_debug": {
        "description": "Diagnostico basico de nginx",
        "steps": ["systemctl status nginx", "nginx -t", "tail /var/log/nginx/error.log"],
    },
    "docker_debug": {
        "description": "Diagnostico de Docker",
        "steps": ["systemctl status docker", "docker ps", "docker images"],
    },
    "systemd_debug": {
        "description": "Chequeo de servicios systemd",
        "steps": ["systemctl status nginx", "systemctl status docker"],
    },
    "network_debug": {
        "description": "Diagnostico de red",
        "steps": ["ip a", "ip route", "ss -tuln"],
    },
}

SERVICE_ACTIONS = {
    "sentinelx": {
        "unit": "sentinelx.service",
        "manager": "systemd",
        "actions": ["status", "start", "stop", "restart"],
        "description": "SentinelX agent service",
        "checks": {"status": "systemctl status sentinelx.service"},
        "risk": "medium",
        "action_commands": {
            "status":  "systemctl status sentinelx.service",
            "start":   "sudo systemctl start sentinelx.service",
            "stop":    "sudo systemctl stop sentinelx.service",
            "restart": "sudo systemctl restart sentinelx.service",
        },
    },
    "nginx": {
        "unit": "nginx",
        "manager": "systemd",
        "actions": ["status", "start", "stop", "restart", "reload", "validate"],
        "description": "Nginx reverse proxy",
        "checks": {"status": "systemctl status nginx", "validate": "sudo nginx -t"},
        "risk": "low",
        "action_commands": {
            "status":   "systemctl status nginx",
            "start":    "sudo systemctl start nginx",
            "stop":     "sudo systemctl stop nginx",
            "restart":  "sudo systemctl restart nginx",
            "reload":   "sudo systemctl reload nginx",
            "validate": "sudo nginx -t",
        },
    },
    "docker": {
        "unit": "docker",
        "manager": "systemd",
        "actions": ["status", "start", "stop", "restart"],
        "description": "Docker daemon",
        "checks": {"status": "systemctl status docker", "runtime": "docker ps"},
        "risk": "high",
        "action_commands": {
            "status":  "systemctl status docker",
            "start":   "sudo systemctl start docker",
            "stop":    "sudo systemctl stop docker",
            "restart": "sudo systemctl restart docker",
        },
    },
}


class EditRequest(BaseModel):
    path: str
    sudo: bool = False
    mode: Literal["replace", "regex", "replace-block", "append", "prepend", "write"]
    old: Optional[str] = None
    new_text: Optional[str] = None
    pattern: Optional[str] = None
    start_marker: Optional[str] = None
    end_marker: Optional[str] = None
    count: int = 0
    multiline: bool = False
    dotall: bool = False
    interpret_escapes: bool = False
    backup_dir: Optional[str] = None
    validator: Optional[str] = None
    validator_preset: Optional[Literal["nginx", "json", "python", "sh", "yaml", "systemd"]] = None
    diff: bool = False
    dry_run: bool = False
    allow_no_change: bool = False
    create: bool = False

    @model_validator(mode="after")
    def validate_request(self):
        if not self.path or not self.path.strip():
            raise ValueError("path es obligatorio")
        if self.validator and self.validator_preset:
            raise ValueError("No puedes usar validator y validator_preset juntos")
        if self.count < 0:
            raise ValueError("count no puede ser negativo")
        if self.mode == "replace":
            if self.old is None: raise ValueError("En mode=replace debes indicar old")
            if self.new_text is None: raise ValueError("En mode=replace debes indicar new_text")
        elif self.mode == "regex":
            if not self.pattern: raise ValueError("En mode=regex debes indicar pattern")
            if self.new_text is None: raise ValueError("En mode=regex debes indicar new_text")
        elif self.mode == "replace-block":
            if not self.start_marker or not self.end_marker:
                raise ValueError("En mode=replace-block debes indicar start_marker y end_marker")
            if self.new_text is None: raise ValueError("En mode=replace-block debes indicar new_text")
        elif self.mode in ("append", "prepend", "write"):
            if self.new_text is None: raise ValueError(f"En mode={self.mode} debes indicar new_text")
        return self


class EditCompleteRequest(BaseModel):
    upload_id: str
    path: str
    sudo: bool = False
    mode: Literal["replace", "regex", "replace-block", "append", "prepend", "write"]
    pattern: Optional[str] = None
    start_marker: Optional[str] = None
    end_marker: Optional[str] = None
    count: int = 0
    multiline: bool = False
    dotall: bool = False
    interpret_escapes: bool = False
    backup_dir: Optional[str] = None
    validator: Optional[str] = None
    validator_preset: Optional[Literal["nginx", "json", "python", "sh", "yaml", "systemd"]] = None
    diff: bool = False
    dry_run: bool = False
    allow_no_change: bool = False
    create: bool = False

    @model_validator(mode="after")
    def validate_request(self):
        if not self.upload_id: raise ValueError("upload_id es obligatorio")
        if not self.path or not self.path.strip(): raise ValueError("path es obligatorio")
        if self.validator and self.validator_preset: raise ValueError("No puedes usar validator y validator_preset juntos")
        if self.count < 0: raise ValueError("count no puede ser negativo")
        if self.mode == "regex" and not self.pattern: raise ValueError("En mode=regex debes indicar pattern")
        if self.mode == "replace-block" and (not self.start_marker or not self.end_marker):
            raise ValueError("En mode=replace-block debes indicar start_marker y end_marker")
        return self


class ScriptRunRequest(BaseModel):
    interpreter: Literal["bash", "python3"]
    content: str
    args: Optional[list[str]] = None
    cwd: Optional[str] = None
    timeout: int = 60
    sudo: bool = False
    cleanup: bool = True
    filename: Optional[str] = None
    env: Optional[dict[str, str]] = None

    @model_validator(mode="after")
    def validate_request(self):
        if not self.content or not self.content.strip():
            raise ValueError("content es obligatorio")
        if self.timeout < 1 or self.timeout > 300:
            raise ValueError("timeout debe estar entre 1 y 300 segundos")
        return self


def execute_command(cmd: str):
    start = time.time()
    try:
        print(f"[SentinelX:nsenter] {cmd}", flush=True)
        result = subprocess.run(
            _prefix(["bash", "-lc", cmd]),
            text=True, capture_output=True, timeout=60,
        )
        duration = round(time.time() - start, 2)
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        output = f"{stdout}\n{stderr}".strip() if (stdout or stderr) else "Sin salida"
        return {"output": output, "duration": duration, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"output": "Timeout", "duration": round(time.time() - start, 2), "returncode": -1}
    except Exception as e:
        return {"output": f"Error: {e}", "duration": round(time.time() - start, 2), "returncode": -1}


def run_process(args: list[str], timeout: int = 60, env=None, cwd=None):
    start = time.time()
    try:
        result = subprocess.run(
            _prefix(args),
            text=True, capture_output=True, timeout=timeout, env=env, cwd=cwd,
        )
        duration = round(time.time() - start, 2)
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        output = f"{stdout}\n{stderr}".strip() if (stdout or stderr) else "Sin salida"
        return {"output": output, "duration": duration, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"output": "Timeout", "duration": round(time.time() - start, 2), "returncode": -1}
    except Exception as e:
        return {"output": f"Error: {e}", "duration": round(time.time() - start, 2), "returncode": -1}


def get_command_help(cmd: str):
    try:
        result = subprocess.run(
            _prefix(["bash", "-lc", cmd]),
            text=True, capture_output=True, timeout=10,
        )
        return (result.stdout or result.stderr or "No help available").strip()
    except Exception as e:
        return f"Error getting help: {e}"


def execute_service_action(service: str, action: str):
    meta = SERVICE_ACTIONS.get(service)
    if not meta:
        return {"error": f"Service not allowed: {service}", "status": "blocked"}
    action = (action or "").strip()
    if not action or action not in meta.get("actions", []):
        return {"error": f"Action not allowed: {action}", "status": "blocked", "allowed_actions": meta.get("actions", [])}
    cmd = meta.get("action_commands", {}).get(action)
    if not cmd:
        return {"error": f"No command mapped for {service}:{action}", "status": "blocked"}
    result = execute_command(cmd)
    result["ok"] = result.get("returncode", 1) == 0
    result["service"] = service
    result["action"] = action
    result["command"] = cmd
    return result


def _ensure_upload_dirs():
    UPLOAD_BASE_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)


def _require_agent_token(authorization: str):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.split(" ")[1]
    if token != AGENT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


def _safe_upload_path(target_path: str) -> Path:
    if not target_path or not target_path.strip():
        raise HTTPException(status_code=400, detail="Missing target_path")
    raw = target_path.strip().lstrip("/")
    candidate = (UPLOAD_BASE_DIR / raw).resolve()
    base = UPLOAD_BASE_DIR.resolve()
    if candidate != base and base not in candidate.parents:
        raise HTTPException(status_code=400, detail="target_path escapes upload base dir")
    return candidate


def _write_upload_file(src: UploadFile, dest: Path):
    hasher = hashlib.sha256()
    size = 0
    with dest.open("wb") as f:
        while True:
            chunk = src.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="File too large")
            hasher.update(chunk)
            f.write(chunk)
    return size, hasher.hexdigest()


def _build_edit_command(workdir, path, sudo, mode, old=None, new_text=None,
    pattern=None, start_marker=None, end_marker=None, count=0,
    multiline=False, dotall=False, interpret_escapes=False, backup_dir=None,
    validator=None, validator_preset=None, diff=False, dry_run=False,
    allow_no_change=False, create=False, old_file_path=None, new_file_path=None):
    args = []
    if sudo: args.append("sudo")
    args.extend([PENSA_SAFE_EDIT_BIN, path, "--mode", mode])
    if old_file_path is not None: args.extend(["--old-file", str(old_file_path)])
    elif old is not None:
        p = workdir / "old.txt"; p.write_text(old, encoding="utf-8"); args.extend(["--old-file", str(p)])
    if new_file_path is not None: args.extend(["--new-file", str(new_file_path)])
    elif new_text is not None:
        p = workdir / "new.txt"; p.write_text(new_text, encoding="utf-8"); args.extend(["--new-file", str(p)])
    if pattern: args.extend(["--pattern", pattern])
    if start_marker: args.extend(["--start-marker", start_marker])
    if end_marker: args.extend(["--end-marker", end_marker])
    if count: args.extend(["--count", str(count)])
    if multiline: args.append("--multiline")
    if dotall: args.append("--dotall")
    if interpret_escapes: args.append("--interpret-escapes")
    if backup_dir: args.extend(["--backup-dir", backup_dir])
    if validator: args.extend(["--validator", validator])
    if validator_preset: args.extend(["--validator-preset", validator_preset])
    if diff: args.append("--diff")
    if dry_run: args.append("--dry-run")
    if allow_no_change: args.append("--allow-no-change")
    if create: args.append("--create")
    return args


def _edit_upload_dir(upload_id):
    _ensure_upload_dirs()
    d = UPLOAD_TMP_DIR / f"edit_{upload_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cleanup_edit_upload(upload_id):
    try: shutil.rmtree(UPLOAD_TMP_DIR / f"edit_{upload_id}", ignore_errors=True)
    except Exception: pass


@app.get("/capabilities")
async def get_capabilities(authorization: str = Header(None)):
    _require_agent_token(authorization)
    pensa_safe_edit_help = get_command_help(PENSA_SAFE_EDIT_BIN)
    return {
        "agent": "sentinelx", "version": "0.3.5-docker", "mode": f"docker-{EXEC_MODE}",
        "exec_mode": EXEC_MODE,
        "allowed_commands": ALLOWED_COMMANDS,
        "service_actions": {n: {k: v for k, v in m.items() if k != "action_commands"} for n, m in SERVICE_ACTIONS.items()},
        "categories": {
            "read": ["cat","head","tail","grep","find","journalctl","jq","stat","realpath"],
            "write": ["echo","printf","tee","sed","touch","chmod","chown"],
            "filesystem": ["ls","cp","mv","rm","mkdir","ln -s","unlink","tree"],
            "edit": ["/edit","/edit/upload/init","/edit/upload/file","/edit/upload/complete"],
            "script": ["/script/run"],
            "services": ["systemctl","docker","nginx -t","cloudflared tunnel list"],
            "network": ["ip a","ip route","ss -tuln","netstat -tuln","ping","curl","wget"],
            "tooling": ["python3","git","bash -lc"],
            "upload": ["/upload","/upload/init","/upload/chunk","/upload/complete"],
            "privileged": [c for c in ALLOWED_COMMANDS if c.startswith("sudo")],
        },
        "upload_capabilities": {
            "base_dir": str(UPLOAD_BASE_DIR), "temp_dir": str(UPLOAD_TMP_DIR),
            "max_upload_bytes": MAX_UPLOAD_BYTES,
        },
        "locations": PATH_INDEX,
        "playbooks": PLAYBOOKS,
        "help": {
            "pensa-safe-edit": pensa_safe_edit_help,
            "exec_mode": (
                f"Modo activo: {EXEC_MODE}. "
                "host → comandos ejecutados en el host real via nsenter (requiere pid=host + privileged=true). "
                "container → comandos ejecutados dentro del container (aislado, sin privilegios especiales)."
            ),
        },
    }


@app.post("/exec")
async def exec_command(request: Request, authorization: str = Header(None)):
    _require_agent_token(authorization)
    data = await request.json()
    cmd = data.get("cmd")
    if not cmd: raise HTTPException(status_code=400, detail="Missing command")
    allowed_match = any(cmd.startswith(a) for a in ALLOWED_COMMANDS)
    if not allowed_match:
        context.update(cmd, "blocked", status="blocked")
        log_exec(cmd, "blocked", allowed=False)
        return {"error": f"Command not allowed: {cmd}"}
    result = execute_command(cmd)
    log_exec(cmd, result["output"])
    context.update(cmd, result["output"], status="ok")
    log_command(cmd, result["output"], source="sentinelx-docker")
    return result


@app.post("/edit")
async def edit_file(request: EditRequest, authorization: str = Header(None)):
    _require_agent_token(authorization)
    _ensure_upload_dirs()
    workdir = UPLOAD_TMP_DIR / f"edit_job_{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        args = _build_edit_command(workdir, request.path, request.sudo, request.mode,
            request.old, request.new_text, request.pattern, request.start_marker,
            request.end_marker, request.count, request.multiline, request.dotall,
            request.interpret_escapes, request.backup_dir, request.validator,
            request.validator_preset, request.diff, request.dry_run,
            request.allow_no_change, request.create)
        result = run_process(args)
        ok = result.get("returncode", 1) == 0
        key = f"edit:{request.path}"
        log_exec(f"sentinelx-safe-edit {request.path} --mode {request.mode}", result.get("output",""), allowed=True)
        context.update(key, result.get("output",""), status="ok" if ok else "error")
        return {"ok": ok, "path": request.path, "mode": request.mode, "command": args, **result}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.post("/script/run")
async def script_run(request: ScriptRunRequest, authorization: str = Header(None)):
    _require_agent_token(authorization)
    _ensure_upload_dirs()
    script_id = uuid.uuid4().hex
    workdir = UPLOAD_TMP_DIR / f"script_job_{script_id}"
    workdir.mkdir(parents=True, exist_ok=True)
    ext = "sh" if request.interpreter == "bash" else "py"
    safe_name = Path(request.filename).name if request.filename else f"script.{ext}"
    script_path = workdir / safe_name
    try:
        script_path.write_text(request.content, encoding="utf-8")
        os.chmod(script_path, 0o700)
        args = []
        if request.sudo: args.append("sudo")
        args.extend(["bash" if request.interpreter == "bash" else "python3", str(script_path)])
        if request.args: args.extend(request.args)
        env = os.environ.copy()
        if request.env: env.update(request.env)
        result = run_process(args, timeout=request.timeout, env=env, cwd=request.cwd)
        ok = result.get("returncode", 1) == 0
        key = f"script:{safe_name}"
        log_exec(f"{request.interpreter} {safe_name}", result.get("output",""), allowed=True)
        context.update(key, result.get("output",""), status="ok" if ok else "error")
        resp = {"ok": ok, "interpreter": request.interpreter, "command": args, **result}
        if not request.cleanup: resp["script_path"] = str(script_path)
        return resp
    finally:
        if request.cleanup: shutil.rmtree(workdir, ignore_errors=True)


@app.post("/restart")
async def restart_service(request: Request, authorization: str = Header(None)):
    _require_agent_token(authorization)
    data = await request.json()
    service = data.get("service")
    if not service: raise HTTPException(status_code=400, detail="Missing service")
    meta = SERVICE_ACTIONS.get(service)
    if not meta: return {"error": f"Service not allowed: {service}"}
    cmd = meta.get("action_commands", {}).get("restart")
    if not cmd: return {"error": f"Restart not configured for service: {service}"}
    subprocess.Popen(_prefix(["bash", "-lc", cmd]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    message = f"{service} restart triggered"
    log_exec(service, message)
    context.update(service, message, status="ok")
    return {"ok": True, "service": service, "message": message}


@app.post("/service")
async def service_action(request: Request, authorization: str = Header(None)):
    _require_agent_token(authorization)
    data = await request.json()
    service = data.get("service")
    action = data.get("action")
    if not service: raise HTTPException(status_code=400, detail="Missing service")
    if not action: raise HTTPException(status_code=400, detail="Missing action")
    result = execute_service_action(service, action)
    key = f"{service}:{action}"
    if result.get("status") == "blocked":
        log_exec(key, "blocked", allowed=False)
        return result
    log_exec(key, result.get("output",""))
    context.update(key, result.get("output",""), status="ok")
    return result


@app.post("/upload")
async def upload_file_endpoint(
    authorization: str = Header(None),
    file: UploadFile = File(...),
    target_path: str = Form(...),
    overwrite: bool = Form(False),
):
    _require_agent_token(authorization)
    _ensure_upload_dirs()
    dest = _safe_upload_path(target_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not overwrite:
        raise HTTPException(status_code=409, detail="File already exists")
    tmp = UPLOAD_TMP_DIR / (str(uuid.uuid4()) + ".upload")
    try:
        size, sha256 = _write_upload_file(file, tmp)
        tmp.replace(dest)
    finally:
        if tmp.exists(): tmp.unlink(missing_ok=True)
    return {"ok": True, "mode": "single", "target_path": str(dest), "size": size, "sha256": sha256}


@app.post("/upload/init")
async def upload_init_endpoint(request: Request, authorization: str = Header(None)):
    _require_agent_token(authorization)
    _ensure_upload_dirs()
    data = await request.json()
    dest = _safe_upload_path(data.get("target_path"))
    overwrite = bool(data.get("overwrite", False))
    if dest.exists() and not overwrite:
        raise HTTPException(status_code=409, detail="File already exists")
    upload_id = uuid.uuid4().hex
    upload_dir = UPLOAD_TMP_DIR / upload_id
    (upload_dir / "parts").mkdir(parents=True, exist_ok=True)
    (upload_dir / "meta.json").write_text(json.dumps({
        "upload_id": upload_id, "target_path": str(dest),
        "overwrite": overwrite, "total_size": int(data.get("total_size", 0) or 0),
    }))
    return {"ok": True, "mode": "chunked", "upload_id": upload_id, "target_path": str(dest)}


@app.post("/upload/chunk")
async def upload_chunk_endpoint(
    authorization: str = Header(None),
    upload_id: str = Form(...),
    index: int = Form(...),
    chunk: UploadFile = File(...),
):
    _require_agent_token(authorization)
    upload_dir = UPLOAD_TMP_DIR / upload_id
    if not (upload_dir / "meta.json").exists():
        raise HTTPException(status_code=404, detail="upload_id not found")
    part_path = upload_dir / "parts" / f"{index:08d}.part"
    size = 0
    with part_path.open("wb") as f:
        while True:
            data = chunk.file.read(1024 * 1024)
            if not data: break
            size += len(data); f.write(data)
    return {"ok": True, "upload_id": upload_id, "index": index, "chunk_size": size}


@app.post("/upload/complete")
async def upload_complete_endpoint(request: Request, authorization: str = Header(None)):
    _require_agent_token(authorization)
    data = await request.json()
    upload_id = data.get("upload_id")
    if not upload_id: raise HTTPException(status_code=400, detail="Missing upload_id")
    upload_dir = UPLOAD_TMP_DIR / upload_id
    meta = json.loads((upload_dir / "meta.json").read_text())
    dest = Path(meta["target_path"]).resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = upload_dir / "assembled.bin"
    parts = sorted((upload_dir / "parts").glob("*.part"))
    if not parts: raise HTTPException(status_code=400, detail="No chunks uploaded")
    hasher = hashlib.sha256(); total = 0
    with tmp.open("wb") as out:
        for part in parts:
            with part.open("rb") as pf:
                while True:
                    chunk = pf.read(1024 * 1024)
                    if not chunk: break
                    total += len(chunk); hasher.update(chunk); out.write(chunk)
    sha256 = hasher.hexdigest()
    sha256_expected = data.get("sha256")
    if sha256_expected and sha256_expected != sha256:
        raise HTTPException(status_code=400, detail="sha256 mismatch")
    if dest.exists() and not meta.get("overwrite", False):
        raise HTTPException(status_code=409, detail="File already exists")
    tmp.replace(dest)
    shutil.rmtree(upload_dir, ignore_errors=True)
    return {"ok": True, "upload_id": upload_id, "target_path": str(dest), "size": total, "sha256": sha256}


@app.post("/edit/upload/init")
async def edit_upload_init(request: Request, authorization: str = Header(None)):
    _require_agent_token(authorization)
    _ensure_upload_dirs()
    upload_id = uuid.uuid4().hex
    upload_dir = _edit_upload_dir(upload_id)
    (upload_dir / "meta.json").write_text(json.dumps({"upload_id": upload_id, "created_at": int(time.time())}), encoding="utf-8")
    return {"ok": True, "upload_id": upload_id, "temp_dir": str(upload_dir)}


@app.post("/edit/upload/file")
async def edit_upload_file(
    authorization: str = Header(None),
    upload_id: str = Form(...),
    role: str = Form(...),
    file: UploadFile = File(...),
):
    _require_agent_token(authorization)
    if role not in ("new", "old"): raise HTTPException(status_code=400, detail="role must be new or old")
    upload_dir = _edit_upload_dir(upload_id)
    if not (upload_dir / "meta.json").exists(): raise HTTPException(status_code=404, detail="upload_id not found")
    dest = upload_dir / f"{role}.bin"
    size, sha256 = _write_upload_file(file, dest)
    return {"ok": True, "upload_id": upload_id, "role": role, "size": size, "sha256": sha256}


@app.post("/edit/upload/complete")
async def edit_upload_complete(request: EditCompleteRequest, authorization: str = Header(None)):
    _require_agent_token(authorization)
    _ensure_upload_dirs()
    upload_dir = _edit_upload_dir(request.upload_id)
    if not (upload_dir / "meta.json").exists(): raise HTTPException(status_code=404, detail="upload_id not found")
    new_file = upload_dir / "new.bin"
    old_file = upload_dir / "old.bin"
    if not new_file.exists(): raise HTTPException(status_code=400, detail="Missing role=new file")
    if request.mode == "replace" and not old_file.exists(): raise HTTPException(status_code=400, detail="Missing role=old file")
    workdir = UPLOAD_TMP_DIR / f"edit_job_{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        args = _build_edit_command(workdir, request.path, request.sudo, request.mode,
            pattern=request.pattern, start_marker=request.start_marker, end_marker=request.end_marker,
            count=request.count, multiline=request.multiline, dotall=request.dotall,
            interpret_escapes=request.interpret_escapes, backup_dir=request.backup_dir,
            validator=request.validator, validator_preset=request.validator_preset,
            diff=request.diff, dry_run=request.dry_run, allow_no_change=request.allow_no_change,
            create=request.create, old_file_path=old_file if old_file.exists() else None, new_file_path=new_file)
        result = run_process(args)
        ok = result.get("returncode", 1) == 0
        log_exec(f"sentinelx-safe-edit {request.path} --mode {request.mode}", result.get("output",""), allowed=True)
        context.update(f"edit:{request.path}", result.get("output",""), status="ok" if ok else "error")
        return {"ok": ok, "path": request.path, "mode": request.mode, "upload_id": request.upload_id, "command": args, **result}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _cleanup_edit_upload(request.upload_id)


@app.get("/state")
async def get_state(authorization: str = Header(None)):
    _require_agent_token(authorization)
    return context.get_state()

import subprocess
import yaml
from fastapi import FastAPI, Request, HTTPException, Query, Body
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import psutil
import time
import libvirt
import os
import shutil
import atexit

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_config(path="config.yaml"):
    with open(path, 'r') as f:
        return yaml.safe_load(f)

config = load_config()

tunnel_process = None

def start_main_tunnel():
    global tunnel_process
    jump = config["jump_host"]
    vm_host = config["vms"][0]
    LOCAL_PORT = config.get("local_tunnel_port", 14444)
    REMOTE_LIBVIRT_PORT = vm_host.get("port", 16509)
    
    for proc in psutil.process_iter(attrs=['cmdline']):
        if proc.info['cmdline'] is None:
            continue
        cmd = ' '.join(proc.info['cmdline'])
        if f"-L {LOCAL_PORT}:{vm_host['host']}:{REMOTE_LIBVIRT_PORT}" in cmd and jump['host'] in cmd:
            return
    
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{LOCAL_PORT}:{vm_host['host']}:{REMOTE_LIBVIRT_PORT}",
        f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
    ]
    tunnel_process = subprocess.Popen(cmd)
    time.sleep(2)

def stop_main_tunnel():
    global tunnel_process
    if tunnel_process:
        tunnel_process.terminate()
        try:
            tunnel_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel_process.kill()

@app.on_event("startup")
async def startup_event():
    start_main_tunnel()

@app.on_event("shutdown")
async def shutdown_event():
    stop_main_tunnel()

atexit.register(stop_main_tunnel)

@app.get("/vms/list")
def list_vm_hosts():
    return {"vms": [v["name"] for v in config["vms"]]}

@app.get("/vms/available")
def get_vms(vm: str = Query(..., description="VM name")):
    jump = config["jump_host"]
    vm_hosts = {v["name"]: v for v in config["vms"]}
    if vm not in vm_hosts:
        raise HTTPException(status_code=404, detail="VM host not found")
    vm_host = vm_hosts[vm]
    LOCAL_PORT = config.get("local_tunnel_port", 14444)
    REMOTE_LIBVIRT_PORT = vm_host.get("port", 16509)

    def tunnel_exists():
        for proc in psutil.process_iter(attrs=['cmdline']):
            if proc.info['cmdline'] is None:
                continue
            cmd = ' '.join(proc.info['cmdline'])
            if f"-L {LOCAL_PORT}:{vm_host['host']}:{REMOTE_LIBVIRT_PORT}" in cmd and jump['host'] in cmd and jump['user'] in cmd:
                return True
        return False

    if not tunnel_exists():
        if shutil.which("sshpass") and jump.get("password"):
            cmd = [
                "sshpass", "-p", jump["password"],
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-N", "-L", f"{LOCAL_PORT}:{vm_host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
            ]
        else:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-L", f"{LOCAL_PORT}:{vm_host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port']), "-N"
            ]
        subprocess.Popen(cmd)
        time.sleep(2)

    try:
        conn = libvirt.open(f"qemu+tcp://127.0.0.1:{LOCAL_PORT}/system")
        if conn is None:
            raise HTTPException(status_code=500, detail="Не удалось подключиться к libvirt через туннель.")
        all_domains = conn.listAllDomains(0)
        state_map = {
            libvirt.VIR_DOMAIN_NOSTATE: "nostate",
            libvirt.VIR_DOMAIN_RUNNING: "running",
            libvirt.VIR_DOMAIN_BLOCKED: "blocked",
            libvirt.VIR_DOMAIN_PAUSED: "paused",
            libvirt.VIR_DOMAIN_SHUTDOWN: "shutdown",
            libvirt.VIR_DOMAIN_SHUTOFF: "shutoff",
            libvirt.VIR_DOMAIN_CRASHED: "crashed",
            libvirt.VIR_DOMAIN_PMSUSPENDED: "pmsuspended"
        }
        vms = []
        for dom in all_domains:
            state_code, _ = dom.state()
            state = state_map.get(state_code, "unknown")
            vms.append({
                "name": dom.name(),
                "id": dom.ID(),
                "state": state
            })
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

    # Кэшируем в YAML
    cache_dir = "vms_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_filename = os.path.join(cache_dir, f"cached_vms_{vm}.yaml")
    with open(cache_filename, 'w', encoding='utf-8') as cache_file:
        yaml.dump({"vms": vms}, cache_file, allow_unicode=True)
    return {"vms": vms}

@app.get("/vms/cached")
def get_cached_vms(vm: str):
    cache_dir = "vms_cache"
    cache_filename = os.path.join(cache_dir, f"cached_vms_{vm}.yaml")
    if not os.path.exists(cache_filename):
        raise HTTPException(status_code=404, detail="VMs cache not found for this server.")
    with open(cache_filename, 'r', encoding='utf-8') as cache_file:
        data = yaml.safe_load(cache_file)
    return data

@app.post("/vms/start")
def start_vm(vm_host: str = Body(...), vm_name: str = Body(...)):
    jump = config["jump_host"]
    vm_hosts = {v["name"]: v for v in config["vms"]}
    if vm_host not in vm_hosts:
        raise HTTPException(status_code=404, detail="VM host not found")
    host = vm_hosts[vm_host]
    LOCAL_PORT = config.get("local_tunnel_port", 14444)
    REMOTE_LIBVIRT_PORT = host.get("port", 16509)

    def tunnel_exists():
        for proc in psutil.process_iter(attrs=['cmdline']):
            if proc.info['cmdline'] is None:
                continue
            cmd = ' '.join(proc.info['cmdline'])
            if f"-L {LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}" in cmd and jump['host'] in cmd and jump['user'] in cmd:
                return True
        return False

    if not tunnel_exists():
        if shutil.which("sshpass") and jump.get("password"):
            cmd = [
                "sshpass", "-p", jump["password"],
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-N", "-L", f"{LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
            ]
        else:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-L", f"{LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port']), "-N"
            ]
        subprocess.Popen(cmd)
        time.sleep(2)

    try:
        conn = libvirt.open(f"qemu+tcp://127.0.0.1:{LOCAL_PORT}/system")
        if conn is None:
            raise HTTPException(status_code=500, detail="Не удалось подключиться к libvirt через туннель.")
        domain = conn.lookupByName(vm_name)
        if domain is None:
            raise HTTPException(status_code=404, detail="ВМ не найдена.")
        domain.create()
        status = "started"
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

    log = {"vm": vm_name, "action": "start", "status": status, "host": vm_host}
    return log

@app.post("/vms/stop")
def stop_vm(vm_host: str = Body(...), vm_name: str = Body(...)):
    jump = config["jump_host"]
    vm_hosts = {v["name"]: v for v in config["vms"]}
    if vm_host not in vm_hosts:
        raise HTTPException(status_code=404, detail="VM host not found")
    host = vm_hosts[vm_host]
    LOCAL_PORT = config.get("local_tunnel_port", 14444)
    REMOTE_LIBVIRT_PORT = host.get("port", 16509)

    def tunnel_exists():
        for proc in psutil.process_iter(attrs=['cmdline']):
            if proc.info['cmdline'] is None:
                continue
            cmd = ' '.join(proc.info['cmdline'])
            if f"-L {LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}" in cmd and jump['host'] in cmd and jump['user'] in cmd:
                return True
        return False

    if not tunnel_exists():
        if shutil.which("sshpass") and jump.get("password"):
            cmd = [
                "sshpass", "-p", jump["password"],
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-N", "-L", f"{LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
            ]
        else:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-L", f"{LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port']), "-N"
            ]
        subprocess.Popen(cmd)
        time.sleep(2)

    try:
        conn = libvirt.open(f"qemu+tcp://127.0.0.1:{LOCAL_PORT}/system")
        if conn is None:
            raise HTTPException(status_code=500, detail="Не удалось подключиться к libvirt через туннель.")
        domain = conn.lookupByName(vm_name)
        if domain is None:
            raise HTTPException(status_code=404, detail="ВМ не найдена.")
        domain.shutdown()
        status = "stopped"
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

    log = {"vm": vm_name, "action": "stop", "status": status, "host": vm_host}
    return log

@app.get("/vms/running")
def get_running_vms(vm: str = Query(..., description="VM name")):
    jump = config["jump_host"]
    vm_hosts = {v["name"]: v for v in config["vms"]}
    if vm not in vm_hosts:
        raise HTTPException(status_code=404, detail="VM host not found")
    vm_host = vm_hosts[vm]
    LOCAL_PORT = config.get("local_tunnel_port", 14444)
    REMOTE_LIBVIRT_PORT = vm_host.get("port", 16509)

    try:
        conn = libvirt.open(f"qemu+tcp://127.0.0.1:{LOCAL_PORT}/system")
        ids = conn.listDomainsID()
        running = []
        for id in ids:
            dom = conn.lookupByID(id)
            running.append(dom.name())
        conn.close()
        return {"running_vms": running}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/vms/force_stop")
def force_stop_vm(vm_host: str = Body(...), vm_name: str = Body(...)):
    import time as time_mod
    jump = config["jump_host"]
    vm_hosts = {v["name"]: v for v in config["vms"]}
    if vm_host not in vm_hosts:
        raise HTTPException(status_code=404, detail="VM host not found")
    host = vm_hosts[vm_host]
    LOCAL_PORT = config.get("local_tunnel_port", 14444)
    REMOTE_LIBVIRT_PORT = host.get("port", 16509)

    def tunnel_exists():
        for proc in psutil.process_iter(attrs=['cmdline']):
            if proc.info['cmdline'] is None:
                continue
            cmd = ' '.join(proc.info['cmdline'])
            if f"-L {LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}" in cmd and jump['host'] in cmd and jump['user'] in cmd:
                return True
        return False

    if not tunnel_exists():
        if shutil.which("sshpass") and jump.get("password"):
            cmd = [
                "sshpass", "-p", jump["password"],
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-N", "-L", f"{LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
            ]
        else:
            cmd = [
                "ssh",
                "-o", "StrictHostKeyChecking=no",
                "-L", f"{LOCAL_PORT}:{host['host']}:{REMOTE_LIBVIRT_PORT}",
                f"{jump['user']}@{jump['host']}", "-p", str(jump['port']), "-N"
            ]
        subprocess.Popen(cmd)
        time_mod.sleep(2)

    try:
        conn = libvirt.open(f"qemu+tcp://127.0.0.1:{LOCAL_PORT}/system")
        if conn is None:
            raise HTTPException(status_code=500, detail="Не удалось подключиться к libvirt через туннель.")
        domain = conn.lookupByName(vm_name)
        if domain is None:
            raise HTTPException(status_code=404, detail="ВМ не найдена.")
        domain.destroy()
        conn.close()
        return {"vm": vm_name, "action": "force_stop", "status": "forced_off", "host": vm_host}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

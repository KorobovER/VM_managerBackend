import subprocess
import yaml
import json
from fastapi import FastAPI, HTTPException, Query, Body, Depends, status
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
import psutil
import time
import libvirt
import os
import shutil
import atexit
import socket
import threading
import select
from sqlalchemy.orm import Session
from datetime import timedelta
from typing import List

from db.database import engine, Base, get_db, SessionLocal
from db.models import User, Server, VM, UserVM, JumpHost, RoleEnum
from db.schemas import (
    UserCreate, UserResponse, Token, ServerCreate, ServerResponse, ServerUpdate,
    VMCreate, VMResponse, JumpHostCreate, JumpHostResponse,
    VMHostParams, VMActionRequest
)
from auth import (
    verify_password, get_password_hash, create_access_token,
    get_current_user, get_current_admin_user, ACCESS_TOKEN_EXPIRE_MINUTES
)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="CloudGaming Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def load_config(path="config.json"):
    with open(path, 'r') as f:
        return json.load(f)

config = load_config()

tunnel_process = None
server_tunnels = {}
video_tunnels = {}
tcp_proxies = {}
proxy_threads = {}

def get_available_local_port_from(start_port=14440):
    port = start_port
    while port < 15000:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('127.0.0.1', port))
            sock.close()
            return port
        except OSError:
            port += 1
    raise Exception("No available ports found")

def tunnel_exists(local_port, remote_host, remote_port, jump_host_info):
    for proc in psutil.process_iter(attrs=['cmdline']):
        if proc.info['cmdline'] is None:
            continue
        cmd = ' '.join(proc.info['cmdline'])
        if (f"-L {local_port}:{remote_host}:{remote_port}" in cmd and 
            jump_host_info['host'] in cmd):
            return True
    return False

def start_server_tunnel(server_id, server_host, server_port, jump_host_info):
    global server_tunnels
    
    tunnel_key = f"server_{server_id}"
    
    if tunnel_key in server_tunnels:
        proc, local_port = server_tunnels[tunnel_key]
        if proc.poll() is None:
            return local_port
        else:
            del server_tunnels[tunnel_key]
    
    if tunnel_exists(14440 + server_id, server_host, server_port, jump_host_info):
        local_port = 14440 + server_id
        server_tunnels[tunnel_key] = (None, local_port)
        return local_port
    
    local_port = 14440 + server_id
    
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{local_port}:{server_host}:{server_port}",
        f"{jump_host_info['user']}@{jump_host_info['host']}", "-p", str(jump_host_info['port'])
    ]
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise Exception(f"SSH tunnel for server {server_id} failed: {stderr}")
    
    server_tunnels[tunnel_key] = (proc, local_port)
    return local_port

def start_all_server_tunnels():
    db = SessionLocal()
    servers = db.query(Server).filter(Server.deleted == 0, Server.jump_host_id != None).all()
    
    for server in servers:
        try:
            if server.jump_host:
                jump_info = {
                    'host': server.jump_host.host,
                    'port': server.jump_host.port,
                    'user': server.jump_host.user
                }
                local_port = start_server_tunnel(server.id, server.host, server.port, jump_info)
                server.local_tunnel_port = local_port
                db.add(server)
        except Exception as e:
            print(f"Failed to start tunnel for server {server.name}: {e}")
    
    db.commit()
    db.close()

def stop_all_server_tunnels():
    global server_tunnels
    for tunnel_key, (proc, _) in server_tunnels.items():
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    server_tunnels.clear()

def start_main_tunnel():
    start_all_server_tunnels()

def stop_main_tunnel():
    stop_all_server_tunnels()

def get_libvirt_connection(server):
    """
    Получить подключение к libvirt для сервера.
    Если jump_host_id указан - использует туннель, иначе прямое подключение.
    """
    if server.jump_host_id is not None and server.jump_host:
        local_port = 14440 + server.id
        conn_uri = f"qemu+tcp://127.0.0.1:{local_port}/system"
    else:
        conn_uri = f"qemu+tcp://{server.host}:{server.port}/system"
    
    try:
        conn = libvirt.open(conn_uri)
        if conn is None:
            raise Exception(f"Failed to connect to {conn_uri}")
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt connection error: {e}")

def get_available_local_port(start_port=9001):
    port = start_port
    while port < 10000:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(('127.0.0.1', port))
            sock.close()
            return port
        except OSError:
            port += 1
    raise Exception("No available ports found")

def create_video_tunnel(vm_ip, video_port=55554):
    global video_tunnels
    jump = config["jump_host"]
    
    tunnel_key = f"{vm_ip}:{video_port}"
    
    if tunnel_key in video_tunnels:
        proc, local_port = video_tunnels[tunnel_key]
        if proc.poll() is None:
            return local_port
        else:
            del video_tunnels[tunnel_key]
    
    local_port = get_available_local_port()
    
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{local_port}:{vm_ip}:{video_port}",
        f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
    ]
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise Exception(f"SSH tunnel failed: {stderr}")
    
    video_tunnels[tunnel_key] = (proc, local_port)
    return local_port

def cleanup_video_tunnels():
    global video_tunnels
    for tunnel_key, (proc, _) in video_tunnels.items():
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    video_tunnels.clear()

def tcp_relay_handler(client_socket, remote_host, remote_port, vm_ip, proxy_port):
    try:
        client_addr = client_socket.getpeername()
    except:
        client_addr = "unknown"
    print(f"[PROXY {vm_ip}:{proxy_port}] Client connected from {client_addr}")
    
    remote_socket = None
    try:
        remote_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        remote_socket.settimeout(5)
        remote_socket.connect((remote_host, remote_port))
        print(f"[PROXY {vm_ip}:{proxy_port}] Connected to {remote_host}:{remote_port}")
        
        client_socket.setblocking(False)
        remote_socket.setblocking(False)
        
        while True:
            try:
                readable, _, exceptional = select.select(
                    [client_socket, remote_socket],
                    [],
                    [client_socket, remote_socket],
                    1
                )
                
                if exceptional:
                    print(f"[PROXY {vm_ip}:{proxy_port}] Socket exception")
                    break
                
                for sock in readable:
                    try:
                        data = sock.recv(4096)
                        if not data:
                            print(f"[PROXY {vm_ip}:{proxy_port}] Connection closed by {'client' if sock == client_socket else 'server'}")
                            return
                        
                        if sock == client_socket:
                            remote_socket.sendall(data)
                            print(f"[PROXY {vm_ip}:{proxy_port}] Client→Server: {len(data)} bytes")
                        else:
                            client_socket.sendall(data)
                            print(f"[PROXY {vm_ip}:{proxy_port}] Server→Client: {len(data)} bytes")
                    except socket.error as e:
                        print(f"[PROXY {vm_ip}:{proxy_port}] Socket error: {e}")
                        return
                        
            except select.error as e:
                print(f"[PROXY {vm_ip}:{proxy_port}] Select error: {e}")
                break
                
    except Exception as e:
        print(f"[PROXY {vm_ip}:{proxy_port}] Connection error: {e}")
    finally:
        try:
            client_socket.close()
        except:
            pass
        try:
            if remote_socket:
                remote_socket.close()
        except:
            pass
        print(f"[PROXY {vm_ip}:{proxy_port}] Client disconnected from {client_addr}")

def start_tcp_proxy(vm_ip, video_port=55554):
    global tcp_proxies, proxy_threads
    
    proxy_key = f"{vm_ip}:{video_port}"
    
    if proxy_key in tcp_proxies:
        proxy_info = tcp_proxies[proxy_key]
        if proxy_info["active"]:
            print(f"[PROXY] Proxy already running for {proxy_key} on port {proxy_info['port']}")
            return proxy_info["port"]
    
    ssh_tunnel_port = create_video_tunnel(vm_ip, video_port)
    print(f"[PROXY] SSH tunnel created on port {ssh_tunnel_port}")
    
    local_tunnel_port = get_available_local_port(start_port=9002)
    
    proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_socket.bind(('0.0.0.0', local_tunnel_port))
    proxy_socket.listen(5)
    
    print(f"[PROXY] Started TCP proxy on 0.0.0.0:{local_tunnel_port} for {vm_ip}:{video_port}")
    
    def proxy_listener():
        print(f"[PROXY] Listener started for {proxy_key} on port {local_tunnel_port}")
        try:
            while tcp_proxies.get(proxy_key, {}).get("active", False):
                try:
                    proxy_socket.settimeout(1)
                    client_socket, client_addr = proxy_socket.accept()
                    print(f"[PROXY] Accepted connection from {client_addr}")
                    
                    handler_thread = threading.Thread(
                        target=tcp_relay_handler,
                        args=(client_socket, "127.0.0.1", ssh_tunnel_port, vm_ip, local_tunnel_port),
                        daemon=True
                    )
                    handler_thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"[PROXY] Accept error: {e}")
                    break
        except Exception as e:
            print(f"[PROXY] Listener error: {e}")
        finally:
            try:
                proxy_socket.close()
            except:
                pass
            print(f"[PROXY] Proxy listener stopped for {proxy_key}")
    
    tcp_proxies[proxy_key] = {
        "port": local_tunnel_port,
        "socket": proxy_socket,
        "active": True
    }
    
    listener_thread = threading.Thread(target=proxy_listener, daemon=True)
    listener_thread.start()
    proxy_threads[proxy_key] = listener_thread
    
    print(f"[PROXY] Listener thread started: {listener_thread.is_alive()}")
    
    return local_tunnel_port

def stop_tcp_proxy(vm_ip, video_port=55554):
    global tcp_proxies, proxy_threads
    
    proxy_key = f"{vm_ip}:{video_port}"
    
    if proxy_key in tcp_proxies:
        proxy_info = tcp_proxies[proxy_key]
        proxy_info["active"] = False
        
        try:
            proxy_info["socket"].close()
        except:
            pass
        
        del tcp_proxies[proxy_key]
        
        if proxy_key in proxy_threads:
            del proxy_threads[proxy_key]
        
        print(f"[PROXY] Stopped TCP proxy for {proxy_key}")
        return True
    
    return False

def cleanup_tcp_proxies():
    global tcp_proxies
    for proxy_key in list(tcp_proxies.keys()):
        vm_ip, video_port = proxy_key.split(':')
        stop_tcp_proxy(vm_ip, int(video_port))

@app.on_event("startup")
async def startup_event():
    start_main_tunnel()

@app.on_event("shutdown")
async def shutdown_event():
    stop_main_tunnel()
    cleanup_video_tunnels()
    cleanup_tcp_proxies()

atexit.register(stop_main_tunnel)
atexit.register(cleanup_video_tunnels)
atexit.register(cleanup_tcp_proxies)

@app.post("/auth/register", response_model=UserResponse)
def register(user: UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(User).filter(User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    
    db_user = db.query(User).filter(User.email == user.email).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = get_password_hash(user.password)
    db_user = User(
        username=user.username,
        email=user.email,
        hashed_password=hashed_password,
        role=user.role
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

@app.post("/auth/token", response_model=Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/auth/me", response_model=UserResponse)
def get_current_user_info(current_user: User = Depends(get_current_user)):
    return current_user

@app.get("/jump-hosts", response_model=List[JumpHostResponse])
def list_jump_hosts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    jump_hosts = db.query(JumpHost).all()
    return jump_hosts

@app.post("/servers", response_model=ServerResponse)
def create_server(
    server: ServerCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    db_server = Server(**server.dict())
    db.add(db_server)
    db.commit()
    db.refresh(db_server)
    return db_server

@app.get("/servers", response_model=List[ServerResponse])
def list_servers(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    servers = db.query(Server).filter(Server.deleted == 0).all()
    return servers

@app.get("/servers/{server_id}", response_model=ServerResponse)
def get_server(
    server_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    server = db.query(Server).filter(Server.id == server_id, Server.deleted == 0).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    return server

@app.put("/servers/{server_id}", response_model=ServerResponse)
def update_server(
    server_id: int,
    server_update: ServerUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    from sqlalchemy.exc import IntegrityError
    
    server = db.query(Server).filter(Server.id == server_id, Server.deleted == 0).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    update_data = server_update.dict(exclude_unset=True)
    
    if "name" in update_data and update_data["name"] != server.name:
        existing_server = db.query(Server).filter(Server.name == update_data["name"], Server.deleted == 0).first()
        if existing_server:
            raise HTTPException(status_code=400, detail="Server with this name already exists")
    
    for field, value in update_data.items():
        setattr(server, field, value)
    
    try:
        db.add(server)
        db.commit()
        db.refresh(server)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Server with this name already exists")
    
    return server

@app.delete("/servers/{server_id}")
def delete_server(
    server_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    server = db.query(Server).filter(Server.id == server_id, Server.deleted == 0).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    server.deleted = 1
    db.add(server)
    db.commit()
    return {"message": "Server deleted successfully"}

@app.post("/proxy/start")
def start_proxy(vm_ip: str = Query(...), video_port: int = Query(default=55554), current_user: User = Depends(get_current_user)):
    try:
        proxy_key = f"{vm_ip}:{video_port}"
        
        if proxy_key in tcp_proxies:
            proxy_info = tcp_proxies[proxy_key]
            if proxy_info["active"]:
                return {
                    "status": "proxy_worked",
                    "vm_ip": vm_ip,
                    "video_port": video_port,
                    "proxy_port": proxy_info["port"],
                    "connection_address": "sinthy.fvds.ru",
                    "instructions": f"Connect CrystalCloudeClient to sinthy.fvds.ru:{proxy_info['port']}"
                }
        
        ssh_tunnel_port = create_video_tunnel(vm_ip, video_port)
        proxy_port = start_tcp_proxy(vm_ip, video_port)
        
        tcp_proxies[proxy_key]["ssh_tunnel_port"] = ssh_tunnel_port
        
        return {
            "status": "proxy_started",
            "vm_ip": vm_ip,
            "video_port": video_port,
            "proxy_port": proxy_port,
            "connection_address": "sinthy.fvds.ru",
            "ssh_tunnel_port": ssh_tunnel_port,
            "instructions": f"Connect CrystalCloudeClient to sinthy.fvds.ru:{proxy_port}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy start error: {str(e)}")

def detect_format(data: bytes) -> dict:
    result = {
        "is_http": False,
        "is_websocket": False,
        "is_mjpeg": False,
        "is_h264": False,
        "is_binary": False,
        "detected_protocol": "unknown"
    }
    
    if data.startswith(b'HTTP'):
        result["is_http"] = True
        result["detected_protocol"] = "HTTP"
        if b'MJPEG' in data or b'image/jpeg' in data or b'multipart' in data:
            result["is_mjpeg"] = True
            result["detected_protocol"] = "HTTP MJPEG"
    elif data.startswith(b'GET') or data.startswith(b'POST'):
        result["is_http"] = True
        result["detected_protocol"] = "HTTP Request"
    elif b'Upgrade: websocket' in data or b'upgrade: websocket' in data:
        result["is_websocket"] = True
        result["detected_protocol"] = "WebSocket"
    elif b'H.264' in data or b'h264' in data or b'avc1' in data:
        result["is_h264"] = True
        result["detected_protocol"] = "H.264"
    elif data[:4] == b'\x00\x00\x00\x01' or data[:3] == b'\x00\x00\x01':
        result["is_h264"] = True
        result["detected_protocol"] = "H.264 (NAL unit)"
    elif data[:2] == b'\xff\xd8':
        result["detected_protocol"] = "JPEG Frame"
    else:
        result["is_binary"] = True
        result["detected_protocol"] = "Binary/Unknown"
    
    return result

@app.get("/vms/list")
def list_vm_hosts(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    servers = db.query(Server).all()
    return {"vms": [s.name for s in servers]}

@app.get("/vms/available")
def get_vms(host: str = Query(...), port: int = Query(default=16509), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    server = db.query(Server).filter(Server.host == host, Server.port == port).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    conn = get_libvirt_connection(server)
    
    try:
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

    cache_dir = "vms_cache"
    os.makedirs(cache_dir, exist_ok=True)
    cache_filename = os.path.join(cache_dir, f"cached_vms_{host}.yaml")
    with open(cache_filename, 'w', encoding='utf-8') as cache_file:
        yaml.dump({"vms": vms}, cache_file, allow_unicode=True)
    return {"vms": vms}

@app.get("/vms/cached")
def get_cached_vms(vm: str, current_user: User = Depends(get_current_user)):
    cache_dir = "vms_cache"
    cache_filename = os.path.join(cache_dir, f"cached_vms_{vm}.yaml")
    if not os.path.exists(cache_filename):
        raise HTTPException(status_code=404, detail="VMs cache not found for this server.")
    with open(cache_filename, 'r', encoding='utf-8') as cache_file:
        data = yaml.safe_load(cache_file)
    return data

@app.post("/vms/start")
def start_vm_legacy(request: VMActionRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    server = db.query(Server).filter(Server.host == request.host, Server.port == request.port).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    conn = get_libvirt_connection(server)
    
    try:
        domain = conn.lookupByName(request.vm_name)
        if domain is None:
            raise HTTPException(status_code=404, detail="ВМ не найдена.")
        domain.create()
        status = "started"
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

    log = {"vm": request.vm_name, "action": "start", "status": status, "host": request.vm_host}
    return log

@app.post("/vms/stop")
def stop_vm_legacy(request: VMActionRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    server = db.query(Server).filter(Server.host == request.host, Server.port == request.port).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    conn = get_libvirt_connection(server)
    
    try:
        domain = conn.lookupByName(request.vm_name)
        if domain is None:
            raise HTTPException(status_code=404, detail="ВМ не найдена.")
        domain.shutdown()
        status = "stopped"
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

    log = {"vm": request.vm_name, "action": "stop", "status": status, "host": request.vm_host}
    return log

@app.get("/vms/running")
def get_running_vms(host: str = Query(...), port: int = Query(default=16509), current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    server = db.query(Server).filter(Server.host == host, Server.port == port).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    conn = get_libvirt_connection(server)

    try:
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
def force_stop_vm(request: VMActionRequest, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    server = db.query(Server).filter(Server.host == request.host, Server.port == request.port).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")
    
    conn = get_libvirt_connection(server)
    
    try:
        domain = conn.lookupByName(request.vm_name)
        if domain is None:
            raise HTTPException(status_code=404, detail="ВМ не найдена.")
        domain.destroy()
        conn.close()
        return {"vm": request.vm_name, "action": "force_stop", "status": "forced_off", "host": request.vm_host}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Libvirt error: {e}")

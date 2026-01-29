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
import platform

from db.database import engine, Base, get_db, SessionLocal
from db.models import User, Server, VM, UserVM, JumpHost, RoleEnum
from db.schemas import (
    UserCreate, UserResponse, Token, ServerCreate, ServerResponse, ServerUpdate,
    VMCreate, VMResponse, JumpHostCreate, JumpHostResponse,
    VMHostParams, VMActionRequest, UserVMCreate, UserVMResponse, UserWithVMsResponse
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

def ping_server(host: str, port: int = None, jump_host_info: dict = None) -> float:
    """
    Измеряет пинг до сервера.
    Для серверов с jump_host проверяет доступность TCP порта SSH.
    Если jump host недоступен, пробует пинговать сервер напрямую.
    Возвращает время в миллисекундах или -1 если недоступен.
    """
    try:
        if jump_host_info:
            # Для серверов с jump host проверяем доступность SSH порта
            target_host = jump_host_info['host']
            target_port = jump_host_info['port']
            
            print(f"[PING] Checking TCP connection to {target_host}:{target_port} (SSH)")
            
            start_time = time.time()
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            
            try:
                result = sock.connect_ex((target_host, target_port))
                end_time = time.time()
                sock.close()
                
                if result == 0:
                    ping_ms = (end_time - start_time) * 1000
                    print(f"[PING] TCP connection successful: {ping_ms:.2f}ms")
                    return ping_ms
                else:
                    print(f"[PING] TCP connection failed: error code {result}")
            except Exception as e:
                print(f"[PING] TCP connection error: {e}")
            
            # Если TCP не удался, пробуем ping напрямую к серверу
            print(f"[PING] TCP to jump host failed, trying direct ping to {host}")
            
            if platform.system().lower() == "windows":
                cmd = ["ping", "-n", "1", "-w", "1000", host]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", host]
            
            print(f"[PING] Pinging {host} directly with command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            
            print(f"[PING] Direct ping return code: {result.returncode}")
            print(f"[PING] Direct ping stdout: {result.stdout}")
            if result.stderr:
                print(f"[PING] Direct ping stderr: {result.stderr}")
            
            if result.returncode == 0:
                if platform.system().lower() == "windows":
                    import re
                    match = re.search(r'time[=<](\d+)ms', result.stdout)
                    if match:
                        ping_value = float(match.group(1))
                        print(f"[PING] Direct ping successful: {ping_value}ms")
                        return ping_value
                    else:
                        if "time<1ms" in result.stdout:
                            print(f"[PING] Direct ping successful: <1ms (counted as 0.5ms)")
                            return 0.5
                else:
                    import re
                    match = re.search(r'time=(\d+\.?\d*)\s*ms', result.stdout)
                    if match:
                        ping_value = float(match.group(1))
                        print(f"[PING] Direct ping successful: {ping_value}ms")
                        return ping_value
        else:
            # Для серверов без jump host используем обычный ping
            target_host = host
            
            if platform.system().lower() == "windows":
                cmd = ["ping", "-n", "1", "-w", "1000", target_host]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", target_host]
            
            print(f"[PING] Pinging {target_host} with command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            
            print(f"[PING] Return code: {result.returncode}")
            print(f"[PING] Stdout: {result.stdout}")
            if result.stderr:
                print(f"[PING] Stderr: {result.stderr}")
            
            if result.returncode == 0:
                if platform.system().lower() == "windows":
                    import re
                    match = re.search(r'time[=<](\d+)ms', result.stdout)
                    if match:
                        ping_value = float(match.group(1))
                        print(f"[PING] Ping successful: {ping_value}ms")
                        return ping_value
                    else:
                        if "time<1ms" in result.stdout:
                            print(f"[PING] Ping successful: <1ms (counted as 0.5ms)")
                            return 0.5
                else:
                    import re
                    match = re.search(r'time=(\d+\.?\d*)\s*ms', result.stdout)
                    if match:
                        ping_value = float(match.group(1))
                        print(f"[PING] Ping successful: {ping_value}ms")
                        return ping_value
        
        print(f"[PING] All connectivity checks failed for {host}")
        return -1.0  # Сервер недоступен
    except Exception as e:
        print(f"[PING] Error for {host}: {e}")
        return -1.0

tunnel_process = None
server_tunnels = {}
video_tunnels = {}
winrm_tunnels = {}
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

def server_tunnel_exists(server_id, server_host, server_port, jump_host_info):
    """Проверяет, существует ли ЛЮБОЙ туннель до указанного сервера"""
    for proc in psutil.process_iter(attrs=['cmdline']):
        if proc.info['cmdline'] is None:
            continue
        cmd = ' '.join(proc.info['cmdline'])
        # Ищем любой туннель до этого сервера с этим jump host
        if (f"{server_host}:{server_port}" in cmd and 
            jump_host_info['host'] in cmd and
            "ssh" in cmd):
            return True, proc
    return False, None

def get_existing_server_tunnel(server_id, server_host, server_port, jump_host_info):
    """Возвращает локальный порт существующего туннеля до сервера"""
    for proc in psutil.process_iter(attrs=['cmdline']):
        if proc.info['cmdline'] is None:
            continue
        cmd = ' '.join(proc.info['cmdline'])
        
        # Ищем туннель к нужному серверу
        if (f"{server_host}:{server_port}" in cmd and 
            jump_host_info['host'] in cmd and
            "ssh" in cmd and "-L" in cmd):
            
            # Извлекаем локальный порт из команды
            import re
            match = re.search(r'-L\s+(\d+):' + re.escape(server_host) + ':' + re.escape(server_port), cmd)
            if match:
                local_port = int(match.group(1))
                print(f"[TUNNEL] Found existing tunnel for server {server_id}: localhost:{local_port} -> {server_host}:{server_port}")
                return local_port, proc
    
    return None, None

def start_server_tunnel(server_id, server_host, server_port, jump_host_info):
    global server_tunnels
    
    tunnel_key = f"server_{server_id}"
    
    # Проверяем, есть ли уже активный туннель в нашем кеше
    if tunnel_key in server_tunnels:
        proc, local_port = server_tunnels[tunnel_key]
        if proc and proc.poll() is None:
            print(f"[TUNNEL] Using cached tunnel for server {server_id}: localhost:{local_port}")
            return local_port
        else:
            print(f"[TUNNEL] Cached tunnel for server {server_id} is dead, removing...")
            del server_tunnels[tunnel_key]
    
    # Проверяем, существует ли туннель в системе
    existing_port, existing_proc = get_existing_server_tunnel(server_id, server_host, server_port, jump_host_info)
    if existing_port:
        # Туннель уже существует, используем его
        server_tunnels[tunnel_key] = (existing_proc, existing_port)
        print(f"[TUNNEL] Using existing system tunnel for server {server_id}: localhost:{existing_port}")
        return existing_port
    
    # Создаем новый туннель
    local_port = 14440 + server_id
    
    print(f"[TUNNEL] Creating new tunnel for server {server_id}: localhost:{local_port} -> {server_host}:{server_port}")
    
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
    print(f"[TUNNEL] Successfully created tunnel for server {server_id}: localhost:{local_port}")
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

def start_winrm_tunnel(vm_ip, jump_host_info):
    """Создает туннель для WinRM (порт 5985)"""
    global winrm_tunnels
    
    tunnel_key = f"winrm_{vm_ip}"
    
    # Проверяем, есть ли уже активный туннель в нашем кеше
    if tunnel_key in winrm_tunnels:
        proc, local_port = winrm_tunnels[tunnel_key]
        if proc and proc.poll() is None:
            print(f"[WINRM] Using cached tunnel for {vm_ip}: localhost:{local_port}")
            return local_port
        else:
            print(f"[WINRM] Cached tunnel for {vm_ip} is dead, removing...")
            del winrm_tunnels[tunnel_key]
    
    # Проверяем, существует ли туннель в системе
    existing_port, existing_proc = get_existing_server_tunnel(f"winrm_{vm_ip}", vm_ip, "5985", jump_host_info)
    if existing_port:
        # Туннель уже существует, используем его
        winrm_tunnels[tunnel_key] = (existing_proc, existing_port)
        print(f"[WINRM] Using existing system tunnel for {vm_ip}: localhost:{existing_port}")
        return existing_port
    
    # Создаем новый туннель
    local_port = 15985  # Базовый порт для WinRM
    
    print(f"[WINRM] Creating new tunnel for {vm_ip}: localhost:{local_port} -> {vm_ip}:5985")
    
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{local_port}:{vm_ip}:5985",
        f"{jump_host_info['user']}@{jump_host_info['host']}", "-p", str(jump_host_info['port'])
    ]
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise Exception(f"SSH WinRM tunnel for {vm_ip} failed: {stderr}")
    
    winrm_tunnels[tunnel_key] = (proc, local_port)
    print(f"[WINRM] Successfully created tunnel for {vm_ip}: localhost:{local_port}")
    return local_port

def stop_winrm_tunnels():
    """Останавливает все WinRM туннели"""
    global winrm_tunnels
    for tunnel_key, (proc, _) in winrm_tunnels.items():
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    winrm_tunnels.clear()

# RDP туннели
rdp_tunnels = {}

def start_rdp_tunnel(vm_ip, jump_host_info):
    """Создает туннель для RDP (порт 3389)"""
    global rdp_tunnels
    
    tunnel_key = f"rdp_{vm_ip}"
    
    # Проверяем, есть ли уже активный туннель в нашем кеше
    if tunnel_key in rdp_tunnels:
        proc, local_port = rdp_tunnels[tunnel_key]
        if proc and proc.poll() is None:
            print(f"[RDP] Using cached tunnel for {vm_ip}: localhost:{local_port}")
            return local_port
        else:
            print(f"[RDP] Cached tunnel for {vm_ip} is dead, removing...")
            del rdp_tunnels[tunnel_key]
    
    # Создаем новый туннель
    local_port = 13389  # Базовый порт для RDP
    
    print(f"[RDP] Creating new tunnel for {vm_ip}: localhost:{local_port} -> {vm_ip}:3389")
    
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{local_port}:{vm_ip}:3389",
        f"{jump_host_info['user']}@{jump_host_info['host']}", "-p", str(jump_host_info['port'])
    ]
    
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise Exception(f"SSH RDP tunnel for {vm_ip} failed: {stderr}")
    
    rdp_tunnels[tunnel_key] = (proc, local_port)
    print(f"[RDP] Successfully created tunnel for {vm_ip}: localhost:{local_port}")
    return local_port

def stop_rdp_tunnels():
    """Останавливает все RDP туннели"""
    global rdp_tunnels
    for tunnel_key, (proc, _) in rdp_tunnels.items():
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    rdp_tunnels.clear()

def list_all_tunnels():
    """Отладочная функция: показывает все активные туннели"""
    print("\n=== ACTIVE TUNNELS ===")
    
    # Server tunnels
    print("\nServer tunnels:")
    for tunnel_key, (proc, port) in server_tunnels.items():
        status = "alive" if (proc and proc.poll() is None) else "dead"
        print(f"  {tunnel_key}: localhost:{port} ({status})")
    
    # WinRM tunnels
    print("\nWinRM tunnels:")
    for tunnel_key, (proc, port) in winrm_tunnels.items():
        status = "alive" if (proc and proc.poll() is None) else "dead"
        print(f"  {tunnel_key}: localhost:{port} ({status})")
    
    # Video tunnels
    print("\nVideo tunnels:")
    for tunnel_key, (proc, port) in video_tunnels.items():
        status = "alive" if (proc and proc.poll() is None) else "dead"
        print(f"  {tunnel_key}: localhost:{port} ({status})")
    
    # System processes
    print("\nSSH processes in system:")
    ssh_processes = []
    for proc in psutil.process_iter(attrs=['cmdline']):
        if proc.info['cmdline'] is None:
            continue
        cmd = ' '.join(proc.info['cmdline'])
        if 'ssh' in cmd and '-L' in cmd:
            ssh_processes.append(cmd)
    
    for i, cmd in enumerate(ssh_processes):
        print(f"  Process {i+1}: {cmd}")
    
    print("\n=== END TUNNELS ===\n")

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
    stop_winrm_tunnels()

def get_libvirt_connection(server):
    """
    Получить подключение к libvirt для сервера.
    Если jump_host_id указан - использует туннель, иначе прямое подключение.
    """
    if server.jump_host_id is not None and server.jump_host:
        local_port = 14440 + server.id
        conn_uri = f"qemu+tcp://127.0.0.1:{local_port}/system"
        
        # Проверяем и восстанавливаем туннель при необходимости
        tunnel_key = f"server_{server.id}"
        if tunnel_key not in server_tunnels:
            jump_info = {
                'host': server.jump_host.host,
                'port': server.jump_host.port,
                'user': server.jump_host.user
            }
            print(f"[LIBVIRT] Tunnel missing for server {server.name}, creating...")
            start_server_tunnel(server.id, server.host, server.port, jump_info)
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
            print(f"[SSH TUNNEL] Using cached tunnel for {tunnel_key} on port {local_port}")
            return local_port
        else:
            print(f"[SSH TUNNEL] Cached tunnel for {tunnel_key} is dead, removing...")
            del video_tunnels[tunnel_key]
    
    local_port = get_available_local_port()
    
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-N", "-L", f"{local_port}:{vm_ip}:{video_port}",
        f"{jump['user']}@{jump['host']}", "-p", str(jump['port'])
    ]
    
    print(f"[SSH TUNNEL] Creating SSH tunnel: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2)
    
    if proc.poll() is not None:
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        print(f"[SSH TUNNEL] SSH tunnel process exited immediately!")
        print(f"[SSH TUNNEL] STDERR: {stderr}")
        print(f"[SSH TUNNEL] STDOUT: {stdout}")
        raise Exception(f"SSH tunnel failed: {stderr}")
    
    video_tunnels[tunnel_key] = (proc, local_port)
    print(f"[SSH TUNNEL] SSH tunnel created successfully on port {local_port} for {tunnel_key}")
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
        print(f"[PROXY {vm_ip}:{proxy_port}] Attempting to connect to {remote_host}:{remote_port}")
        remote_socket.connect((remote_host, remote_port))
        print(f"[PROXY {vm_ip}:{proxy_port}] Successfully connected to {remote_host}:{remote_port}")
        
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

def start_tcp_proxy(vm_ip, video_port=55554, jump_host_info=None):
    global tcp_proxies, proxy_threads
    
    proxy_key = f"{vm_ip}:{video_port}"
    
    if proxy_key in tcp_proxies:
        proxy_info = tcp_proxies[proxy_key]
        if proxy_info["active"]:
            print(f"[PROXY] Proxy already running for {proxy_key} on port {proxy_info['port']}")
            return proxy_info["port"]
    
    # Создаем SSH туннель (jump_host_info используется из конфигурации)
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

# WinRM функции
try:
    import winrm
except ImportError:
    print("[WINRM] Warning: pywinrm not installed. Run: pip install pywinrm")
    winrm = None

def run_winrm_command(vm_ip, command, jump_host_info=None):
    """Выполняет команду на ВМ через WinRM"""
    if not winrm:
        raise Exception("pywinrm not installed")
    
    try:
        # Создаем туннель для WinRM
        if jump_host_info:
            local_port = start_winrm_tunnel(vm_ip, jump_host_info)
            winrm_url = f"http://127.0.0.1:{local_port}/wsman"
        else:
            winrm_url = f"http://{vm_ip}:5985/wsman"
        
        session = winrm.Session(
            winrm_url,
            auth=('ggg', '123'),  # Учетные данные
            transport='ntlm'
        )
        
        result = session.run_cmd(command)
        
        return {
            'success': result.status_code == 0,
            'stdout': result.std_out.decode('cp866'),
            'stderr': result.std_err.decode('cp866'),
            'status_code': result.status_code
        }
        
    except Exception as e:
        return {
            'success': False,
            'stdout': '',
            'stderr': str(e),
            'status_code': 1
        }

def start_application_on_vm(vm_ip, app_path, jump_host_info=None):
    """Запускает GUI приложение на ВМ через RDP сессию"""
    # Проверяем, что приложение существует
    app_path_windows = app_path.replace("/", "\\")
    check_cmd = f'dir {app_path_windows}'
    check_result = run_winrm_command(vm_ip, check_cmd, jump_host_info)
    
    if not check_result['success']:
        print(f"[APP] Application not found at {app_path}")
        return False
    
    print(f"[APP] Application found, starting...")
    
    # Проверяем активные RDP сессии
    session_cmd = 'query user'
    session_result = run_winrm_command(vm_ip, session_cmd, jump_host_info)
    
    if not session_result['success'] or 'ggg' not in session_result['stdout']:
        print(f"[APP] No active RDP session found, creating scheduled task for auto-start")
        
        # Создаем scheduled task для автозапуска при RDP подключении
        cleanup_cmd = f'schtasks /delete /tn CrystalCloudServer /f'
        run_winrm_command(vm_ip, cleanup_cmd, jump_host_info)
        
        create_cmd = f'schtasks /create /tn CrystalCloudServer /tr "{app_path_windows}" /sc onlogon /it /f'
        create_result = run_winrm_command(vm_ip, create_cmd, jump_host_info)
        
        if not create_result['success']:
            print(f"[APP] Failed to create scheduled task: {create_result['stderr']}")
            return False
        
        print(f"[APP] Scheduled task created. Application will start when RDP session is active.")
        return True
    
    # Если есть активная RDP сессия, запускаем через PowerShell
    print(f"[APP] Active RDP session found, starting application...")
    start_cmd = f'powershell -Command "Start-Process -FilePath \'{app_path_windows}\' -WindowStyle Normal"'
    result = run_winrm_command(vm_ip, start_cmd, jump_host_info)
    
    if result['success']:
        print(f"[APP] Application started successfully on {vm_ip}")
        return True
    else:
        print(f"[APP] Failed to start application: {result['stderr']}")
        return False

def wait_for_vm_ready(vm_ip, jump_host_info=None, timeout=300):
    """Ожидает загрузки ВМ и доступности WinRM"""
    import time
    start_time = time.time()
    
    while time.time() - start_time < timeout:
        try:
            # Пробуем выполнить простую команду
            result = run_winrm_command(vm_ip, 'whoami', jump_host_info)
            if result['success']:
                print(f"[VM] VM {vm_ip} is ready")
                return True
        except Exception as e:
            print(f"[VM] Waiting for {vm_ip} to be ready: {e}")
        
        time.sleep(10)
    
    print(f"[VM] Timeout waiting for {vm_ip} to be ready")
    return False

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
    print(f"[SERVER SORT] Found {len(servers)} servers")
    
    # Измеряем пинг для каждого сервера
    servers_with_ping = []
    for server in servers:
        jump_host_info = None
        if server.jump_host:
            jump_host_info = {
                'host': server.jump_host.host,
                'port': server.jump_host.port,
                'user': server.jump_host.user
            }
            print(f"[SERVER SORT] Server '{server.name}' has jump host: {server.jump_host.host}")
        else:
            print(f"[SERVER SORT] Server '{server.name}' has no jump host, pinging directly")
        
        ping = ping_server(server.host, server.port, jump_host_info)
        print(f"[SERVER SORT] Server '{server.name}' ({server.host}) - ping: {ping}ms")
        
        # Создаем словарь с данными сервера и пингом
        server_dict = {
            'id': server.id,
            'name': server.name,
            'host': server.host,
            'port': server.port,
            'user': server.user,
            'jump_host_id': server.jump_host_id,
            'local_tunnel_port': server.local_tunnel_port,
            'created_at': server.created_at,
            'ping': ping
        }
        servers_with_ping.append(server_dict)
    
    # Сортируем по пингу (меньше пинг = выше в списке), недоступные (-1) в конец
    servers_with_ping.sort(key=lambda x: (x['ping'] < 0, x['ping']))
    
    print(f"[SERVER SORT] Sorted servers by ping:")
    for i, server in enumerate(servers_with_ping):
        status = "✓" if server['ping'] > 0 else "✗"
        print(f"[SERVER SORT] {i+1}. {server['name']} - {server['ping']}ms {status}")
    
    return servers_with_ping

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
        
        # Получаем все ВМ из libvirt
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

@app.post("/vms/debug")
def debug_vm(
    vm_ip: str = Query(...),
    command: str = Query(...),
    current_user: User = Depends(get_current_user)
):
    """Временный эндпоинт для отладки"""
    jump_host_info = None
    if "jump_host" in config:
        jump_host_info = config["jump_host"]
    
    try:
        result = run_winrm_command(vm_ip, command, jump_host_info)
        return {
            "vm_ip": vm_ip,
            "command": command,
            "success": result['success'],
            "stdout": result['stdout'],
            "stderr": result['stderr'],
            "status_code": result['status_code']
        }
    except Exception as e:
        return {
            "vm_ip": vm_ip,
            "command": command,
            "success": False,
            "error": str(e)
        }

@app.post("/vms/ready")
def check_vm_ready(
    vm_ip: str = Query(...),
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Проверяет готовность ВМ к выполнению команд"""
    # Получаем информацию о jump host из конфигурации
    jump_host_info = None
    if "jump_host" in config:
        jump_host_info = config["jump_host"]
    
    print(f"[VM] Checking readiness for VM {vm_ip}")
    ready = wait_for_vm_ready(vm_ip, jump_host_info, timeout=60)
    
    return {
        "vm_ip": vm_ip,
        "ready": ready,
        "message": "VM is ready" if ready else "VM is not ready or timeout"
    }

@app.get("/proxy/rdp/games")
def get_available_games(
    vm_ip: str = Query(...),
    games_folder: str = Query(default="C:\\Games"),
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Получает список доступных игр на ВМ"""
    jump_host_info = None
    if "jump_host" in config:
        jump_host_info = config["jump_host"]
    
    if not jump_host_info:
        raise HTTPException(status_code=400, detail="Jump host not configured")
    
    try:
        print(f"[GAMES] Scanning games folder on {vm_ip}: {games_folder}")
        
        # Сканируем папку с играми
        games_folder_windows = games_folder.replace("/", "\\")
        scan_cmd = f"""
$folder = '{games_folder_windows}'
if (Test-Path $folder) {{
    Get-ChildItem -Path $folder -Filter '*.exe' -Recurse | Select-Object -ExpandProperty FullName
}} else {{
    Write-Host "Folder not found"
}}
"""
        result = run_winrm_command(vm_ip, scan_cmd, jump_host_info)
        
        if result['success']:
            games = [line.strip() for line in result['stdout'].split('\n') if line.strip() and line.endswith('.exe')]
            return {
                "vm_ip": vm_ip,
                "games_folder": games_folder,
                "games": games,
                "count": len(games)
            }
        else:
            return {
                "vm_ip": vm_ip,
                "games_folder": games_folder,
                "games": [],
                "count": 0,
                "error": result['stderr']
            }
    except Exception as e:
        print(f"[GAMES] Failed to scan games: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to scan games: {str(e)}")

@app.post("/proxy/rdp/start")
def start_rdp_proxy(
    vm_ip: str = Query(...),
    game_path: str = Query(default="C:\\Users\\ggg\\Desktop\\G-SYNC Pendulum Demo.lnk"),
    game_args: str = Query(default=""),
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Запускает RDP туннель с автоматическим логином и запуском игры"""
    jump_host_info = None
    if "jump_host" in config:
        jump_host_info = config["jump_host"]
    
    if not jump_host_info:
        raise HTTPException(status_code=400, detail="Jump host not configured")
    
    try:
        print(f"[RDP TUNNEL] Starting RDP tunnel for {vm_ip}, game_path: {game_path}")
        
        # Создаем RDP туннель с TCP прокси
        tunnel_port = start_tcp_proxy(vm_ip, video_port=3389, jump_host_info=jump_host_info)
        
        # Создаем батник в Startup папке для запуска игры при логине
        print(f"[RDP TUNNEL] Setting up game autostart via Startup batch: {game_path} {game_args}")
        game_path_windows = game_path.replace("/", "\\")
        
        # Создаем батник через cmd.exe
        # Используем echo для создания содержимого батника
        startup_batch_path = '%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\StartGame.bat'
        
        # Команда создает батник с правильным содержимым
        # Обязательно обворачиваем в cmd /c чтобы WinRM выполнил через cmd.exe, а не PowerShell
        create_batch_cmd = f'cmd /c (echo @echo off & echo timeout /t 3 /nobreak & echo start "" "{game_path_windows}" {game_args}) > "{startup_batch_path}"'
        
        result = run_winrm_command(vm_ip, create_batch_cmd, jump_host_info)
        print(f"[RDP TUNNEL] Batch file creation result: {result['success']}")
        if not result['success']:
            print(f"[RDP TUNNEL] Batch creation error: {result['stderr']}")
        else:
            print(f"[RDP TUNNEL] Batch created successfully")
            
        # Проверяем, что батник создался
        check_batch_cmd = f'cmd /c dir "{startup_batch_path}"'
        check_result = run_winrm_command(vm_ip, check_batch_cmd, jump_host_info)
        print(f"[RDP TUNNEL] Batch file check: {check_result['success']}")
        if check_result['success']:
            print(f"[RDP TUNNEL] Batch file exists")
        
        return {
            "status": "tunnel_started",
            "vm_ip": vm_ip,
            "tunnel_port": tunnel_port,
            "tunnel_host": "127.0.0.1",
            "game_path": game_path,
            "rdp_command": f"mstsc /v:127.0.0.1:{tunnel_port}",
            "message": f"RDP tunnel ready. Game will start on login. Connect with: mstsc /v:127.0.0.1:{tunnel_port}"
        }
    except Exception as e:
        print(f"[RDP TUNNEL] Failed to start tunnel: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to start RDP tunnel: {str(e)}")

@app.post("/proxy/rdp/stop")
def stop_rdp_proxy(
    vm_ip: str = Query(...),
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Останавливает RDP прокси-сервер"""
    try:
        print(f"[RDP PROXY] Stopping RDP proxy for {vm_ip}")
        stopped = stop_tcp_proxy(vm_ip, video_port=3389)
        
        return {
            "status": "proxy_stopped" if stopped else "proxy_not_found",
            "vm_ip": vm_ip,
            "message": "RDP proxy stopped" if stopped else "RDP proxy was not running"
        }
    except Exception as e:
        print(f"[RDP PROXY] Failed to stop proxy: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to stop RDP proxy: {str(e)}")

@app.post("/vms/start-app")
def start_app_on_vm(
    vm_ip: str = Query(...),
    app_path: str = Query(default="C:\\CloudServer\\CrystalCloudServer.exe"),
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    """Запускает приложение на уже работающей ВМ"""
    # Получаем информацию о jump host из конфигурации
    jump_host_info = None
    if "jump_host" in config:
        jump_host_info = config["jump_host"]
    
    print(f"[APP] Starting application {app_path} on VM {vm_ip}")
    app_started = start_application_on_vm(vm_ip, app_path, jump_host_info)
    
    return {
        "vm_ip": vm_ip,
        "app_started": app_started,
        "app_path": app_path
    }

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

@app.post("/admin/assign-vm", response_model=UserVMResponse)
def assign_vm_to_user(
    assignment: UserVMCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == assignment.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    existing_assignment = db.query(UserVM).filter(
        UserVM.user_id == assignment.user_id,
        UserVM.vm_name == assignment.vm_name,
        UserVM.server_host == assignment.server_host,
        UserVM.server_port == assignment.server_port
    ).first()
    
    if existing_assignment:
        raise HTTPException(status_code=400, detail="VM already assigned to this user")
    
    user_vm = UserVM(
        user_id=assignment.user_id,
        vm_name=assignment.vm_name,
        server_host=assignment.server_host,
        server_port=assignment.server_port
    )
    db.add(user_vm)
    db.commit()
    db.refresh(user_vm)
    
    return user_vm

@app.delete("/admin/assign-vm")
def remove_vm_assignment(
    user_id: int = Query(...),
    vm_name: str = Query(...),
    server_host: str = Query(...),
    server_port: int = Query(default=16509),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    assignment = db.query(UserVM).filter(
        UserVM.user_id == user_id,
        UserVM.vm_name == vm_name,
        UserVM.server_host == server_host,
        UserVM.server_port == server_port
    ).first()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    db.delete(assignment)
    db.commit()
    
    return {"message": "VM assignment removed successfully"}

@app.get("/users/{user_id}/vms")
def get_user_vms(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Получаем назначения ВМ для пользователя
    user_vms = db.query(UserVM).filter(UserVM.user_id == user_id).all()
    
    result = []
    for user_vm in user_vms:
        # Получаем сервер для ВМ
        server = db.query(Server).filter(
            Server.host == user_vm.server_host,
            Server.port == user_vm.server_port
        ).first()
        
        if server:
            try:
                # Получаем ВМ с сервера
                conn = get_libvirt_connection(server)
                domain = conn.lookupByName(user_vm.vm_name)
                if domain:
                    state_code, _ = domain.state()
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
                    state = state_map.get(state_code, "unknown")
                    
                    result.append({
                        "name": user_vm.vm_name,
                        "server_host": user_vm.server_host,
                        "server_port": user_vm.server_port,
                        "state": state,
                        "assigned_at": user_vm.created_at.isoformat()
                    })
                conn.close()
            except Exception as e:
                print(f"Error getting VM {user_vm.vm_name}: {e}")
                continue
    
    return {"user": user.username, "vms": result}

@app.get("/users/me/vms")
def get_my_vms(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    # Получаем назначения ВМ для текущего пользователя
    user_vms = db.query(UserVM).filter(UserVM.user_id == current_user.id).all()
    
    result = []
    for user_vm in user_vms:
        # Получаем сервер для ВМ
        server = db.query(Server).filter(
            Server.host == user_vm.server_host,
            Server.port == user_vm.server_port
        ).first()
        
        if server:
            try:
                # Получаем ВМ с сервера
                conn = get_libvirt_connection(server)
                domain = conn.lookupByName(user_vm.vm_name)
                if domain:
                    state_code, _ = domain.state()
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
                    state = state_map.get(state_code, "unknown")
                    
                    result.append({
                        "name": user_vm.vm_name,
                        "server_host": user_vm.server_host,
                        "server_port": user_vm.server_port,
                        "state": state,
                        "assigned_at": user_vm.created_at.isoformat()
                    })
                conn.close()
            except Exception as e:
                print(f"Error getting VM {user_vm.vm_name}: {e}")
                continue
    
    return {"user": current_user.username, "vms": result}

@app.get("/admin/vms/assignments", response_model=List[UserVMResponse])
def get_all_vm_assignments(
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db)
):
    assignments = db.query(UserVM).all()
    return assignments

@app.get("/admin/debug/tunnels")
def debug_tunnels(current_user: User = Depends(get_current_admin_user)):
    """Отладка: показывает все активные туннели"""
    list_all_tunnels()
    
    # Собираем информацию для ответа
    tunnel_info = {
        "server_tunnels": {},
        "winrm_tunnels": {},
        "video_tunnels": {},
        "system_ssh_processes": []
    }
    
    # Server tunnels
    for tunnel_key, (proc, port) in server_tunnels.items():
        tunnel_info["server_tunnels"][tunnel_key] = {
            "local_port": port,
            "alive": (proc and proc.poll() is None)
        }
    
    # WinRM tunnels
    for tunnel_key, (proc, port) in winrm_tunnels.items():
        tunnel_info["winrm_tunnels"][tunnel_key] = {
            "local_port": port,
            "alive": (proc and proc.poll() is None)
        }
    
    # Video tunnels
    for tunnel_key, (proc, port) in video_tunnels.items():
        tunnel_info["video_tunnels"][tunnel_key] = {
            "local_port": port,
            "alive": (proc and proc.poll() is None)
        }
    
    # System processes
    for proc in psutil.process_iter(attrs=['cmdline']):
        if proc.info['cmdline'] is None:
            continue
        cmd = ' '.join(proc.info['cmdline'])
        if 'ssh' in cmd and '-L' in cmd:
            tunnel_info["system_ssh_processes"].append(cmd)
    
    return tunnel_info

@app.post("/admin/debug/cleanup-tunnels")
def cleanup_dead_tunnels(current_user: User = Depends(get_current_admin_user)):
    """Очищает мертвые туннели"""
    cleaned = {
        "server_tunnels": 0,
        "winrm_tunnels": 0,
        "video_tunnels": 0
    }
    
    # Очистка server tunnels
    dead_server_keys = []
    for tunnel_key, (proc, port) in server_tunnels.items():
        if not proc or proc.poll() is not None:
            dead_server_keys.append(tunnel_key)
    
    for key in dead_server_keys:
        del server_tunnels[key]
        cleaned["server_tunnels"] += 1
    
    # Очистка winrm tunnels
    dead_winrm_keys = []
    for tunnel_key, (proc, port) in winrm_tunnels.items():
        if not proc or proc.poll() is not None:
            dead_winrm_keys.append(tunnel_key)
    
    for key in dead_winrm_keys:
        del winrm_tunnels[key]
        cleaned["winrm_tunnels"] += 1
    
    # Очистка video tunnels
    dead_video_keys = []
    for tunnel_key, (proc, port) in video_tunnels.items():
        if not proc or proc.poll() is not None:
            dead_video_keys.append(tunnel_key)
    
    for key in dead_video_keys:
        del video_tunnels[key]
        cleaned["video_tunnels"] += 1
    
    return {
        "message": "Dead tunnels cleaned up",
        "cleaned_count": cleaned
    }

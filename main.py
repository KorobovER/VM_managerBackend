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
import socket
import threading
import select

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
video_tunnels = {}
tcp_proxies = {}
proxy_threads = {}

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

def get_available_local_port(start_port=9001):
    import socket
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
    """Перенаправляет TCP трафик между клиентом и удалённым хостом"""
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
        
        import select
        
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
    """Запускает TCP proxy сервер для видеопотока"""
    global tcp_proxies, proxy_threads
    
    proxy_key = f"{vm_ip}:{video_port}"
    
    if proxy_key in tcp_proxies:
        proxy_info = tcp_proxies[proxy_key]
        if proxy_info["active"]:
            print(f"[PROXY] Proxy already running for {proxy_key} on port {proxy_info['port']}")
            return proxy_info["port"]
    
    # Создаём SSH туннель
    ssh_tunnel_port = create_video_tunnel(vm_ip, video_port)
    print(f"[PROXY] SSH tunnel created on port {ssh_tunnel_port}")
    
    # Proxy слушает на отдельном порту
    local_tunnel_port = get_available_local_port(start_port=9002)
    
    proxy_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy_socket.bind(('0.0.0.0', local_tunnel_port))
    proxy_socket.listen(5)
    
    print(f"[PROXY] Started TCP proxy on 0.0.0.0:{local_tunnel_port} for {vm_ip}:{video_port}")
    
    def proxy_listener():
        """Слушает входящие подключения и перенаправляет их"""
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
    """Останавливает TCP proxy сервер"""
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
    """Очищает все TCP proxy при завершении"""
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

@app.post("/proxy/start")
def start_proxy(vm_ip: str = Query(...), video_port: int = Query(default=55554)):
    """
    Запустить TCP proxy для видеопотока CrystalCloudeServer.
    
    Создаёт SSH туннель и TCP proxy сервер.
    CrystalCloudeClient подключается к sinthy.fvds.ru:PORT
    
    vm_ip: IP адрес ВМ (внутри VMS Host)
    video_port: порт видеопотока на ВМ (по умолчанию 55554)
    """
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
                    "connection_address": f"sinthy.fvds.ru:{proxy_info['port']}",
                    "ssh_tunnel_port": proxy_info.get("ssh_tunnel_port", 9001),
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
            "connection_address": f"sinthy.fvds.ru:{proxy_port}",
            "ssh_tunnel_port": ssh_tunnel_port,
            "instructions": f"Connect CrystalCloudeClient to sinthy.fvds.ru:{proxy_port}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy start error: {str(e)}")

@app.post("/proxy/stop")
def stop_proxy(vm_ip: str = Query(...), video_port: int = Query(default=55554)):
    """
    Остановить TCP proxy для видеопотока.
    
    vm_ip: IP адрес ВМ
    video_port: порт видеопотока на ВМ
    """
    try:
        proxy_stopped = stop_tcp_proxy(vm_ip, video_port)
        
        global video_tunnels
        tunnel_key = f"{vm_ip}:{video_port}"
        tunnel_stopped = False
        
        if tunnel_key in video_tunnels:
            proc, _ = video_tunnels[tunnel_key]
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            del video_tunnels[tunnel_key]
            tunnel_stopped = True
        
        return {
            "status": "stopped",
            "vm_ip": vm_ip,
            "video_port": video_port,
            "proxy_stopped": proxy_stopped,
            "tunnel_stopped": tunnel_stopped
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy stop error: {str(e)}")

@app.get("/proxy/status")
def get_proxy_status():
    """Получить статус всех активных proxy серверов"""
    global tcp_proxies, video_tunnels
    
    proxies_info = []
    for proxy_key, proxy_info in tcp_proxies.items():
        vm_ip, video_port = proxy_key.split(':')
        tunnel_key = f"{vm_ip}:{video_port}"
        tunnel_active = tunnel_key in video_tunnels and video_tunnels[tunnel_key][0].poll() is None
        
        proxies_info.append({
            "vm_ip": vm_ip,
            "video_port": int(video_port),
            "proxy_port": proxy_info["port"],
            "proxy_active": proxy_info["active"],
            "tunnel_active": tunnel_active,
            "connection_address": f"sinthy.fvds.ru:{proxy_info['port']}"
        })
    
    return {"active_proxies": proxies_info, "total": len(proxies_info)}

@app.post("/video/connect")
def connect_video_stream(vm_ip: str = Body(...), video_port: int = Body(default=55554)):
    """
    Создать SSH туннель для видеопотока CrystalCloudeServer.
    
    vm_ip: IP адрес ВМ (внутри VMS Host)
    video_port: порт видеопотока на ВМ (по умолчанию 55554 для CrystalCloudeServer)
    """
    try:
        local_port = create_video_tunnel(vm_ip, video_port)
        return {
            "status": "connected",
            "vm_ip": vm_ip,
            "video_port": video_port,
            "local_port": local_port,
            "connection_uri": f"127.0.0.1:{local_port}"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Video tunnel error: {str(e)}")

@app.post("/video/disconnect")
def disconnect_video_stream(vm_ip: str = Body(...), video_port: int = Body(default=55554)):
    """
    Закрыть SSH туннель видеопотока.
    
    vm_ip: IP адрес ВМ
    video_port: порт видеопотока на ВМ
    """
    global video_tunnels
    tunnel_key = f"{vm_ip}:{video_port}"
    
    if tunnel_key in video_tunnels:
        proc, local_port = video_tunnels[tunnel_key]
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        del video_tunnels[tunnel_key]
        return {"status": "disconnected", "vm_ip": vm_ip, "video_port": video_port}
    else:
        return {"status": "not_connected", "vm_ip": vm_ip, "video_port": video_port}

@app.get("/video/status")
def get_video_status():
    """Получить статус всех активных видеопотоков"""
    tunnels_info = []
    for tunnel_key, (proc, local_port) in video_tunnels.items():
        vm_ip, video_port = tunnel_key.split(':')
        tunnels_info.append({
            "vm_ip": vm_ip,
            "video_port": int(video_port),
            "local_port": local_port,
            "active": proc.poll() is None
        })
    return {"active_tunnels": tunnels_info}

@app.get("/video/stream")
def video_stream(local_port: int = Query(default=9002)):
    """Получить видеопоток от CrystalCloudeServer и передать клиенту"""
    import socket
    from fastapi.responses import StreamingResponse
    
    def generate_stream():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect(('127.0.0.1', local_port))
            print(f"Connected to CrystalCloudServer on 127.0.0.1:{local_port}")
            
            chunk_count = 0
            while True:
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        print(f"Stream closed after {chunk_count} chunks")
                        break
                    chunk_count += 1
                    if chunk_count == 1:
                        print(f"First chunk received: {len(chunk)} bytes")
                    if chunk_count % 100 == 0:
                        print(f"Video stream: {chunk_count} chunks transmitted ({chunk_count * 4096 / 1024 / 1024:.2f} MB)")
                    yield chunk
                except socket.timeout:
                    print(f"Stream timeout after {chunk_count} chunks")
                    break
        except Exception as e:
            print(f"Stream error: {e}")
        finally:
            if sock:
                sock.close()
    
    return StreamingResponse(
        generate_stream(),
        media_type="application/octet-stream"
    )

@app.get("/rdp/stream")
def rdp_stream(local_port: int = Query(default=33188), username: str = Query(default=""), password: str = Query(default="")):
    """Получить видеопоток с RDP сессии через ffmpeg"""
    import subprocess
    from fastapi.responses import StreamingResponse
    
    def generate_rdp_stream():
        proc = None
        try:
            # Команда для запуска ffmpeg с подключением к RDP
            cmd = [
                "ffmpeg",
                "-f", "gdigrab",  # Захват экрана Windows
                "-i", f"rdp://127.0.0.1:{local_port}",  # Источник RDP
                "-c:v", "libx264",  # Кодек видео
                "-preset", "ultrafast",  # Скорость кодирования
                "-b:v", "2000k",  # Битрейт
                "-f", "h264",  # Формат вывода
                "-"  # Вывод в stdout
            ]
            
            print(f"Starting ffmpeg RDP stream from 127.0.0.1:{local_port}")
            
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=4096
            )
            
            chunk_count = 0
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    print(f"RDP stream closed after {chunk_count} chunks")
                    break
                chunk_count += 1
                if chunk_count == 1:
                    print(f"First chunk received: {len(chunk)} bytes")
                if chunk_count % 100 == 0:
                    print(f"RDP stream: {chunk_count} chunks transmitted ({chunk_count * 4096 / 1024 / 1024:.2f} MB)")
                yield chunk
                
        except FileNotFoundError:
            print("ERROR: ffmpeg not found. Install with: sudo apt-get install ffmpeg")
            yield b"ERROR: ffmpeg not installed\n"
        except Exception as e:
            print(f"RDP stream error: {e}")
            yield f"ERROR: {str(e)}\n".encode()
        finally:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    
    return StreamingResponse(
        generate_rdp_stream(),
        media_type="video/h264"
    )

@app.get("/rdp/launch")
def get_rdp_launch_url(vm_ip: str = Query(...), rdp_port: int = Query(default=3389), local_port: int = Query(default=33188), username: str = Query(default=""), password: str = Query(default="")):
    """Получить gcloud URL для запуска в gci-launcher"""
    
    # Генерировать gcloud URL для gci-launcher
    gcloud_url = f"gcloud://rdp?host=127.0.0.1&port={local_port}&vm_ip={vm_ip}&rdp_port={rdp_port}"
    if username:
        gcloud_url += f"&username={username}"
    if password:
        gcloud_url += f"&password={password}"
    
    return {
        "protocol": "RDP",
        "gcloud_url": gcloud_url,
        "launch_link": f'<a href="{gcloud_url}">Подключиться к ВМ</a>',
        "connection_string": f"mstsc /v:127.0.0.1:{local_port}",
        "rdp_file_content": f"""full address:s:127.0.0.1:{local_port}
username:s:{username}
password:s:{password}
domain:s:
""",
        "instructions": [
            f"1. Открыть SSH туннель: ssh -o StrictHostKeyChecking=no -L {local_port}:{vm_ip}:{rdp_port} gate@212.192.123.3 -p 3003 -N",
            f"2. Кликнуть на ссылку: {gcloud_url}",
            f"3. Или подключиться вручную: mstsc /v:127.0.0.1:{local_port}"
        ],
        "tunnel_info": {
            "vm_ip": vm_ip,
            "rdp_port": rdp_port,
            "local_port": local_port,
            "ssh_command": f"ssh -o StrictHostKeyChecking=no -L {local_port}:{vm_ip}:{rdp_port} gate@212.192.123.3 -p 3003 -N"
        }
    }

@app.get("/video/launch")
def get_launch_url(vm_ip: str = Query(...), video_port: int = Query(default=55554), local_port: int = Query(default=9002)):
    """Получить gcloud:// URL для запуска в gcl-launcher"""
    import urllib.parse
    
    gcloud_url = f"gcloud://connect?host=127.0.0.1&port={local_port}&stream_url=http://sinthy.fvds.ru:9922/video/stream?local_port={local_port}"
    
    return {
        "gcloud_url": gcloud_url,
        "launch_link": f'<a href="{gcloud_url}">Запустить видеопоток</a>',
        "instructions": "Кликни на ссылку выше или скопируй URL в адресную строку",
        "tunnel_info": {
            "vm_ip": vm_ip,
            "video_port": video_port,
            "local_port": local_port,
            "stream_endpoint": f"http://sinthy.fvds.ru:9922/video/stream?local_port={local_port}"
        }
    }

@app.get("/video/analyze")
def analyze_video_stream(local_port: int = Query(default=9001)):
    """Анализировать формат видеопотока CrystalCloudeServer через готовый туннель"""
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        sock.connect(('127.0.0.1', local_port))
        
        data = sock.recv(512)
        sock.close()
        
        if not data:
            return {"error": "No data received"}
        
        hex_data = data[:64].hex()
        text_data = data.decode('utf-8', errors='ignore')[:200]
        
        analysis = {
            "data_length": len(data),
            "first_64_bytes_hex": hex_data,
            "first_200_chars_text": text_data,
            "detected_format": detect_format(data),
            "tunnel_info": {
                "local_port": local_port,
                "note": "Используется готовый SSH туннель"
            }
        }
        return analysis
    except socket.timeout:
        return {"error": f"Connection timeout on port {local_port} - check if SSH tunnel is running"}
    except ConnectionRefusedError:
        return {"error": f"Connection refused on port {local_port} - open SSH tunnel first"}
    except Exception as e:
        return {"error": str(e)}

def detect_format(data: bytes) -> dict:
    """Определить формат видеопотока"""
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

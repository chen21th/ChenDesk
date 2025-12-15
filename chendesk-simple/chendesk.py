"""
ChenDesk Simple - โคตรง่าย Remote Desktop สำหรับ Windows
- Auto-discover เครื่องใน LAN
- ไม่มี password
- Adaptive screen
- ลากวางไฟล์ได้
"""

import socket
import threading
import struct
import io
import os
import sys
import time
import json
import zlib
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageTk
import mss
import numpy as np
import cv2
from pynput import mouse, keyboard
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

# ==================== CONFIG ====================
APP_NAME = "ChenDesk"
SERVICE_TYPE = "_chendesk._tcp.local."
SCREEN_PORT = 5900
CONTROL_PORT = 5901
FILE_PORT = 5902
DISCOVERY_PORT = 5903
BUFFER_SIZE = 65536
QUALITY = 50  # JPEG quality (1-100)
FPS = 30

# ==================== NETWORK DISCOVERY ====================
class LANDiscovery:
    """Auto-discover ChenDesk instances on LAN"""
    
    def __init__(self, on_found=None, on_removed=None):
        self.on_found = on_found
        self.on_removed = on_removed
        self.peers = {}  # {name: (ip, hostname)}
        self.zeroconf = None
        self.browser = None
        self.running = False
        
    def start(self):
        """Start discovery service"""
        self.running = True
        self.zeroconf = Zeroconf()
        self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, self)
        
    def stop(self):
        """Stop discovery service"""
        self.running = False
        if self.browser:
            self.browser.cancel()
        if self.zeroconf:
            self.zeroconf.close()
            
    def add_service(self, zc, type_, name):
        """Called when a service is discovered"""
        info = zc.get_service_info(type_, name)
        if info:
            ip = socket.inet_ntoa(info.addresses[0])
            hostname = info.properties.get(b'hostname', b'Unknown').decode()
            self.peers[name] = (ip, hostname)
            if self.on_found:
                self.on_found(name, ip, hostname)
                
    def remove_service(self, zc, type_, name):
        """Called when a service is removed"""
        if name in self.peers:
            del self.peers[name]
            if self.on_removed:
                self.on_removed(name)
                
    def update_service(self, zc, type_, name):
        """Called when a service is updated"""
        self.add_service(zc, type_, name)


class ServiceAnnouncer:
    """Announce this ChenDesk instance on LAN"""
    
    def __init__(self):
        self.zeroconf = None
        self.info = None
        
    def start(self):
        """Start announcing service"""
        self.zeroconf = Zeroconf()
        hostname = socket.gethostname()
        local_ip = self._get_local_ip()
        
        self.info = ServiceInfo(
            SERVICE_TYPE,
            f"{hostname}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=SCREEN_PORT,
            properties={'hostname': hostname},
        )
        self.zeroconf.register_service(self.info)
        print(f"📢 Announcing ChenDesk on {local_ip}")
        
    def stop(self):
        """Stop announcing"""
        if self.zeroconf and self.info:
            self.zeroconf.unregister_service(self.info)
            self.zeroconf.close()
            
    def _get_local_ip(self):
        """Get local IP address"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        finally:
            s.close()


# ==================== SCREEN CAPTURE & STREAMING ====================
class ScreenServer:
    """Server that streams screen to clients"""
    
    def __init__(self):
        self.running = False
        self.clients = []
        self.server_socket = None
        
    def start(self):
        """Start screen server"""
        self.running = True
        
        # Start server socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', SCREEN_PORT))
        self.server_socket.listen(5)
        
        # Accept clients thread
        threading.Thread(target=self._accept_clients, daemon=True).start()
        # Stream thread
        threading.Thread(target=self._stream_screen, daemon=True).start()
        
        print(f"🖥️ Screen server started on port {SCREEN_PORT}")
        
    def stop(self):
        """Stop screen server"""
        self.running = False
        for client in self.clients:
            try:
                client.close()
            except:
                pass
        if self.server_socket:
            self.server_socket.close()
            
    def _accept_clients(self):
        """Accept incoming client connections"""
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                self.clients.append(client)
                print(f"👤 Client connected: {addr}")
            except:
                break
                
    def _stream_screen(self):
        """Capture and stream screen"""
        # Create mss instance inside the thread (required for Windows)
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            
            while self.running:
                try:
                    # Capture screen
                    img = sct.grab(monitor)
                    frame = np.array(img)
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    
                    # Resize for bandwidth (adaptive)
                    h, w = frame.shape[:2]
                    scale = min(1.0, 1920 / w)  # Max 1920px width
                    if scale < 1.0:
                        frame = cv2.resize(frame, None, fx=scale, fy=scale)
                    
                    # Encode as JPEG
                    _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, QUALITY])
                    data = zlib.compress(buffer.tobytes(), 1)
                    
                    # Send to all clients
                    header = struct.pack('!II', len(data), int(scale * 100))
                    dead_clients = []
                    
                    for client in self.clients:
                        try:
                            client.sendall(header + data)
                        except:
                            dead_clients.append(client)
                            
                    # Remove dead clients
                    for client in dead_clients:
                        self.clients.remove(client)
                        
                    time.sleep(1 / FPS)
                    
                except Exception as e:
                    if self.running:
                        print(f"Stream error: {e}")
                    time.sleep(0.1)


class ScreenClient:
    """Client that receives screen stream"""
    
    def __init__(self, on_frame=None):
        self.on_frame = on_frame
        self.running = False
        self.socket = None
        
    def connect(self, ip):
        """Connect to screen server"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((ip, SCREEN_PORT))
        self.running = True
        threading.Thread(target=self._receive_stream, daemon=True).start()
        print(f"📺 Connected to screen server at {ip}")
        
    def disconnect(self):
        """Disconnect from server"""
        self.running = False
        if self.socket:
            self.socket.close()
            
    def _receive_stream(self):
        """Receive and decode screen stream"""
        while self.running:
            try:
                # Read header
                header = self._recv_exact(8)
                if not header:
                    break
                size, scale = struct.unpack('!II', header)
                
                # Read frame data
                data = self._recv_exact(size)
                if not data:
                    break
                    
                # Decompress and decode
                buffer = zlib.decompress(data)
                nparr = np.frombuffer(buffer, np.uint8)
                frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                if self.on_frame and frame is not None:
                    self.on_frame(frame)
                    
            except Exception as e:
                print(f"Receive error: {e}")
                break
                
    def _recv_exact(self, size):
        """Receive exact number of bytes"""
        data = b''
        while len(data) < size:
            chunk = self.socket.recv(min(size - len(data), BUFFER_SIZE))
            if not chunk:
                return None
            data += chunk
        return data


# ==================== REMOTE CONTROL ====================
class ControlServer:
    """Server that receives and executes control commands"""
    
    def __init__(self):
        self.running = False
        self.server_socket = None
        self.mouse_ctrl = mouse.Controller()
        self.keyboard_ctrl = keyboard.Controller()
        
    def start(self):
        """Start control server"""
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', CONTROL_PORT))
        self.server_socket.listen(5)
        
        threading.Thread(target=self._accept_clients, daemon=True).start()
        print(f"🎮 Control server started on port {CONTROL_PORT}")
        
    def stop(self):
        """Stop control server"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
            
    def _accept_clients(self):
        """Accept control clients"""
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
            except:
                break
                
    def _handle_client(self, client):
        """Handle control commands from client"""
        while self.running:
            try:
                data = client.recv(BUFFER_SIZE)
                if not data:
                    break
                    
                cmd = json.loads(data.decode())
                self._execute_command(cmd)
                
            except:
                break
        client.close()
        
    def _execute_command(self, cmd):
        """Execute a control command"""
        try:
            if cmd['type'] == 'mouse_move':
                self.mouse_ctrl.position = (cmd['x'], cmd['y'])
                
            elif cmd['type'] == 'mouse_click':
                btn = mouse.Button.left if cmd['button'] == 'left' else mouse.Button.right
                if cmd['action'] == 'press':
                    self.mouse_ctrl.press(btn)
                else:
                    self.mouse_ctrl.release(btn)
                    
            elif cmd['type'] == 'mouse_scroll':
                self.mouse_ctrl.scroll(cmd['dx'], cmd['dy'])
                
            elif cmd['type'] == 'key':
                key = self._parse_key(cmd['key'])
                if cmd['action'] == 'press':
                    self.keyboard_ctrl.press(key)
                else:
                    self.keyboard_ctrl.release(key)
                    
        except Exception as e:
            print(f"Control error: {e}")
            
    def _parse_key(self, key_str):
        """Parse key string to pynput key"""
        special_keys = {
            'shift': keyboard.Key.shift,
            'ctrl': keyboard.Key.ctrl,
            'alt': keyboard.Key.alt,
            'enter': keyboard.Key.enter,
            'backspace': keyboard.Key.backspace,
            'tab': keyboard.Key.tab,
            'escape': keyboard.Key.esc,
            'space': keyboard.Key.space,
            'up': keyboard.Key.up,
            'down': keyboard.Key.down,
            'left': keyboard.Key.left,
            'right': keyboard.Key.right,
            'delete': keyboard.Key.delete,
            'home': keyboard.Key.home,
            'end': keyboard.Key.end,
            'page_up': keyboard.Key.page_up,
            'page_down': keyboard.Key.page_down,
        }
        
        # F keys
        for i in range(1, 13):
            special_keys[f'f{i}'] = getattr(keyboard.Key, f'f{i}')
            
        return special_keys.get(key_str.lower(), key_str)


class ControlClient:
    """Client that sends control commands"""
    
    def __init__(self):
        self.socket = None
        self.connected = False
        self.scale = 1.0  # Screen scale factor
        
    def connect(self, ip):
        """Connect to control server"""
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.connect((ip, CONTROL_PORT))
        self.connected = True
        print(f"🎮 Connected to control server at {ip}")
        
    def disconnect(self):
        """Disconnect from server"""
        self.connected = False
        if self.socket:
            self.socket.close()
            
    def send_mouse_move(self, x, y):
        """Send mouse move command"""
        if self.connected:
            self._send({'type': 'mouse_move', 'x': int(x / self.scale), 'y': int(y / self.scale)})
            
    def send_mouse_click(self, button, action):
        """Send mouse click command"""
        if self.connected:
            self._send({'type': 'mouse_click', 'button': button, 'action': action})
            
    def send_mouse_scroll(self, dx, dy):
        """Send mouse scroll command"""
        if self.connected:
            self._send({'type': 'mouse_scroll', 'dx': dx, 'dy': dy})
            
    def send_key(self, key, action):
        """Send key command"""
        if self.connected:
            self._send({'type': 'key', 'key': key, 'action': action})
            
    def _send(self, cmd):
        """Send command to server"""
        try:
            self.socket.sendall(json.dumps(cmd).encode())
        except:
            self.connected = False


# ==================== FILE TRANSFER ====================
class FileServer:
    """Server that receives files"""
    
    def __init__(self, save_dir=None):
        self.save_dir = save_dir or str(Path.home() / "Desktop" / "ChenDesk_Files")
        self.running = False
        self.server_socket = None
        os.makedirs(self.save_dir, exist_ok=True)
        
    def start(self):
        """Start file server"""
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', FILE_PORT))
        self.server_socket.listen(5)
        
        threading.Thread(target=self._accept_files, daemon=True).start()
        print(f"📁 File server started on port {FILE_PORT}, saving to {self.save_dir}")
        
    def stop(self):
        """Stop file server"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
            
    def _accept_files(self):
        """Accept file transfers"""
        while self.running:
            try:
                client, addr = self.server_socket.accept()
                threading.Thread(target=self._receive_file, args=(client,), daemon=True).start()
            except:
                break
                
    def _receive_file(self, client):
        """Receive a file from client"""
        try:
            # Receive header (filename length + file size)
            header = client.recv(8)
            name_len, file_size = struct.unpack('!II', header)
            
            # Receive filename
            filename = client.recv(name_len).decode()
            
            # Receive file data
            filepath = os.path.join(self.save_dir, filename)
            received = 0
            
            with open(filepath, 'wb') as f:
                while received < file_size:
                    chunk = client.recv(min(BUFFER_SIZE, file_size - received))
                    if not chunk:
                        break
                    f.write(chunk)
                    received += len(chunk)
                    
            print(f"📥 Received file: {filename} ({file_size} bytes)")
            
        except Exception as e:
            print(f"File receive error: {e}")
        finally:
            client.close()


class FileClient:
    """Client that sends files"""
    
    def send_file(self, ip, filepath):
        """Send a file to server"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((ip, FILE_PORT))
            
            filename = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)
            
            # Send header
            header = struct.pack('!II', len(filename.encode()), file_size)
            sock.sendall(header)
            sock.sendall(filename.encode())
            
            # Send file data
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    sock.sendall(chunk)
                    
            print(f"📤 Sent file: {filename} ({file_size} bytes)")
            sock.close()
            return True
            
        except Exception as e:
            print(f"File send error: {e}")
            return False


# ==================== GUI APPLICATION ====================
class ChenDeskApp(ctk.CTk):
    """Main ChenDesk Application"""
    
    def __init__(self):
        super().__init__()
        
        # Window setup
        self.title(f"{APP_NAME} - Remote Desktop ง่ายๆ")
        self.geometry("1200x800")
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        
        # State
        self.connected = False
        self.current_peer = None
        
        # Services
        self.announcer = ServiceAnnouncer()
        self.discovery = LANDiscovery(
            on_found=self._on_peer_found,
            on_removed=self._on_peer_removed
        )
        self.screen_server = ScreenServer()
        self.control_server = ControlServer()
        self.file_server = FileServer()
        
        # Clients (when connecting to remote)
        self.screen_client = None
        self.control_client = None
        self.file_client = FileClient()
        
        # Build UI
        self._build_ui()
        
        # Start services
        self._start_services()
        
        # Cleanup on close
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
    def _build_ui(self):
        """Build the user interface"""
        # Main container
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        
        # === Left Panel (Peers List) ===
        left_frame = ctk.CTkFrame(self, width=250)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        left_frame.grid_propagate(False)
        
        # Title
        title_label = ctk.CTkLabel(left_frame, text="🖥️ เครื่องใน LAN", font=("Segoe UI", 18, "bold"))
        title_label.pack(pady=15)
        
        # My info
        my_ip = self._get_local_ip()
        my_hostname = socket.gethostname()
        my_info = ctk.CTkLabel(left_frame, text=f"📍 คุณ: {my_hostname}\n    IP: {my_ip}", 
                               font=("Segoe UI", 12), text_color="gray")
        my_info.pack(pady=5)
        
        # Peers list
        self.peers_frame = ctk.CTkScrollableFrame(left_frame, label_text="เครื่องที่พบ")
        self.peers_frame.pack(fill="both", expand=True, padx=10, pady=10)
        
        # Refresh button
        refresh_btn = ctk.CTkButton(left_frame, text="🔄 ค้นหาใหม่", command=self._refresh_peers)
        refresh_btn.pack(pady=10)
        
        # === Right Panel (Remote Screen) ===
        right_frame = ctk.CTkFrame(self)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=10, pady=10)
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(1, weight=1)
        
        # Connection status
        self.status_label = ctk.CTkLabel(right_frame, text="⏳ รอเชื่อมต่อ...", 
                                          font=("Segoe UI", 14))
        self.status_label.grid(row=0, column=0, pady=10)
        
        # Screen canvas
        self.screen_frame = ctk.CTkFrame(right_frame, fg_color="black")
        self.screen_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        
        self.screen_label = ctk.CTkLabel(self.screen_frame, text="")
        self.screen_label.pack(fill="both", expand=True)
        
        # Bind mouse/keyboard events
        self.screen_label.bind("<Motion>", self._on_mouse_move)
        self.screen_label.bind("<Button-1>", lambda e: self._on_mouse_click(e, 'left', 'press'))
        self.screen_label.bind("<ButtonRelease-1>", lambda e: self._on_mouse_click(e, 'left', 'release'))
        self.screen_label.bind("<Button-3>", lambda e: self._on_mouse_click(e, 'right', 'press'))
        self.screen_label.bind("<ButtonRelease-3>", lambda e: self._on_mouse_click(e, 'right', 'release'))
        self.screen_label.bind("<MouseWheel>", self._on_mouse_scroll)
        
        self.bind("<KeyPress>", self._on_key_press)
        self.bind("<KeyRelease>", self._on_key_release)
        
        # Bottom toolbar
        toolbar = ctk.CTkFrame(right_frame)
        toolbar.grid(row=2, column=0, sticky="ew", pady=10)
        
        # Disconnect button
        self.disconnect_btn = ctk.CTkButton(toolbar, text="❌ ยกเลิกการเชื่อมต่อ", 
                                            command=self._disconnect, state="disabled")
        self.disconnect_btn.pack(side="left", padx=10)
        
        # File transfer button
        self.file_btn = ctk.CTkButton(toolbar, text="📁 ส่งไฟล์", 
                                       command=self._send_file, state="disabled")
        self.file_btn.pack(side="left", padx=10)
        
        # Enable drag and drop
        self._setup_drag_drop()
        
    def _setup_drag_drop(self):
        """Setup drag and drop for files"""
        try:
            # Using tkinter dnd
            self.drop_target_register('DND_Files')
            self.dnd_bind('<<Drop>>', self._on_file_drop)
        except:
            # Fallback if TkDnD not available
            pass
            
    def _on_file_drop(self, event):
        """Handle file drop"""
        if self.connected and self.current_peer:
            files = self.tk.splitlist(event.data)
            for f in files:
                self.file_client.send_file(self.current_peer[0], f)
                
    def _start_services(self):
        """Start all background services"""
        self.announcer.start()
        self.discovery.start()
        self.screen_server.start()
        self.control_server.start()
        self.file_server.start()
        
    def _stop_services(self):
        """Stop all background services"""
        self.announcer.stop()
        self.discovery.stop()
        self.screen_server.stop()
        self.control_server.stop()
        self.file_server.stop()
        
    def _on_peer_found(self, name, ip, hostname):
        """Called when a peer is found"""
        self.after(0, lambda: self._add_peer_button(name, ip, hostname))
        
    def _on_peer_removed(self, name):
        """Called when a peer is removed"""
        self.after(0, self._refresh_peer_list)
        
    def _add_peer_button(self, name, ip, hostname):
        """Add a peer button to the list"""
        btn = ctk.CTkButton(
            self.peers_frame, 
            text=f"🖥️ {hostname}\n{ip}",
            font=("Segoe UI", 12),
            height=60,
            command=lambda: self._connect_to_peer(ip, hostname)
        )
        btn.pack(fill="x", pady=5)
        btn._peer_name = name
        
    def _refresh_peer_list(self):
        """Refresh the peer list UI"""
        for widget in self.peers_frame.winfo_children():
            widget.destroy()
        for name, (ip, hostname) in self.discovery.peers.items():
            self._add_peer_button(name, ip, hostname)
            
    def _refresh_peers(self):
        """Refresh peer discovery"""
        self.discovery.stop()
        for widget in self.peers_frame.winfo_children():
            widget.destroy()
        self.discovery = LANDiscovery(
            on_found=self._on_peer_found,
            on_removed=self._on_peer_removed
        )
        self.discovery.start()
        
    def _connect_to_peer(self, ip, hostname):
        """Connect to a remote peer"""
        try:
            # Create clients
            self.screen_client = ScreenClient(on_frame=self._on_frame_received)
            self.control_client = ControlClient()
            
            # Connect
            self.screen_client.connect(ip)
            self.control_client.connect(ip)
            
            self.connected = True
            self.current_peer = (ip, hostname)
            
            # Update UI
            self.status_label.configure(text=f"✅ เชื่อมต่อกับ {hostname} ({ip})")
            self.disconnect_btn.configure(state="normal")
            self.file_btn.configure(state="normal")
            
        except Exception as e:
            self.status_label.configure(text=f"❌ เชื่อมต่อไม่ได้: {e}")
            
    def _disconnect(self):
        """Disconnect from current peer"""
        if self.screen_client:
            self.screen_client.disconnect()
        if self.control_client:
            self.control_client.disconnect()
            
        self.connected = False
        self.current_peer = None
        
        self.status_label.configure(text="⏳ รอเชื่อมต่อ...")
        self.disconnect_btn.configure(state="disabled")
        self.file_btn.configure(state="disabled")
        self.screen_label.configure(image=None)
        
    def _on_frame_received(self, frame):
        """Handle received frame"""
        try:
            # Convert BGR to RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Get display size
            display_w = self.screen_frame.winfo_width()
            display_h = self.screen_frame.winfo_height()
            
            if display_w > 1 and display_h > 1:
                # Calculate scale to fit
                h, w = frame_rgb.shape[:2]
                scale = min(display_w / w, display_h / h)
                new_w = int(w * scale)
                new_h = int(h * scale)
                
                # Resize
                frame_resized = cv2.resize(frame_rgb, (new_w, new_h))
                
                # Update control client scale
                if self.control_client:
                    self.control_client.scale = scale
                
                # Convert to PIL Image
                img = Image.fromarray(frame_resized)
                photo = ctk.CTkImage(light_image=img, dark_image=img, size=(new_w, new_h))
                
                # Update UI (must be in main thread)
                self.after(0, lambda: self.screen_label.configure(image=photo))
                self.screen_label._photo = photo  # Keep reference
                
        except Exception as e:
            pass
            
    def _on_mouse_move(self, event):
        """Handle mouse move"""
        if self.connected and self.control_client:
            self.control_client.send_mouse_move(event.x, event.y)
            
    def _on_mouse_click(self, event, button, action):
        """Handle mouse click"""
        if self.connected and self.control_client:
            self.control_client.send_mouse_click(button, action)
            
    def _on_mouse_scroll(self, event):
        """Handle mouse scroll"""
        if self.connected and self.control_client:
            dy = 1 if event.delta > 0 else -1
            self.control_client.send_mouse_scroll(0, dy)
            
    def _on_key_press(self, event):
        """Handle key press"""
        if self.connected and self.control_client:
            key = self._tk_key_to_str(event)
            if key:
                self.control_client.send_key(key, 'press')
                
    def _on_key_release(self, event):
        """Handle key release"""
        if self.connected and self.control_client:
            key = self._tk_key_to_str(event)
            if key:
                self.control_client.send_key(key, 'release')
                
    def _tk_key_to_str(self, event):
        """Convert tkinter key event to string"""
        special = {
            'Shift_L': 'shift', 'Shift_R': 'shift',
            'Control_L': 'ctrl', 'Control_R': 'ctrl',
            'Alt_L': 'alt', 'Alt_R': 'alt',
            'Return': 'enter',
            'BackSpace': 'backspace',
            'Tab': 'tab',
            'Escape': 'escape',
            'space': 'space',
            'Up': 'up', 'Down': 'down', 'Left': 'left', 'Right': 'right',
            'Delete': 'delete',
            'Home': 'home', 'End': 'end',
            'Prior': 'page_up', 'Next': 'page_down',
        }
        
        # F keys
        for i in range(1, 13):
            special[f'F{i}'] = f'f{i}'
            
        keysym = event.keysym
        if keysym in special:
            return special[keysym]
        elif len(event.char) == 1:
            return event.char
        return None
        
    def _send_file(self):
        """Open file dialog and send file"""
        if self.connected and self.current_peer:
            from tkinter import filedialog
            filepath = filedialog.askopenfilename(title="เลือกไฟล์ที่จะส่ง")
            if filepath:
                if self.file_client.send_file(self.current_peer[0], filepath):
                    self.status_label.configure(text=f"✅ ส่งไฟล์สำเร็จ!")
                else:
                    self.status_label.configure(text=f"❌ ส่งไฟล์ไม่สำเร็จ")
                    
    def _get_local_ip(self):
        """Get local IP address"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        finally:
            s.close()
            
    def _on_close(self):
        """Handle window close"""
        self._disconnect()
        self._stop_services()
        self.destroy()


# ==================== MAIN ====================
if __name__ == "__main__":
    print(f"""
╔═══════════════════════════════════════════════════╗
║         🖥️  ChenDesk Simple Remote Desktop         ║
║         ───────────────────────────────            ║
║  • Auto-discover เครื่องใน LAN                     ║
║  • ไม่ต้องใส่ password                              ║
║  • ลากวางไฟล์ได้                                    ║
╚═══════════════════════════════════════════════════╝
    """)
    
    app = ChenDeskApp()
    app.mainloop()

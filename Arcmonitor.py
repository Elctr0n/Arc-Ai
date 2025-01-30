import psutil
import platform
import cv2
import mss
import numpy as np
from flask import Flask, jsonify, render_template_string, send_from_directory, request
from threading import Thread, Event, Lock
from datetime import datetime, timedelta
from PIL import ImageGrab
import base64
import io
import os
import sqlite3
import time
import pytesseract

# ================== CONFIGURATION ================== #
DATA_DIR = "JARVIS_DATA"
SCREENSHOT_INTERVAL_ACTIVE = 30
INACTIVITY_THRESHOLD = 60
VIDEO_INTERVAL = 1800
VIDEO_DURATION = 10
DB_PATH = os.path.join(DATA_DIR, 'system_data.db')
OCR_ENABLED = True

DB_SCHEMA = {
    'system': '''
        CREATE TABLE IF NOT EXISTS metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            cpu REAL,
            memory REAL,
            network_sent INTEGER,
            network_recv INTEGER
        )''',
    'activities': '''
        CREATE TABLE IF NOT EXISTS activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            window_title TEXT,
            process_name TEXT,
            screenshot BLOB,
            ocr_text TEXT,
            active_session BOOLEAN
        )''',
    'videos': '''
        CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME,
            duration INTEGER,
            file_path TEXT
        )'''
}

# ================== DATABASE HANDLER ================== #
class Database:
    _instance = None
    _lock = Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance.init_db()
            return cls._instance

    def init_db(self):
        self.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        for table, schema in DB_SCHEMA.items():
            self.conn.execute(schema)
        self.conn.commit()

    def get_conn(self):
        return self.conn

# ================== CORE MONITOR CLASS ================== #
class JarvisMonitor:
    def __init__(self):
        self.stop_event = Event()
        self.db_lock = Lock()
        self.monitoring_lock = Lock()
        self.monitoring_enabled = True
        self.last_activity = datetime.now()
        self.last_screenshot = datetime.now()
        self.last_video = datetime.now()
        self.current_window = ("", "")
        
        self.setup_environment()
        self.db = Database().get_conn()
        
        self.app = Flask(__name__)
        self.setup_routes()
        
        self.server_thread = Thread(target=self.run_server)
        self.server_thread.start()
        
        Thread(target=self.monitor_system).start()
        Thread(target=self.monitor_activities).start()
        Thread(target=self.monitor_videos).start()

    def setup_environment(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "screenshots"), exist_ok=True)
        os.makedirs(os.path.join(DATA_DIR, "videos"), exist_ok=True)

    def setup_routes(self):
        self.app.add_url_rule('/', 'dashboard', self.show_dashboard)
        self.app.add_url_rule('/data', 'system_data', self.get_system_data)
        self.app.add_url_rule('/history', 'activity_history', self.get_activity_history)
        self.app.add_url_rule('/media-list', 'media_list', self.get_media_list)
        self.app.add_url_rule('/screenshots/<path:filename>', 'get_screenshot', self.get_screenshot)
        self.app.add_url_rule('/videos/<path:filename>', 'get_video', self.get_video)
        self.app.add_url_rule('/control', 'control', self.control_monitoring, methods=['POST'])
        self.app.add_url_rule('/status', 'status', self.get_status)

    def control_monitoring(self):
        action = request.json.get('action')
        if action == 'toggle':
            self.toggle_monitoring()
        return jsonify({'status': self.monitoring_enabled})

    def get_status(self):
        return jsonify({
            'monitoring': self.monitoring_enabled,
            'ocr_enabled': OCR_ENABLED
        })

    def toggle_monitoring(self):
        with self.monitoring_lock:
            self.monitoring_enabled = not self.monitoring_enabled

    def get_screenshot(self, filename):
        return send_from_directory(os.path.join(DATA_DIR, "screenshots"), filename)

    def get_video(self, filename):
        return send_from_directory(os.path.join(DATA_DIR, "videos"), filename)

    def run_server(self):
        self.app.run(host='0.0.0.0', port=5000, debug=False)

    # ================== MONITORING FUNCTIONS ================== #
    def get_active_window(self):
        try:
            if platform.system() == 'Windows':
                import win32gui
                hwnd = win32gui.GetForegroundWindow()
                title = win32gui.GetWindowText(hwnd)
                return title, "Windows Process"
            else:
                return "Terminal", "bash"
        except:
            return "Unknown", "Unknown"

    def check_activity(self):
        current_window = self.get_active_window()
        if current_window != self.current_window:
            self.current_window = current_window
            self.last_activity = datetime.now()
            return True
        return False

    def process_ocr(self, image):
        try:
            if OCR_ENABLED:
                text = pytesseract.image_to_string(image.convert('L'))
                return text.strip()
            return ""
        except Exception as e:
            print(f"OCR Error: {e}")
            return ""

    def capture_screenshot(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"screenshot_{timestamp}.jpg"
            path = os.path.join(DATA_DIR, "screenshots", filename)
            
            img = ImageGrab.grab()
            img.save(path, "JPEG")
            ocr_text = self.process_ocr(img)
            
            img.thumbnail((320, 240))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG")
            b64_img = base64.b64encode(buffer.getvalue()).decode()
            
            return path, b64_img, ocr_text
        except Exception as e:
            print(f"Screenshot error: {e}")
            return None, None, ""

    def monitor_activities(self):
        while not self.stop_event.is_set():
            try:
                with self.monitoring_lock:
                    if not self.monitoring_enabled:
                        time.sleep(1)
                        continue
                
                activity_detected = self.check_activity()
                now = datetime.now()
                inactive_time = (now - self.last_activity).total_seconds()
                
                if inactive_time < INACTIVITY_THRESHOLD:
                    if (now - self.last_screenshot).total_seconds() >= SCREENSHOT_INTERVAL_ACTIVE:
                        path, b64_img, ocr_text = self.capture_screenshot()
                        if b64_img:
                            with self.db_lock:
                                self.db.execute('''
                                    INSERT INTO activities 
                                    (timestamp, window_title, process_name, 
                                     screenshot, ocr_text, active_session)
                                    VALUES (?, ?, ?, ?, ?, ?)
                                ''', (now.isoformat(), 
                                     self.current_window[0], 
                                     self.current_window[1], 
                                     b64_img,
                                     ocr_text,
                                     True))
                                self.db.commit()
                            self.last_screenshot = now
                
                time.sleep(1)
            except Exception as e:
                print(f"Activity monitoring error: {e}")

    def monitor_system(self):
        while not self.stop_event.is_set():
            try:
                with self.monitoring_lock:
                    if not self.monitoring_enabled:
                        time.sleep(1)
                        continue
                
                timestamp = datetime.now().isoformat()
                cpu = psutil.cpu_percent()
                mem = psutil.virtual_memory().percent
                net = psutil.net_io_counters()
                
                with self.db_lock:
                    self.db.execute('''
                        INSERT INTO metrics 
                        (timestamp, cpu, memory, network_sent, network_recv)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (timestamp, cpu, mem, net.bytes_sent, net.bytes_recv))
                    self.db.commit()
                
                time.sleep(5)
            except Exception as e:
                print(f"System monitoring error: {e}")

    def record_video(self):
        try:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"recording_{timestamp}.mp4"
            path = os.path.join(DATA_DIR, "videos", filename)
            
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                out = cv2.VideoWriter(path, fourcc, 20.0, 
                                    (monitor["width"], monitor["height"]))
                
                start_time = time.time()
                while (time.time() - start_time) < VIDEO_DURATION:
                    frame = np.array(sct.grab(monitor))
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
                    out.write(frame)
                    time.sleep(0.04)
                    
                out.release()
                
                with self.db_lock:
                    self.db.execute('''
                        INSERT INTO videos 
                        (timestamp, duration, file_path)
                        VALUES (?, ?, ?)
                    ''', (datetime.now().isoformat(), 
                         VIDEO_DURATION, 
                         path))
                    self.db.commit()
                
                return path
        except Exception as e:
            print(f"Video recording error: {e}")
            return None

    def monitor_videos(self):
        while not self.stop_event.is_set():
            try:
                with self.monitoring_lock:
                    if not self.monitoring_enabled:
                        time.sleep(1)
                        continue
                
                if (datetime.now() - self.last_video).total_seconds() > VIDEO_INTERVAL:
                    self.record_video()
                    self.last_video = datetime.now()
                time.sleep(1)
            except Exception as e:
                print(f"Video scheduler error: {e}")

    # ================== WEB INTERFACE ================== #
    def get_system_data(self):
        return jsonify({
            'cpu': psutil.cpu_percent(),
            'memory': psutil.virtual_memory().percent,
            'network': psutil.net_io_counters()._asdict(),
            'current_window': self.current_window[0],
            'current_process': self.current_window[1],
            'inactive_time': (datetime.now() - self.last_activity).total_seconds()
        })

    def get_activity_history(self):
        with self.db_lock:
            cursor = self.db.execute('''
                SELECT timestamp, window_title, process_name, screenshot, ocr_text 
                FROM activities 
                WHERE active_session = 1
                ORDER BY timestamp DESC 
                LIMIT 10
            ''')
            results = []
            for row in cursor.fetchall():
                results.append({
                    'timestamp': row['timestamp'],
                    'window': row['window_title'],
                    'process': row['process_name'],
                    'screenshot': row['screenshot'],
                    'ocr_text': row['ocr_text']
                })
            return jsonify(results)

    def get_media_list(self):
        media = {
            'screenshots': sorted([
                f for f in os.listdir(os.path.join(DATA_DIR, "screenshots"))
                if f.endswith('.jpg')
            ], reverse=True)[:5],
            'videos': sorted([
                f for f in os.listdir(os.path.join(DATA_DIR, "videos"))
                if f.endswith('.mp4')
            ], reverse=True)[:3]
        }
        return jsonify(media)

    def show_dashboard(self):
        return render_template_string('''
            <!DOCTYPE html>
            <html>
            <head>
                <title>J.A.R.V.I.S Mobile</title>
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <style>
                    :root { --bg: #1a1a1a; --card-bg: #2d2d2d; }
                    body { font-family: Arial; margin: 0; padding: 20px; background: var(--bg); color: white; }
                    .card { background: var(--card-bg); border-radius: 10px; padding: 15px; margin: 10px 0; }
                    .grid { display: grid; gap: 15px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
                    img.thumbnail { width: 100%; border-radius: 8px; margin-top: 10px; }
                    video { width: 100%; border-radius: 8px; }
                    .status-item { display: flex; justify-content: space-between; padding: 8px 0; }
                    .toggle-btn { padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; 
                                font-weight: bold; margin-left: 20px; }
                    .monitoring-on { background: #4CAF50; color: white; }
                    .monitoring-off { background: #f44336; color: white; }
                    .ocr-status { color: #4CAF50; margin-left: 20px; }
                </style>
            </head>
            <body>
                <h1>🖥️ J.A.R.V.I.S Monitor 
                    <button id="monitoringToggle" class="toggle-btn"></button>
                    <span id="ocrStatus" class="ocr-status"></span>
                </h1>
                
                <div class="card">
                    <h2>Current Activity</h2>
                    <div id="current-activity"></div>
                </div>

                <div class="card">
                    <h2>System Status</h2>
                    <div id="system-status"></div>
                </div>

                <div class="card">
                    <h2>Recent Activities</h2>
                    <div id="activities"></div>
                </div>

                <div class="card">
                    <h2>Media</h2>
                    <div class="grid">
                        <div>
                            <h3>Screenshots</h3>
                            <div id="screenshots"></div>
                        </div>
                        <div>
                            <h3>Recordings</h3>
                            <div id="videos"></div>
                        </div>
                    </div>
                </div>

                <script>
                    function updateStatus() {
                        fetch('/data')
                            .then(r => r.json())
                            .then(data => {
                                document.getElementById('current-activity').innerHTML = `
                                    <div class="status-item">
                                        <span>📱 Current App:</span>
                                        <span>${data.current_window}</span>
                                    </div>
                                    <div class="status-item">
                                        <span>⚙️ Process:</span>
                                        <span>${data.current_process}</span>
                                    </div>
                                    <div class="status-item">
                                        <span>⏱️ Inactive Time:</span>
                                        <span>${Math.floor(data.inactive_time)}s</span>
                                    </div>`;

                                document.getElementById('system-status').innerHTML = `
                                    <div class="status-item">
                                        <span>🔥 CPU:</span>
                                        <span>${data.cpu.toFixed(1)}%</span>
                                    </div>
                                    <div class="status-item">
                                        <span>💾 Memory:</span>
                                        <span>${data.memory.toFixed(1)}%</span>
                                    </div>
                                    <div class="status-item">
                                        <span>📤 Network Sent:</span>
                                        <span>${(data.network.bytes_sent/1e6).toFixed(2)} MB</span>
                                    </div>`;
                            });
                    }

                    function loadActivities() {
                        fetch('/history')
                            .then(r => r.json())
                            .then(data => {
                                document.getElementById('activities').innerHTML = data
                                    .map(item => `
                                        <div class="card">
                                            <h3>${item.window}</h3>
                                            <p>${new Date(item.timestamp).toLocaleString()}</p>
                                            ${item.screenshot ? 
                                                `<img src="data:image/jpeg;base64,${item.screenshot}" 
                                                     class="thumbnail">` : ''}
                                            ${item.ocr_text ? `
                                                <div class="ocr-text">
                                                    <h4>Extracted Text:</h4>
                                                    <p>${item.ocr_text}</p>
                                                </div>` : ''}
                                        </div>
                                    `).join('');
                            });
                    }

                    function loadMedia() {
                        fetch('/media-list')
                            .then(r => r.json())
                            .then(data => {
                                document.getElementById('screenshots').innerHTML = data.screenshots
                                    .map(file => `<img src="/screenshots/${file}" class="thumbnail">`)
                                    .join('');
                                
                                document.getElementById('videos').innerHTML = data.videos
                                    .map(file => `
                                        <video controls class="thumbnail">
                                            <source src="/videos/${file}" type="video/mp4">
                                        </video>
                                    `).join('');
                            });
                    }

                    function updateToggleButton(status) {
                        const btn = document.getElementById('monitoringToggle');
                        btn.className = `toggle-btn ${status ? 'monitoring-on' : 'monitoring-off'}`;
                        btn.textContent = status ? 'Monitoring: ON' : 'Monitoring: OFF';
                    }

                    function toggleMonitoring() {
                        fetch('/control', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ action: 'toggle' })
                        })
                        .then(r => r.json())
                        .then(data => updateToggleButton(data.status));
                    }

                    // Initial load
                    fetch('/status')
                        .then(r => r.json())
                        .then(data => {
                            updateToggleButton(data.monitoring);
                            if(data.ocr_enabled) {
                                document.getElementById('ocrStatus').textContent = 'OCR: Enabled';
                            }
                        });

                    document.getElementById('monitoringToggle').addEventListener('click', toggleMonitoring);

                    // Auto-update every 3 seconds
                    setInterval(() => {
                        updateStatus();
                        loadActivities();
                        loadMedia();
                    }, 3000);
                </script>
            </body>
            </html>
        ''')

    def shutdown(self):
        self.stop_event.set()
        self.db.close()
        if self.server_thread.is_alive():
            self.server_thread.join(timeout=5)
        print("✅ System shutdown completed")
# Add at the top
import pystray
from PIL import Image

# Modify the __init__ method
def __init__(self):
    # ... existing code ...
    self.tray_icon = None
    self.create_tray_icon()

# Add tray icon methods
def create_tray_icon(self):
    image = Image.new('RGB', (64, 64), 'black')
    menu = pystray.Menu(
        pystray.MenuItem('Open Dashboard', self.open_dashboard),
        pystray.MenuItem('Exit', self.shutdown)
    )
    self.tray_icon = pystray.Icon("jarvis_icon", image, "JARVIS Monitor", menu)

def open_dashboard(self):
    import webbrowser
    webbrowser.open('http://localhost:5000')
if __name__ == "__main__":
    monitor = JarvisMonitor()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.shutdown()
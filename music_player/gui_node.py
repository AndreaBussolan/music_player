#!/usr/bin/env python3
"""
gui_node.py — Music Player Control Panel  (PyQt5)
====================================================

Control panel for the music_player system. Publishes to /music/command and
/music/volume, subscribes to /music/status, and shows a live per-host status
table plus a phase banner (IDLE / LOADING / PLAYING / PAUSED / STOPPED).

Requires PyQt5:
    sudo apt install python3-pyqt5

Usage:
    ros2 run music_player music_gui
"""
import json
import sys
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import String

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QTextEdit, QGroupBox, QFrame,
    QSpinBox, QSlider,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject


# ═══════════════════════════════════════════════════════════════════════════
#  Design tokens
# ═══════════════════════════════════════════════════════════════════════════

NAVY    = "#0D1B2A";  PANEL   = "#152030";  BORDER  = "#1E3048"
SUBTEXT = "#5B7FA6";  TEXT    = "#C8DCF0";  TEXT_HI = "#E8F4FF"
CYAN    = "#00C2FF";  GREEN   = "#22C55E";  AMBER   = "#F59E0B"
RED     = "#EF4444";  SLATE   = "#334155"
MONO    = "'Courier New','Lucida Console',monospace"

_BTN = """
QPushButton {{
    background:{bg}; color:{fg}; border:none; border-radius:5px;
    padding:9px 14px; font-size:12px; font-weight:700;
}}
QPushButton:hover    {{ background:{hv}; }}
QPushButton:disabled {{ background:#1E3048; color:#3A526B; }}
"""


def _lx(h, a=22):
    c = h.lstrip("#")
    r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    return f"#{min(255,r+a):02X}{min(255,g+a):02X}{min(255,b+a):02X}"


def mkbtn(label, bg, fg=NAVY, hv=None):
    b = QPushButton(label)
    b.setStyleSheet(_BTN.format(bg=bg, fg=fg, hv=hv or _lx(bg)))
    b.setMinimumHeight(40)
    b.setCursor(Qt.PointingHandCursor)
    return b


def card(title=""):
    g = QGroupBox(title)
    g.setStyleSheet(f"""
        QGroupBox {{
            background:{PANEL}; border:1px solid {BORDER};
            border-radius:8px; margin-top:14px; padding:10px;
            color:{SUBTEXT}; font-size:10px; font-weight:700; letter-spacing:1px;
        }}
        QGroupBox::title {{ subcontrol-origin:margin; left:12px; padding:0 6px; }}
    """)
    return g


def mklbl(text, size=11, color=SUBTEXT, bold=False):
    l = QLabel(text)
    l.setStyleSheet(
        f"color:{color}; font-size:{size}px;"
        f" font-weight:{'700' if bold else '400'};")
    return l


def mkfld(placeholder=""):
    e = QLineEdit()
    e.setPlaceholderText(placeholder)
    e.setStyleSheet(f"""
        QLineEdit {{
            background:{NAVY}; border:1px solid {BORDER}; border-radius:5px;
            color:{TEXT}; font-size:12px; padding:6px 10px;
        }}
        QLineEdit:focus {{ border-color:{CYAN}; }}
    """)
    return e


def div():
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color:{BORDER}; background:{BORDER}; max-height:1px;")
    return f


def frow(label_text, widget, lw=110):
    h = QHBoxLayout()
    l = mklbl(label_text, 11)
    l.setFixedWidth(lw)
    h.addWidget(l)
    h.addWidget(widget)
    return h


SPIN_STYLE = f"""
    QSpinBox {{
        background:{NAVY}; border:1px solid {BORDER};
        border-radius:5px; color:{TEXT};
        font-size:12px; padding:5px 8px;
    }}
"""

SLIDER_STYLE = f"""
    QSlider::groove:horizontal {{
        height:6px; background:{BORDER}; border-radius:3px;
    }}
    QSlider::sub-page:horizontal {{
        background:{CYAN}; border-radius:3px;
    }}
    QSlider::handle:horizontal {{
        background:{TEXT_HI}; width:16px; margin:-6px 0; border-radius:8px;
    }}
"""

PHASE_STATES = {
    "idle":    (SLATE, TEXT,    "IDLE"),
    "loading": (CYAN,  NAVY,    "LOADING"),
    "playing": (GREEN, NAVY,    "PLAYING"),
    "paused":  (AMBER, NAVY,    "PAUSED"),
    "stopped": (SLATE, TEXT,    "STOPPED"),
}


# ═══════════════════════════════════════════════════════════════════════════
#  Qt signals  (thread-safe bridge from the ROS callback thread to the GUI)
# ═══════════════════════════════════════════════════════════════════════════

class Signals(QObject):
    status_updated = pyqtSignal(str, str, float)  # host, status, t


# ═══════════════════════════════════════════════════════════════════════════
#  ROS2 node
# ═══════════════════════════════════════════════════════════════════════════

class MusicGuiNode(Node):
    def __init__(self, signals: Signals):
        super().__init__('music_gui')
        self.signals = signals

        command_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )
        volume_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        self.command_pub = self.create_publisher(String, '/music/command', command_qos)
        self.volume_pub = self.create_publisher(String, '/music/volume', volume_qos)
        self.create_subscription(String, '/music/status', self._on_status, command_qos)

    def send_play(self, query: str, lead: float):
        msg = String()
        msg.data = json.dumps({
            'action': 'play',
            'query': query,
            'start_at': time.time() + lead,
        })
        self.command_pub.publish(msg)

    def send_action(self, action: str):
        msg = String()
        msg.data = json.dumps({'action': action})
        self.command_pub.publish(msg)

    def send_volume(self, value: float):
        msg = String()
        msg.data = json.dumps({'value': value})
        self.volume_pub.publish(msg)

    def _on_status(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        host = payload.get('host', '?')
        status = payload.get('status', '')
        t = payload.get('t', time.time())
        # Cross-thread emit: safe, PyQt queues this onto the GUI thread
        # since Signals() was constructed there.
        self.signals.status_updated.emit(host, status, t)


# ═══════════════════════════════════════════════════════════════════════════
#  Main window
# ═══════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self, ros: MusicGuiNode):
        super().__init__()
        self.ros = ros
        self._status_by_host = {}

        ros.signals.status_updated.connect(self._on_status)

        self.setWindowTitle("Music Player Control")
        self.setMinimumSize(560, 640)
        self.setStyleSheet(
            f"background:{NAVY}; color:{TEXT};"
            f" font-family:'Segoe UI','Helvetica Neue',sans-serif;")

        self._build()
        self._phase("idle")

        self._ticker = QTimer()
        self._ticker.setInterval(500)
        self._ticker.timeout.connect(self._refresh_status_text)
        self._ticker.start()

    # ── build ────────────────────────────────────────────────────────────

    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.banner = QLabel("IDLE")
        self.banner.setAlignment(Qt.AlignCenter)
        self.banner.setFixedHeight(36)
        outer.addWidget(self.banner)

        body = QVBoxLayout()
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(12)
        outer.addLayout(body)

        # -- now playing --
        np_card = card("Now playing")
        np_l = QVBoxLayout(np_card)
        np_l.setSpacing(7)

        self.f_query = mkfld("YouTube URL / search / playlist link")
        self.f_query.returnPressed.connect(self._on_play)
        np_l.addWidget(self.f_query)

        self.sb_lead = QSpinBox()
        self.sb_lead.setRange(0, 30)
        self.sb_lead.setValue(3)
        self.sb_lead.setStyleSheet(SPIN_STYLE)
        np_l.addLayout(frow("Lead time (s)", self.sb_lead))

        self.btn_play = mkbtn("▶  Play", GREEN, NAVY)
        self.btn_play.clicked.connect(self._on_play)
        np_l.addWidget(self.btn_play)
        body.addWidget(np_card)

        # -- transport --
        tr_card = card("Transport")
        tr_l = QHBoxLayout(tr_card)
        tr_l.setSpacing(8)

        self.btn_pause = mkbtn("⏸  Pause", AMBER, NAVY)
        self.btn_resume = mkbtn("⏵  Resume", CYAN, NAVY)
        self.btn_skip = mkbtn("⏭  Skip", BORDER, TEXT)
        self.btn_stop = mkbtn("⏹  Stop", RED, TEXT_HI)

        self.btn_pause.clicked.connect(lambda: self.ros.send_action('pause'))
        self.btn_resume.clicked.connect(lambda: self.ros.send_action('resume'))
        self.btn_skip.clicked.connect(lambda: self.ros.send_action('skip'))
        self.btn_stop.clicked.connect(lambda: self.ros.send_action('stop'))

        for b in (self.btn_pause, self.btn_resume, self.btn_skip, self.btn_stop):
            tr_l.addWidget(b)
        body.addWidget(tr_card)

        # -- volume --
        vol_card = card("Volume  —  all nodes")
        vol_l = QHBoxLayout(vol_card)

        self.slider_vol = QSlider(Qt.Horizontal)
        self.slider_vol.setRange(0, 100)
        self.slider_vol.setValue(100)
        self.slider_vol.setStyleSheet(SLIDER_STYLE)
        self.slider_vol.valueChanged.connect(
            lambda v: self.lbl_vol.setText(str(v)))
        self.slider_vol.sliderReleased.connect(self._on_volume_commit)

        self.lbl_vol = mklbl("100", 12, TEXT_HI, bold=True)
        self.lbl_vol.setFixedWidth(34)

        vol_l.addWidget(self.slider_vol)
        vol_l.addWidget(self.lbl_vol)
        body.addWidget(vol_card)

        body.addWidget(div())

        # -- node status --
        st_card = card("Node status")
        st_l = QVBoxLayout(st_card)
        self.status_view = QTextEdit()
        self.status_view.setReadOnly(True)
        self.status_view.setMinimumHeight(200)
        self.status_view.setStyleSheet(f"""
            QTextEdit {{
                background:{NAVY}; border:1px solid {BORDER};
                border-radius:5px; font-family:{MONO};
                font-size:11px; color:{TEXT}; padding:6px;
            }}
        """)
        st_l.addWidget(self.status_view)
        body.addWidget(st_card)

        body.addStretch()

    # ── phase banner ─────────────────────────────────────────────────────

    def _phase(self, name: str):
        bg, fg, txt = PHASE_STATES[name]
        self.banner.setText(txt)
        self.banner.setStyleSheet(
            f"background:{bg}; color:{fg}; font-size:12px;"
            f" font-weight:800; letter-spacing:3px;")

    # ── handlers ─────────────────────────────────────────────────────────

    def _on_play(self):
        query = self.f_query.text().strip()
        if not query:
            return
        self.ros.send_play(query, self.sb_lead.value())
        self._phase("loading")

    def _on_volume_commit(self):
        self.ros.send_volume(float(self.slider_vol.value()))

    def _on_status(self, host: str, status: str, t: float):
        self._status_by_host[host] = (status, t)

        s = status.lower()
        if 'loading' in s:
            self._phase("loading")
        elif 'playing' in s or 'skipped' in s:
            # a skip keeps playing the next playlist item
            self._phase("playing")
        elif 'paused' in s:
            self._phase("paused")
        elif 'stopped' in s:
            self._phase("stopped")

        self._refresh_status_text()

    def _refresh_status_text(self):
        now = time.time()
        lines = []
        for host in sorted(self._status_by_host):
            status, t = self._status_by_host[host]
            age = now - t
            lines.append(f"{host:<20} {status:<28} {age:4.1f}s ago")
        text = "\n".join(lines) if lines else "(no status received yet)"
        self.status_view.setPlainText(text)


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════

def main(args=None):
    rclpy.init(args=args)
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    signals = Signals()
    ros_node = MusicGuiNode(signals)

    spin_thread = threading.Thread(target=rclpy.spin, args=(ros_node,), daemon=True)
    spin_thread.start()

    window = MainWindow(ros_node)
    window.show()

    code = app.exec_()
    ros_node.destroy_node()
    rclpy.shutdown()
    sys.exit(code)


if __name__ == '__main__':
    main()
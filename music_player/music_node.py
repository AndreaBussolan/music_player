import copy
import json
import os
import shlex
import socket as pysocket
import subprocess
import threading
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import String


def clean_subprocess_env():
    """
    Return a copy of the current environment safe to launch mpv with.

    Two independent sources of library poisoning are handled:
    1. A sourced ROS/colcon workspace prepending its own libs onto
       LD_LIBRARY_PATH (fixed by dropping those vars).
    2. A vendor SDK (e.g. Arena SDK camera drivers) that registered its own
       bundled, older ffmpeg libs system-wide via /etc/ld.so.conf.d + a
       system ldconfig run. In that case LD_LIBRARY_PATH may be empty and
       the poisoning lives in the ldconfig cache instead -- simply
       stripping LD_LIBRARY_PATH does nothing for this case. The fix is to
       explicitly set LD_LIBRARY_PATH to the real system lib dirs, since an
       explicit LD_LIBRARY_PATH takes priority over the ldconfig cache.

    This is what fixes errors like:
      mpv: symbol lookup error: libavfilter.so.7: undefined symbol ...
    which, with stderr silenced, surface upstream simply as
    "mpv IPC socket never appeared" (mpv crashes before it ever opens the
    socket).
    """
    env = copy.deepcopy(os.environ)
    for var in ('LD_PRELOAD', 'PYTHONPATH', 'AMENT_PREFIX_PATH'):
        env.pop(var, None)

    system_lib_dirs = [
        '/usr/lib/x86_64-linux-gnu',
        '/lib/x86_64-linux-gnu',
        '/usr/lib',
        '/lib',
    ]
    env['LD_LIBRARY_PATH'] = ':'.join(system_lib_dirs)
    return env


def log_clock_offset(get_logger, hostname):
    """
    Best-effort diagnostic: log this machine's NTP offset from chrony so
    sync problems can be told apart from buffering problems. Never fatal --
    if chrony isn't installed or reachable this just logs a warning.
    """
    try:
        result = subprocess.run(
            ['chronyc', 'tracking'],
            capture_output=True, text=True, timeout=2.0,
        )
        offset_line = next(
            (l for l in result.stdout.splitlines() if l.startswith('System time')),
            None,
        )
        if offset_line:
            get_logger().info(f'[{hostname}] chrony: {offset_line.strip()}')
        else:
            get_logger().warn(
                f'[{hostname}] could not parse chronyc tracking output '
                f'-- is chrony installed and running?')
    except Exception as e:
        get_logger().warn(
            f'[{hostname}] chronyc not available ({e}); clock sync quality '
            f'unknown. Install/run chrony on every machine.')


class MpvIpc:
    """Minimal client for mpv's JSON IPC socket (--input-ipc-server)."""

    def __init__(self, socket_path: str):
        self.socket_path = socket_path
        self._sock = None

    def connect(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(self.socket_path):
                try:
                    s = pysocket.socket(pysocket.AF_UNIX, pysocket.SOCK_STREAM)
                    s.connect(self.socket_path)
                    self._sock = s
                    return True
                except OSError:
                    pass
            time.sleep(0.05)
        return False

    def command(self, *args):
        if not self._sock:
            return None
        payload = json.dumps({'command': list(args)}) + '\n'
        self._sock.sendall(payload.encode('utf-8'))

    def get_property(self, name, timeout=2.0):
        if not self._sock:
            return None
        req_id = 9000
        payload = json.dumps({'command': ['get_property', name], 'request_id': req_id}) + '\n'
        self._sock.sendall(payload.encode('utf-8'))
        self._sock.settimeout(timeout)
        buf = b''
        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                buf += self._sock.recv(4096)
                for line in buf.split(b'\n'):
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get('request_id') == req_id:
                        return msg.get('data')
        except pysocket.timeout:
            return None
        return None

    def close(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class MusicNode(Node):
    """
    Runs on every PC sharing the same ROS_DOMAIN_ID.
    Subscribes to /music/command and plays the requested YouTube audio
    locally via mpv, synchronized to an absolute start timestamp so all
    machines start together (requires clock sync, e.g. chrony/NTP).

    Command payload (JSON string) on /music/command:
      {"action": "play", "query": "<url or search text>", "start_at": <unix_epoch_float>}
      {"action": "stop"}
      {"action": "pause"}
      {"action": "resume"}
      {"action": "skip"}          # playlist-next; works when "query" was a playlist URL

    If "start_at" is omitted, playback starts immediately (unsynchronized).

    Volume payload (JSON string) on /music/volume (latched / TRANSIENT_LOCAL,
    so a node that joins or restarts later immediately picks up the current
    volume instead of waiting for the next change):
      {"value": <0-100>}
    """

    LOAD_WAIT_TIMEOUT = 15.0   # max seconds to wait for media to be ready
    MIN_BUFFER_AHEAD_S = 3.0   # seconds of audio that must be pre-buffered
                               # before we consider mpv "ready" -- this is
                               # what actually prevents a post-unpause stall,
                               # unlike just checking that duration is known
    BUSY_WAIT_MARGIN_S = 0.02  # final slice done via a tight spin-loop
                               # instead of time.sleep(), since sleep() can
                               # wake several ms late depending on OS load
    DEFAULT_VOLUME = 100.0

    def __init__(self):
        super().__init__('music_node')

        command_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.VOLATILE,
            history=HistoryPolicy.KEEP_LAST,
            depth=10,
        )

        # Latched: a node that (re)starts after the last volume change still
        # gets it, instead of staying at the mpv default until someone
        # nudges the slider again.
        volume_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        self.hostname = pysocket.gethostname()
        self._proc = None
        self._ipc = None
        self._ipc_path = f'/tmp/mpv_music_node_{os.getpid()}.sock'
        self._lock = threading.Lock()
        self._volume = self.DEFAULT_VOLUME

        # Prefer a snap-installed mpv if present -- fully isolated runtime,
        # immune to LD_LIBRARY_PATH / ldconfig pollution regardless of what
        # launched it.
        self._mpv_bin = self._resolve_mpv_binary()
        self._clean_env = clean_subprocess_env()

        self.sub = self.create_subscription(
            String, '/music/command', self.on_command, command_qos)
        self.volume_sub = self.create_subscription(
            String, '/music/volume', self.on_volume, volume_qos)
        self.status_pub = self.create_publisher(String, '/music/status', command_qos)

        log_clock_offset(self.get_logger, self.hostname)

        self.get_logger().info(
            f'[{self.hostname}] music_node ready (mpv: {self._mpv_bin}), '
            f'listening on /music/command and /music/volume')

    def _resolve_mpv_binary(self):
        for candidate in ('/snap/bin/mpv', 'mpv'):
            if candidate.startswith('/'):
                if os.path.exists(candidate):
                    return candidate
            else:
                from shutil import which
                found = which(candidate)
                if found:
                    return found
        return 'mpv'  # let it fail loudly if truly not installed

    # ------------------------------------------------------------------
    # Command handling
    # ------------------------------------------------------------------

    def on_command(self, msg: String):
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError:
            payload = {'action': 'play', 'query': msg.data}

        action = payload.get('action', 'play')
        self.get_logger().info(f'[{self.hostname}] action={action} payload={payload}')

        if action == 'play':
            threading.Thread(
                target=self._play_synced,
                args=(payload.get('query', ''), payload.get('start_at')),
                daemon=True,
            ).start()
        elif action == 'stop':
            self.stop()
        elif action == 'pause':
            self.pause()
        elif action == 'resume':
            self.resume()
        elif action == 'skip':
            self.skip()
        else:
            self.get_logger().warn(f'Unknown action: {action}')

    def on_volume(self, msg: String):
        try:
            payload = json.loads(msg.data)
            value = float(payload.get('value'))
        except (json.JSONDecodeError, TypeError, ValueError):
            self.get_logger().warn(f'[{self.hostname}] bad volume payload: {msg.data}')
            return

        value = max(0.0, min(100.0, value))
        self._volume = value
        with self._lock:
            if self._ipc:
                self._ipc.command('set_property', 'volume', self._volume)
        self.get_logger().info(f'[{self.hostname}] volume -> {self._volume}')

    def _play_synced(self, query: str, start_at):
        if not query:
            self.get_logger().warn('Empty query/URL, ignoring')
            return

        with self._lock:
            self.stop()

            target = query if query.startswith('http') else f'ytdl://ytsearch:{query}'
            sock_path = f'/tmp/mpv_music_node_{os.getpid()}_{int(time.time()*1000)}.sock'
            self._ipc_path = sock_path

            cmd = [
                self._mpv_bin, '--no-video', '--ytdl', '--idle=yes', '--pause',
                f'--input-ipc-server={sock_path}',
                # Pre-buffer aggressively so that unpausing at start_at
                # doesn't have to wait on the network -- this is the main
                # source of "sometimes lags more, sometimes less", since
                # that stall length depends on each machine's link at that
                # exact moment.
                '--cache=yes',
                '--cache-secs=30',
                '--demuxer-readahead-secs=20',
            ]
            self.get_logger().info(
                f'[{self.hostname}] running: '
                f'{" ".join(shlex.quote(c) for c in cmd)}')

            log_path = f'/tmp/mpv_music_node_{os.getpid()}.log'
            self._proc = subprocess.Popen(
                cmd,
                stdout=open(log_path, 'w'),
                stderr=subprocess.STDOUT,
                env=self._clean_env,
            )

            self._ipc = MpvIpc(sock_path)
            if not self._ipc.connect(timeout=5.0):
                exit_code = self._proc.poll()
                self.get_logger().error(
                    f'[{self.hostname}] mpv IPC socket never appeared '
                    f'(mpv exit code: {exit_code}, log: {log_path})')
                return

            # Re-apply the current shared volume to this fresh mpv process
            # (volume lives on the process, and every play spawns a new one).
            self._ipc.command('set_property', 'volume', self._volume)

            self._ipc.command('loadfile', target, 'replace')
            self.publish_status(f'loading: {query}')

        # Wait until media is actually pre-buffered (outside the lock,
        # so a stop/pause/skip command can still interrupt promptly).
        ready = self._wait_until_ready()
        if not ready:
            self.get_logger().warn(f'[{self.hostname}] media not ready within timeout, '
                                    f'starting anyway')

        if start_at:
            delay = start_at - time.time()
            if delay > 0:
                self._sleep_until(start_at)
            else:
                self.get_logger().warn(
                    f'[{self.hostname}] start_at already in the past by {-delay:.3f}s, '
                    f'starting immediately')

        with self._lock:
            if self._ipc:
                self._ipc.command('set_property', 'pause', False)
        self.publish_status(f'playing: {query}')

    def _sleep_until(self, target_time: float):
        """
        Sleep until target_time as precisely as possible.

        time.sleep() alone can wake anywhere from 0 to ~15ms late depending
        on OS scheduling -- fine normally, but enough to be audible when
        several machines are meant to start in the same instant. So: sleep
        coarsely (cheap on CPU) for most of the wait, then busy-spin the
        last small slice for sub-millisecond precision.
        """
        margin = self.BUSY_WAIT_MARGIN_S
        remaining = target_time - time.time()
        if remaining > margin:
            time.sleep(remaining - margin)
        while time.time() < target_time:
            pass

    def _wait_until_ready(self):
        """
        Wait until mpv has both parsed the media's duration AND actually
        buffered enough audio ahead of the playback position. Checking
        duration alone (as before) only confirms metadata was read -- it
        says nothing about whether unpausing will hit a network stall,
        which is exactly what caused inconsistent lag between machines.
        """
        deadline = time.time() + self.LOAD_WAIT_TIMEOUT
        while time.time() < deadline:
            with self._lock:
                if not self._ipc:
                    return False
                duration = self._ipc.get_property('duration', timeout=1.0)
                cache_ahead = self._ipc.get_property(
                    'demuxer-cache-duration', timeout=1.0)
            if duration and cache_ahead is not None and \
                    cache_ahead >= self.MIN_BUFFER_AHEAD_S:
                return True
            time.sleep(0.1)
        return False

    # ------------------------------------------------------------------
    # Transport controls
    # ------------------------------------------------------------------

    def pause(self):
        with self._lock:
            if self._ipc:
                self._ipc.command('set_property', 'pause', True)
        self.publish_status('paused')

    def resume(self):
        with self._lock:
            if self._ipc:
                self._ipc.command('set_property', 'pause', False)
        self.publish_status('playing')

    def skip(self):
        """Advance to the next item in the loaded playlist, if any.

        Only meaningful when the 'query' that was played was a playlist URL
        (mpv expands it into multiple playlist entries on its own); a single
        search/URL has nothing to skip to and mpv will just stop.
        """
        with self._lock:
            if self._ipc:
                self._ipc.command('playlist-next', 'force')
        self.publish_status('skipped')

    def stop(self):
        if self._ipc:
            self._ipc.close()
            self._ipc = None
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            self._proc = None
            self.publish_status('stopped')

    def publish_status(self, text: str):
        m = String()
        m.data = json.dumps({'host': self.hostname, 'status': text, 't': time.time()})
        self.status_pub.publish(m)


def main():
    rclpy.init()
    node = MusicNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
    
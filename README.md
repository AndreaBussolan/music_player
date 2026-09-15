# music_player

Broadcasts a YouTube playback command over a ROS2 topic so that **every**
node sharing the same `ROS_DOMAIN_ID` plays it locally, synchronized to a
shared start time.

## Why a topic, not a service

If several service *servers* advertise the same service name, a client's
request only reaches **one** of them (chosen arbitrarily). A topic reaches
every subscriber, which is what you want for "one command, all PCs react."

## How synchronization works

1. `send_command` computes an absolute start timestamp
   (`start_at = now + lead_seconds`) **once**, and includes it in the
   broadcast message. All machines get the same target time.
2. Each `music_node` launches mpv idle+paused, loads the media over its own
   IPC socket, and waits until mpv reports the media is actually loaded
   (not just "command sent").
3. Each node then sleeps until `start_at` and unpauses via IPC at that
   moment — so playback starts at the same wall-clock instant everywhere,
   independent of small message-delivery jitter or per-machine download
   speed differences (as long as loading finishes before `start_at`).

This depends on machine clocks agreeing with each other.

## 1. Dependencies (every PC that plays audio)

```bash
sudo apt install mpv chrony
pip install -U yt-dlp   # mpv shells out to yt-dlp to resolve YouTube streams
```

## 2. Synchronize clocks (every PC)

Point all machines at the same NTP source (a public pool, your router, or
one PC acting as reference). With `chrony`:

```bash
sudo systemctl enable --now chrony
chronyc tracking     # check "System time" offset — aim for low single-digit ms on a LAN
```

On a normal LAN this easily gets you well under 10ms of drift, which is
more than enough for "everyone starts together" — it is *not* meant for
sample-accurate multi-room audio sync.

## 3. Build

```bash
cd music_player_ws
colcon build --packages-select music_player
source install/setup.bash
```

## 4. Run (every PC, same ROS_DOMAIN_ID)

```bash
export ROS_DOMAIN_ID=42   # must match on all machines
ros2 run music_player music_node
```

## 5. Trigger synchronized playback from any single terminal

```bash
ros2 run music_player send_command --query "lofi hip hop radio" --lead 3
ros2 run music_player send_command --query "https://www.youtube.com/watch?v=eKuTQXE6lc8" --lead 5
ros2 run music_player send_command --stop
```

`--lead` is how many seconds in the future the start time is — give slower
machines/networks enough time to finish buffering before that moment. If a
node isn't ready by `start_at`, it logs a warning and starts as soon as it
can rather than blocking forever.

## Watching status from all machines

```bash
ros2 topic echo /music/status
```

Each node reports `loading` then `playing`/`stopped` with a timestamp, so
you can measure actual start-time spread across machines.

## Remaining sources of drift

- **Buffering variance**: if one machine has a much slower connection to
  YouTube, it may still be buffering when `start_at` arrives (it'll start
  late once ready). For tighter sync, download the audio once and serve it
  from a local file share (NFS/rsync) so every node loads the identical
  file from local disk instead of independently hitting YouTube.
- **Audio hardware latency**: different sound cards/drivers have different
  output latency, usually a few ms to a few tens of ms — outside what ROS2
  or mpv can control.
- **This is not sample-accurate**: fine for "everyone in the building hears
  the same song start together," not for synchronized multi-speaker audio
  processing (that needs PTP + a proper audio-over-IP protocol, e.g. Dante
  or AES67).

#!/usr/bin/env python3
"""
Publish a command to /music/command (or /music/volume) so every music_node
on the same ROS_DOMAIN_ID reacts together.

Usage:
  ros2 run music_player send_command --query "lofi hip hop radio" --lead 3
  ros2 run music_player send_command --query "https://youtu.be/dQw4w9WgXcQ" --lead 5
  ros2 run music_player send_command --stop
  ros2 run music_player send_command --pause
  ros2 run music_player send_command --resume
  ros2 run music_player send_command --skip
  ros2 run music_player send_command --volume 40

Requires clocks to be synchronized across machines (e.g. chrony/NTP) for
--query's start_at to mean the same thing everywhere.
"""
import argparse
import json
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy
from std_msgs.msg import String


def build_command_qos():
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.VOLATILE,
        history=HistoryPolicy.KEEP_LAST,
        depth=10,
    )


def build_volume_qos():
    # Latched, so a music_node started after this call still picks it up.
    return QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', type=str, default=None,
                         help='YouTube URL (playlist URLs work too) or search text')
    parser.add_argument('--lead', type=float, default=3.0,
                         help='Seconds of lead time before start_at, gives '
                              'every machine time to buffer (default: 3.0)')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--stop', action='store_true', help='Stop playback everywhere')
    group.add_argument('--pause', action='store_true', help='Pause playback everywhere')
    group.add_argument('--resume', action='store_true', help='Resume playback everywhere')
    group.add_argument('--skip', action='store_true',
                        help='Skip to next playlist item everywhere (needs a playlist URL)')
    parser.add_argument('--volume', type=float, default=None,
                         help='Set volume 0-100 for all nodes (published on /music/volume, '
                              'latched so late-joining nodes pick it up too)')
    args = parser.parse_args()

    if not any([args.query, args.stop, args.pause, args.resume, args.skip,
                args.volume is not None]):
        parser.error('Give one of --query, --stop, --pause, --resume, --skip, --volume')

    rclpy.init()
    node = Node('send_command')

    command_pub = node.create_publisher(String, '/music/command', build_command_qos())
    volume_pub = node.create_publisher(String, '/music/volume', build_volume_qos())

    # Give discovery a moment to find subscribers on other machines.
    time.sleep(1.0)

    if args.volume is not None:
        value = max(0.0, min(100.0, args.volume))
        msg = String()
        msg.data = json.dumps({'value': value})
        volume_pub.publish(msg)
        node.get_logger().info(f'Published volume: {msg.data}')
    elif args.stop:
        msg = String()
        msg.data = json.dumps({'action': 'stop'})
        command_pub.publish(msg)
        node.get_logger().info(f'Published: {msg.data}')
    elif args.pause:
        msg = String()
        msg.data = json.dumps({'action': 'pause'})
        command_pub.publish(msg)
        node.get_logger().info(f'Published: {msg.data}')
    elif args.resume:
        msg = String()
        msg.data = json.dumps({'action': 'resume'})
        command_pub.publish(msg)
        node.get_logger().info(f'Published: {msg.data}')
    elif args.skip:
        msg = String()
        msg.data = json.dumps({'action': 'skip'})
        command_pub.publish(msg)
        node.get_logger().info(f'Published: {msg.data}')
    else:
        msg = String()
        msg.data = json.dumps({
            'action': 'play',
            'query': args.query,
            'start_at': time.time() + args.lead,
        })
        command_pub.publish(msg)
        node.get_logger().info(f'Published: {msg.data}')

    time.sleep(0.5)  # let it flush before shutting down

    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
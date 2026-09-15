"""
Launch file for the music_player system.

Starts the GUI, and optionally the local music_node (so this machine also
plays audio, not just controls the others).

Usage:
  ros2 launch music_player music_player_launch.py
  ros2 launch music_player music_player_launch.py play_locally:=false
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    play_locally_arg = DeclareLaunchArgument(
        'play_locally',
        default_value='true',
        description='Also run music_node on this machine, so it plays audio '
                    'as well as controlling the others.',
    )

    music_node = Node(
        package='music_player',
        executable='music_node',
        name='music_node',
        output='screen',
        condition=IfCondition(LaunchConfiguration('play_locally')),
    )

    gui_node = Node(
        package='music_player',
        executable='music_gui',
        name='music_gui',
        output='screen',
    )

    return LaunchDescription([
        play_locally_arg,
        music_node,
        gui_node,
    ])

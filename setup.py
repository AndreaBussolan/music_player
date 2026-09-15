import os
from glob import glob
from setuptools import setup

package_name = 'music_player'

setup(
    name=package_name,
    version='0.0.1',
    packages=[package_name],
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'launch'),
            glob(os.path.join('launch', '*launch.[pxy][yma]*'))),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='you',
    maintainer_email='you@example.com',
    description='Broadcasts synchronized YouTube playback commands to every node sharing the same ROS_DOMAIN_ID',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'music_node = music_player.music_node:main',
            'send_command = music_player.send_command:main',
            'music_gui = music_player.gui_node:main',
        ],
    },
)
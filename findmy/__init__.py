#!/usr/bin/python3
# .                                       .           .                   .
# |                                 o     |           |        ,- o       |          
# |-. ,-. ;-.-. ,-. --- ,-: ,-. ,-. . ,-. |-  ,-: ;-. |-  ---  |  . ;-. ,-| ;-.-. . .
# | | | | | | | |-'     | | `-. `-. | `-. |   | | | | |        |- | | | | | | | | | |
# ' ' `-' ' ' ' `-'     `-` `-' `-' ' `-' `-' `-` ' ' `-'      |  ' ' ' `-' ' ' ' `-|
#                                                             -'                  `-'
# made with ♡ by muehlt
# github.com/muehlt
# version 1.1.0
#
# DESCRIPTION:  This python script reads the FindMy cache files and publishes the location
#               data to MQTT to be used in Home Assistant. It uses auto discovery so no
#               further entity configuration is needed in Home Assistant. Consult the
#               documentation on how to set up an MQTT broker for Home Assistant. The script
#               needs to be executed on macOS with a running FindMy installation. It needs
#               to be executed in a terminal with full disk access to be able to read the
#               cache files. The script must be configured using the variables below and the
#               mqtt client password as environment variable.
#
# DISCLAIMER:   This script is provided as-is, without any warranty. Use at your own risk.
#               This code is not tested and should only be used for experimental purposes.
#               Loading the FindMy cache files is not intended by Apple and might cause problems.
#
# LICENSE:      See the LICENSE.md file of the original authors' repository
#               (https://github.com/muehlt/home-assistant-findmy).
import logging
from datetime import datetime
import math
import re
import time
import click
import paho.mqtt.client as mqtt
import os
import json
import typing

import watchdog.events
from unidecode import unidecode
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from watchdog.observers import Observer
from watchdog.events import FileSystemEvent

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S', filename='/tmp/findmy.log')
load_dotenv()

device_updates = {}

client = mqtt.Client("ha-client")


def connect_broker(username: str, password: str, broker_ip: str, broker_port: int):
    client.username_pw_set(username, password)
    client.reconnect_delay_set(1, 256)
    client.connect(host=broker_ip, port=broker_port)
    client.loop_start()


def get_time(timestamp):
    if type(timestamp) is not int:
        return "unknown"
    return str(datetime.fromtimestamp(timestamp / 1000))


def load_data(data_file):
    with open(data_file, 'r') as f:
        data = json.load(f)
        f.close()
        return data


def get_device_id(name):
    device_id = unidecode(re.sub(r'[\s-]', '_', name).lower())
    return re.sub(r'_+', '_', re.sub('[^0-9a-zA-Z_-]+', '', device_id))


def get_source_type(apple_position_type):
    switcher = {
        "crowdsourced": "gps",  # ble only used for stationary ble trackers
        "safeLocation": "gps",
        "Wifi": "router"
    }
    return switcher.get(apple_position_type, "gps")


class Device(object):
    def __init__(self, device: typing.Dict):
        self.name = device['name']
        self.battery_level = device['batteryLevel'] if 'batteryLevel' in device else None
        self.battery_status = device['batteryStatus']
        self.source_type = self.latitude = self.longitude = \
            self.address = self.accuracy = self.location_name = self.last_update = None

        location = device.get('location')
        if location is not None:
            self.source_type = get_source_type(location.get('positionType'))
            self.latitude = location['latitude']
            self.longitude = location['longitude']
            self.address = device['address']
            self.accuracy = math.sqrt(location['horizontalAccuracy'] ** 2 + location['verticalAccuracy'] ** 2)
            self.last_update = location['timeStamp']

        self.id = get_device_id(self.name)
        self.updates_identifier = f"{self.name} ({self.id})"


def send_location_data(force_sync, findmy_data_file):
    for device_data in load_data(findmy_data_file):
        device = Device(device_data)
        previous_update = device_updates.get(device.updates_identifier)

        if not device.last_update:
            if previous_update:
                del device_updates[device.updates_identifier]
            continue

        if force_sync or not previous_update or previous_update.last_update != device.last_update:
            device_updates[device.updates_identifier] = device
            publish_to_mqtt(device)


def publish_to_mqtt(device):
    logging.debug("Publishing device: %s", device.id)
    device_topic = f"homeassistant/device_tracker/{device.id}/"
    device_config = {
        "unique_id": device.id,
        "state_topic": device_topic + "state",
        "json_attributes_topic": device_topic + "attributes",
        "device": {
            "identifiers": device.id,
            "manufacturer": "Apple",
            "name": device.name
        },
        "source_type": device.source_type,
        "payload_home": "home",
        "payload_not_home": "not_home",
        "payload_reset": "unknown"
    }
    device_attributes = {
        "latitude": device.latitude,
        "longitude": device.longitude,
        "gps_accuracy": device.accuracy,
        "address": device.address,
        "battery_status": device.battery_status,
        "last_update_timestamp": device.last_update,
        "last_update": get_time(device.last_update),
        "provider": "FindMy (muehlt/home-assistant-findmy)"
    }

    if device.battery_level:
        device_attributes["battery_level"] = device.battery_level

    client.publish(device_topic + "config", json.dumps(device_config))
    client.publish(device_topic + "attributes", json.dumps(device_attributes))
    client.publish(device_topic + "state", device.location_name)


class FindMyFileHandler(watchdog.events.PatternMatchingEventHandler):
    def __init__(self, force_sync):
        super().__init__(patterns=["Items.data", "Devices.data"])
        self.force_sync = force_sync

    def on_modified(self, event: FileSystemEvent) -> None:
        super().on_modified(event)
        logging.info("Sending updates from: %s", event.src_path)
        send_location_data(self.force_sync, event.src_path)


def update_console(privacy, refresh_interval):
    console = Console()
    with console.status(f"[bold green]Synchronizing {len(device_updates)} devices ") as status:
        while True:
            os.system('clear')

            if not privacy:
                device_table = Table()
                device_table.add_column("Device")
                device_table.add_column("Last Update")
                device_table.add_column("Location")
                for device_key, device in sorted(device_updates.items(), key=lambda x: x[0]):
                    device_table.add_row(device_key,
                                         get_time(device.last_update),
                                         f"{device.latitude}, {device.longitude}")
                console.print(device_table)

            status.update(get_status_message())

            time.sleep(refresh_interval)


def get_status_message():
    if client.is_connected():
        return f"[bold green]Synchronizing {len(device_updates)} devices"
    return "[bold red]Client is disconnected"


def start_file_watch(findmy_data_dir, force_sync) -> Observer:
    send_location_data(force_sync, os.path.join(findmy_data_dir, "Items.data"))
    send_location_data(force_sync, os.path.join(findmy_data_dir, "Devices.data"))
    event_handler = FindMyFileHandler(force_sync)
    observer = Observer()
    observer.schedule(event_handler, findmy_data_dir)
    observer.start()
    return observer


@click.command("home-assistant-findmy", no_args_is_help=True)
@click.option('--privacy', '-p',
              is_flag=True,
              help='Hides specific device data from the console output')
@click.option('--force-sync', '-f',
              is_flag=True,
              help='Disables the timestamp check and provides and update every FINDMY_FILE_SCAN_INTERVAL seconds')
@click.option('--ip',
              envvar='MQTT_BROKER_IP',
              required=True,
              help="IP of the MQTT broker.")
@click.option('--port',
              envvar='MQTT_BROKER_PORT',
              default=1883, type=int,
              help="Port of the MQTT broker.")
@click.option('--username',
              envvar='MQTT_CLIENT_USERNAME',
              required=True,
              help="MQTT client username.")
@click.option('--password',
              envvar='MQTT_CLIENT_PASSWORD',
              required=True,
              help="[WARNING] Set this via environment variable! MQTT client password.")
@click.option('--refresh-interval',
              envvar='FINDMY_REFRESH_INTERVAL',
              default=5,
              type=int,
              help="Refresh console interval in seconds.")
@click.option('--findmy-data-dir', '-f',
              type=click.Path(),
              default=os.path.expanduser('~') + '/Library/Caches/com.apple.findmy.fmipcore/',
              required=False,
              help='Path to findmy data (for testing)')
def main(privacy, force_sync, ip, port, username, password, refresh_interval, findmy_data_dir):
    observer = None
    try:
        connect_broker(username, password, ip, port)
        observer = start_file_watch(findmy_data_dir, force_sync)
        update_console(privacy, refresh_interval)
    finally:
        client.loop_stop()
        if observer:
            observer.stop()
            observer.join()


if __name__ == '__main__':
    main()

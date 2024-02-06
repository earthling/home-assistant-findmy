#!/usr/bin/python3
import typing
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

from datetime import datetime
import math
import re
import time
import click
import paho.mqtt.client as mqtt
import os
import json
from unidecode import unidecode
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

DEFAULT_TOLERANCE = 70  # meters

known_locations = {}
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


def get_lat_lng_approx(meters):
    return meters / 111111


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


def get_location_name(pos):
    if len(known_locations) == 0:
        return "unknown"

    for name, location in known_locations.items():
        tolerance = get_lat_lng_approx(location['tolerance'] or DEFAULT_TOLERANCE)
        if (math.isclose(location['latitude'], pos[0], abs_tol=tolerance) and
                math.isclose(location['longitude'], pos[1], abs_tol=tolerance)):
            return name
    return "not_home"


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
            self.location_name = get_location_name((self.latitude, self.longitude))
            self.last_update = location['timeStamp']

        self.id = get_device_id(self.name)
        self.updates_identifier = f"{self.name} ({self.id})"


def send_location_data(force_sync, findmy_data_file):
    for device_data in load_data(findmy_data_file):
        device = Device(device_data)
        device_update = device_updates.get(device.updates_identifier)
        if not force_sync and device_update and len(device_update) > 0 and device_update[0] == device.last_update:
            continue

        device_updates[device.updates_identifier] = (device.last_update, device.location_name)
        publish_to_mqtt(device)


def publish_to_mqtt(device):
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


def scan_cache(privacy, force_sync, scan_interval, findmy_data_dir):
    cache_file_location_items = findmy_data_dir + 'Items.data'
    cache_file_location_devices = findmy_data_dir + 'Devices.data'

    console = Console()
    with console.status(
            f"[bold green]Synchronizing {len(device_updates)} devices "
            f"and {len(known_locations)} known locations") as status:
        while True:
            send_location_data(force_sync, cache_file_location_items)
            send_location_data(force_sync, cache_file_location_devices)

            os.system('clear')

            if not privacy:
                device_table = Table()
                device_table.add_column("Device")
                device_table.add_column("Last Update")
                device_table.add_column("Location")
                for device, details in sorted(device_updates.items(), key=lambda x: get_time(x[1][0])):
                    device_table.add_row(device, get_time(details[0]), details[1])
                console.print(device_table)

            status.update(
                f"[bold green]Synchronizing {len(device_updates)} devices and {len(known_locations)} known locations")

            time.sleep(scan_interval)


def validate_param_locations(_, __, path):
    if not path:
        return {}

    if not os.path.isfile(path):
        raise click.BadParameter('The provided path is not a file.')

    with open(path, 'r') as f:
        try:
            locations = json.load(f)
        except json.JSONDecodeError:
            raise click.BadParameter('The provided file does not contain valid JSON data.')

    if not isinstance(locations, dict):
        raise click.BadParameter('The provided file does not contain a valid JSON object.')

    for name, location in locations.items():
        if not isinstance(name, str):
            raise click.BadParameter(f'The location name "{name}" is not a string.')
        if not isinstance(location, dict):
            raise click.BadParameter(f'The location "{name}" is not a valid JSON object.')
        if not isinstance(location.get('latitude'), float):
            raise click.BadParameter(f'The location "{name}" does not contain a valid latitude.')
        if not isinstance(location.get('longitude'), float):
            raise click.BadParameter(f'The location "{name}" does not contain a valid longitude.')
        if not isinstance(location.get('tolerance'), int):
            raise click.BadParameter(f'The location "{name}" does not contain a valid tolerance in meters.')

    return path, locations


def set_known_locations(locations):
    global known_locations
    _path, _known_locations = locations
    known_locations = _known_locations


@click.command("home-assistant-findmy", no_args_is_help=True)
@click.option('--locations', '-l',
              type=click.Path(),
              callback=validate_param_locations,
              required=False,
              help='Path to the known locations JSON configuration file')
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
@click.option('--scan-interval',
              envvar='FINDMY_FILE_SCAN_INTERVAL',
              default=5,
              type=int,
              help="File scan interval in seconds.")
@click.option('--findmy-data-dir', '-f',
              type=click.Path(),
              default=os.path.expanduser('~') + '/Library/Caches/com.apple.findmy.fmipcore/',
              required=False,
              help='Path to findmy data (for testing)')
def main(locations, privacy, force_sync, ip, port, username, password, scan_interval, findmy_data_dir):
    connect_broker(username, password, ip, port)
    if locations:
        set_known_locations(locations)
    scan_cache(privacy, force_sync, scan_interval, findmy_data_dir)


if __name__ == '__main__':
    main()

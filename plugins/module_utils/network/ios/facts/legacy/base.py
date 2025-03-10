#
# -*- coding: utf-8 -*-
# Copyright 2019 Red Hat
# GNU General Public License v3.0+
# (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

"""
The ios legacy fact class
It is in this file the configuration is collected from the device
for a given resource, parsed, and the facts tree is populated
based on the configuration.
"""

from __future__ import absolute_import, division, print_function


__metaclass__ = type


import platform
import re

from ansible.module_utils.six import iteritems
from ansible.module_utils.six.moves import zip

from ansible_collections.cisco.ios.plugins.module_utils.network.ios.ios import (
    get_capabilities,
    normalize_interface,
    run_commands,
)


class FactsBase(object):
    COMMANDS = list()

    def __init__(self, module):
        self.module = module
        self.facts = dict()
        self.warnings = list()
        self.responses = None

    def populate(self):
        self.responses = run_commands(self.module, commands=self.COMMANDS, check_rc=False)

    def run(self, cmd):
        return run_commands(self.module, commands=cmd, check_rc=False)


class Default(FactsBase):
    COMMANDS = ["show version", "show switch virtual", "show inventory"]

    def populate(self):
        super(Default, self).populate()
        self.facts.update(self.platform_facts())
        data = self.responses[0]
        if data:
            self.facts["iostype"] = self.parse_iostype(data)
            self.facts["operatingmode"] = self.parse_operatingmode(data, self.facts["iostype"])
            self.facts["serialnum"] = self.parse_serialnum(data)
            self.parse_stacks(data)
        data = self.responses[1] + self.responses[2]
        vss_errs = ["Invalid input", "Switch Mode : Standalone"]
        if data and not any(err in data for err in vss_errs):
            self.parse_virtual_switch(data)

    def parse_iostype(self, data):
        match = re.search(r"\sIOS-XE\s", data)
        if match:
            return "IOS-XE"
        else:
            return "IOS"

    def parse_operatingmode(self, data, iostype):
        # for older ios versions default being autonomous where operating mode classification not present
        match = re.search(r"Router\soperating\smode: (\S+)", data)
        if (match and "autonomous" in match.group(1).lower()) or iostype == "IOS":
            return "autonomous"
        else:
            return "controller"

    def parse_serialnum(self, data):
        match = re.search(r"board ID (\S+)", data)
        if match:
            return match.group(1)

    def parse_stacks(self, data):
        match = re.findall(r"^Model [Nn]umber\s+: (\S+)", data, re.M)
        if match:
            self.facts["stacked_models"] = match

        match = re.findall(r"^System [Ss]erial [Nn]umber\s+: (\S+)", data, re.M)
        if match:
            self.facts["stacked_serialnums"] = match

        if "stacked_models" in self.facts:
            self.facts["virtual_switch"] = "STACK"

    def parse_virtual_switch(self, data):
        match = re.search(r"^Virtual switch domain number : ([0-9]+)", data, re.M)
        if match:
            self.facts["virtual_switch"] = "VSS"
            self.facts["virtual_switch_domain"] = match.group(1)

        match = re.findall(r"System\".*?SN:\s*([^\s]+)", data, re.S)
        if match:
            self.facts["virtual_switch_serialnums"] = match

    def platform_facts(self):
        platform_facts = {}

        resp = get_capabilities(self.module)
        device_info = resp["device_info"]

        platform_facts["system"] = device_info["network_os"]

        for item in ("model", "image", "version", "platform", "hostname"):
            val = device_info.get("network_os_%s" % item)
            if val:
                platform_facts[item] = val

        platform_facts["api"] = resp["network_api"]
        platform_facts["python_version"] = platform.python_version()

        return platform_facts


class Hardware(FactsBase):
    COMMANDS = ["dir", "show memory statistics", "show processes cpu | include CPU utilization"]

    def populate(self):
        warnings = list()
        super(Hardware, self).populate()

        data = self.responses[0]
        if data:
            self.facts["filesystems"] = self.parse_filesystems(data)
            self.facts["filesystems_info"] = self.parse_filesystems_info(data)
        self.facts["cpu_utilization"] = self.parse_cpu_utilization(self.responses[2])

        data = self.responses[1]
        if data:
            if "Invalid input detected" in data:
                warnings.append("Unable to gather memory statistics")
            else:
                for line in data.splitlines():
                    match = re.match(
                        r"Processor\s+(\S+|\d+)\s+(?P<total>\d+)\s+\d+\s+(?P<free>\d+)",
                        line,
                    )
                    if match:
                        self.facts["memtotal_mb"] = int(match.group("total")) / 1048576
                        self.facts["memfree_mb"] = int(match.group("free")) / 1048576

    def parse_filesystems(self, data):
        return re.findall(r"^Directory of (\S+)/", data, re.M)

    def parse_filesystems_info(self, data):
        facts = dict()
        fs = ""
        for line in data.split("\n"):
            match = re.match(r"^Directory of (\S+)/", line)
            if match:
                fs = match.group(1)
                facts[fs] = dict()
                continue
            match = re.match(r"^(\d+) bytes total \((\d+) bytes free\)", line)
            if match:
                facts[fs]["spacetotal_kb"] = int(match.group(1)) / 1024
                facts[fs]["spacefree_kb"] = int(match.group(2)) / 1024
        return facts

    def parse_cpu_utilization(self, data):
        facts = {}
        regex_cpu_utilization = re.compile(
            r"""
            (^Core\s(?P<core>\d+)?:)?
            (^|\s)CPU\sutilization\sfor\sfive\sseconds:
            (\s(?P<f_sec>\d+)?%)?
            (\s(?P<f_se_nom>\d+)%/(?P<f_s_denom>\d+)%\)?)?
            ;\sone\sminute:\s(?P<a_min>\d+)?%
            ;\sfive\sminutes:\s(?P<f_min>\d+)?%
            """,
            re.VERBOSE,
        )
        for line in data.split("\n"):
            match_cpu_utilization = regex_cpu_utilization.match(line)
            if match_cpu_utilization:
                _core = "core"
                if match_cpu_utilization.group("core"):
                    _core = "core_" + str(match_cpu_utilization.group("core"))
                facts[_core] = {}
                facts[_core]["five_seconds"] = int(
                    match_cpu_utilization.group("f_se_nom") or match_cpu_utilization.group("f_sec"),
                )
                facts[_core]["one_minute"] = int(match_cpu_utilization.group("a_min"))
                facts[_core]["five_minutes"] = int(match_cpu_utilization.group("f_min"))
                if match_cpu_utilization.group("f_s_denom"):
                    facts[_core]["five_seconds_interrupt"] = int(
                        match_cpu_utilization.group("f_s_denom"),
                    )
        return facts


class Config(FactsBase):
    COMMANDS = ["show running-config"]

    def populate(self):
        super(Config, self).populate()
        data = self.responses[0]
        if data:
            data = re.sub(
                r"""^Building configuration
                ...\s+Current configuration : \d+ bytes\n""",
                "",
                data,
                flags=re.MULTILINE,
            )
            self.facts["config"] = data


class Interfaces(FactsBase):
    COMMANDS = [
        "show interfaces",
        "show ip interface",
        "show ipv6 interface",
        "show lldp",
        "show cdp",
    ]

    def populate(self):
        super(Interfaces, self).populate()

        self.facts["all_ipv4_addresses"] = list()
        self.facts["all_ipv6_addresses"] = list()
        self.facts["neighbors"] = {}

        data = self.responses[0]
        if data:
            interfaces = self.parse_interfaces(data)
            self.facts["interfaces"] = self.populate_interfaces(interfaces)

        data = self.responses[1]
        if data:
            data = self.parse_interfaces(data)
            self.populate_ipv4_interfaces(data)

        data = self.responses[2]
        if data:
            data = self.parse_interfaces(data)
            self.populate_ipv6_interfaces(data)

        data = self.responses[3]
        lldp_errs = ["Invalid input", "LLDP is not enabled"]

        if data and not any(err in data for err in lldp_errs):
            neighbors = self.run(["show lldp neighbors detail"])
            if neighbors:
                self.facts["neighbors"].update(self.parse_neighbors(neighbors[0]))

        data = self.responses[4]
        cdp_errs = ["CDP is not enabled"]

        if data and not any(err in data for err in cdp_errs):
            cdp_neighbors = self.run(["show cdp neighbors detail"])
            if cdp_neighbors:
                self.facts["neighbors"].update(self.parse_cdp_neighbors(cdp_neighbors[0]))

    def populate_interfaces(self, interfaces):
        facts = dict()
        for key, value in iteritems(interfaces):
            intf = dict()
            intf["description"] = self.parse_description(value)
            intf["macaddress"] = self.parse_macaddress(value)

            intf["mtu"] = self.parse_mtu(value)
            intf["bandwidth"] = self.parse_bandwidth(value)
            intf["mediatype"] = self.parse_mediatype(value)
            intf["duplex"] = self.parse_duplex(value)
            intf["lineprotocol"] = self.parse_lineprotocol(value)
            intf["operstatus"] = self.parse_operstatus(value)
            intf["type"] = self.parse_type(value)

            facts[key] = intf
        return facts

    def populate_ipv4_interfaces(self, data):
        for key, value in data.items():
            try:
                self.facts["interfaces"][key]["ipv4"] = list()
            except KeyError:
                self.facts["interfaces"][key] = dict()
                self.facts["interfaces"][key]["ipv4"] = list()
                self.parse_deleted_status(key, value)
            primary_address = addresses = []
            primary_address = re.findall(r"Internet address is (.+)$", value, re.M)
            addresses = re.findall(r"Secondary address (.+)$", value, re.M)
            if len(primary_address) == 0:
                continue
            addresses.append(primary_address[0])
            for address in addresses:
                addr, subnet = address.split("/")
                ipv4 = dict(address=addr.strip(), subnet=subnet.strip())
                self.add_ip_address(addr.strip(), "ipv4")
                self.facts["interfaces"][key]["ipv4"].append(ipv4)

    def populate_ipv6_interfaces(self, data):
        for key, value in iteritems(data):
            try:
                self.facts["interfaces"][key]["ipv6"] = list()
            except KeyError:
                self.facts["interfaces"][key] = dict()
                self.facts["interfaces"][key]["ipv6"] = list()
                self.parse_deleted_status(key, value)
            addresses = re.findall(r"\s+(.+), subnet", value, re.M)
            subnets = re.findall(r", subnet is (.+)$", value, re.M)
            for addr, subnet in zip(addresses, subnets):
                ipv6 = dict(address=addr.strip(), subnet=subnet.strip())
                self.add_ip_address(addr.strip(), "ipv6")
                self.facts["interfaces"][key]["ipv6"].append(ipv6)

    def add_ip_address(self, address, family):
        if family == "ipv4":
            self.facts["all_ipv4_addresses"].append(address)
        else:
            self.facts["all_ipv6_addresses"].append(address)

    def parse_neighbors(self, neighbors):
        facts = dict()
        for entry in neighbors.split("------------------------------------------------"):
            if entry == "":
                continue
            intf = self.parse_lldp_intf(entry)
            if intf is None:
                return facts
            intf = normalize_interface(intf)
            if intf not in facts:
                facts[intf] = list()
            fact = dict()
            fact["host"] = self.parse_lldp_host(entry)
            fact["port"] = self.parse_lldp_port(entry)
            fact["ip"] = self.parse_lldp_ip(entry)
            facts[intf].append(fact)
        return facts

    def parse_cdp_neighbors(self, neighbors):
        facts = dict()
        for entry in neighbors.split("-------------------------"):
            if entry == "":
                continue
            intf_port = self.parse_cdp_intf_port(entry)
            if intf_port is None:
                return facts
            intf, port = intf_port
            if intf not in facts:
                facts[intf] = list()
            fact = dict()
            fact["host"] = self.parse_cdp_host(entry)
            fact["platform"] = self.parse_cdp_platform(entry)
            fact["port"] = port
            fact["ip"] = self.parse_cdp_ip(entry)
            facts[intf].append(fact)
        return facts

    def parse_interfaces(self, data):
        parsed = dict()
        key = ""
        for line in data.split("\n"):
            if len(line) == 0:
                continue
            if line[0] == " ":
                parsed[key] += "\n%s" % line
            else:
                match = re.match(r"^(\S+)", line)
                if match:
                    key = match.group(1)
                    parsed[key] = line
        return parsed

    def parse_deleted_status(self, interface, value):
        status = self.parse_operstatus(value)
        if status == "deleted":
            self.facts["interfaces"][interface]["operstatus"] = status

    def parse_description(self, data):
        match = re.search(r"Description: (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_macaddress(self, data):
        match = re.search(r"Hardware is (?:.*), address is (\S+)", data)
        if match:
            return match.group(1)

    def parse_ipv4(self, data):
        match = re.search(r"Internet address is (\S+)", data)
        if match:
            addr, masklen = match.group(1).split("/")
            return dict(address=addr, masklen=int(masklen))

    def parse_mtu(self, data):
        match = re.search(r"MTU (\d+)", data)
        if match:
            return int(match.group(1))

    def parse_bandwidth(self, data):
        match = re.search(r"BW (\d+)", data)
        if match:
            return int(match.group(1))

    def parse_duplex(self, data):
        match = re.search(r"(\w+) Duplex", data, re.M)
        if match:
            return match.group(1)

    def parse_mediatype(self, data):
        match = re.search(r"media type is (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_type(self, data):
        match = re.search(r"Hardware is (.+),", data, re.M)
        if match:
            return match.group(1)

    def parse_lineprotocol(self, data):
        match = re.search(r"line protocol is (up|down)(.+)?$", data, re.M)
        if match:
            return match.group(1)

    def parse_operstatus(self, data):
        match = re.search(r"^(?:.+) is (.+),", data, re.M)
        if match:
            return (match.group(1)).lstrip()

    def parse_lldp_intf(self, data):
        match = re.search(r"^Local Intf: (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_lldp_host(self, data):
        match = re.search(r"System Name: (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_lldp_port(self, data):
        match = re.search(r"Port id: (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_lldp_ip(self, data):
        match = re.search(r"^    IP: (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_cdp_intf_port(self, data):
        match = re.search(r"^Interface: (.+),  Port ID \(outgoing port\): (.+)$", data, re.M)
        if match:
            return match.group(1), match.group(2)

    def parse_cdp_host(self, data):
        match = re.search(r"^Device ID: (.+)$", data, re.M)
        if match:
            return match.group(1)

    def parse_cdp_platform(self, data):
        match = re.search(r"^Platform: (.+),", data, re.M)
        if match:
            return match.group(1)

    def parse_cdp_ip(self, data):
        match = re.search(r"^  IP address: (.+)$", data, re.M)
        if match:
            return match.group(1)

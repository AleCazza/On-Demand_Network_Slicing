#!/usr/bin/env bash
# Removes leftovers of a previous run: Mininet state, OVS QoS records and a
# controller still bound to the OpenFlow / REST ports.
sudo mn -c >/dev/null 2>&1
sudo ovs-vsctl --all destroy qos -- --all destroy queue 2>/dev/null
pkill -f "ryu-manager.*slice_controller" 2>/dev/null
echo "clean"

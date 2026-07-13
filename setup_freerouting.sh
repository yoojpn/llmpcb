#!/bin/bash
# Downloads Freerouting (PCB auto-router) into _tools/, required by
# kicad_utils.kicad_wrapper.route_pcb(). Not committed to the repo since
# it's a 55MB pre-built binary -- fetch it once per environment instead.
set -e
mkdir -p _tools
curl -sL "https://github.com/freerouting/freerouting/releases/download/v2.2.4/freerouting-2.2.4.jar" -o _tools/freerouting.jar
echo "Freerouting downloaded to _tools/freerouting.jar"
echo "Requires Java 21+ (freerouting 2.2.4 needs class file version 69, i.e. Java 25) -- check with: java -version"

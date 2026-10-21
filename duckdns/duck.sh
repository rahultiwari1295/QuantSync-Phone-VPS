#!/bin/sh
echo url="https://www.duckdns.org/update?domains=quantsync-api&token=fb541758-536a-4348-acab-29d8993dbd96&ip=51.68.204.210&ipv6=" | curl -k -s -o /root/duckdns/duck.log -K -

#!/usr/bin/env bash
# Open Central Server Web Platform
echo "Central Server Web Platform: http://192.168.1.103:8080/dashboard"
if which xdg-open > /dev/null; then
  xdg-open "http://192.168.1.103:8080/dashboard"
elif which open > /dev/null; then
  open "http://192.168.1.103:8080/dashboard"
fi

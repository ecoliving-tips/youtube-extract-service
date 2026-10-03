#!/bin/sh
set -eu
exec supervisord -c /app/supervisord.conf

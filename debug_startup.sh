#!/bin/bash
set -e
exec > >(tee /home/LogFiles/startup-debug.log) 2>&1
echo "=== STARTUP $(date -u) ==="
echo "PWD: $(pwd)"
echo "WWWROOT contents:"
ls -la /home/site/wwwroot/ 2>&1 | head -30
echo "PYTHON: $(which python)"
python --version
echo "Trying to import app..."
cd /home/site/wwwroot
python -u -c "import sys; sys.path.insert(0, '/home/site/wwwroot/src'); import app; print('App import OK:', app.app)" || echo "IMPORT FAILED with exit $?"
echo "Starting gunicorn..."
exec gunicorn --bind 0.0.0.0:8000 --timeout 600 --workers 2 --threads 4 --chdir /home/site/wwwroot --access-logfile - --error-logfile - --capture-output --log-level debug app:app

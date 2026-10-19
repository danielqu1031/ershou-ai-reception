#!/bin/zsh
cd -- "${0:A:h}"
exec .venv/bin/python manage.py start

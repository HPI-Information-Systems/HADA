#!/bin/bash

isort --trailing-comma --line-width 120 --multi-line 3 python -q
black --line-length 120 python -q

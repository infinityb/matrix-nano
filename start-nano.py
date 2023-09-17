#!/usr/bin/env python
import nano
import asyncio
import argparse


def _get_arg_parser():
    parser = argparse.ArgumentParser(prog='start-nano')
    parser.add_argument('--config', default='config.json', help='The profile to load')
    return parser


args = _get_arg_parser().parse_args()
try:
    asyncio.run(nano.main(args))
except KeyboardInterrupt:
    pass

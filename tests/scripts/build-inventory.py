# coding: utf-8

import argparse
import os
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from lib import docker

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--compose-dir',
        dest='compose_dir',
        type=Path,
        help="Docker Compose directory. Default: %(default)s",
        default='.',
    )
    env = parser.parse_args()

    docker_inventory = docker.DockerInventory(cwd=env.compose_dir)
    docker_inventory.discover()

    inventory_vars = {}
    for c in docker_inventory.containers:
        # Not `os`: that would shadow the module, and the EDB_OS lookup below
        # would then be a method call on a string.
        (inventory_name, os_name) = c['Service'].split('-')
        container = docker.DockerOSContainer(c['ID'], os_name)
        inventory_vars["%s_ip" % inventory_name] = container.ip()

    templates_dir = str(env.compose_dir)
    file_loader = FileSystemLoader(templates_dir)
    jenv = Environment(loader=file_loader, trim_blocks=True)
    os_template = 'inventory.%s.yml.j2' % os.getenv('EDB_OS', '')
    template_name = (
        os_template
        if (env.compose_dir / os_template).exists()
        else 'inventory.yml.j2'
    )
    template = jenv.get_template(template_name)

    with open(env.compose_dir / 'inventory.yml', 'w') as f:
        f.write(template.render(inventory_vars=inventory_vars))

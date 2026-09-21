#!/usr/bin/env python3

import json
import re
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _relative(path: str) -> str:
    """Normalize a Docker build-context path ("./x" -> "x")."""
    return path[2:] if path.startswith('./') else path


def _logical_lines(text: str):
    """Join Dockerfile lines continued with a trailing backslash."""
    pending = ''
    for raw in text.splitlines():
        stripped = raw.strip()
        if pending:
            pending = f'{pending} {stripped}'
        else:
            pending = stripped
        if pending.endswith('\\'):
            pending = pending[:-1]
            continue
        yield pending
        pending = ''
    if pending:
        yield pending


def check_build_inputs(dockerfile: str) -> None:
    """Fail fast when the build context is missing an input the Dockerfile needs.

    A missing COPY source or bind mount only surfaces after a long image build,
    so it is validated here instead.
    """
    required_vendored = (
        'superset/.xpbuilder-vendor',
        'superset/pyproject.toml',
        'superset/setup.py',
        'superset/MANIFEST.in',
        'superset/README.md',
        'superset/requirements/base.txt',
        'superset/requirements/translations.txt',
        'superset/scripts/check-env.py',
        'superset/docker/apt-install.sh',
        'superset/docker/pip-install.sh',
        'superset/docker/frontend-mem-nag.sh',
        'superset/docker/docker-healthcheck.sh',
        'superset/docker/entrypoints/run-server.sh',
        'superset/superset/translations',
        'superset/superset-core',
        'superset/superset-frontend/package.json',
        'superset/superset-frontend/package-lock.json',
    )
    for relative in required_vendored:
        if not (ROOT / relative).exists():
            raise AssertionError(f'vendored build input missing: {relative}')

    for line in _logical_lines(dockerfile):
        if not line or line.startswith('#'):
            continue
        if line.startswith('RUN') and '--mount=type=bind' in line:
            for token in shlex.split(line):
                if not token.startswith('--mount='):
                    continue
                for part in token.split(','):
                    if part.startswith('source='):
                        source = _relative(part.split('=', 1)[1])
                        if not (ROOT / source).exists():
                            raise AssertionError(f'bind mount source missing: {source}')
            continue
        if not line.startswith('COPY'):
            continue
        tokens = shlex.split(line)
        if any(token.startswith('--from=') for token in tokens):
            continue
        sources = [
            token for token in tokens[1:]
            if not token.startswith('--')
        ][:-1]
        for source in sources:
            relative = _relative(source)
            if '*' in relative:
                if not list(ROOT.glob(relative)):
                    raise AssertionError(f'COPY source glob matched nothing: {source}')
            elif not (ROOT / relative).exists():
                raise AssertionError(f'COPY source missing: {source}')


def main():
    version = (ROOT / 'VERSION').read_text(encoding='utf-8').strip()
    compatibility = json.loads(
        (ROOT / 'compatibility.json').read_text(encoding='utf-8')
    )

    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise AssertionError(f'VERSION is not semantic: {version}')
    if compatibility['xpbuilder_version'] != version:
        raise AssertionError('VERSION and compatibility.json disagree')
    if compatibility['contract_version'] != '1.0':
        raise AssertionError('unexpected runtime contract version')
    if compatibility.get('runtime') != 'standalone':
        raise AssertionError('runtime must be declared standalone')
    if compatibility.get('moodle_integration') is not False:
        raise AssertionError('moodle_integration must be false for this runtime')

    superset = compatibility['superset']
    if superset['build'] != 'source':
        raise AssertionError('superset build must be "source"')
    if superset['version'] != '6.1.0':
        raise AssertionError('superset version changed unexpectedly')

    marker = (ROOT / 'superset' / '.xpbuilder-vendor').read_text(encoding='utf-8')
    if superset['source_tag'] not in marker:
        raise AssertionError('vendored source tag does not match compatibility.json')
    if superset['source_commit'] not in marker:
        raise AssertionError('vendored source commit does not match compatibility.json')

    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    if re.search(r'^\s*FROM\s+apache/superset', dockerfile, re.M):
        raise AssertionError('runtime must be built from the vendored source, not a registry image')
    if 'apache/superset:latest' in dockerfile:
        raise AssertionError('floating Superset image tag is forbidden')
    if 'superset-src' not in dockerfile:
        raise AssertionError('Dockerfile is missing the source prep stage')

    # This runtime is Moodle-free: no Moodle wiring may leak back into the
    # scaffolding (the vendored Superset source is checked separately).
    forbidden = ('MOODLE_', 'moodle_network', 'local_xpromptsuperset', 'mariadb-replica')
    for relative in ('compose.yml', 'config/superset_config.py', 'docker/initialize.sh'):
        content = (ROOT / relative).read_text(encoding='utf-8')
        for token in forbidden:
            if token in content:
                raise AssertionError(f'Moodle wiring "{token}" found in {relative}')

    check_build_inputs(dockerfile)

    print('Compatibility contract checks passed')


if __name__ == '__main__':
    main()

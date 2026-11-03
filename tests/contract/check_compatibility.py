#!/usr/bin/env python3

import json
import os
import re
import shlex
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

MOODLE_FLAG = 'XPBUILDER_MOODLE_INTEGRATION'
MOODLE_FLAG_TRUTHY = ('1', 'true', 'yes', 'on')


def moodle_integration_enabled() -> bool:
    """Whether this run exercises the optional Moodle integration mode.

    One image serves both modes, so the contract is evaluated for the mode the
    caller asked for instead of assuming the standalone default.
    """
    value = os.environ.get(MOODLE_FLAG, 'false').strip().lower()
    return value in MOODLE_FLAG_TRUTHY


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


def check_ai_provider_wiring(compatibility: dict) -> None:
    """The AI Provider settings page spans two services; keep them in sync.

    Provider API keys are typed into a Superset settings page but sealed in the
    XP Prompt database, so the two sides share one token derivation and one
    routing contract. A silent drift here would break every provider save with a
    401 that is hard to diagnose from the UI.
    """
    settings_page = compatibility.get('optional_features', {}).get(
        'ai_provider_settings'
    )
    if not isinstance(settings_page, dict):
        raise AssertionError(
            'ai_provider_settings must be declared under optional_features'
        )

    secret_module = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'security.py'
    ).read_text(encoding='utf-8')
    proxy_view = (
        ROOT / 'superset' / 'superset' / 'views' / 'ai_provider' / 'views.py'
    ).read_text(encoding='utf-8')

    salt = settings_page['internal_token_salt']
    pattern = re.compile(r'xp-prompt-internal-admin-v1')
    if not pattern.search(secret_module):
        raise AssertionError('XP Prompt security module lost the token salt')
    if f'"{salt}"'.replace('"', "'") not in proxy_view.replace('"', "'"):
        raise AssertionError(
            'AI Provider proxy token salt drifted from the XP Prompt service'
        )

    compose = (ROOT / 'compose.yml').read_text(encoding='utf-8')
    for marker in (
        'XP_PROMPT_SERVICE_SECRET',
        'XP_PROMPT_API_URL',
    ):
        if marker not in compose:
            raise AssertionError(f'compose.yml must pass {marker} to the services')

    initialize = (ROOT / 'docker' / 'initialize.sh').read_text(encoding='utf-8')
    if 'ensure_ai_provider_permissions.py' not in initialize:
        raise AssertionError(
            'initialize.sh must grant the AI Provider permissions'
        )

    # Health/retry policy: the routing constants are mirrored in the manifest so
    # the settings page, docs, and tests cannot drift from the service.
    health_module = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'providers' / 'health.py'
    ).read_text(encoding='utf-8')
    retry_policy = settings_page.get('retry_policy', {})
    for key, constant in (
        ('max_attempts_per_provider', 'MAX_ATTEMPTS_PER_PROVIDER'),
        ('failure_threshold', 'FAILURE_THRESHOLD'),
        ('max_open_seconds', 'MAX_OPEN_SECONDS'),
    ):
        expected = retry_policy.get(key)
        if expected is None:
            raise AssertionError(f'retry_policy.{key} must be declared')
        if not re.search(rf'^{constant}\s*=\s*{expected}\b', health_module, re.MULTILINE):
            raise AssertionError(
                f'{constant} drifted from compatibility.json retry_policy.{key}'
            )

    migrations = ROOT / 'services' / 'xp_prompt' / 'migrations' / 'versions'
    revision = settings_page.get('health_migration')
    if not revision or not list(migrations.glob(f'{revision}_*.py')):
        raise AssertionError(
            'health_migration must name an existing XP Prompt migration'
        )


def check_ai_prompt_wiring(compatibility: dict) -> None:
    """The XP Prompt chat spans the same two services as provider settings.

    Two invariants keep the workspace safe: the proxy must derive the caller's
    identity from the Superset session (never from the request body), and the
    store must scope every query by that identity. A regression in either place
    would expose another user's conversation.
    """
    workspace = compatibility.get('optional_features', {}).get(
        'ai_prompt_workspace'
    )
    if not isinstance(workspace, dict):
        raise AssertionError(
            'ai_prompt_workspace must be declared under optional_features'
        )

    proxy_view = (ROOT / workspace['proxy']).read_text(encoding='utf-8')
    reads_session = 'g.user' in proxy_view or 'getattr(g, "user"' in proxy_view
    if not reads_session:
        raise AssertionError(
            'the XP Prompt proxy must read the caller from the FAB session (g.user)'
        )
    if 'payload["superset_user_id"] = owner' not in proxy_view:
        raise AssertionError(
            'the XP Prompt proxy must overwrite, not trust, superset_user_id'
        )
    if 'call_xp_prompt' not in proxy_view:
        raise AssertionError(
            'the XP Prompt proxy must reuse call_xp_prompt so the token stays shared'
        )

    store = (ROOT / workspace['store']).read_text(encoding='utf-8')
    if store.count('superset_user_id = %s') < 3:
        raise AssertionError(
            'every conversation query must be scoped by superset_user_id'
        )

    turn = (ROOT / workspace['turn']).read_text(encoding='utf-8')
    if 'resolve_router' not in turn:
        raise AssertionError(
            'the chat turn must go through resolve_router so routing policy applies'
        )

    # Intent routing (P2-F04) runs before any provider cost is spent, so the
    # classifier and the clarification outcome must stay wired into the turn.
    intent_router = workspace.get('intent_router')
    if not intent_router:
        raise AssertionError(
            'ai_prompt_workspace.intent_router must be declared'
        )
    router_source = (ROOT / intent_router).read_text(encoding='utf-8')
    intent_names = workspace.get('intents') or []
    if len(intent_names) != 7:
        raise AssertionError(
            'ai_prompt_workspace.intents must list the seven routable outcomes'
        )
    for name in intent_names:
        if f'{name.upper()} = "{name}"' not in router_source:
            raise AssertionError(
                f'intent "{name}" is missing from {intent_router}'
            )
    if 'intents.classify(' not in turn:
        raise AssertionError(
            'the chat turn must route every message through intents.classify'
        )
    if '"kind": "clarification"' not in turn:
        raise AssertionError(
            'a clarification turn must be stored with kind=clarification'
        )

    # Explicit context reset (P2-F03): the transcript is kept, the replay is
    # scoped by generation, and the client must be able to reset it.
    if 'context_version=current_version' not in turn:
        raise AssertionError(
            'the chat turn must replay only the current context generation'
        )
    if 'm.context_version = %s' not in store:
        raise AssertionError(
            'list_messages must be able to scope by context_version'
        )
    if 'context_version = c.context_version + 1' not in store:
        raise AssertionError(
            'reset_context must bump the generation instead of deleting messages'
        )
    if 'DELETE' in store.split('def reset_context', 1)[-1].split('def ', 1)[0]:
        raise AssertionError('reset_context must not delete the transcript')

    service = (ROOT / workspace['service']).read_text(encoding='utf-8')
    if '/context/reset' not in service:
        raise AssertionError(
            'the XP Prompt service must expose the context reset endpoint'
        )
    if 'reset-context' not in proxy_view:
        raise AssertionError(
            'the XP Prompt proxy must expose the context reset route'
        )

    context_migration = workspace.get('context_migration')
    if not context_migration:
        raise AssertionError(
            'ai_prompt_workspace.context_migration must be declared'
        )
    versions = ROOT / 'services' / 'xp_prompt' / 'migrations' / 'versions'
    if not list(versions.glob(f'{context_migration}_*.py')):
        raise AssertionError(
            'context_migration must name an existing XP Prompt migration'
        )

    initialize = (ROOT / 'docker' / 'initialize.sh').read_text(encoding='utf-8')
    script_name = workspace['permission_script'].rsplit('/', 1)[-1]
    if script_name not in initialize:
        raise AssertionError(
            f'initialize.sh must run {script_name}'
        )

    dockerfile = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    if script_name not in dockerfile:
        raise AssertionError(f'Dockerfile must ship {script_name}')

    routes = (
        ROOT / 'superset' / 'superset-frontend' / 'src' / 'views' / 'routes.tsx'
    ).read_text(encoding='utf-8')
    if workspace['page'] not in routes:
        raise AssertionError(
            f"SPA routes must register {workspace['page']}"
        )

    conversations = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'conversations.py'
    ).read_text(encoding='utf-8')
    expected_limit = workspace.get('max_message_chars')
    if expected_limit is not None and (
        f'MAX_MESSAGE_CHARS = {expected_limit:_}' not in conversations
        and f'MAX_MESSAGE_CHARS = {expected_limit}' not in conversations
    ):
        raise AssertionError(
            'MAX_MESSAGE_CHARS drifted from compatibility.json max_message_chars'
        )


def check_ai_job_wiring(compatibility: dict) -> None:
    """The job engine must stay the only way a turn does multi-step work.

    Three invariants: the declared status vocabulary matches the database check
    constraint (a new status that the schema rejects would fail at runtime), every
    declared job type has a registered handler, and the API/worker entry points
    exist. Recovery is part of the contract, because a job that cannot be
    reclaimed after a worker restart is a job that silently disappears.
    """
    workspace = compatibility.get('optional_features', {}).get(
        'ai_prompt_workspace'
    )
    if not isinstance(workspace, dict):
        raise AssertionError(
            'ai_prompt_workspace must be declared under optional_features'
        )

    jobs_module = workspace.get('jobs_module')
    jobs_migration = workspace.get('jobs_migration')
    if not jobs_module or not jobs_migration:
        raise AssertionError(
            'jobs_module and jobs_migration must be declared'
        )

    source = (ROOT / jobs_module).read_text(encoding='utf-8')
    for status in workspace.get('job_statuses', []):
        if f'{status.upper()} = "{status}"' not in source:
            raise AssertionError(f'job status "{status}" is missing from {jobs_module}')

    versions = ROOT / 'services' / 'xp_prompt' / 'migrations' / 'versions'
    migration_files = list(versions.glob(f'{jobs_migration}_*.py'))
    if not migration_files:
        raise AssertionError('jobs_migration must name an existing migration')

    # The schema is the authority for the status vocabulary.
    initial = (
        ROOT / 'services' / 'xp_prompt' / 'migrations' / 'versions'
        / '20261103_01_initial_control_plane.py'
    ).read_text(encoding='utf-8')
    for status in workspace.get('job_statuses', []):
        if f"'{status}'" not in initial:
            raise AssertionError(
                f'job status "{status}" is not accepted by ck_ai_job_status'
            )

    for job_type in workspace.get('job_types', []):
        constant = f'{job_type.upper()}_JOB_TYPE'
        if f'{constant} = "{job_type}"' not in source:
            raise AssertionError(
                f'job type "{job_type}" must be declared as {constant}'
            )
        if f'register_job_handler({constant})' not in source:
            raise AssertionError(
                f'job type "{job_type}" has no registered handler'
            )

    for marker in ('def recover_expired', 'def claim_job', 'def run_job'):
        if marker not in source:
            raise AssertionError(f'{jobs_module} lost {marker}')

    service = (ROOT / workspace['service']).read_text(encoding='utf-8')
    for route in ('/ai/jobs', '/cancel', '/retry', '/confirm', '/reject'):
        if route not in service:
            raise AssertionError(
                f'the XP Prompt service must expose the job route {route}'
            )

    # Confirmation (P2-F08) must be structural: mutating jobs start unconfirmed
    # and the claim statement only takes queued work.
    if 'MUTATING_JOB_TYPES' not in source:
        raise AssertionError(
            f'{jobs_module} must track mutating job types'
        )
    if "status = 'queued'" not in source:
        raise AssertionError(
            'claim_job must only claim queued (confirmed) work'
        )

    proxy_view = (ROOT / workspace['proxy']).read_text(encoding='utf-8')
    for marker in ('/jobs/', '_JOB_ACTIONS', '"confirm": "confirm"'):
        if marker not in proxy_view:
            raise AssertionError(
                f'the XP Prompt proxy must expose job operations ({marker})'
            )

    tasks = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'tasks.py'
    ).read_text(encoding='utf-8')
    if 'xp_prompt.run_job' not in tasks or 'xp_prompt.recover_jobs' not in tasks:
        raise AssertionError(
            'the worker must expose run_job and recover_jobs tasks'
        )


def check_ai_response_blocks(compatibility: dict) -> None:
    """One block vocabulary must be shared by the service, the types, the renderer.

    A block type the service can emit but the page cannot render is an invisible
    answer; a renderer for a type the service never sends is dead code. Both are
    caught here instead of in a user's transcript.
    """
    workspace = compatibility.get('optional_features', {}).get(
        'ai_prompt_workspace'
    )
    if not isinstance(workspace, dict):
        raise AssertionError(
            'ai_prompt_workspace must be declared under optional_features'
        )

    blocks = workspace.get('response_blocks') or []
    if not blocks:
        raise AssertionError('response_blocks must be declared')

    module_path = workspace.get('response_blocks_module')
    types_path = workspace.get('response_blocks_types')
    renderer_path = workspace.get('response_blocks_renderer')
    for label, path in (
        ('response_blocks_module', module_path),
        ('response_blocks_types', types_path),
        ('response_blocks_renderer', renderer_path),
    ):
        if not path:
            raise AssertionError(f'{label} must be declared')

    source = (ROOT / module_path).read_text(encoding='utf-8')
    types_file = (ROOT / types_path).read_text(encoding='utf-8')
    renderer = (ROOT / renderer_path).read_text(encoding='utf-8')
    for block in blocks:
        if f'{block.upper()} = "{block}"' not in source:
            raise AssertionError(f'block "{block}" is missing from {module_path}')
        if f"type: '{block}'" not in types_file:
            raise AssertionError(f'block "{block}" is missing from {types_path}')
        if f"case '{block}':" not in renderer:
            raise AssertionError(f'block "{block}" has no renderer case')
    if 'BLOCK_TYPES' not in source:
        raise AssertionError(f'{module_path} must publish BLOCK_TYPES')

    turn = (ROOT / workspace['turn']).read_text(encoding='utf-8')
    for marker in ('responses.answer_blocks(', 'responses.clarification_blocks('):
        if marker not in turn:
            raise AssertionError(f'the chat turn must emit blocks via {marker})')


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

    # Moodle integration is an OPT-IN mode of the same runtime: the manifest
    # still declares the standalone default, plus the optional feature that
    # turns the linked mode on.
    optional = compatibility.get('optional_features', {})
    moodle = optional.get('moodle_integration')
    if not isinstance(moodle, dict):
        raise AssertionError(
            'moodle_integration must be declared under optional_features'
        )
    if moodle.get('flag') != MOODLE_FLAG:
        raise AssertionError('unexpected Moodle integration flag name')
    if moodle.get('default') is not False:
        raise AssertionError('Moodle integration must default to off')

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

    # The scaffolding must not wire Moodle CONNECTIVITY (a replica network, a
    # read-only replica, or the Moodle plugin bridge) in either mode — the
    # integration only adds the opt-in Sync-tables API. MOODLE_FLAG itself is
    # allowed, since it is the mode switch rather than a connection.
    forbidden = (
        'moodle_network',
        'MARIADB_SERVER_ID',
        'MOODLE_DB_HOST',
        'MOODLE_DB_PORT',
        'MOODLE_DB_NAME',
        'MOODLE_REPORTING_USER',
        'MOODLE_REPORTING_PASSWORD',
        'local_xpromptsuperset',
        'mariadb-replica',
    )
    scanned = ('compose.yml', 'config/superset_config.py', 'docker/initialize.sh')
    for relative in scanned:
        content = (ROOT / relative).read_text(encoding='utf-8')
        for token in forbidden:
            if token in content:
                raise AssertionError(f'Moodle wiring "{token}" found in {relative}')

    # When the linked mode is requested the flag must actually reach the
    # container, otherwise the mode would silently fall back to standalone.
    if moodle_integration_enabled():
        compose = (ROOT / 'compose.yml').read_text(encoding='utf-8')
        if MOODLE_FLAG not in compose:
            raise AssertionError(f'{MOODLE_FLAG} must be passed through by compose.yml')

    check_build_inputs(dockerfile)

    check_ai_provider_wiring(compatibility)

    check_ai_prompt_wiring(compatibility)

    check_ai_job_wiring(compatibility)

    check_ai_response_blocks(compatibility)

    print('Compatibility contract checks passed')


if __name__ == '__main__':
    main()

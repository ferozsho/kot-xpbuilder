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

    # A route whose permission was never granted answers 403 for every non-admin
    # role, so the grant list has to follow the view: derive it from the source.
    permission_script = (ROOT / workspace['permission_script']).read_text(encoding='utf-8')
    routed_methods = re.findall(
        r'^    def (api_[a-z0-9_]+)\(', proxy_view, re.MULTILINE
    )
    if not routed_methods:
        raise AssertionError('the XP Prompt proxy exposes no api_* routes')
    for method in routed_methods:
        if f'"can_{method}"' not in permission_script:
            raise AssertionError(
                f'route {method} has no granted permission "can_{method}"'
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


def check_ai_artifact_wiring(compatibility: dict) -> None:
    """Every artifact type shares one lifecycle, and publishing needs confirmation.

    The status/type vocabulary must match the database constraint (a status the
    schema rejects would fail at runtime), the publish statement must be guarded by
    ``status = 'confirmed'``, and the API must expose the three transitions.
    """
    workspace = compatibility.get('optional_features', {}).get(
        'ai_prompt_workspace'
    )
    if not isinstance(workspace, dict):
        raise AssertionError(
            'ai_prompt_workspace must be declared under optional_features'
        )

    module_path = workspace.get('artifacts_module')
    migration = workspace.get('artifacts_migration')
    if not module_path or not migration:
        raise AssertionError('artifacts_module and artifacts_migration must be declared')

    source = (ROOT / module_path).read_text(encoding='utf-8')
    for status in workspace.get('artifact_statuses', []):
        if f'{status.upper()} = "{status}"' not in source:
            raise AssertionError(f'artifact status "{status}" is missing from {module_path}')
    for artifact_type in workspace.get('artifact_types', []):
        if f'"{artifact_type}"' not in source:
            raise AssertionError(
                f'artifact type "{artifact_type}" is missing from {module_path}'
            )

    versions = ROOT / 'services' / 'xp_prompt' / 'migrations' / 'versions'
    migration_files = list(versions.glob(f'{migration}_*.py'))
    if not migration_files:
        raise AssertionError('artifacts_migration must name an existing migration')
    schema_source = migration_files[0].read_text(encoding='utf-8')
    for status in workspace.get('artifact_statuses', []):
        if f"'{status}'" not in schema_source:
            raise AssertionError(
                f'artifact status "{status}" is not accepted by ck_ai_artifact_status'
            )
    for artifact_type in workspace.get('artifact_types', []):
        if f"'{artifact_type}'" not in schema_source:
            raise AssertionError(
                f'artifact type "{artifact_type}" is not accepted by ck_ai_artifact_type'
            )

    if "a.status = 'confirmed'" not in source:
        raise AssertionError(
            'publish_artifact must only accept a confirmed artifact'
        )
    if 'def publish_artifact' not in source or 'def confirm_artifact' not in source:
        raise AssertionError(f'{module_path} lost a lifecycle transition')

    service = (ROOT / workspace['service']).read_text(encoding='utf-8')
    for route in ('/ai/artifacts', '/artifacts', '"confirm", "discard", "publish"'):
        if route not in service:
            raise AssertionError(
                f'the XP Prompt service must expose the artifact route {route}'
            )


def check_ai_sql_guard(compatibility: dict) -> None:
    """The read-only guard is the only thing standing between a model and a DB.

    The manifest is the acceptance list: every declared dialect must be supported
    by the guard, every declared denied schema must be in its denylist, the row cap
    must match, and the adversarial corpus must cover the refusal codes — a guard
    without its corpus is a guard nobody has tried to break.
    """
    guard = compatibility.get('ai_sql_guard')
    if not isinstance(guard, dict):
        raise AssertionError('ai_sql_guard must be declared')

    module_path = guard.get('module')
    corpus_path = guard.get('corpus')
    if not module_path or not corpus_path:
        raise AssertionError('ai_sql_guard.module and .corpus must be declared')

    source = (ROOT / module_path).read_text(encoding='utf-8')
    for dialect in guard.get('dialects', []):
        if f'"{dialect}"' not in source:
            raise AssertionError(f'SQL guard must support dialect "{dialect}"')
    for schema in guard.get('denied_schemas', []):
        if f'"{schema}"' not in source:
            raise AssertionError(f'SQL guard must deny schema "{schema}"')

    max_rows = guard.get('max_rows')
    if max_rows is None or f'MAX_ROWS = {max_rows:_}' not in source and (
        f'MAX_ROWS = {max_rows}' not in source
    ):
        raise AssertionError('MAX_ROWS drifted from compatibility.json ai_sql_guard.max_rows')

    for marker in (
        'def check(',
        'DENIED_FUNCTIONS',
        'DENIED_SCHEMAS',
        '_EXECUTABLE_COMMENT',
        'multiple_statements',
        'not_read_only',
        'unsafe_function',
        'system_schema',
        'unknown_table',
    ):
        if marker not in source:
            raise AssertionError(f'{module_path} lost {marker}')

    corpus = (ROOT / corpus_path).read_text(encoding='utf-8')
    for code in (
        'multiple_statements',
        'not_read_only',
        'unsafe_function',
        'system_schema',
        'unknown_table',
        'obfuscation',
        'parse_error',
        'unsupported_dialect',
    ):
        if code not in corpus:
            raise AssertionError(
                f'the adversarial corpus must cover the "{code}" refusal'
            )

    # P4-F04: a candidate is only a candidate once the guard has seen it, and the
    # structured answer must not be starved by a thinking budget.
    candidate_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'sql_candidate.py'
    candidate = candidate_path.read_text(encoding='utf-8')
    if 'sql_guard.check(' not in candidate:
        raise AssertionError('candidates must pass through sql_guard.check')
    if 'reasoning_budget_tokens=0' not in candidate:
        raise AssertionError(
            'structured candidates must disable provider thinking so the JSON is not truncated'
        )
    if 'allowed_tables' not in candidate:
        raise AssertionError('candidates must be checked against the schema allowlist')
    service = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'api.py'
    ).read_text(encoding='utf-8')
    if '/ai/sql/candidate' not in service:
        raise AssertionError('the XP Prompt service must expose /ai/sql/candidate')

    # P4-F09: execution stays inside Superset (it owns the analytics credentials),
    # and the statement is re-validated server-side immediately before it runs.
    proxy_source = (
        ROOT / compatibility['ai_schema_slice']['proxy']
    ).read_text(encoding='utf-8')
    if '/api/sql/execute/' not in proxy_source:
        raise AssertionError('the proxy must expose the guarded execution route')
    for marker in ('raise_for_access(', 'get_df('):
        if marker not in proxy_source:
            raise AssertionError(
                f'the execution route must go through Superset ({marker})'
            )
    if '/ai/sql/validate' not in service:
        raise AssertionError(
            'the service must re-validate a statement before execution'
        )
    execution = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'sql_execution.py'
    )
    if not execution.exists():
        raise AssertionError('sql_execution.py must exist')
    if 'sql_guard.check(' not in execution.read_text(encoding='utf-8'):
        raise AssertionError('execution-time validation must call sql_guard.check')

    # P4-F10: the service decides what a caller may see of a result; the proxy must
    # not shape or mask results itself.
    validation_path = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'result_validation.py'
    )
    if not validation_path.exists():
        raise AssertionError('result_validation.py must exist')
    validation = validation_path.read_text(encoding='utf-8')
    for marker in ('def validate_result', 'SENSITIVE_HEADER', 'RESULT_PREVIEW'):
        if marker not in validation:
            raise AssertionError(f'result validation lost {marker}')
    if '/ai/sql/result' not in service:
        raise AssertionError('the service must expose /ai/sql/result')
    if '/ai/sql/result' not in proxy_source:
        raise AssertionError(
            'the execution route must ask the service to validate the result'
        )

    # The whole pipeline has to be reachable from one turn: candidate in the
    # service, execution in Superset, storage back in the service.
    if '/api/conversations/<conversation_id>/sql/' not in proxy_source:
        raise AssertionError('the proxy must expose the end-to-end SQL turn')
    if '/ai/sql/candidate' not in proxy_source:
        raise AssertionError('the SQL turn must ask the service for a candidate')
    if '@app.post("/ai/conversations/<conversation_id>/blocks")' not in service:
        raise AssertionError('the service must expose the block-append endpoint')
    blocks_route = service.split('def conversation_append_blocks', 1)[-1][:1200]
    if 'responses.normalize_blocks(' not in blocks_route:
        raise AssertionError(
            'a stored turn must re-normalize its blocks instead of trusting the caller'
        )

    # A Superset SQL dataset has no physical table, so the pipeline must be able to
    # inline its definition; without that, every virtual dataset fails at execution
    # with UndefinedTable (found live, runtime-evidence §7.24–§7.25).
    slice_source = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'schema_slice.py'
    ).read_text(encoding='utf-8')
    guard_source = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'sql_guard.py'
    ).read_text(encoding='utf-8')
    execution_source = execution.read_text(encoding='utf-8')
    for marker in ('def virtual_tables', 'def is_virtual', 'def _dataset_query'):
        if marker not in slice_source:
            raise AssertionError(
                f'the schema slice lost virtual dataset support: {marker}'
            )
    for marker in ('def _apply_dataset_scope', 'def _virtual_dataset_query'):
        if marker not in guard_source:
            raise AssertionError(f'the SQL guard lost virtual dataset inlining: {marker}')
    if 'virtual_tables=schema.virtual_tables(' not in execution_source:
        raise AssertionError(
            'execution must inline virtual datasets before running a statement'
        )
    if '"sql": str(definition)' not in proxy_source:
        raise AssertionError(
            'the proxy must forward a virtual dataset definition to the service'
        )

    # P4-F06: row-level security is rendered by Superset (it owns identity and
    # Jinja) and applied by the guard to every reference — never requested from a
    # prompt, never silently dropped.
    if 'get_sqla_row_level_filters' not in proxy_source:
        raise AssertionError(
            'the slice must carry the principals rendered row-level rules'
        )
    if '"rls":' not in proxy_source:
        raise AssertionError('the dataset payload must include its row-level rules')
    for marker in ('def rls_targets', 'def rls_clause'):
        if marker not in slice_source:
            raise AssertionError(f'the schema slice lost row-level support: {marker}')
    for marker in ('_rls_definitions(', '_apply_dataset_scope('):
        if marker not in guard_source:
            raise AssertionError(f'the SQL guard lost row-level rewriting: {marker}')
    if 'rls_invalid' not in guard_source:
        raise AssertionError(
            'an unparseable row-level policy must be refused, not dropped'
        )
    if 'rls_targets=schema.rls_targets(' not in execution_source:
        raise AssertionError(
            'execution must re-apply row-level scope before running a statement'
        )

    # P4-F08: the deployment measures the plan where the connection is, and the
    # service owns the budget — a caller must not be able to grade its own query.
    cost_guard_path = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'cost_guard.py'
    )
    if not cost_guard_path.exists():
        raise AssertionError('cost_guard.py must exist')
    cost_guard_source = cost_guard_path.read_text(encoding='utf-8')
    for marker in ('def evaluate', 'PLAN_UNAVAILABLE', 'ESTIMATED_ROWS_TOO_HIGH'):
        if marker not in cost_guard_source:
            raise AssertionError(f'the cost guard lost {marker}')
    if '/ai/sql/plan' not in service:
        raise AssertionError('the service must expose /ai/sql/plan')
    if 'cost_guard.evaluate(' not in service:
        raise AssertionError('the plan route must use the cost guard')
    for marker in ('def _explain_estimate', 'def _postgres_plan', 'def _cost_gate'):
        if marker not in proxy_source:
            raise AssertionError(f'the cost gate is missing from the proxy: {marker}')
    if '"/ai/sql/plan"' not in proxy_source:
        raise AssertionError('the joined turn must ask the service to judge its cost')
    if 'runtime_budget_exceeded' not in proxy_source:
        raise AssertionError('the wall-clock budget must be enforced in the turn')

    # P4-F12: every turn — including a refusal — leaves an audit row, the statement
    # is sealed at rest, and it is only revealed on an explicit, enabled request.
    audit_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'audit.py'
    if not audit_path.exists():
        raise AssertionError('audit.py must exist')
    audit_source = audit_path.read_text(encoding='utf-8')
    service_audit_settings = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'config.py'
    ).read_text(encoding='utf-8')
    permissions_source = (
        ROOT / 'docker' / 'ensure_ai_prompt_permissions.py'
    ).read_text(encoding='utf-8')
    for marker in ('def record_turn', 'def list_turns', 'sql_ciphertext', 'SecretBox'):
        if marker not in audit_source:
            raise AssertionError(f'the audit trail lost {marker}')
    if 'include_sql' not in audit_source:
        raise AssertionError('a stored statement must be hidden unless requested')
    if '/ai/sql/audit' not in service:
        raise AssertionError('the service must expose /ai/sql/audit')
    if '/audit' not in service:
        raise AssertionError('the service must expose the conversation audit reader')
    if 'allow_sql_diagnostics' not in service_audit_settings:
        raise AssertionError(
            'revealing a stored statement must be gated by a deployment setting'
        )
    if 'def _audit_turn' not in proxy_source or '"/ai/sql/audit"' not in proxy_source:
        raise AssertionError('the joined turn must record its outcome')
    if '/audit/' not in proxy_source:
        raise AssertionError('the proxy must expose the audit reader')
    if 'can_api_conversation_audit' not in permissions_source:
        raise AssertionError('the audit route must be granted to the workspace roles')

    # P4-F07: the guard proves a statement is safe; semantics prove it is meaningful.
    # Both run on the statement the *model* wrote (never on an inlined dataset
    # definition, which is the administrator's SQL).
    semantics_path = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'sql_semantics.py'
    )
    if not semantics_path.exists():
        raise AssertionError('sql_semantics.py must exist')
    semantics_source = semantics_path.read_text(encoding='utf-8')
    for marker in (
        'UNKNOWN_COLUMN',
        'NON_NUMERIC_AGGREGATE',
        'UNGROUPED_COLUMN',
        'def validate',
    ):
        if marker not in semantics_source:
            raise AssertionError(f'semantic validation lost {marker}')
    if 'model_sql' not in guard_source:
        raise AssertionError(
            'the guard must expose the pre-inlining statement for semantic checks'
        )
    if '_check_semantics(' not in candidate:
        raise AssertionError('a candidate must be checked for meaning, not only safety')
    if 'sql_semantics.validate(' not in execution_source:
        raise AssertionError(
            'execution must re-validate semantics before running a statement'
        )
    if '"semantic_invalid": 422' not in service:
        raise AssertionError('a semantic refusal must answer 422, not a server error')

    # P4-F11: only candidates are cached, the key covers schema *and* policy, an
    # explicit bypass is honoured, and the status is recorded in the audit.
    cache_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'cache.py'
    if not cache_path.exists():
        raise AssertionError('cache.py must exist')
    cache_source = cache_path.read_text(encoding='utf-8')
    for marker in ('def cache_key', 'CACHE_VERSION', 'def load', 'def store'):
        if marker not in cache_source:
            raise AssertionError(f'the candidate cache lost {marker}')
    if 'fingerprint' not in cache_source:
        raise AssertionError(
            'the cache key must include the slice fingerprint (schema + policy)'
        )
    if 'bypass_cache' not in candidate:
        raise AssertionError('candidate generation must honour an explicit bypass')
    if 'bypass_cache' not in service:
        raise AssertionError('the candidate route must accept a bypass request')
    if 'regenerate' not in proxy_source:
        raise AssertionError('the proxy must forward an explicit regenerate request')
    if 'cache_status' not in audit_source:
        raise AssertionError('a cached turn must be auditable')
    if 'cache_status' not in service:
        raise AssertionError('the audit route must accept the cache status')
    migration = (
        ROOT
        / 'services'
        / 'xp_prompt'
        / 'migrations'
        / 'versions'
        / '20261103_08_ai_sql_audit_cache.py'
    )
    if not migration.exists():
        raise AssertionError('the cache-status column needs its own migration')

    # P4-F01: a materially ambiguous question is asked about, not guessed at.
    plan_path = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'question_plan.py'
    )
    if not plan_path.exists():
        raise AssertionError('question_plan.py must exist')
    plan_source = plan_path.read_text(encoding='utf-8')
    for marker in ('MEASURE_AMBIGUOUS', 'UNKNOWN_REFERENCE', 'BLOCKING_CODES'):
        if marker not in plan_source:
            raise AssertionError(f'question planning lost {marker}')
    if 'ambiguous_question' not in candidate:
        raise AssertionError('a blocking ambiguity must stop candidate generation')
    if '"ambiguous_question": 422' not in service:
        raise AssertionError('an ambiguous question must answer 422')
    if 'question_plan.analyse(' not in candidate:
        raise AssertionError('the candidate prompt must carry the question outline')

    # P4-F03: examples are tenant-scoped, dialect-scoped and slice-scoped, and are
    # only recorded after a statement passed both checks.
    examples_path = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'examples.py'
    )
    if not examples_path.exists():
        raise AssertionError('examples.py must exist')
    examples_source = examples_path.read_text(encoding='utf-8')
    for marker in ('def find_examples', 'def record_example', 'tenant_id = %s'):
        if marker not in examples_source:
            raise AssertionError(f'the example library lost {marker}')
    if 'schema_fingerprint' not in examples_source:
        raise AssertionError('examples must be scoped to the slice fingerprint')
    if 'examples.record_example(' not in candidate:
        raise AssertionError('a validated candidate must feed the example library')
    example_migration = (
        ROOT
        / 'services'
        / 'xp_prompt'
        / 'migrations'
        / 'versions'
        / '20261103_09_ai_sql_example.py'
    )
    if not example_migration.exists():
        raise AssertionError('the example library needs its own migration')

    # P5-F01/F06: one taxonomy, one budget, and terminal policy outcomes that can
    # never be retried.
    failures_path = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'failures.py'
    )
    if not failures_path.exists():
        raise AssertionError('failures.py must exist')
    failures_source = failures_path.read_text(encoding='utf-8')
    for marker in ('TERMINAL_KINDS', 'def classify', 'def can_repair', 'def fingerprint'):
        if marker not in failures_source:
            raise AssertionError(f'the failure taxonomy lost {marker}')
    if 'terminal_policy' not in failures_source:
        raise AssertionError('a policy refusal must be terminal for the repair loop')
    if '/ai/sql/failure' not in service:
        raise AssertionError('the service must classify failures')
    if 'def _classify_failure' not in proxy_source:
        raise AssertionError('the proxy must ask the service to classify a failure')
    repair_migration = (
        ROOT
        / 'services'
        / 'xp_prompt'
        / 'migrations'
        / 'versions'
        / '20261103_10_ai_repair_attempt.py'
    )
    if not repair_migration.exists():
        raise AssertionError('the repair ledger needs its own migration')

    # P5-F02/F03/F08/F09: knowledge is tenant- and scope-scoped, only a promoted
    # pattern is reusable, and a disabled one stays disabled.
    patterns_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'patterns.py'
    if not patterns_path.exists():
        raise AssertionError('patterns.py must exist')
    patterns_source = patterns_path.read_text(encoding='utf-8')
    for marker in (
        'def find(',
        'def rank(',
        'def promote(',
        'def set_state(',
        'def revalidate_stale(',
        "state = 'promoted'",
    ):
        if marker not in patterns_source:
            raise AssertionError(f'the pattern library lost {marker}')
    if 'tenant_id = %s AND state = ' not in patterns_source:
        raise AssertionError('pattern search must filter by tenant and state')
    if 'def confidence(' not in patterns_source:
        raise AssertionError('ranking must be explainable from validation history')
    pattern_migration = (
        ROOT
        / 'services'
        / 'xp_prompt'
        / 'migrations'
        / 'versions'
        / '20261103_11_ai_repair_pattern.py'
    )
    if not pattern_migration.exists():
        raise AssertionError('the pattern library needs its own migration')
    if 'uq_ai_repair_pattern_scope' not in pattern_migration.read_text(encoding='utf-8'):
        raise AssertionError('a correction must be unique within its scope')

    # P5-F04/F05: a repair re-enters the same pipeline; it never bypasses it.
    repair_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'repair.py'
    if not repair_path.exists():
        raise AssertionError('repair.py must exist')
    repair_source = repair_path.read_text(encoding='utf-8')
    for marker in ('sql_execution.validate(', 'def propose(', 'def record_outcome('):
        if marker not in repair_source:
            raise AssertionError(f'the repair engine lost {marker}')
    if 'bypass_cache=True' not in repair_source:
        raise AssertionError('a repair must not replay the statement that failed')
    if '/ai/sql/repair' not in service:
        raise AssertionError('the service must expose /ai/sql/repair')
    if '/ai/sql/patterns' not in service:
        raise AssertionError('the service must expose the pattern review endpoints')
    if 'repair_note' not in candidate:
        raise AssertionError('the candidate prompt must be able to carry a repair note')
    if 'def _repair(' not in proxy_source:
        raise AssertionError('the proxy must run the repair loop')
    for marker in ('MAX_REPAIR_ROUNDS', '/ai/sql/repair/outcome'):
        if marker not in proxy_source:
            raise AssertionError(f'the proxy repair loop lost {marker}')
    if proxy_source.count('_authorize(database, expression, schema_name)') < 2:
        raise AssertionError('a repaired statement must be authorized like a first one')
    if 'raise_for_access(' not in proxy_source:
        raise AssertionError('a repaired statement must pass Superset authorization')

    # P5-F10: healing decisions are reviewable, and the review carries no statements.
    for marker in (
        '/api/repair/metrics/',
        '/api/repair/patterns/',
        'def api_repair_pattern_action',
    ):
        if marker not in proxy_source:
            raise AssertionError(f'the operator repair API lost {marker}')
    permissions = (
        ROOT / 'docker' / 'ensure_ai_prompt_permissions.py'
    ).read_text(encoding='utf-8')
    for permission in (
        'can_api_repair_metrics',
        'can_api_repair_patterns',
        'can_api_repair_pattern_action',
    ):
        if permission not in permissions:
            raise AssertionError(f'the operator repair API needs {permission}')
    panel = (
        ROOT
        / 'superset'
        / 'superset-frontend'
        / 'src'
        / 'features'
        / 'aiPrompt'
        / 'RepairPanel.tsx'
    )
    if not panel.exists():
        raise AssertionError('the repair knowledge needs an operator panel')
    panel_source = panel.read_text(encoding='utf-8')
    for marker in ('fetchRepairMetrics', 'fetchRepairPatterns', 'quarantine'):
        if marker not in panel_source:
            raise AssertionError(f'the repair panel lost {marker}')

    # P6-F02/F04/F05/F08/F09: a dataset draft is compiled, judged, and stored before
    # anything is created in Superset — and its statement is never executed inlined
    # without the same guard.
    spec_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'dataset_spec.py'
    if not spec_path.exists():
        raise AssertionError('dataset_spec.py must exist')
    spec_source = spec_path.read_text(encoding='utf-8')
    for marker in ('def from_payload', 'def normalise_type', 'def fingerprint_of'):
        if marker not in spec_source:
            raise AssertionError(f'dataset_spec.py lost {marker}')
    if 'validate_definition(' not in spec_source:
        raise AssertionError(
            'a stored dataset definition must be validated by the guard'
        )
    execution_source = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'sql_execution.py'
    ).read_text(encoding='utf-8')
    if 'def validate_definition' not in execution_source:
        raise AssertionError('a dataset definition needs its own validation path')
    if 'enforce_limit=False' not in execution_source:
        raise AssertionError(
            'a dataset definition must not carry a preview cap'
        )

    quality_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'dataset_quality.py'
    if not quality_path.exists():
        raise AssertionError('dataset_quality.py must exist')
    quality_source = quality_path.read_text(encoding='utf-8')
    for code in (
        'empty_result',
        'duplicate_grain',
        'null_in_grain',
        'type_drift',
        'invalid_dates',
        'metric_not_numeric',
    ):
        if code not in quality_source:
            raise AssertionError(f'the quality rules lost "{code}"')

    draft_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'dataset_draft.py'
    if not draft_path.exists():
        raise AssertionError('dataset_draft.py must exist')
    draft_source = draft_path.read_text(encoding='utf-8')
    for marker in (
        'def create_draft',
        'def update_draft',
        'def mark_saved',
        'def scrap_draft',
        'DraftConflict',
    ):
        if marker not in draft_source:
            raise AssertionError(f'dataset_draft.py lost {marker}')
    if 'idempotency_key' not in draft_source:
        raise AssertionError('a replayed dataset create must be idempotent')
    draft_migration = (
        ROOT
        / 'services'
        / 'xp_prompt'
        / 'migrations'
        / 'versions'
        / '20261103_12_ai_dataset_draft.py'
    )
    if not draft_migration.exists():
        raise AssertionError('dataset drafts need their own migration')
    if 'uq_ai_dataset_draft_idempotency' not in draft_migration.read_text(
        encoding='utf-8'
    ):
        raise AssertionError('the idempotency key must be unique in its tenant')
    if '/ai/datasets/drafts' not in service:
        raise AssertionError('the service must expose the dataset draft API')

    # P6-F01/F03/F06/F07/F09/F10: the proxy resolves equivalence from the authorized
    # slice, previews through the guarded path, and saves through Superset's command.
    for marker in (
        'def api_dataset_equivalents',
        'def api_dataset_draft_create',
        'def api_dataset_draft_save',
        'def api_dataset_draft_scrap',
    ):
        if marker not in proxy_source:
            raise AssertionError(f'the dataset API lost {marker}')
    if 'CreateDatasetCommand' not in proxy_source:
        raise AssertionError('a dataset must be saved through Superset CreateDatasetCommand')
    if 'DatasetDAO.update_metrics' not in proxy_source:
        raise AssertionError('declared metrics must be attached through the DAO')
    if '_dataset_equivalents(' not in proxy_source:
        raise AssertionError('a duplicate dataset must be detected before saving')
    if 'def _verify_dataset' not in proxy_source:
        raise AssertionError('a saved dataset must be read back')
    if 'def _scrap_draft' not in proxy_source:
        raise AssertionError('a failed save must close its draft')
    for marker in ('/ai/sql/validate', 'raise_for_access('):
        if marker not in proxy_source:
            raise AssertionError(f'a dataset preview must reuse {marker}')
    if 'authorized_datasets()' not in proxy_source.split('def _dataset_equivalents')[1][:600]:
        raise AssertionError(
            'equivalence must be answered from the authorized slice'
        )
    permissions = (
        ROOT / 'docker' / 'ensure_ai_prompt_permissions.py'
    ).read_text(encoding='utf-8')
    for permission in (
        'can_api_dataset_equivalents',
        'can_api_dataset_draft_create',
        'can_api_dataset_draft_save',
    ):
        if permission not in permissions:
            raise AssertionError(f'the dataset API needs {permission}')

    # P7-F01/F02/F03: the recommendation is bounded by what the deployment can render,
    # the profile is explicit, and every candidate passes its own control contract.
    catalog_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'chart_catalog.py'
    if not catalog_path.exists():
        raise AssertionError('chart_catalog.py must exist')
    catalog_source = catalog_path.read_text(encoding='utf-8')
    for marker in ('VIZ_CONTRACTS', 'def accepts', 'def supported', 'def validate'):
        if marker not in catalog_source:
            raise AssertionError(f'the chart catalog lost {marker}')
    profile_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'chart_profile.py'
    if not profile_path.exists():
        raise AssertionError('chart_profile.py must exist')
    profile_source = profile_path.read_text(encoding='utf-8')
    for marker in ('def profile(', 'def fingerprint_of', 'LOW_CARDINALITY'):
        if marker not in profile_source:
            raise AssertionError(f'the chart profile lost {marker}')
    rules_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'chart_rules.py'
    if not rules_path.exists():
        raise AssertionError('chart_rules.py must exist')
    rules_source = rules_path.read_text(encoding='utf-8')
    for marker in ('def recommend(', 'class Elimination', 'catalog.validate('):
        if marker not in rules_source:
            raise AssertionError(f'the chart rules lost {marker}')
    if 'MAX_CANDIDATES' not in rules_source:
        raise AssertionError('a recommendation must be bounded')
    if '/ai/charts/recommend' not in service:
        raise AssertionError('the service must expose /ai/charts/recommend')
    if '/ai/charts/contracts' not in service:
        raise AssertionError('the service must expose the control contracts')

    # P7-F05/F09: a chart draft is validated against its viz contract, versioned and
    # tied to the dataset it reads; P7-F04: the model may reorder, never add.
    chart_draft_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'chart_draft.py'
    if not chart_draft_path.exists():
        raise AssertionError('chart_draft.py must exist')
    chart_draft_source = chart_draft_path.read_text(encoding='utf-8')
    for marker in (
        'def validate_form',
        'def create_draft',
        'def update_draft',
        'def mark_saved',
        'def scrap_draft',
        'ChartDraftConflict',
    ):
        if marker not in chart_draft_source:
            raise AssertionError(f'chart_draft.py lost {marker}')
    if 'catalog.validate(' not in chart_draft_source:
        raise AssertionError('a chart draft must be validated against its contract')
    chart_migration = (
        ROOT
        / 'services'
        / 'xp_prompt'
        / 'migrations'
        / 'versions'
        / '20261103_13_ai_chart_draft.py'
    )
    if not chart_migration.exists():
        raise AssertionError('chart drafts need their own migration')
    if 'uq_ai_chart_draft_idempotency' not in chart_migration.read_text(
        encoding='utf-8'
    ):
        raise AssertionError('a replayed chart draft must be idempotent')

    ranking_path = ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'chart_ranking.py'
    if not ranking_path.exists():
        raise AssertionError('chart_ranking.py must exist')
    ranking_source = ranking_path.read_text(encoding='utf-8')
    for marker in ('def rank(', 'def clamp(', 'def apply_order('):
        if marker not in ranking_source:
            raise AssertionError(f'chart_ranking.py lost {marker}')
    if 'if name in known' not in ranking_source:
        raise AssertionError('model ranking must be clamped to offered candidates')
    if '/ai/charts/drafts' not in service:
        raise AssertionError('the service must expose the chart draft API')

    # P7-F06/F07/F08/F10: the render goes through Superset's own chart-data path in the
    # caller's context, the save goes through the chart command, and a failed save
    # closes its draft.
    for marker in (
        'def api_charts_recommend',
        'def api_chart_draft_create',
        'def api_chart_preview',
        'def api_chart_draft_save',
        'def api_chart_draft_scrap',
    ):
        if marker not in proxy_source:
            raise AssertionError(f'the chart API lost {marker}')
    for marker in (
        'QueryContextFactory',
        'ChartDataCommand',
        'CreateChartCommand',
        'raise_for_access(datasource=dataset)',
        'def _scrap_chart_draft',
    ):
        if marker not in proxy_source:
            raise AssertionError(f'the chart path lost {marker}')
    if 'def _chart_catalog_payload' not in proxy_source:
        raise AssertionError('the chart catalog must be declared by the deployment')
    if 'VIZ_TYPE_DENYLIST' not in proxy_source:
        raise AssertionError('the deployment denylist must narrow the catalog')
    permissions = (
        ROOT / 'docker' / 'ensure_ai_prompt_permissions.py'
    ).read_text(encoding='utf-8')
    for permission in (
        'can_api_charts_recommend',
        'can_api_chart_draft_create',
        'can_api_chart_preview',
        'can_api_chart_draft_save',
    ):
        if permission not in permissions:
            raise AssertionError(f'the chart API needs {permission}')
    chart_panel = (
        ROOT
        / 'superset'
        / 'superset-frontend'
        / 'src'
        / 'features'
        / 'aiPrompt'
        / 'ChartPanel.tsx'
    )
    if not chart_panel.exists():
        raise AssertionError('the chart loop needs a workspace panel')
    chart_panel_source = chart_panel.read_text(encoding='utf-8')
    for marker in ('recommendCharts', 'createChartDraft', 'previewChart', 'saveChart'):
        if marker not in chart_panel_source:
            raise AssertionError(f'the chart panel lost {marker}')
    if 'onSaved' not in (
        ROOT
        / 'superset'
        / 'superset-frontend'
        / 'src'
        / 'features'
        / 'aiPrompt'
        / 'DatasetPanel.tsx'
    ).read_text(encoding='utf-8'):
        raise AssertionError('a saved dataset must hand off to the chart step')


def check_ai_dashboard_builder(compatibility: dict) -> None:
    """Phase 8 may only plan from what the principal is authorized to see.

    The plan is a proposal, so the contract pins the two properties that make it
    trustworthy: reuse is preferred over regeneration, and a gap is reported as a
    ``need`` rather than silently dropped. The layout must be validated before it can
    be persisted, and a native filter must require proof that two in-scope charts
    share the column.
    """
    services = ROOT / 'services' / 'xp_prompt' / 'xp_prompt'
    plan_path = services / 'dashboard_plan.py'
    layout_path = services / 'dashboard_layout.py'
    for path in (plan_path, layout_path):
        if not path.exists():
            raise AssertionError(f'{path.name} must exist')
    plan_source = plan_path.read_text(encoding='utf-8')
    layout_source = layout_path.read_text(encoding='utf-8')

    for marker in ('def build(', 'class ArtifactNeed', 'class Slot', 'def _quality('):
        if marker not in plan_source:
            raise AssertionError(f'dashboard_plan.py lost {marker}')
    for marker in ('"no_existing_chart_and_no_recommendation"', '"source"', 'reuse'):
        if marker not in plan_source:
            raise AssertionError(f'dashboard_plan.py lost {marker}')
    if 'source="existing"' not in plan_source:
        raise AssertionError('an existing chart must win its slot before generation')
    if 'MAX_CHARTS_PER_DATASET' not in plan_source:
        raise AssertionError('one dataset must not fill the whole dashboard')

    for marker in ('def build_position(', 'def validate_position(', 'def plan_filters('):
        if marker not in layout_source:
            raise AssertionError(f'dashboard_layout.py lost {marker}')
    for marker in ('GRID_COLUMNS', 'MIN_FILTER_SCOPE', '"parents"', 'chartId'):
        if marker not in layout_source:
            raise AssertionError(f'dashboard_layout.py lost {marker}')

    service_source = (services / 'api.py').read_text(encoding='utf-8')
    for route in ('/ai/dashboards/plan', '/ai/dashboards/layout'):
        if route not in service_source:
            raise AssertionError(f'the service must expose {route}')

    proxy_source = (
        ROOT / 'superset' / 'superset' / 'views' / 'ai_prompt' / 'views.py'
    ).read_text(encoding='utf-8')
    for marker in ('def api_dashboard_plan', 'def api_dashboard_layout'):
        if marker not in proxy_source:
            raise AssertionError(f'the dashboard API lost {marker}')
    if 'authorized_datasets()' not in proxy_source:
        raise AssertionError('the dashboard plan must be built from the authorized slice')
    if 'def _plan_charts' not in proxy_source:
        raise AssertionError('reuse requires the saved charts on those datasets')

    permissions = (
        ROOT / 'docker' / 'ensure_ai_prompt_permissions.py'
    ).read_text(encoding='utf-8')
    for permission in ('can_api_dashboard_plan', 'can_api_dashboard_layout'):
        if permission not in permissions:
            raise AssertionError(f'the dashboard API needs {permission}')


def check_ai_schema_slice(compatibility: dict) -> None:
    """Only Superset may decide which table names an AI prompt can mention.

    If the slice were assembled from a request body, or from the metadata database
    directly, an unauthorized name could reach a prompt. The check therefore pins
    the authorization source (``get_user_datasources``) and the bounds.
    """
    section = compatibility.get('ai_schema_slice')
    if not isinstance(section, dict):
        raise AssertionError('ai_schema_slice must be declared')

    module_path = section.get('module')
    proxy_path = section.get('proxy')
    if not module_path or not proxy_path:
        raise AssertionError('ai_schema_slice.module and .proxy must be declared')

    source = (ROOT / module_path).read_text(encoding='utf-8')
    for constant, key in (
        ('MAX_DATASETS', 'max_datasets'),
        ('MAX_COLUMNS', 'max_columns'),
        ('MAX_PROMPT_CHARS', 'max_prompt_chars'),
    ):
        expected = section.get(key)
        if expected is None:
            raise AssertionError(f'ai_schema_slice.{key} must be declared')
        if (
            f'{constant} = {expected:_}' not in source
            and f'{constant} = {expected}' not in source
        ):
            raise AssertionError(f'{constant} drifted from ai_schema_slice.{key}')

    for marker in ('def from_payload', 'def allowed_tables', 'def prompt_text', 'def _fingerprint'):
        if marker not in source:
            raise AssertionError(f'{module_path} lost {marker}')

    proxy = (ROOT / proxy_path).read_text(encoding='utf-8')
    if 'security_manager.get_user_datasources' not in proxy:
        raise AssertionError(
            'the schema slice must be resolved by Superset security_manager'
        )
    if 'def authorized_datasets' not in proxy:
        raise AssertionError(f'{proxy_path} lost the authorized_datasets helper')

    service = (
        ROOT / 'services' / 'xp_prompt' / 'xp_prompt' / 'api.py'
    ).read_text(encoding='utf-8')
    if '/ai/schema' not in service:
        raise AssertionError('the XP Prompt service must expose /ai/schema')


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

    check_ai_artifact_wiring(compatibility)

    check_ai_sql_guard(compatibility)

    check_ai_dashboard_builder(compatibility)

    check_ai_schema_slice(compatibility)

    print('Compatibility contract checks passed')


if __name__ == '__main__':
    main()

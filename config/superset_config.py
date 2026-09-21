# Superset configuration for the standalone Kot XPBuilder runtime.
# Baked into the XPBuilder image at /app/xpbuilder/superset_config.py.
#
# Moodle-free: there is no reporting replica and no pre-registered data
# source. External databases are connected through Superset's own
# "Connect a database" flow once the stack is running.

import os
from urllib.parse import quote_plus

import pymysql

pymysql.install_as_MySQLdb()


def _required(name):
    """Return a required environment value without providing unsafe defaults."""
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'{name} must be set through the deployment .env file')
    return value

# ── Security ─────────────────────────────────────────────
SECRET_KEY = _required('SUPERSET_SECRET_KEY')

# ── Metadata Database (Superset's own state) ─────────────
SQLALCHEMY_DATABASE_URI = (
    'postgresql+psycopg2://'
    f"{quote_plus(_required('POSTGRES_USER'))}:"
    f"{quote_plus(_required('POSTGRES_PASSWORD'))}"
    f"@superset-db:5432/{quote_plus(_required('POSTGRES_DB'))}"
)

# ── Redis (cache + Celery broker) ────────────────────────
REDIS_URL = (
    f"redis://:{quote_plus(_required('SUPERSET_REDIS_PASSWORD'))}"
    '@superset-redis:6379/0'
)

CACHE_CONFIG = {
    'CACHE_TYPE': 'RedisCache',
    'CACHE_DEFAULT_TIMEOUT': 300,
    'CACHE_KEY_PREFIX': 'superset_',
    'CACHE_REDIS_URL': REDIS_URL,
}

# ── Celery ───────────────────────────────────────────────
# Superset reads broker/backend from the CELERY_CONFIG class, NOT from
# top-level CELERY_BROKER_URL / CELERY_RESULT_BACKEND. The stock default
# uses SQLite files which the worker/beat containers cannot write
# (permission denied → crash loop). Override with Redis, keeping the
# stock imports + beat_schedule so alerts/reports keep working.
from datetime import timedelta  # noqa: E402
from celery.schedules import crontab  # noqa: E402


class XpBuilderCeleryConfig:  # pylint: disable=too-few-public-methods
    broker_url = REDIS_URL
    imports = (
        "superset.sql_lab",
        "superset.tasks.scheduler",
        "superset.tasks.thumbnails",
        "superset.tasks.cache",
        "superset.tasks.slack",
    )
    result_backend = REDIS_URL
    worker_prefetch_multiplier = 1
    task_acks_late = False
    # beat must write its schedule file somewhere writable (container runs
    # as `superset`, /app is root-owned → permission denied crash loop).
    beat_schedule_filename = "/app/superset_home/celerybeat-schedule"
    task_annotations = {
        "sql_lab.get_sql_results": {
            "rate_limit": "100/s",
        },
    }
    beat_schedule = {
        "reports.scheduler": {
            "task": "reports.scheduler",
            "schedule": crontab(minute="*", hour="*"),
            "options": {"expires": int(timedelta(weeks=1).total_seconds())},
        },
        "reports.prune_log": {
            "task": "reports.prune_log",
            "schedule": crontab(minute=0, hour=0),
        },
    }


CELERY_CONFIG = XpBuilderCeleryConfig

# ── Feature Flags ────────────────────────────────────────
FEATURE_FLAGS = {
    # Core embedding support — REQUIRED for embedded dashboards.
    'EMBEDDED_SUPERSET': True,

    # Dashboard-level RBAC.
    'DASHBOARD_RBAC': True,

    # Jinja template processing in SQL Lab / datasets.
    'ENABLE_TEMPLATE_PROCESSING': True,

    # Cross-filter between charts on a dashboard.
    'DASHBOARD_CROSS_FILTERS': True,

    # Drill-by (click a chart element to drill into detail).
    'DRILL_BY': True,

    # Drill-to-detail (show row-level detail modal).
    'DRILL_TO_DETAIL': True,

    # Advanced analytics (moving averages, etc.).
    'ENABLE_ADVANCED_DATA_TYPES': True,

    # Dynamic plugins (for future custom viz types).
    'DYNAMIC_PLUGINS': False,
}

# Disable CSRF for REST API (authenticated via Bearer tokens).
WTF_CSRF_ENABLED = False
REST_CSRF_ENABLED = False

# ── Guest Token (JWT) for Embedded Dashboards ────────────
GUEST_TOKEN_JWT_SECRET = _required('GUEST_TOKEN_JWT_SECRET')
GUEST_TOKEN_JWT_ALGO = 'HS256'
GUEST_TOKEN_HEADER_NAME = 'X-GuestToken'
GUEST_TOKEN_JWT_EXP_SECONDS = 3600  # 1 hour
GUEST_TOKEN_JWT_AUDIENCE = _required('GUEST_TOKEN_JWT_AUDIENCE')

# The role granted to guest-token users. Gamma has the read/view permissions
# needed by the embedded dashboard frontend (/api/v1/me/roles/, dashboard,
# chart and dataset reads). The default 'Public' role has no permissions and
# causes 403 on every API call.
GUEST_ROLE_NAME = 'Gamma'

# ── Embedded Dashboard Allowed Domains ────────────────────
EMBEDDED_SUPERSET_DOMAINS = [
    origin.strip()
    for origin in _required('XPBUILDER_ALLOWED_ORIGINS').split(',')
    if origin.strip()
]

# ── Content Security Policy (relaxed for embedding) ──────
TALISMAN_ENABLED = False  # Superset is served behind an external reverse proxy.
ENABLE_CORS = True
CORS_OPTIONS = {
    'supports_credentials': True,
    'allow_headers': ['*'],
    'resources': ['*'],
    'origins': EMBEDDED_SUPERSET_DOMAINS,
}

# ── Silence benign startup advisories ────────────────────
CONTENT_SECURITY_POLICY_WARNING = False  # Talisman intentionally off behind the proxy.
RATELIMIT_STORAGE_URI = REDIS_URL        # Use Redis (not in-memory) for rate limiting.

# ── Logging ──────────────────────────────────────────────
LOG_LEVEL = os.environ.get('SUPERSET_LOG_LEVEL', 'INFO')

# ── Runtime patches — silence upstream Superset noise ─────
# Applied via FLASK_APP_MUTATOR (runs once after app init), so the fixes
# survive container recreations without editing root-owned image files.
def _xpbuilder_runtime_patches(app):
    """Apply small, safe monkey-patches to quiet upstream Superset noise."""

    # 1) Flask 3 stores g.user as a werkzeug LocalProxy -> SQLAlchemy
    #    "Class 'werkzeug.local.LocalProxy' is not mapped" warnings on every
    #    embedded/guest-token request. Unwrap LocalProxy values read via g.get().
    import flask.ctx as _ctx
    import werkzeug.local as _wl
    _orig_get = _ctx._AppCtxGlobals.get

    def _safe_get(self, name, default=None):
        value = _orig_get(self, name, default)
        if isinstance(value, _wl.LocalProxy):
            try:
                return value._get_current_object()
            except Exception:
                return default
        return value

    _ctx._AppCtxGlobals.get = _safe_get

    # 1b) Superset's event logger also tries db.session.add() on anonymous /
    #     guest users, which are not SQLAlchemy models (they can never be
    #     persisted) -> "Class '...AnonymousUserMixin'/'...GuestUser' is not
    #     mapped" warnings on every embedded load. Skip non-model instances.
    import sqlalchemy.orm as _orm
    _orig_add = _orm.Session.add

    def _safe_add(self, instance, _warn=True):
        if not hasattr(instance, '_sa_instance_state'):
            return  # Not a SQLAlchemy model — nothing to persist.
        return _orig_add(self, instance, _warn)

    _orm.Session.add = _safe_add

    # 2) Default spinner SVG is read from a frontend-source path that does not
    #    exist in the image -> "Could not load default spinner SVG" warnings.
    #    Replace with an inline CSS-spinning SVG (never touches the filesystem).
    import superset.views.base as _base
    _base.get_default_spinner_svg = lambda: (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" '
        'height="24"><circle cx="12" cy="12" r="9" fill="none" stroke="#20a7c9" '
        'stroke-width="3" stroke-linecap="round" stroke-dasharray="42 14">'
        '<animateTransform attributeName="transform" type="rotate" from="0 12 12" '
        'to="360 12 12" dur="1s" repeatCount="indefinite"/></circle></svg>'
    )

    # 3) The embedded frontend registers a service worker at /static/service-worker.js,
    #    which is absent from the image -> 404 console noise. Serve a no-op worker.
    @app.route('/static/service-worker.js')
    def _service_worker_noop():
        return (
        '/* no-op service worker (Kot XPBuilder) */\n'
            "self.addEventListener('install', function (e) { self.skipWaiting(); });\n"
            "self.addEventListener('activate', function (e) { self.clients.claim(); });\n"
        ), 200, {'Content-Type': 'application/javascript', 'Cache-Control': 'no-cache'}

    # 4) Defensive: dashboard layouts can carry "meta": [] (a list) on ROOT/GRID/TABS
    #    components, which crashes set_dash_metadata with "'list' object has no
    #    attribute 'get'" on every Save. Normalize list metas to {} before saving.
    import superset.daos.dashboard as _dash_dao
    _orig_sdm = _dash_dao.DashboardDAO.set_dash_metadata

    def _safe_sdm(dashboard, data, old_to_new_slice_ids=None):
        positions = data.get('positions') if isinstance(data, dict) else None
        if isinstance(positions, dict):
            for obj in positions.values():
                if isinstance(obj, dict) and isinstance(obj.get('meta'), list):
                    obj['meta'] = {}
        return _orig_sdm(dashboard, data, old_to_new_slice_ids)

    _dash_dao.DashboardDAO.set_dash_metadata = staticmethod(_safe_sdm)

    # 5) Hide the "SQL" menu (and its submenu: SQL Editor / Saved Queries / Query
    #    Search) from the navbar. The menu entry's `name` is "SQL Lab" while the
    #    rendered label is "SQL"; match either so the whole group is dropped.
    import superset.views.base as _base
    _orig_menu_data = _base.menu_data

    def _safe_menu_data(user):
        result = _orig_menu_data(user)
        if isinstance(result, dict) and isinstance(result.get('menu'), list):
            result['menu'] = [
                item for item in result['menu']
                if str(item.get('name', '')).lower() != 'sql lab'
                and str(item.get('label', '')).lower() != 'sql'
            ]
        return result

    _base.menu_data = _safe_menu_data

    # 6) Branding: "Advance BI" (persistent). THEME_DEFAULT lives in the image's
    #    config.py and is NOT visible in this module's namespace (superset_config
    #    is exec'd standalone), so mutate it here via superset.config — the same
    #    dict object the app serves to the frontend. APP_NAME / APP_ICON below
    #    (module level) are copied back into config.py by `from superset_config
    #    import *` and drive the page <title> / browser tab / navbar brand.
    import superset.config as _superset_config
    app_name = os.environ.get('XPBUILDER_APP_NAME', 'Advance BI')
    _superset_config.THEME_DEFAULT["token"]["brandAppName"] = app_name
    _superset_config.THEME_DEFAULT["token"]["brandLogoAlt"] = app_name
    # Fresh filename (advance-bi-logo.png) so a CDN cannot serve a cached
    # original at the default superset-logo-horiz.png URL.
    _superset_config.THEME_DEFAULT["token"]["brandLogoUrl"] = (
        "/static/assets/images/advance-bi-logo.png"
    )


FLASK_APP_MUTATOR = _xpbuilder_runtime_patches

# ── Branding: "Advance BI" (persistent — overrides the image's default
#    config.py; survives container rebuilds because this file is baked into the
#    XPBuilder image at build time).
#    APP_NAME drives the page <title>, browser tab and appbuilder navbar brand.
#    The navbar logo/alt theme tokens are set in _xpbuilder_runtime_patches() above.
APP_NAME = os.environ.get('XPBUILDER_APP_NAME', 'Advance BI')
# Custom logo under a FRESH filename (advance-bi-logo.png) so the public URL is
# never served a CDN-cached original. Baked in from customizations/images/.
APP_ICON = "/static/assets/images/advance-bi-logo.png"

# When set in the per-site .env (XPBUILDER_BRAND_URL), the tail-JS brand fixer
# points the navbar brand logo at this URL instead of "/". Default "/" keeps
# Superset's own home page.
XPBUILDER_BRAND_URL = os.environ.get('XPBUILDER_BRAND_URL', '/')

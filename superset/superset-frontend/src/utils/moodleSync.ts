/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

import getBootstrapData from 'src/utils/getBootstrapData';

/**
 * "Sync to Moodle" helpers.
 *
 * The XP Superset fork is served same-origin behind Moodle's reverse proxy
 * (Moodle Apache proxies /api, /static, /superset, /embedded, ... to Superset),
 * so the frontend can query the Moodle `local_xpromptsuperset` plugin directly
 * to find out whether a dashboard is already linked into the Moodle XP
 * dashboard list.
 */

export type MoodleSyncStatus = {
  synced: boolean;
  xprompt_dashboard_id: number | null;
  title: string | null;
};

/**
 * Whether this deployment runs with the optional Moodle integration enabled.
 *
 * A single built image serves both modes: the mode is chosen at runtime with
 * XPBUILDER_MOODLE_INTEGRATION and handed to the SPA through
 * COMMON_BOOTSTRAP_OVERRIDES_FUNC (common.xpbuilder.moodle_integration).
 * When disabled, every Moodle-only action must stay hidden — the supporting
 * server routes do not exist at all in that mode.
 */
export function isMoodleIntegrationEnabled(): boolean {
  try {
    return !!getBootstrapData().common?.xpbuilder?.moodle_integration;
  } catch {
    return false;
  }
}

/**
 * Whether the given Superset dashboard is already linked into the Moodle XP
 * dashboard list (`local_xpromptsuperset_dash` mapping, status active).
 *
 * Fail-safe: on any error (not reachable, not logged in, malformed response)
 * returns `true` so the "Sync to Moodle" button stays hidden rather than
 * showing a dead action.
 *
 * @param dashboardId Superset dashboard id
 */
export async function isDashboardSyncedToMoodle(
  dashboardId: number,
): Promise<boolean> {
  try {
    const res = await fetch(
      `/local/xpromptsuperset/sync_status.php?superset_dashboard_id=${dashboardId}`,
      { credentials: 'same-origin' },
    );
    if (!res.ok) return true;
    const data = (await res.json()) as MoodleSyncStatus;
    return !!data.synced;
  } catch {
    return true;
  }
}

/**
 * Build the URL for the Moodle import (sync) page.
 *
 * @param dashboardId Superset dashboard id
 * @param title Dashboard title (becomes the Moodle dashboard name)
 * @param redirect Same-origin path to return to after the transfer
 */
export function moodleSyncUrl(
  dashboardId: number,
  title: string,
  redirect?: string,
): string {
  const params = new URLSearchParams({
    superset_dashboard_id: String(dashboardId),
    name: title || '',
  });
  if (redirect) {
    params.set('redirect', redirect);
  }
  return `/local/xpromptsuperset/import.php?${params.toString()}`;
}

/**
 * True when the current page is the embedded (Moodle-hosted) dashboard view.
 * Inside the Moodle embed the "Sync to Moodle" button is redundant, so it is
 * hidden there.
 */
export function isMoodleEmbeddedView(): boolean {
  if (typeof window === 'undefined') return true;
  return window.location.pathname.startsWith('/embedded/');
}

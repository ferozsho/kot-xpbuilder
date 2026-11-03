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
import { useCallback, useEffect, useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Space, Tag, Tooltip } from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import { actOnJob, describeError, fetchJobs, submitJob } from './api';
import { JobRecord, JobStatus } from './types';

/**
 * Job state for one conversation.
 *
 * The database is the queue state (ADR-0005), so this panel only polls: while a
 * job is `queued`/`running`/`clarification` it re-reads every two seconds and
 * stops as soon as the job reaches a terminal status. Nothing is kept in memory
 * that a reload would need to restore.
 *
 * A mutating job arrives as `clarification` with `requires_confirmation`, which is
 * the only place the approve/decline controls can appear (P2-F08).
 */

const ACTIVE_STATUSES: JobStatus[] = ['queued', 'running', 'clarification'];
const POLL_MS = 2000;

const STATUS_COLORS: Record<JobStatus, string> = {
  queued: 'blue',
  running: 'geekblue',
  clarification: 'gold',
  succeeded: 'green',
  failed: 'red',
  cancelled: 'default',
};

const StyledPanel = styled.section`
  ${({ theme }) => `
    border-top: 1px solid ${theme.colorBorder};
    padding-top: ${theme.sizeUnit * 2}px;
    margin-top: ${theme.sizeUnit * 3}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
  `}
`;

const StyledHeadline = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    flex-wrap: wrap;
  `}
`;

const StyledSteps = styled.ol`
  ${({ theme }) => `
    margin: 0;
    padding-left: ${theme.sizeUnit * 4}px;
    font-size: ${theme.fontSizeSM}px;
    color: ${theme.colorTextSecondary};
  `}
`;

const StyledMeta = styled.p`
  ${({ theme }) => `
    margin: 0;
    font-size: ${theme.fontSizeSM}px;
    color: ${theme.colorTextSecondary};
  `}
`;

const stepLabel = (status: string) => t(status.charAt(0).toUpperCase() + status.slice(1));

export default function JobPanel({
  conversationId,
}: {
  conversationId: string | null;
}) {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [jobs, setJobs] = useState<JobRecord[]>([]);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!conversationId) {
      setJobs([]);
      return;
    }
    try {
      const response = await fetchJobs(conversationId);
      setJobs(response.result ?? []);
    } catch {
      // A failed poll must not spam toasts; the next tick retries.
    }
  }, [conversationId]);

  useEffect(() => {
    load();
  }, [load]);

  const latest = jobs[0];
  const isActive = latest ? ACTIVE_STATUSES.includes(latest.status) : false;

  useEffect(() => {
    if (!conversationId || !isActive) {
      return undefined;
    }
    const timer = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(timer);
  }, [conversationId, isActive, load]);

  const run = useCallback(
    async (action: 'cancel' | 'retry' | 'confirm' | 'reject', jobId: string) => {
      setBusy(jobId);
      try {
        await actOnJob(jobId, action);
        addSuccessToast(t('Job %(action)s requested', { action }));
        await load();
      } catch (caught) {
        addDangerToast(await describeError(caught));
      } finally {
        setBusy(null);
      }
    },
    [addDangerToast, addSuccessToast, load],
  );

  const startSelfTest = useCallback(async () => {
    if (!conversationId) {
      return;
    }
    setBusy('submit');
    try {
      await submitJob(conversationId, {
        job_type: 'ping',
        idempotency_key: `self-test-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        request: { source: 'workspace-self-test' },
        steps: ['plan', 'run'],
      });
      addSuccessToast(t('Self-test job queued'));
      await load();
    } catch (caught) {
      addDangerToast(await describeError(caught));
    } finally {
      setBusy(null);
    }
  }, [addDangerToast, addSuccessToast, conversationId, load]);

  const canRetry = useMemo(
    () =>
      Boolean(
        latest &&
          (latest.status === 'failed' || latest.status === 'cancelled') &&
          latest.attempt_count < latest.max_attempts,
      ),
    [latest],
  );

  return (
    <StyledPanel aria-live="polite">
      <StyledHeadline>
        <strong>{t('Jobs')}</strong>
        <Tooltip
          title={t(
            'Queues a no-op job to check that the worker and queue are healthy. ' +
              'It changes nothing in Superset.',
          )}
        >
          <Button
            buttonStyle="tertiary"
            buttonSize="small"
            loading={busy === 'submit'}
            disabled={!conversationId || busy !== null}
            onClick={startSelfTest}
          >
            {t('Run self-test')}
          </Button>
        </Tooltip>
        {!latest && <StyledMeta>{t('No jobs in this conversation yet.')}</StyledMeta>}
      </StyledHeadline>

      {latest && (
        <>
          <StyledHeadline>
            <Tag color={STATUS_COLORS[latest.status]}>{latest.status}</Tag>
            <StyledMeta>
              {latest.job_type} · {t('attempt %(used)s of %(max)s', {
                used: latest.attempt_count,
                max: latest.max_attempts,
              })}
              {latest.error_code ? ` · ${latest.error_code}` : ''}
            </StyledMeta>
          </StyledHeadline>

          <StyledSteps>
            {latest.steps.map(step => (
              <li key={`${step.step_key}-${step.ordinal}`}>
                {step.step_key}: {stepLabel(step.status)}
              </li>
            ))}
          </StyledSteps>

          <Space>
            {latest.requires_confirmation && (
              <>
                <Button
                  buttonStyle="primary"
                  buttonSize="small"
                  loading={busy === latest.id}
                  onClick={() => run('confirm', latest.id)}
                >
                  {t('Approve')}
                </Button>
                <Button
                  buttonStyle="secondary"
                  buttonSize="small"
                  loading={busy === latest.id}
                  onClick={() => run('reject', latest.id)}
                >
                  {t('Decline')}
                </Button>
              </>
            )}
            {isActive && !latest.requires_confirmation && (
              <Button
                buttonStyle="secondary"
                buttonSize="small"
                loading={busy === latest.id}
                onClick={() => run('cancel', latest.id)}
              >
                {t('Cancel job')}
              </Button>
            )}
            {canRetry && (
              <Button
                buttonStyle="tertiary"
                buttonSize="small"
                loading={busy === latest.id}
                onClick={() => run('retry', latest.id)}
              >
                {t('Retry job')}
              </Button>
            )}
          </Space>
        </>
      )}
    </StyledPanel>
  );
}

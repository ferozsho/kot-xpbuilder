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
import { useCallback, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Space, Tag } from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import type {
  DatasetDraftRecord,
  DatasetSavedResponse,
} from './api';
import {
  createDatasetDraft,
  describeError,
  saveDatasetDraft,
  scrapDatasetDraft,
} from './api';

/**
 * Turn an answer into a dataset, deliberately in two steps (P6-F03/F06).
 *
 * The draft is created first: the statement is previewed through the guarded path
 * and measured, and the quality rules run before anything exists in Superset. Only
 * "Save" creates the dataset — through Superset's own command, so ownership,
 * uniqueness and schema detection stay Superset's job.
 */

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

const StyledMeta = styled.span`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const StyledWarning = styled.li`
  ${({ theme }) => `
    color: ${theme.colorWarningText};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

export interface DatasetSource {
  question: string;
  sql: string;
  database?: string;
}

interface DatasetPanelProps {
  /** The last statement of this conversation, if there is one. */
  source?: DatasetSource;
  conversationId?: string;
  /** Told when a dataset is saved, so the chart step knows what to build on. */
  onSaved?: (dataset: { dataset_id: number; name: string }) => void;
}

export default function DatasetPanel({
  source,
  conversationId,
  onSaved,
}: DatasetPanelProps) {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [draft, setDraft] = useState<DatasetDraftRecord | null>(null);
  const [saved, setSaved] = useState<DatasetSavedResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const startDraft = useCallback(async () => {
    if (!source?.sql) {
      return;
    }
    setBusy(true);
    try {
      const response = await createDatasetDraft({
        question: source.question,
        sql: source.sql,
        database: source.database,
        conversationId,
        // One draft per statement per conversation: a second click is a replay,
        // not a second draft.
        idempotencyKey: `${conversationId ?? 'adhoc'}:${source.sql.length}:${source.sql.slice(
          0,
          24,
        )}`,
      });
      setDraft(response.draft);
      setSaved(null);
      if (response.quality?.blocking) {
        addDangerToast(
          t('The sample has blocking quality issues; review them before saving'),
        );
      }
    } catch (caught) {
      addDangerToast(await describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, conversationId, source]);

  const save = useCallback(async () => {
    if (!draft) {
      return;
    }
    setBusy(true);
    try {
      const response = await saveDatasetDraft(draft.id);
      setSaved(response);
      setDraft({ ...draft, status: 'saved' });
      onSaved?.({
        dataset_id: response.dataset.dataset_id,
        name: response.dataset.name,
      });
      addSuccessToast(
        t('Dataset saved with %(count)s columns', {
          count: response.dataset.columns,
        }),
      );
    } catch (caught) {
      addDangerToast(await describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, addSuccessToast, draft, onSaved]);

  const discard = useCallback(async () => {
    if (!draft) {
      return;
    }
    setBusy(true);
    try {
      await scrapDatasetDraft(draft.id, 'discarded_by_user');
      setDraft({ ...draft, status: 'scrapped' });
    } catch (caught) {
      addDangerToast(await describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, draft]);

  if (!source?.sql && !draft) {
    return null;
  }

  const warnings = draft?.quality?.warnings ?? [];

  return (
    <StyledPanel aria-label={t('Save as dataset')}>
      <strong>{t('Save as dataset')}</strong>
      {!draft && (
        <>
          <StyledMeta>
            {t(
              'Preview the answer as a dataset, check the sample, then save it.',
            )}
          </StyledMeta>
          <Space>
            <Button buttonStyle="secondary" loading={busy} onClick={startDraft}>
              {t('Build a draft')}
            </Button>
          </Space>
        </>
      )}
      {draft && (
        <>
          <StyledMeta>
            {t('%(name)s · %(kind)s · version %(version)s', {
              name: draft.name,
              kind: draft.kind,
              version: draft.version,
            })}
            {draft.quality_ok ? (
              <Tag color="green" style={{ marginLeft: 8 }}>
                {t('sample ok')}
              </Tag>
            ) : (
              <Tag color="orange" style={{ marginLeft: 8 }}>
                {t('review needed')}
              </Tag>
            )}
          </StyledMeta>
          {warnings.length > 0 && (
            <ul>
              {warnings.slice(0, 6).map(warning => (
                <StyledWarning key={`${warning.code}-${warning.column}`}>
                  {warning.column
                    ? `${warning.column}: ${warning.message}`
                    : warning.message}
                </StyledWarning>
              ))}
            </ul>
          )}
          <Space>
            {draft.status === 'draft' && (
              <Button buttonStyle="primary" loading={busy} onClick={save}>
                {t('Save dataset')}
              </Button>
            )}
            {draft.status === 'draft' && (
              <Button buttonStyle="tertiary" loading={busy} onClick={discard}>
                {t('Discard draft')}
              </Button>
            )}
            {saved && (
              <a href={`/tablemodelview/edit/${saved.dataset.dataset_id}`}>
                {t('Open the dataset')}
              </a>
            )}
          </Space>
        </>
      )}
    </StyledPanel>
  );
}

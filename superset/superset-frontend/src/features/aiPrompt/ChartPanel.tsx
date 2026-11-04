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
import { Button, Space, Table, Tag } from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import type { ChartCandidate, ChartPreviewResponse } from './api';
import {
  createChartDraft,
  describeError,
  previewChart,
  recommendCharts,
  saveChart,
} from './api';

/**
 * Suggest a chart for a dataset, preview the real numbers, then save it (P7-F03/F06/F08).
 *
 * Three deliberate steps, because each one answers a different question: the rules answer
 * "what suits this data" (with the alternatives they rejected), the preview answers "is it
 * actually right", and save is the only step that creates anything in Superset.
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

const StyledCandidate = styled.button<{ selected: boolean }>`
  ${({ theme, selected }) => `
    display: flex;
    align-items: flex-start;
    gap: ${theme.sizeUnit * 2}px;
    width: 100%;
    text-align: left;
    padding: ${theme.sizeUnit * 1.5}px;
    border-radius: ${theme.borderRadius}px;
    border: 1px solid ${selected ? theme.colorPrimary : theme.colorBorderSecondary};
    background: ${selected ? theme.colorPrimaryBg : 'transparent'};
    cursor: pointer;
    font: inherit;
    color: inherit;
  `}
`;

const StyledRationale = styled.span`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

export interface ChartDataset {
  dataset_id: number;
  name: string;
}

interface ChartPanelProps {
  /** Set once a dataset exists — charts are built for a dataset, never for a question. */
  dataset?: ChartDataset;
  conversationId?: string;
}

export default function ChartPanel({
  dataset,
  conversationId,
}: ChartPanelProps) {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [candidates, setCandidates] = useState<ChartCandidate[]>([]);
  const [eliminated, setEliminated] = useState<
    { viz_type: string; reason: string }[]
  >([]);
  const [measured, setMeasured] = useState(false);
  const [selected, setSelected] = useState<ChartCandidate | null>(null);
  const [draftId, setDraftId] = useState<string>('');
  const [preview, setPreview] = useState<ChartPreviewResponse | null>(null);
  const [savedId, setSavedId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  const suggest = useCallback(async () => {
    if (!dataset) {
      return;
    }
    setBusy(true);
    try {
      const response = await recommendCharts({ dataset_id: dataset.dataset_id });
      setCandidates(response.candidates || []);
      setEliminated(
        (response.eliminated || []).map(item => ({
          viz_type: item.viz_type,
          reason: item.reason,
        })),
      );
      setMeasured(Boolean(response.measured));
      setSelected(null);
      setDraftId('');
      setPreview(null);
      setSavedId(null);
      if (!response.candidates?.length) {
        addDangerToast(t('No visualization suits this dataset yet'));
      }
    } catch (caught) {
      addDangerToast(await describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, dataset]);

  const choose = useCallback(
    async (candidate: ChartCandidate) => {
      if (!dataset) {
        return;
      }
      setBusy(true);
      setSelected(candidate);
      setDraftId('');
      setPreview(null);
      setSavedId(null);
      try {
        const created = await createChartDraft({
          dataset_id: dataset.dataset_id,
          candidate,
          conversationId,
          rankingSource: 'rules',
          // One draft per candidate per dataset: a second click is a replay.
          idempotencyKey: `${dataset.dataset_id}:${candidate.id}`,
        });
        setDraftId(created.draft.id);
        const rendered = await previewChart(created.draft.id);
        setPreview(rendered);
      } catch (caught) {
        addDangerToast(await describeError(caught));
      } finally {
        setBusy(false);
      }
    },
    [addDangerToast, conversationId, dataset],
  );

  const save = useCallback(async () => {
    if (!draftId) {
      return;
    }
    setBusy(true);
    try {
      const response = await saveChart(draftId);
      setSavedId(response.chart.slice_id);
      addSuccessToast(
        t('Chart saved: %(name)s', { name: response.chart.name }),
      );
    } catch (caught) {
      addDangerToast(await describeError(caught));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, addSuccessToast, draftId]);

  if (!dataset) {
    return null;
  }

  const previewColumns: { title: string; dataIndex: string }[] = (
    preview?.columns ?? []
  ).map(column => ({ title: column, dataIndex: column }));
  const previewRows: Record<string, string | number>[] = (preview?.rows ?? [])
    .slice(0, 8)
    .map((row, index) => {
      const record: Record<string, string | number> = { key: index };
      (preview?.columns ?? []).forEach((column, position) => {
        record[column] = row[position];
      });
      return record;
    });

  return (
    <StyledPanel aria-label={t('Suggest a chart')}>
      <strong>{t('Suggest a chart')}</strong>
      <StyledMeta>
        {t('For the dataset %(name)s', { name: dataset.name })}
        {measured
          ? ` · ${t('cardinalities measured from a sample')}`
          : ` · ${t('suggestions are conservative without measurements')}`}
      </StyledMeta>
      <Space>
        <Button buttonStyle="secondary" loading={busy} onClick={suggest}>
          {t('Suggest charts')}
        </Button>
        {draftId && selected && (
          <Button buttonStyle="primary" loading={busy} onClick={save}>
            {t('Save chart')}
          </Button>
        )}
        {savedId && (
          <a href={`/explore/?slice_id=${savedId}`}>{t('Open the chart')}</a>
        )}
      </Space>
      {candidates.map(candidate => (
        <StyledCandidate
          key={candidate.id}
          type="button"
          selected={selected?.id === candidate.id}
          onClick={() => choose(candidate)}
        >
          <Tag color="blue">{candidate.viz_type}</Tag>
          <span>
            <strong>{candidate.title}</strong>
            <br />
            <StyledRationale>{candidate.rationale}</StyledRationale>
          </span>
        </StyledCandidate>
      ))}
      {eliminated.length > 0 && (
        <StyledMeta>
          {t('Not suggested:')}{' '}
          {eliminated
            .slice(0, 6)
            .map(item => `${item.viz_type} (${item.reason})`)
            .join(' · ')}
        </StyledMeta>
      )}
      {preview && previewColumns.length > 0 && (
        <>
          <StyledMeta>
            {t('%(rows)s rows rendered in your own context', {
              rows: preview.row_count,
            })}
            {preview.truncated ? ` · ${t('showing the first rows')}` : ''}
          </StyledMeta>
          <Table
            columns={previewColumns}
            dataSource={previewRows}
            rowKey="key"
            size="small"
            pagination={false}
          />
        </>
      )}
    </StyledPanel>
  );
}

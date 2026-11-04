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
import { useCallback, useEffect, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import { Button, Card, Popconfirm } from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import { applyRetention, describeError, fetchRetention, RetentionPreview } from './api';

/**
 * Retention, previewed before it deletes anything.
 *
 * A sweep that only reports afterwards is a sweep an operator learns to fear, so
 * the panel always shows what *would* go first — and the windows themselves live
 * in the settings page, where they are audited like every other value.
 */

const Heading = styled.h3`
  ${({ theme }) => `
    margin: 0 0 ${theme.sizeUnit * 2}px;
    font-size: ${theme.fontSizeLG}px;
    font-weight: ${theme.fontWeightStrong};
  `}
`;

const Row = styled.div`
  ${({ theme }) => `
    display: flex;
    gap: ${theme.sizeUnit * 3}px;
    align-items: center;
    flex-wrap: wrap;
    font-size: ${theme.fontSize}px;
    margin-top: ${theme.sizeUnit * 2}px;
  `}
`;

const List = styled.ul`
  ${({ theme }) => `
    margin: ${theme.sizeUnit}px 0 0;
    padding-left: ${theme.sizeUnit * 5}px;
    font-size: ${theme.fontSize}px;
  `}
`;

export default function RetentionCard() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [preview, setPreview] = useState<RetentionPreview | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setPreview(await fetchRetention());
    } catch (error) {
      addDangerToast(await describeError(error));
    }
  }, [addDangerToast]);

  useEffect(() => {
    load();
    // One preview on mount; the numbers only change when a sweep runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onApply = useCallback(async () => {
    setBusy(true);
    try {
      const result = await applyRetention();
      addSuccessToast(
        result.total
          ? t('Removed %s row(s)', result.total)
          : t('Nothing was old enough to remove'),
      );
      await load();
    } catch (error) {
      addDangerToast(await describeError(error));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, addSuccessToast, load]);

  const pending = preview
    ? Object.entries(preview.would_delete).filter(([, count]) => count > 0)
    : [];
  const keepForever = preview
    ? Object.entries(preview.windows)
        .filter(([, days]) => days === 0)
        .map(([key]) => key.replace('retention.', '').replace('_days', ''))
    : [];

  return (
    <Card data-test="ai-retention">
      <Heading>{t('Retention')}</Heading>
        <Row>
          <span>
            {t(
              'Budgets, billing imports, the configuration audit and the tenant row are never swept.',
            )}
          </span>
        </Row>
        <Row>
          <span>
            {keepForever.length
              ? t('Kept forever: %s', keepForever.join(', '))
              : t('Every window is set')}
          </span>
        </Row>
        <Row>
          {pending.length === 0 ? (
            <span>{t('Nothing is older than its window right now.')}</span>
          ) : (
            <List>
              {pending.map(([table, count]) => (
                <li key={table}>{t('%s: %s row(s) would be removed', table, count)}</li>
              ))}
            </List>
          )}
        </Row>
        <Row>
          <Popconfirm
            title={t('Delete every row past its window?')}
            description={t('This cannot be undone. Take a backup first.')}
            onConfirm={onApply}
            okText={t('Delete')}
            cancelText={t('Cancel')}
          >
            <Button
              buttonStyle="danger"
              disabled={busy || pending.length === 0}
              loading={busy}
            >
              {t('Run the sweep now')}
            </Button>
          </Popconfirm>
          <Button buttonStyle="secondary" onClick={load} loading={busy}>
            {t('Recheck')}
          </Button>
        </Row>
    </Card>
  );
}

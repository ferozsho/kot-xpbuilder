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
import { Button, Card, Tag } from '@superset-ui/core/components';
import { useToasts } from 'src/components/MessageToasts/withToasts';
import {
  describeError,
  deliverAlert,
  fetchAlerts,
  AlertReport,
} from './api';

/**
 * Where an alert would go, and what has crossed a line.
 *
 * `delivered` is shown as the service reports it: the panel never implies an
 * inbox is being watched when nothing was sent. "Send now" is the explicit act,
 * and its outcome is reported with the reason it did not happen, if it did not.
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

export default function AlertsCard() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [report, setReport] = useState<AlertReport | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setReport(await fetchAlerts());
    } catch (error) {
      addDangerToast(await describeError(error));
    }
  }, [addDangerToast]);

  useEffect(() => {
    load();
    // Read once on mount: the report is a snapshot, not a live feed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onDeliver = useCallback(async () => {
    setBusy(true);
    try {
      const outcome = await deliverAlert();
      if (outcome.delivered) {
        addSuccessToast(t('Breach report delivered'));
      } else {
        addDangerToast(
          t('Not delivered: %s', outcome.reason || t('unknown reason')),
        );
      }
      await load();
    } catch (error) {
      addDangerToast(await describeError(error));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, addSuccessToast, load]);

  return (
    <Card data-test="ai-usage-alerts">
      <Heading>{t('Alerts')}</Heading>
        <Row>
          {report?.enabled ? (
            <Tag color="blue">{t('addressed to %s', report.recipient)}</Tag>
          ) : (
            <Tag color="default">{t('no address configured')}</Tag>
          )}
          {report?.webhook ? (
            <Tag color="blue">{t('webhook configured')}</Tag>
          ) : (
            <Tag color="orange">{t('no webhook configured')}</Tag>
          )}
          <Tag color={report && report.count > 0 ? 'orange' : 'green'}>
            {t('%s breach(es)', report?.count ?? 0)}
          </Tag>
        </Row>

        {report && report.breaches.length > 0 && (
          <List>
            {report.breaches.map(entry => (
              <li key={entry.id}>
                {t('%s %s: %s%% of its limit (%s)', entry.scope, entry.scope_ref || 'all',
                  entry.percent_used, entry.state)}
              </li>
            ))}
          </List>
        )}

        <Row>
          <Button
            buttonStyle="secondary"
            onClick={load}
            loading={busy}
          >
            {t('Recheck')}
          </Button>
          <Button
            buttonStyle="primary"
            onClick={onDeliver}
            disabled={busy || !report || report.count === 0 || !report.webhook}
            loading={busy}
          >
            {t('Send the breach report now')}
          </Button>
          <span>
            {report?.delivered
              ? t('last delivery accepted')
              : t('nothing has been sent yet')}
          </span>
        </Row>
    </Card>
  );
}

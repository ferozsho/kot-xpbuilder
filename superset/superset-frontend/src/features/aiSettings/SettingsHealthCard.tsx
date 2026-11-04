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
import { describeError, fetchHealth, SettingsHealth } from './api';

/**
 * What the service thinks of its own configuration.
 *
 * The value here is `held_at_ceiling`: an operator who wrote 999999 and is being
 * given 100000 has learned something, while one who sees nothing has a bug to
 * find later. The panel shows the platform's answer rather than restating the
 * form's.
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
    align-items: baseline;
    flex-wrap: wrap;
    font-size: ${theme.fontSize}px;
  `}
`;

const Label = styled.span`
  ${({ theme }) => `
    color: ${theme.colorTextSecondary};
    min-width: 180px;
  `}
`;

const Held = styled.ul`
  ${({ theme }) => `
    margin: ${theme.sizeUnit}px 0 0;
    padding-left: ${theme.sizeUnit * 5}px;
    font-size: ${theme.fontSize}px;
  `}
`;

export default function SettingsHealthCard() {
  const { addDangerToast, addSuccessToast } = useToasts();
  const [health, setHealth] = useState<SettingsHealth | null>(null);
  const [busy, setBusy] = useState(false);

  const check = useCallback(async () => {
    setBusy(true);
    try {
      setHealth(await fetchHealth());
      addSuccessToast(t('Configuration checked'));
    } catch (error) {
      addDangerToast(await describeError(error));
    } finally {
      setBusy(false);
    }
  }, [addDangerToast, addSuccessToast]);

  useEffect(() => {
    check();
    // One check on mount: the panel is a report, not a poller.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Card data-test="ai-settings-health">
      <Heading>{t('Configuration health')}</Heading>
        <Row>
          <Button buttonStyle="secondary" onClick={check} loading={busy}>
            {t('Check now')}
          </Button>
          {health && (
            <Tag color={health.healthy ? 'green' : 'orange'}>
              {health.healthy ? t('healthy') : t('attention')}
            </Tag>
          )}
          {health && (
            <span>
              {t('%s of %s settings stored', health.stored, health.declared)}
            </span>
          )}
        </Row>

        {health && (
          <>
            <Row style={{ marginTop: 12 }}>
              <Label>{t('Restart required')}</Label>
              <span>
                {health.restart_required.length
                  ? health.restart_required.join(', ')
                  : t('none — every setting applies on the next request')}
              </span>
            </Row>
            <Row style={{ marginTop: 8 }}>
              <Label>{t('SQL safeguards in force')}</Label>
              <span>
                {t('rows %s, timeout %sms, concurrent %s, bytes %s',
                  health.limits.max_rows,
                  health.limits.timeout_ms,
                  health.limits.max_concurrent,
                  health.limits.max_bytes)}
              </span>
            </Row>
            <Row style={{ marginTop: 8 }}>
              <Label>{t('Alerts')}</Label>
              <span>
                {health.alerts.enabled
                  ? t('%s breach(es), webhook %s', health.alerts.breaches,
                      health.alerts.webhook ? t('configured') : t('not configured'))
                  : t('no destination configured')}
              </span>
            </Row>
            <Row style={{ marginTop: 8 }}>
              <Label>{t('SQL execution slots')}</Label>
              <span>{health.sql_slots_active}</span>
            </Row>
            <Row style={{ marginTop: 12 }}>
              <Label>{t('Held at a ceiling')}</Label>
              {health.held_at_ceiling.length === 0 ? (
                <span>{t('nothing — every stored value is being honoured')}</span>
              ) : (
                <Held>
                  {health.held_at_ceiling.map(entry => (
                    <li key={entry.key}>
                      {t('%s: stored %s, in force %s', entry.key, String(entry.stored),
                        String(entry.in_force))}
                    </li>
                  ))}
                </Held>
              )}
            </Row>
          </>
        )}
    </Card>
  );
}

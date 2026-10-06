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
import { useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { styled } from '@apache-superset/core/theme';
import {
  Button,
  Select,
  Tag,
  Tooltip,
  Collapse,
  Empty,
  Space,
  Typography,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { isMoodleIntegrationEnabled } from 'src/utils/moodleSync';
import { DatabaseOption, DatasetOption } from '../types';

export type DragPayload = {
  dataset: number;
  column: string;
  label: string;
};

const { Panel } = Collapse;

const StyledPanel = styled.div`
  height: 100%;
  display: flex;
  flex-direction: column;
  gap: ${({ theme }) => theme.sizeUnit * 2}px;

  .dataset-select {
    width: 100%;
  }
`;

const StyledFilterRow = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;

    .db-filter {
      flex: 1;
      min-width: 0;
    }
  `}
`;

const StyledColumn = styled.div`
  ${({ theme }) => `
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit}px;
    padding: ${theme.sizeUnit}px ${theme.sizeUnit * 2}px;
    border: 1px solid ${theme.colorBorder};
    border-radius: ${theme.borderRadius}px;
    background: ${theme.colorFillAlter};
    cursor: grab;
    margin-bottom: ${theme.sizeUnit}px;

    &:hover {
      border-color: ${theme.colorPrimary};
    }
  `}
`;

const StyledHint = styled.div`
  ${({ theme }) => `
    color: ${theme.colorTextDescription};
    font-size: ${theme.fontSizeSM}px;
    margin-top: ${theme.sizeUnit}px;
  `}
`;

type Props = {
  datasets: DatasetOption[];
  /** All Superset database connections the user can reach. */
  databases: DatabaseOption[];
  selectedDatasetIds: number[];
  onSelectDataset: (id: number) => void;
  /**
   * Register the tables of a database (the selected one, else the default).
   * Only supplied by Moodle-integrated deployments; standalone builds omit it
   * and the action is never rendered.
   */
  onSyncTables?: (databaseId?: number) => void;
  syncing?: boolean;
};

export default function DatasetPanel({
  datasets,
  databases,
  selectedDatasetIds,
  onSelectDataset,
  onSyncTables,
  syncing,
}: Props) {
  const [databaseFilter, setDatabaseFilter] = useState<string | undefined>();
  // One image serves both modes: hide the Moodle-only actions when the runtime
  // flag is off, because the /api/sync-tables/ route does not exist then.
  const moodleEnabled = isMoodleIntegrationEnabled();

  // Count registered datasets per database name.
  const datasetCounts = useMemo(
    () =>
      datasets.reduce<Record<string, number>>((acc, ds) => {
        acc[ds.database_name] = (acc[ds.database_name] ?? 0) + 1;
        return acc;
      }, {}),
    [datasets],
  );

  // Every Superset database is listed, so a connection with no datasets yet
  // (e.g. an external client DB) is still selectable and can be synced.
  const databaseOptions = useMemo(() => {
    const names = Array.from(
      new Set([
        ...databases.map(item => item.database_name),
        ...Object.keys(datasetCounts),
      ]),
    ).sort((a, b) => a.localeCompare(b));
    return names.map(name => ({
      label: `${name} (${datasetCounts[name] ?? 0})`,
      value: name,
    }));
  }, [databases, datasetCounts]);

  const selectedDatabase = useMemo(
    () => databases.find(item => item.database_name === databaseFilter),
    [databases, databaseFilter],
  );

  const syncTooltip = selectedDatabase
    ? `${t('Register the tables of')} ${selectedDatabase.database_name}`
    : t('Register all Moodle (read-only) tables as datasets');

  const filteredDatasets = useMemo(
    () =>
      databaseFilter
        ? datasets.filter(ds => ds.database_name === databaseFilter)
        : datasets,
    [datasets, databaseFilter],
  );

  const options = useMemo(
    () =>
      filteredDatasets.map(ds => ({
        label: `${ds.table_name} (${ds.database_name})`,
        value: ds.id,
      })),
    [filteredDatasets],
  );

  // Selected datasets always stay visible, even when filtered out.
  const selectedDatasets = useMemo(
    () => datasets.filter(ds => selectedDatasetIds.includes(ds.id)),
    [datasets, selectedDatasetIds],
  );

  const handleDragStart = (
    event: React.DragEvent<HTMLDivElement>,
    dataset: number,
    column: string,
  ) => {
    const payload: DragPayload = {
      dataset,
      column,
      label: column,
    };
    event.dataTransfer.setData('application/x-report-field', JSON.stringify(payload));
    event.dataTransfer.effectAllowed = 'copy';
  };

  return (
    <StyledPanel>
      <StyledFilterRow>
        <Select
          className="db-filter"
          placeholder={t('All databases')}
          options={databaseOptions}
          allowClear
          showSearch
          optionFilterProp="value"
          value={databaseFilter ?? null}
          onChange={value => setDatabaseFilter(value ?? undefined)}
        />
        {moodleEnabled && (
          <Tooltip title={syncTooltip}>
            <Button
              icon={<Icons.ReloadOutlined />}
              loading={syncing}
              onClick={() => onSyncTables?.(selectedDatabase?.id)}
            >
              {t('Sync tables')}
            </Button>
          </Tooltip>
        )}
      </StyledFilterRow>
      {selectedDatabase && filteredDatasets.length === 0 && (
        <StyledHint>
          {moodleEnabled
            ? `${t('No datasets registered for')} ` +
              `${selectedDatabase.database_name} ${t('yet.')} ` +
              `${t('Click "Sync tables" to register its tables.')}`
            : `${t('No datasets registered for')} ` +
              `${selectedDatabase.database_name} ${t('yet.')}`}
        </StyledHint>
      )}
      <Select
        className="dataset-select"
        placeholder={t('Select a dataset…')}
        options={options}
        showSearch
        value={null}
        onChange={(id: number) => onSelectDataset(id)}
      />
      <StyledHint>
        {t('Drag a column onto a band in the report canvas to add it.')}
      </StyledHint>
      {selectedDatasets.length === 0 ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={t('No datasets added yet')}
        />
      ) : (
        <Collapse
          defaultActiveKey={selectedDatasets.map(ds => String(ds.id))}
          bordered
          style={{ overflow: 'auto' }}
        >
          {selectedDatasets.map(ds => (
            <Panel
              key={ds.id}
              header={
                <Space size={4}>
                  <Tooltip title={ds.database_name}>
                    <Icons.DatabaseOutlined />
                  </Tooltip>
                  <Typography.Text strong>{ds.table_name}</Typography.Text>
                  <Tag>{ds.schema || ds.catalog || 'public'}</Tag>
                </Space>
              }
            >
              {ds.columns.map(col => (
                <Tooltip
                  key={col.column_name}
                  title={
                    col.expression
                      ? `${t('Expression')}: ${col.expression}`
                      : `${col.type || t('unknown type')}`
                  }
                >
                  <StyledColumn
                    draggable
                    onDragStart={event =>
                      handleDragStart(event, ds.id, col.column_name)
                    }
                  >
                    <Icons.Drag />
                    <Typography.Text>{col.column_name}</Typography.Text>
                    <Tag color={col.is_dttm ? 'orange' : 'default'}>
                      {col.is_dttm ? 'dttm' : col.type || '?'}
                    </Tag>
                  </StyledColumn>
                </Tooltip>
              ))}
            </Panel>
          ))}
        </Collapse>
      )}
    </StyledPanel>
  );
}

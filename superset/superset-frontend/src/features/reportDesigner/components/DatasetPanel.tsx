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
  Select,
  Tag,
  Tooltip,
  Collapse,
  Empty,
  Space,
  Typography,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import { DatasetOption } from '../types';

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
  selectedDatasetIds: number[];
  onSelectDataset: (id: number) => void;
};

export default function DatasetPanel({
  datasets,
  selectedDatasetIds,
  onSelectDataset,
}: Props) {
  const [databaseFilter, setDatabaseFilter] = useState<string | undefined>();

  const databaseNames = useMemo(
    () => Array.from(new Set(datasets.map(ds => ds.database_name))),
    [datasets],
  );

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
          options={databaseNames.map(name => ({
            label: name,
            value: name,
          }))}
          allowClear
          showSearch
          value={databaseFilter ?? null}
          onChange={value => setDatabaseFilter(value ?? undefined)}
        />
      </StyledFilterRow>
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
